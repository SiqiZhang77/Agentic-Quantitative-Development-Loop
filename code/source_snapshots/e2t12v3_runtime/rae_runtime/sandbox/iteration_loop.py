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
