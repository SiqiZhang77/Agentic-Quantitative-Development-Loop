"""RAE-18: bounded edit -> backtest -> evaluate -> repeat loop.

run.py's main() calls run_iteration_loop(payload, edit_fn, backtest_fn, tracer).
Each pass edits the strategy (edit_fn = run_pipeline_auto) then backtests + evaluates
it (backtest_fn = run_backtest_via_mcp, which returns recommended_action from RAE-17's
evaluate_result). We loop only while the evaluator says "iterate", and always stop at a
hard max-iteration bound so the container can never loop forever. Each iteration is
recorded on the tracer, so it surfaces in the response's iteration_traces (the only
reporting channel; the response schema forbids extra top-level fields).
"""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from budget_guard import BudgetExceeded, RunTimeout

DEFAULT_MAX_ITERATIONS = 3
DEFAULT_MAX_FAILED_ITERATIONS = 1


def _default_allow_iteration(payload: dict) -> bool:
    """Whether this request type iterates when the request says nothing.

    Backtest runs default to iterating: the engine returns measured metrics, so a
    further pass has a real signal to improve against. General runs are scored by
    the review agent's advisory prose verdict, which is a model's opinion rather
    than a measurement — not a strong enough basis to spend more passes on by
    default, so they run once and must opt in with allow_iteration=true.
    """
    return str(payload.get("command") or "").strip().lower() == "backtest"


def _iteration_allowed(payload: dict) -> bool:
    """iteration_controls.allow_iteration -> RAE_ALLOW_ITERATION env -> per-type default.

    False pins the run to exactly one pass whatever the evaluator recommends, for
    tickets where iteration is known to add nothing.

    Duplicated as pipeline_mcp._iteration_allowed in the proxy, which cannot import
    from this layer. Change both together — see the note there.
    """
    raw = (payload.get("iteration_controls") or {}).get("allow_iteration")
    if raw is None:
        raw = os.environ.get("RAE_ALLOW_ITERATION")
    if raw is None:
        return _default_allow_iteration(payload)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"false", "no", "n", "off", "0"}


def _resolve_max_iterations(payload: dict) -> int:
    """payload.iteration_controls.max_iterations -> legacy/env fallback -> default 3.

    allow_iteration=false clamps this to a single pass; it is the explicit control
    and wins over any max_iterations the request also carries.
    """
    if not _iteration_allowed(payload):
        return 1
    raw = (payload.get("iteration_controls") or {}).get("max_iterations")
    if raw is None:
        raw = (payload.get("execution_objectives") or {}).get("max_iterations")
    if raw is None:
        raw = os.environ.get("RAE_MAX_ITERATIONS")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_ITERATIONS
    return max(1, n)


def _resolve_timeout_seconds(payload: dict) -> int | None:
    raw = (payload.get("iteration_controls") or {}).get("timeout_seconds")
    if raw is None:
        return None
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        return None
    return timeout if timeout > 0 else None


def _resolve_max_failed_iterations(payload: dict) -> int:
    """allow_iteration=false means one pass only, so a failure is returned, not retried."""
    if not _iteration_allowed(payload):
        return 1
    raw = (payload.get("iteration_controls") or {}).get("max_failed_iterations")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_FAILED_ITERATIONS
    return max(1, n)


def _check_timeout(started_at: float, timeout_seconds: int | None) -> None:
    if timeout_seconds and (time.monotonic() - started_at) > timeout_seconds:
        raise RunTimeout(f"timeout_seconds ({timeout_seconds}) exceeded")


def _check_token_budget(payload: dict, pipeline_out: dict, consumed_tokens: int) -> int:
    """Enforce the run-wide token budget between edit/review passes."""
    limit = (payload.get("iteration_controls") or {}).get("max_token_budget_per_run")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return consumed_tokens
    usage = (pipeline_out.get("usage") or {}).get("total_tokens", 0)
    try:
        consumed_tokens += int(usage or 0)
    except (TypeError, ValueError):
        pass
    if limit > 0 and consumed_tokens > limit:
        raise BudgetExceeded(f"token budget ({limit}) exceeded")
    return consumed_tokens


def _state_path(payload: dict) -> Path | None:
    artifact_dir = (payload.get("output_paths") or {}).get("artifact_dir")
    if not artifact_dir:
        return None
    return Path(artifact_dir) / "iteration_state.json"


def _persist_iteration_state(payload: dict, state: dict) -> None:
    path = _state_path(payload)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.json")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        return


def _with_feedback(payload: dict, evaluation: dict | None) -> dict:
    """Return a copy of payload whose feature request carries the evaluation feedback,
    so the next edit knows what to improve. Never mutates the caller's payload."""
    summary = evaluation.get("summary") if isinstance(evaluation, dict) else None
    if not summary:
        return payload
    updated = copy.deepcopy(payload)
    args = updated.setdefault("args", {})
    base = args.get("strategy", "improve the strategy")
    args["strategy"] = (
        f"{base}\n\nPrevious attempt did not meet the criteria: {summary}. "
        "Improve the strategy accordingly."
    )
    # A reused target that the reviewer rejected must reach the editing agent on
    # the next pass rather than being accepted as the same no-op again.
    updated["_skip_reused_target_noop"] = True
    return updated


def _with_failure_feedback(payload: dict, error: Exception) -> dict:
    updated = copy.deepcopy(payload)
    args = updated.setdefault("args", {})
    base = args.get("strategy", "improve the strategy")
    args["strategy"] = (
        f"{base}\n\nPrevious attempt failed with {type(error).__name__}: {error}. "
        "Retry with a safer change."
    )
    return updated


def _ordered_union(*values: Any) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in value or []:
            if isinstance(item, str) and item and item not in result:
                result.append(item)
    return result


def _usage_number(value: Any, *, integer: bool = True) -> int | float:
    """Return a safe numeric usage value without trusting provider payload types."""

    try:
        return int(value or 0) if integer else float(value or 0.0)
    except (TypeError, ValueError):
        return 0 if integer else 0.0


def _merge_usage(previous: dict | None, current: dict | None) -> dict:
    """Accumulate per-pass model usage for run-level telemetry.

    The edit pipeline reports usage for one pass.  The outer loop enforces a
    run-wide budget, so the result and any terminal exception must expose the
    same cumulative view rather than only the final pass.
    """

    previous = previous or {}
    current = current or {}
    previous_cost = previous.get("cost_usd")
    current_cost = current.get("cost_usd")
    return {
        "model": current.get("model") or previous.get("model"),
        "calls": _usage_number(previous.get("calls"))
        + _usage_number(current.get("calls")),
        "prompt_tokens": _usage_number(previous.get("prompt_tokens"))
        + _usage_number(current.get("prompt_tokens")),
        "completion_tokens": _usage_number(previous.get("completion_tokens"))
        + _usage_number(current.get("completion_tokens")),
        "total_tokens": _usage_number(previous.get("total_tokens"))
        + _usage_number(current.get("total_tokens")),
        "cost_usd": (
            None
            if previous_cost is None and current_cost is None
            else round(
                _usage_number(previous_cost, integer=False)
                + _usage_number(current_cost, integer=False),
                6,
            )
        ),
    }


def _merge_pipeline_outputs(
    previous: dict | None,
    current: dict,
) -> dict:
    """Carry real repository changes across edit/review iterations.

    Each pipeline pass reports only commits made by that pass. The workflow and
    Jira report, however, must retain files committed by earlier passes when a
    later repair is a no-op or touches a different repository.
    """

    if not previous:
        return current

    merged = copy.deepcopy(current)
    previous_artifacts = previous.get("artifacts") or {}
    current_artifacts = merged.setdefault("artifacts", {})
    current_artifacts["modified_files"] = _ordered_union(
        previous_artifacts.get("modified_files"),
        current_artifacts.get("modified_files"),
    )
    current_artifacts["new_files"] = _ordered_union(
        previous_artifacts.get("new_files"),
        current_artifacts.get("new_files"),
    )

    previous_branches = {
        (item.get("repo_full_name"), item.get("target_branch")): item
        for item in previous_artifacts.get("repository_branches") or []
        if isinstance(item, dict)
    }
    current_branches = {
        (item.get("repo_full_name"), item.get("target_branch")): item
        for item in current_artifacts.get("repository_branches") or []
        if isinstance(item, dict)
    }
    branch_keys = list(previous_branches)
    branch_keys.extend(key for key in current_branches if key not in previous_branches)
    merged_branches = []
    for key in branch_keys:
        prior = previous_branches.get(key) or {}
        latest = current_branches.get(key) or {}
        record = {**prior, **latest}
        record["modified_files"] = _ordered_union(
            prior.get("modified_files"), latest.get("modified_files")
        )
        record["new_files"] = _ordered_union(
            prior.get("new_files"), latest.get("new_files")
        )
        if prior.get("branch_action") == "created":
            record["branch_action"] = "created"
        merged_branches.append(record)
    if merged_branches:
        current_artifacts["repository_branches"] = merged_branches

    if current_artifacts["modified_files"] or current_artifacts["new_files"]:
        current_artifacts.pop("no_code_changes", None)
    return merged


def run_iteration_loop(
    payload: dict,
    edit_fn: Callable[[dict], dict],
    backtest_fn: Callable[[dict], dict],
    tracer: Any = None,
) -> dict:
    """Bounded edit -> backtest -> evaluate -> repeat.

    Loops while the evaluator returns recommended_action == "iterate"; stops on any
    other action (accept / review / escalate / None) or when max_iterations is hit.
    Returns the last pipeline/backtest outputs; per-iteration progress is recorded on
    the tracer.
    """
    max_iterations = _resolve_max_iterations(payload)
    timeout_seconds = _resolve_timeout_seconds(payload)
    max_failed_iterations = _resolve_max_failed_iterations(payload)
    started_at = time.monotonic()
    current = payload
    pipeline_out: dict | None = None
    backtest: dict | None = None
    failed_iterations = 0
    consumed_tokens = 0
    cumulative_usage: dict = {}
    last_error: Exception | None = None
    state = {
        "run_id": payload.get("run_id"),
        "issue_key": payload.get("issue_key"),
        "status": "running",
        "max_iterations": max_iterations,
        "timeout_seconds": timeout_seconds,
        "max_failed_iterations": max_failed_iterations,
        "current_iteration": 0,
        "failed_iterations": 0,
        "iterations": [],
    }

    completed_iterations = 0
    attempt = 0
    # Failed execution attempts are deliberately separate from completed
    # edit/evaluate passes. Both limits remain bounded, so this cannot loop
    # forever even when an agent failure is retryable.
    while completed_iterations < max_iterations:
        attempt += 1
        i = completed_iterations + 1
        try:
            _check_timeout(started_at, timeout_seconds)
            pass_pipeline_out = edit_fn(current)
            candidate_pipeline_out = _merge_pipeline_outputs(
                pipeline_out, pass_pipeline_out
            )
            candidate_usage = _merge_usage(
                cumulative_usage, pass_pipeline_out.get("usage")
            )
            if candidate_usage.get("model") or any(
                candidate_usage.get(key)
                for key in (
                    "calls",
                    "prompt_tokens",
                    "completion_tokens",
                    "total_tokens",
                )
            ):
                candidate_pipeline_out["usage"] = candidate_usage
            try:
                consumed_tokens = _check_token_budget(
                    current, pass_pipeline_out, consumed_tokens
                )
            except BudgetExceeded as exc:
                # The edit may already have committed valid code. Preserve the
                # exact usage and artifact evidence for the failure response.
                exc.pipeline_out = candidate_pipeline_out
                exc.consumed_tokens = candidate_usage.get("total_tokens", 0)
                raise
            pipeline_out = candidate_pipeline_out
            cumulative_usage = candidate_usage
            _check_timeout(started_at, timeout_seconds)
            # RAE-19: pass the iteration number (non-mutating) so the backtest job
            # name is unique per iteration — job dirs on the shared cluster home
            # must never be reused. pipeline_out rides along for evaluators that
            # score this pass's edit (review_agent); the backtest driver ignores it.
            backtest = backtest_fn({**current, "iteration": i, "pipeline_out": pipeline_out})
            _check_timeout(started_at, timeout_seconds)
            action = backtest.get("recommended_action")
            completed_iterations += 1
            state.update(
                {
                    "current_iteration": completed_iterations,
                    "recommended_action": action,
                    "failed_iterations": failed_iterations,
                    "status": "running" if action == "iterate" else "stopped",
                }
            )
            state["iterations"].append(
                {
                    "iteration": completed_iterations,
                    "status": "succeeded",
                    "recommended_action": action,
                    "evaluation": backtest.get("evaluation"),
                }
            )
            _persist_iteration_state(payload, state)
            if tracer is not None:
                tracer.record(
                    "iteration_loop",
                    f"iteration_{i}",
                    "succeeded",
                    f"iteration {completed_iterations}/{max_iterations}: recommended_action={action}",
                )
            if action != "iterate":
                break
            current = _with_feedback(current, backtest.get("evaluation"))
            # Carry what this pass produced so the next review can tell an
            # improvement from a pass that threw the previous work away.
            reviewed_sizes = backtest.get("reviewed_sizes")
            if reviewed_sizes:
                current = {**current, "_previous_reviewed_sizes": reviewed_sizes}
        except (RunTimeout, BudgetExceeded) as exc:
            if pipeline_out is not None and not hasattr(exc, "pipeline_out"):
                exc.pipeline_out = pipeline_out
            state.update(
                {
                    "current_iteration": completed_iterations + 1,
                    "status": "timeout" if isinstance(exc, RunTimeout) else "failed",
                    "failed_iterations": failed_iterations,
                }
            )
            _persist_iteration_state(payload, state)
            raise
        except Exception as exc:
            failed_iterations += 1
            last_error = exc
            state.update(
                {
                    "current_iteration": i,
                    "status": "failed",
                    "failed_iterations": failed_iterations,
                }
            )
            state["iterations"].append(
                {
                    "iteration": completed_iterations + 1,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            _persist_iteration_state(payload, state)
            if tracer is not None:
                tracer.record(
                    "iteration_loop",
                    f"iteration_{i}",
                    "failed",
                    f"attempt {attempt}, completed {completed_iterations}/{max_iterations}: "
                    f"{type(exc).__name__}: {exc}",
                )
            if failed_iterations >= max_failed_iterations:
                raise
            current = _with_failure_feedback(current, exc)

    # The loop may exhaust max_iterations before it exhausts the separate failure
    # budget.  ``pipeline_out`` is not proof that the pass succeeded: edit_fn may
    # have committed a change before the evaluator/backtest failed.  If the final
    # pass is still marked failed, returning would report that unevaluated edit as a
    # successful run (or pair it with a stale verdict from an earlier pass).
    if state["status"] == "failed" and last_error is not None:
        state["status"] = "failed"
        _persist_iteration_state(payload, state)
        raise last_error

    if state["status"] == "running":
        state["status"] = "max_iterations_reached"
        _persist_iteration_state(payload, state)

    return {"pipeline_out": pipeline_out, "backtest": backtest}


def _provider_call_count_from_failure(error: BaseException) -> int:
    accounting = getattr(error, "exp3_provider_accounting", None)
    if not isinstance(accounting, dict):
        return 0
    shared = ((accounting.get("budget") or {}).get("shared") or {})
    calls = shared.get("calls", 0)
    return calls if type(calls) is int and calls >= 0 else 0


def record_attempt_snapshot_best_effort(
    record: dict[str, Any],
    payload: dict,
    number: int,
    snapshot_fn: Callable[[dict, int], Any],
) -> None:
    """Freeze an audit copy without allowing the recorder to change the outcome.

    The attempt record must already contain the answer-blind completion evidence
    before this function is called.  A snapshot is secondary audit material: if
    it cannot be written, retain the attempt and annotate only that recording
    failure instead of turning saved answers into a runtime failure.
    """

    try:
        snapshot_fn(payload, number)
    except Exception as exc:
        message = " ".join(str(exc).split())
        record["snapshot_status"] = "failed"
        record["snapshot_error"] = (
            f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        )[:512]


def run_exp3_observation_attempts(
    payload: dict,
    edit_fn: Callable[[dict], dict],
    evaluate_fn: Callable[[dict], dict],
    tracer: Any = None,
    *,
    clock: Callable[[], float] | None = None,
) -> dict:
    """Run exactly the prospective, answer-blind two-attempt E3 contract.

    Unlike the legacy loop, every complete architecture execution consumes one
    attempt, including a positive-token workflow failure.  A zero-model failure
    is not retried because it identifies a local framework/preflight defect.
    """

    from exp3.attempt_control import (
        ExperimentTimeoutController,
        MAX_ATTEMPTS,
        attempt_record,
        freeze_attempt_snapshot,
        validate_two_attempt_controls,
        with_attempt_context,
    )

    validate_two_attempt_controls(payload)
    timeout_controller = ExperimentTimeoutController(
        payload,
        maximum_attempts=MAX_ATTEMPTS,
        clock=clock or time.monotonic,
        timeout_error_cls=RunTimeout,
    )
    merged_pipeline: dict | None = None
    cumulative_usage: dict = {}
    consumed_tokens = 0
    last_evaluation: dict | None = None
    attempts: list[dict[str, Any]] = []
    prior_status: str | None = None
    state = {
        "run_id": payload.get("run_id"),
        "issue_key": payload.get("issue_key"),
        "status": "running",
        "max_attempts": MAX_ATTEMPTS,
        "attempts": attempts,
    }

    for number in range(1, MAX_ATTEMPTS + 1):
        try:
            timeout_controller.start_attempt(number)
        except RunTimeout as exc:
            state["status"] = "failed"
            _persist_iteration_state(payload, state)
            exc.experiment_attempts = copy.deepcopy(attempts)
            raise
        current = with_attempt_context(payload, number, prior_status=prior_status)
        try:
            pass_pipeline = edit_fn(current)
            try:
                consumed_tokens = _check_token_budget(
                    payload,
                    pass_pipeline,
                    consumed_tokens,
                )
            except BudgetExceeded as budget_exc:
                budget_exc.budget_scope = "observation"
                budget_exc.pipeline_out = pass_pipeline
                accounting = pass_pipeline.get("experiment3_provider_accounting")
                if isinstance(accounting, dict):
                    budget_exc.exp3_provider_accounting = accounting
                raise
            try:
                timeout_controller.check_attempt(number)
            except RunTimeout as timeout_exc:
                timeout_exc.pipeline_out = pass_pipeline
                accounting = pass_pipeline.get("experiment3_provider_accounting")
                if isinstance(accounting, dict):
                    timeout_exc.exp3_provider_accounting = accounting
                raise
        except Exception as exc:
            try:
                timeout_controller.check_attempt(number)
            except RunTimeout as timeout_exc:
                for attribute in ("exp3_provider_accounting", "pipeline_out"):
                    if hasattr(exc, attribute) and not hasattr(timeout_exc, attribute):
                        setattr(timeout_exc, attribute, getattr(exc, attribute))
                exc = timeout_exc
            calls = _provider_call_count_from_failure(exc)
            failure_pipeline = getattr(exc, "pipeline_out", None)
            if not isinstance(failure_pipeline, dict):
                failure_pipeline = merged_pipeline or {}
            recovered_evaluation = None
            try:
                recovered_evaluation = evaluate_fn(
                    {
                        **current,
                        "iteration": number,
                        "pipeline_out": failure_pipeline,
                    }
                )
            except Exception:
                # Keep the original runtime exception causal when completion
                # evidence itself is unavailable or corrupt.
                recovered_evaluation = None
            evidence = (
                (recovered_evaluation or {}).get("completion_evidence") or {}
            )
            recovered_complete = (
                (recovered_evaluation or {}).get("recommended_action") == "accept"
            )
            record = attempt_record(
                number=number,
                runtime_status="failed",
                provider_call_count=calls,
                evidence=evidence,
                failure_type=type(exc).__name__,
            )
            attempts.append(record)
            record_attempt_snapshot_best_effort(
                record,
                current,
                number,
                freeze_attempt_snapshot,
            )
            if recovered_complete:
                recovered_pipeline = copy.deepcopy(failure_pipeline)
                recovered_pipeline["status"] = "succeeded"
                recovered_pipeline.setdefault(
                    "summary",
                    "All required answer evidence was saved before the runtime ended.",
                )
                recovered_pipeline.setdefault(
                    "artifacts", {"modified_files": [], "new_files": []}
                )
                diagnostics = recovered_pipeline.setdefault("diagnostics", {})
                diagnostics["runtime_exit"] = {
                    "status": "error_after_answer_capture",
                    "failure_type": type(exc).__name__,
                }
                diagnostics.setdefault("retry_safe", {"commit": {"paths": []}})
                merged_pipeline = _merge_pipeline_outputs(
                    merged_pipeline, recovered_pipeline
                )
                last_evaluation = recovered_evaluation
                state["status"] = "completed"
                _persist_iteration_state(payload, state)
                if tracer is not None:
                    tracer.record(
                        "observation_attempts",
                        f"attempt_{number}",
                        "succeeded",
                        (
                            f"attempt {number}/{MAX_ATTEMPTS}: answers complete "
                            f"before {type(exc).__name__}"
                        ),
                    )
                break
            observation_timed_out = (
                isinstance(exc, RunTimeout)
                and getattr(exc, "timeout_scope", None) == "observation"
            )
            observation_budget_exhausted = (
                isinstance(exc, BudgetExceeded)
                and getattr(exc, "budget_scope", None) == "observation"
            )
            state["status"] = (
                "retrying"
                if number < MAX_ATTEMPTS
                and calls > 0
                and not observation_timed_out
                and not observation_budget_exhausted
                else "failed"
            )
            _persist_iteration_state(payload, state)
            if tracer is not None:
                tracer.record(
                    "observation_attempts",
                    f"attempt_{number}",
                    "failed",
                    f"attempt {number}/{MAX_ATTEMPTS}: {type(exc).__name__}",
                )
            if (
                number >= MAX_ATTEMPTS
                or calls == 0
                or observation_timed_out
                or observation_budget_exhausted
            ):
                if merged_pipeline is not None and not hasattr(exc, "pipeline_out"):
                    exc.pipeline_out = merged_pipeline
                exc.experiment_attempts = copy.deepcopy(attempts)
                raise exc
            prior_status = "retryable_workflow_failure"
            continue

        merged_pipeline = _merge_pipeline_outputs(merged_pipeline, pass_pipeline)
        cumulative_usage = _merge_usage(cumulative_usage, pass_pipeline.get("usage"))
        if cumulative_usage.get("model") or any(
            cumulative_usage.get(key)
            for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        ):
            merged_pipeline["usage"] = cumulative_usage
        last_evaluation = evaluate_fn(
            {**current, "iteration": number, "pipeline_out": merged_pipeline}
        )
        action = last_evaluation.get("recommended_action")
        evidence = last_evaluation.get("completion_evidence") or {}
        record = attempt_record(
            number=number,
            runtime_status="succeeded",
            provider_call_count=int(
                (pass_pipeline.get("usage") or {}).get("calls") or 0
            ),
            evidence=evidence,
        )
        attempts.append(record)
        record_attempt_snapshot_best_effort(
            record,
            current,
            number,
            freeze_attempt_snapshot,
        )
        state["status"] = "completed" if action == "accept" else "retrying"
        _persist_iteration_state(payload, state)
        if tracer is not None:
            tracer.record(
                "observation_attempts",
                f"attempt_{number}",
                "succeeded",
                f"attempt {number}/{MAX_ATTEMPTS}: recommended_action={action}",
            )
        if action == "accept":
            break
        prior_status = (
            "required_items_missing"
            if evidence.get("completion_basis") == "t3_items_missing"
            else "required_commit_missing"
        )

    if attempts and attempts[-1].get("completion") != "complete":
        state["status"] = "max_attempts_reached"
        _persist_iteration_state(payload, state)
    return {
        "pipeline_out": merged_pipeline,
        "backtest": last_evaluation,
        "attempts": copy.deepcopy(attempts),
    }
