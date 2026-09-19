"""Answer-blind two-attempt control shared by explicit Experiment 3 arms.

One attempt is one complete architecture execution.  This module never reads an
answer key or scores task correctness.  For the T3 item profile, the primary
submission is the run-local item store, so completion requires all 25
structurally valid item candidates.  A repository artifact remains separate
delivery evidence and cannot substitute for missing primary items.  For other
profiles, completion continues to use the declared target path's successful,
audited repository write.  Neither check reveals whether a submitted value is
mathematically correct.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
import time
from typing import Any, Callable, Final

from budget_guard import RunTimeout

from t3_quant_item_store import (
    ITEM_STORE_FILENAME,
    T3QuantItemStoreError,
    freeze_item_store_attempt,
    load_item_store,
)


MAX_ATTEMPTS: Final = 2
MAX_CALLS_PER_ATTEMPT: Final = 20
ALLOWED_TOKENS_PER_ATTEMPT: Final = frozenset({350_000, 500_000})
DEFAULT_TOKENS_PER_ATTEMPT: Final = 350_000
MAX_TOKENS_PER_ATTEMPT: Final = max(ALLOWED_TOKENS_PER_ATTEMPT)
MAX_CALLS_PER_OBSERVATION: Final = MAX_ATTEMPTS * MAX_CALLS_PER_ATTEMPT
MAX_TOKENS_PER_OBSERVATION: Final = MAX_ATTEMPTS * MAX_TOKENS_PER_ATTEMPT
T3_TOTAL_ITEMS: Final = 25
T3_ITEM_RESULTS_PROFILE: Final = "t3_item_results_v2"
CONTINUATION_PRIOR_STATUSES: Final = frozenset(
    {
        "required_commit_missing",
        "required_items_missing",
        "retryable_workflow_failure",
    }
)


class AttemptControlError(ValueError):
    """The request or attempt evidence cannot honour the frozen contract."""


class ExperimentTimeoutController:
    """Apply the same attempt and observation clocks to either architecture.

    ``iteration_controls.timeout_seconds`` is the maximum wall time for one
    complete architecture execution.  The observation cap is that same limit
    multiplied by the prospectively allowed number of attempts.  Resetting the
    attempt clock therefore never resets the observation-wide clock.
    """

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        maximum_attempts: int,
        clock: Callable[[], float] = time.monotonic,
        timeout_error_cls: type[BaseException] = RunTimeout,
    ) -> None:
        if type(maximum_attempts) is not int or not 1 <= maximum_attempts <= MAX_ATTEMPTS:
            raise AttemptControlError("timeout maximum attempts is invalid")
        if not callable(clock):
            raise AttemptControlError("timeout clock is invalid")
        if not isinstance(timeout_error_cls, type) or not issubclass(
            timeout_error_cls, BaseException
        ):
            raise AttemptControlError("timeout error type is invalid")
        raw = (payload.get("iteration_controls") or {}).get("timeout_seconds")
        if raw is None:
            attempt_timeout = None
        elif type(raw) is not int or raw <= 0:
            raise AttemptControlError("timeout_seconds must be a positive integer")
        else:
            attempt_timeout = raw
        self.attempt_timeout_seconds = attempt_timeout
        self.observation_timeout_seconds = (
            attempt_timeout * maximum_attempts
            if attempt_timeout is not None
            else None
        )
        self._clock = clock
        self._timeout_error_cls = timeout_error_cls
        self._observation_started_at = clock()
        self._attempt_started_at: float | None = None
        self._attempt_number: int | None = None

    def _timeout(
        self,
        *,
        scope: str,
        limit: int,
        attempt: int | None,
    ) -> BaseException:
        error = self._timeout_error_cls(
            f"experiment {scope} timeout ({limit} seconds) exceeded"
        )
        error.timeout_scope = scope
        error.timeout_seconds = limit
        error.attempt_number = attempt
        error.failure_phase = f"{scope}_timeout"
        return error

    def _check_observation(self, now: float, attempt: int | None) -> None:
        limit = self.observation_timeout_seconds
        if limit is not None and now - self._observation_started_at > limit:
            raise self._timeout(scope="observation", limit=limit, attempt=attempt)

    def start_attempt(self, number: int) -> None:
        if type(number) is not int or not 1 <= number <= MAX_ATTEMPTS:
            raise AttemptControlError("timeout attempt number is invalid")
        now = self._clock()
        self._check_observation(now, number)
        self._attempt_number = number
        self._attempt_started_at = now

    def check_attempt(self, number: int) -> None:
        if number != self._attempt_number or self._attempt_started_at is None:
            raise AttemptControlError("timeout attempt has not started")
        now = self._clock()
        self._check_observation(now, number)
        limit = self.attempt_timeout_seconds
        if limit is not None and now - self._attempt_started_at > limit:
            raise self._timeout(scope="attempt", limit=limit, attempt=number)


def _uses_t3_item_profile(payload: Mapping[str, Any]) -> bool:
    objectives = payload.get("execution_objectives") or {}
    parameters = objectives.get("parsed_task_parameters") or {}
    profile = parameters.get("answer_capture_profile")
    if profile is None:
        return False
    if profile != T3_ITEM_RESULTS_PROFILE:
        raise AttemptControlError("unsupported answer_capture_profile")
    return True


def token_budget_per_attempt(payload: Mapping[str, Any]) -> int:
    """Return the frozen per-attempt cap declared by one two-attempt request.

    Historical E3 requests used 700,000 tokens across two attempts.  The
    successor capacity profile uses 1,000,000.  Deriving the attempt cap from
    the request keeps both profiles runnable by one image while still rejecting
    an unreviewed intermediate value.
    """

    controls = payload.get("iteration_controls") or {}
    observation_cap = controls.get("max_token_budget_per_run")
    if type(observation_cap) is not int or observation_cap <= 0:
        raise AttemptControlError(
            "max_token_budget_per_run must be a positive integer"
        )
    if observation_cap % MAX_ATTEMPTS:
        raise AttemptControlError(
            "max_token_budget_per_run must divide evenly across two attempts"
        )
    attempt_cap = observation_cap // MAX_ATTEMPTS
    if attempt_cap not in ALLOWED_TOKENS_PER_ATTEMPT:
        raise AttemptControlError(
            "max_token_budget_per_run is not an approved E3 two-attempt profile"
        )
    return attempt_cap


def validate_two_attempt_controls(payload: Mapping[str, Any]) -> None:
    controls = payload.get("iteration_controls") or {}
    expected = {
        "allow_iteration": True,
        "max_iterations": MAX_ATTEMPTS,
        "max_failed_iterations": MAX_ATTEMPTS,
        "max_agent_turns": MAX_CALLS_PER_ATTEMPT,
    }
    for name, value in expected.items():
        if controls.get(name) != value:
            raise AttemptControlError(
                f"explicit E3 two-attempt request requires {name}={value!r}"
            )
    token_budget_per_attempt(payload)


def attempt_number(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("_experiment_attempt")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AttemptControlError("_experiment_attempt must be an object")
    number = value.get("number")
    maximum = value.get("maximum")
    if type(number) is not int or not 1 <= number <= MAX_ATTEMPTS:
        raise AttemptControlError("experiment attempt number is invalid")
    if maximum != MAX_ATTEMPTS:
        raise AttemptControlError("experiment attempt maximum is invalid")
    return number


def with_attempt_context(
    payload: Mapping[str, Any],
    number: int,
    *,
    prior_status: str | None = None,
) -> dict[str, Any]:
    if type(number) is not int or not 1 <= number <= MAX_ATTEMPTS:
        raise AttemptControlError("attempt number must be 1 or 2")
    if (
        prior_status is not None
        and prior_status not in CONTINUATION_PRIOR_STATUSES
    ):
        raise AttemptControlError("prior attempt status is invalid")
    updated = copy.deepcopy(dict(payload))
    context: dict[str, Any] = {"number": number, "maximum": MAX_ATTEMPTS}
    if prior_status is not None:
        context["prior_status"] = prior_status
        if _uses_t3_item_profile(updated):
            item_state = _saved_t3_item_state(updated)
            saved_ids = item_state["saved_t3_item_ids"]
            missing_ids = item_state["missing_t3_item_ids"]
            context["feedback"] = (
                "The previous complete attempt saved structurally valid candidates "
                f"for item IDs {saved_ids}; item IDs {missing_ids} are still missing. "
                "Inspect the existing target branch, saved item state and checkpoints, "
                "then finish the same public task. Saved means only that the answer "
                "has an acceptable structure; no answer correctness information is "
                "available."
            )
        else:
            context["feedback"] = (
                "The previous complete attempt did not produce an audited successful "
                "write of the required target path. Inspect any existing target-"
                "branch or saved-checkpoint state and finish the same public task. "
                "No answer correctness information is available."
            )
    updated["_experiment_attempt"] = context
    return updated


def audit_filename(payload: Mapping[str, Any], base: str) -> str:
    number = attempt_number(payload)
    if number is None:
        return base
    stem, dot, suffix = base.partition(".")
    return f"{stem}_attempt_{number}{dot}{suffix}" if dot else f"{base}_attempt_{number}"


def required_target_path(payload: Mapping[str, Any]) -> str:
    strategy = payload.get("strategy") or {}
    path = strategy.get("target_path")
    if type(path) is not str or not path.strip():
        raise AttemptControlError("explicit E3 request requires one target_path")
    return path.strip()


def _safe_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({item.strip() for item in value if type(item) is str and item.strip()})


def _saved_t3_item_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return answer-blind, verified T3 coverage without reading candidate values."""

    if not _uses_t3_item_profile(payload):
        return {
            "saved_t3_items": 0,
            "saved_t3_item_ids": [],
            "missing_t3_item_ids": [],
            "required_t3_items": 0,
            "t3_candidates_sha256": None,
        }
    output_paths = payload.get("output_paths") or {}
    artifact_dir = output_paths.get("artifact_dir")
    if type(artifact_dir) is not str or not artifact_dir.strip():
        raise AttemptControlError("T3 item completion requires artifact_dir")
    try:
        store = load_item_store(Path(artifact_dir) / ITEM_STORE_FILENAME)
    except T3QuantItemStoreError as exc:
        raise AttemptControlError(
            f"T3 item completion store is invalid: {exc.code}"
        ) from exc
    count = store.get("candidate_count")
    candidates = store.get("candidates")
    digest = store.get("candidates_sha256")
    if type(count) is not int or not 0 <= count <= T3_TOTAL_ITEMS:
        raise AttemptControlError("T3 saved item count is invalid")
    if not isinstance(candidates, Mapping) or len(candidates) != count:
        raise AttemptControlError("T3 saved item IDs are invalid")
    try:
        saved_ids = sorted(int(key) for key in candidates)
    except (TypeError, ValueError) as exc:
        raise AttemptControlError("T3 saved item IDs are invalid") from exc
    if (
        saved_ids != sorted(set(saved_ids))
        or any(item_id not in range(1, T3_TOTAL_ITEMS + 1) for item_id in saved_ids)
        or {str(item_id) for item_id in saved_ids} != set(candidates)
    ):
        raise AttemptControlError("T3 saved item IDs are invalid")
    if type(digest) is not str or len(digest) != 64:
        raise AttemptControlError("T3 candidate digest is invalid")
    return {
        "saved_t3_items": count,
        "saved_t3_item_ids": saved_ids,
        "missing_t3_item_ids": [
            item_id
            for item_id in range(1, T3_TOTAL_ITEMS + 1)
            if item_id not in set(saved_ids)
        ],
        "required_t3_items": T3_TOTAL_ITEMS,
        "t3_candidates_sha256": digest,
    }


def _with_primary_completion(
    payload: Mapping[str, Any], evidence: Mapping[str, Any]
) -> dict[str, Any]:
    combined = copy.deepcopy(dict(evidence))
    item_state = _saved_t3_item_state(payload)
    saved_items = item_state["saved_t3_items"]
    commit_complete = combined.get("complete") is True
    item_complete = saved_items == T3_TOTAL_ITEMS
    uses_item_profile = _uses_t3_item_profile(payload)
    primary_complete = item_complete if uses_item_profile else commit_complete
    combined.update(
        {
            "complete": primary_complete,
            "completion_basis": (
                "all_t3_items_saved"
                if uses_item_profile and item_complete
                else "t3_items_missing"
                if uses_item_profile
                else "audited_target_commit"
                if commit_complete
                else "incomplete"
            ),
            **item_state,
        }
    )
    return combined


def freeze_attempt_snapshot(
    payload: Mapping[str, Any], number: int
) -> dict[str, Any] | None:
    """Freeze the full T3 answer state once when one model execution ends."""

    if not _uses_t3_item_profile(payload):
        return None
    output_paths = payload.get("output_paths") or {}
    artifact_dir = output_paths.get("artifact_dir")
    if type(artifact_dir) is not str or not artifact_dir.strip():
        raise AttemptControlError("T3 item snapshot requires artifact_dir")
    try:
        return freeze_item_store_attempt(
            Path(artifact_dir) / ITEM_STORE_FILENAME,
            attempt=number,
        )
    except T3QuantItemStoreError as exc:
        raise AttemptControlError(
            f"T3 item attempt snapshot failed: {exc.code}"
        ) from exc


def attempt_record(
    *,
    number: int,
    runtime_status: str,
    provider_call_count: int,
    evidence: Mapping[str, Any],
    failure_type: str | None = None,
) -> dict[str, Any]:
    """Build one common M0/M1 attempt record from answer-blind evidence.

    ``runtime_status`` says whether the architecture program returned normally.
    It is deliberately separate from ``completion``: a program may raise while
    all 25 already-saved answers remain complete and scoreable.
    """

    if type(number) is not int or not 1 <= number <= MAX_ATTEMPTS:
        raise AttemptControlError("attempt record number is invalid")
    if runtime_status not in {"succeeded", "failed"}:
        raise AttemptControlError("attempt runtime status is invalid")
    if type(provider_call_count) is not int or not 0 <= provider_call_count <= MAX_CALLS_PER_ATTEMPT:
        raise AttemptControlError("attempt provider call count is invalid")
    complete = evidence.get("complete") is True
    saved = evidence.get("saved_t3_items", 0)
    required = evidence.get("required_t3_items", 0)
    if type(saved) is not int or type(required) is not int or not 0 <= saved <= required:
        raise AttemptControlError("attempt answer coverage is invalid")
    record: dict[str, Any] = {
        "attempt": number,
        "status": runtime_status,
        "completion": (
            "complete"
            if complete
            else "runtime_failure"
            if runtime_status == "failed"
            else "required_items_missing"
            if required > 0
            else "required_commit_missing"
        ),
        "provider_call_count": provider_call_count,
        "commit_count": int(evidence.get("commit_count") or 0),
        "committed_paths": list(evidence.get("committed_paths") or []),
        "answer_capture_status": (
            "not_applicable"
            if required == 0
            else "complete"
            if saved == required
            else "partial"
            if saved > 0
            else "none"
        ),
        "saved_item_count": saved,
        "required_item_count": required,
        "artifact_delivery_status": (
            "committed"
            if int(evidence.get("commit_count") or 0) > 0
            and evidence.get("target_path") in set(evidence.get("committed_paths") or [])
            else "not_committed"
        ),
        "runtime_exit_status": "clean" if runtime_status == "succeeded" else "error",
    }
    if failure_type is not None:
        record["failure_type"] = failure_type
    return record


def pipeline_commit_evidence(
    payload: Mapping[str, Any], pipeline_out: Mapping[str, Any] | None
) -> dict[str, Any]:
    value = pipeline_out if isinstance(pipeline_out, Mapping) else {}
    accounting = value.get("experiment3_provider_accounting") or {}
    commit_count = accounting.get("commit_count", 0)
    if type(commit_count) is not int or commit_count < 0:
        raise AttemptControlError("single-agent commit_count is invalid")
    retry_safe = ((value.get("diagnostics") or {}).get("retry_safe") or {})
    commit = retry_safe.get("commit") or {}
    paths = _safe_paths(commit.get("paths"))
    validation_issues = _safe_paths(
        ((value.get("artifacts") or {}).get("validation_issues") or [])
    )
    target = required_target_path(payload)
    complete = commit_count > 0 and target in paths and not validation_issues
    return _with_primary_completion(payload, {
        "complete": complete,
        "commit_count": commit_count,
        "committed_paths": paths,
        "target_path": target,
        "validation_issue_count": len(validation_issues),
    })


def manager_star_commit_evidence(
    payload: Mapping[str, Any], result: Mapping[str, Any] | None
) -> dict[str, Any]:
    value = result if isinstance(result, Mapping) else {}
    commit_count = value.get("commit_count", 0)
    if type(commit_count) is not int or commit_count < 0:
        raise AttemptControlError("manager-star commit_count is invalid")
    paths = _safe_paths(value.get("committed_paths"))
    target = required_target_path(payload)
    return _with_primary_completion(payload, {
        "complete": commit_count > 0 and target in paths,
        "commit_count": commit_count,
        "committed_paths": paths,
        "target_path": target,
        "validation_issue_count": 0,
    })


def deterministic_evaluation(evidence: Mapping[str, Any]) -> dict[str, Any]:
    complete = evidence.get("complete") is True
    target = evidence.get("target_path") or "the required target path"
    if evidence.get("completion_basis") == "all_t3_items_saved":
        summary = (
            "Answer-blind completion check found all 25 structurally valid T3 item "
            "candidates. Mathematical correctness was not inspected."
        )
    elif evidence.get("completion_basis") == "t3_items_missing":
        summary = (
            "Answer-blind completion check found fewer than 25 structurally valid "
            "T3 item candidates. A repository artifact does not replace missing "
            "primary item submissions, and mathematical correctness was not inspected."
        )
    elif complete:
        summary = f"Answer-blind completion check found an audited successful write of {target}."
    else:
        summary = f"Answer-blind completion check did not find an audited successful write of {target}."
    return {
        "evaluation": {
            "met_criteria": complete,
            "confidence": 1.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": summary,
        },
        "recommended_action": "accept" if complete else "iterate",
        "completion_evidence": copy.deepcopy(dict(evidence)),
    }


def evaluate_single_agent_attempt(payload: Mapping[str, Any]) -> dict[str, Any]:
    return deterministic_evaluation(
        pipeline_commit_evidence(payload, payload.get("pipeline_out"))
    )
