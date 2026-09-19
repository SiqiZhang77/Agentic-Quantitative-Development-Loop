"""
evaluate_result.py

RAE-17 result evaluation.

This tool should run AFTER get_backtest_results.py has produced the final
runtime_response object, and BEFORE that object is written to result.json.

It scores a completed run's performance_metrics against the optional success
thresholds Team IW supplied in the request (execution_objectives.target_criteria),
and returns an evaluation + recommended_action patch to merge into the final
runtime response.

Supported target_criteria keys (each maps onto one performance_metrics field
and a fixed comparison direction):
- min_total_return  -> total_return,  passes when actual >= threshold
- min_sharpe_ratio  -> sharpe_ratio,  passes when actual >= threshold
- max_drawdown      -> max_drawdown,  passes when actual >= threshold
  (threshold is the worst acceptable, e.g. -0.15; drawdowns are negative, so
  "no worse than" means >=)
- min_alpha         -> alpha,  passes when actual >= threshold
- min_beta          -> beta,   passes when actual >= threshold
- max_beta          -> beta,   passes when actual <= threshold

A target_criteria key outside this table is not scored; it is reported back
verbatim in evaluation.unrecognised_criteria instead of being silently dropped.
A criterion whose threshold is null is skipped (treated as "not set").

recommended_action is one of:
- accept   -> status SUCCESS and evaluation.met_criteria is true
- iterate  -> status SUCCESS and evaluation.met_criteria is false
- review   -> status SUCCESS and evaluation.met_criteria is null (nothing
              usable to score against, a human should look)
- escalate -> status is FAILED or TIMEOUT (the run itself didn't complete)

RAE never reports met_criteria true/false without a clear basis for it: with
no target_criteria supplied, or a non-SUCCESS status, met_criteria is null,
confidence is 0.0, criteria_results is empty, and recommended_action falls
back to "review" or "escalate".
"""

from __future__ import annotations

from typing import Any, Optional


SCHEMA_VERSION = "1.0"

# criterion name -> (performance_metrics key, comparator)
# "min": actual must be >= threshold to pass
# "max": actual must be <= threshold to pass
CRITERIA_METRIC_MAP: dict[str, tuple[str, str]] = {
    "min_total_return": ("total_return", "min"),
    "min_sharpe_ratio": ("sharpe_ratio", "min"),
    # max_drawdown is the worst acceptable (negative) drawdown floor, e.g. -0.15.
    # The run's actual drawdown must not be worse than that floor.
    "max_drawdown": ("max_drawdown", "min"),
    "min_alpha": ("alpha", "min"),
    "min_beta": ("beta", "min"),
    "max_beta": ("beta", "max"),
}

RECOMMENDED_ACTIONS = {"accept", "iterate", "review", "escalate"}

# Statuses for which performance_metrics are considered trustworthy enough to score.
SCORABLE_STATUSES = {"SUCCESS"}


def extract_target_criteria(request_payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Read the optional success thresholds RAE should score the result against.

    IW supplies these through:
        request_payload["execution_objectives"]["target_criteria"]

    Returns None if omitted, null, or not a dict, so callers can tell "no
    criteria supplied" apart from "criteria supplied but empty".
    """
    execution_objectives = request_payload.get("execution_objectives") or {}
    target_criteria = execution_objectives.get("target_criteria")
    return target_criteria if isinstance(target_criteria, dict) else None


def score_criteria(
    performance_metrics: Optional[dict[str, Any]],
    target_criteria: Optional[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Score each supplied criterion against the run's performance metrics.

    Returns (criteria_results, unrecognised_criteria). A criterion with a
    metric value of None scores passed=None (indeterminate) rather than
    failing outright, since the metric may simply not have been produced yet
    (RAE-10 normalises real engine output into this shape).
    """
    metrics = performance_metrics or {}
    results: list[dict[str, Any]] = []
    unrecognised: list[str] = []

    for criterion, threshold in (target_criteria or {}).items():
        if criterion not in CRITERIA_METRIC_MAP:
            unrecognised.append(criterion)
            continue
        if threshold is None:
            continue

        metric_key, comparator = CRITERIA_METRIC_MAP[criterion]
        actual = metrics.get(metric_key)

        if actual is None:
            passed = None
        elif comparator == "min":
            passed = actual >= threshold
        else:
            passed = actual <= threshold

        results.append(
            {
                "criterion": criterion,
                "metric": metric_key,
                "threshold": threshold,
                "actual": actual,
                "passed": passed,
            }
        )

    return results, unrecognised


def _met_criteria(criteria_results: list[dict[str, Any]]) -> Optional[bool]:
    """True only if every criterion passed; False if any explicitly failed;
    otherwise None (no criteria, or some indeterminate and none failed)."""
    if not criteria_results:
        return None
    if any(r["passed"] is False for r in criteria_results):
        return False
    if any(r["passed"] is None for r in criteria_results):
        return None
    return True


def _confidence(criteria_results: list[dict[str, Any]]) -> float:
    if not criteria_results:
        return 0.0
    scored = sum(1 for r in criteria_results if r["passed"] is not None)
    return round(scored / len(criteria_results), 4)


def _summary(
    criteria_results: list[dict[str, Any]],
    unrecognised_criteria: list[str],
    met_criteria: Optional[bool],
    target_criteria: Optional[dict[str, Any]],
    status: str,
) -> str:
    if status not in SCORABLE_STATUSES:
        return f"Run status was {status}; criteria were not evaluated."

    if not target_criteria:
        return "No target_criteria were supplied; nothing to score."

    if not criteria_results:
        return (
            "target_criteria were supplied but none were recognised "
            f"(unrecognised: {', '.join(unrecognised_criteria)})."
            if unrecognised_criteria
            else "target_criteria were supplied but contained no usable thresholds."
        )

    parts = []
    for r in criteria_results:
        mark = "?" if r["passed"] is None else ("PASS" if r["passed"] else "FAIL")
        parts.append(f"{r['criterion']}[{mark}]={r['actual']}")
    detail = ", ".join(parts)

    if met_criteria is True:
        summary = f"All {len(criteria_results)} criteria met: {detail}."
    elif met_criteria is False:
        failed = sum(1 for r in criteria_results if r["passed"] is False)
        summary = f"{failed}/{len(criteria_results)} criteria failed: {detail}."
    else:
        summary = f"Some criteria could not be evaluated (missing metrics): {detail}."

    if unrecognised_criteria:
        summary += f" Ignored unrecognised criteria: {', '.join(unrecognised_criteria)}."
    return summary


def _recommended_action(met_criteria: Optional[bool], status: str) -> str:
    if status not in SCORABLE_STATUSES:
        return "escalate"
    if met_criteria is True:
        return "accept"
    if met_criteria is False:
        return "iterate"
    return "review"


def evaluate_result(
    *,
    performance_metrics: Optional[dict[str, Any]],
    target_criteria: Optional[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    """
    Score a completed run's metrics against optional target_criteria and
    return the evaluation + recommended_action patch for the runtime response.

    Defaults (no target_criteria, or a non-SUCCESS status): met_criteria=None,
    confidence=0.0, criteria_results=[], recommended_action="review" or
    "escalate" - RAE never guesses pass/fail without a clear basis to do so.
    """
    if status in SCORABLE_STATUSES:
        criteria_results, unrecognised_criteria = score_criteria(performance_metrics, target_criteria)
    else:
        criteria_results, unrecognised_criteria = [], []

    met_criteria = _met_criteria(criteria_results)
    confidence = _confidence(criteria_results)
    summary = _summary(criteria_results, unrecognised_criteria, met_criteria, target_criteria, status)
    recommended_action = _recommended_action(met_criteria, status)

    return {
        "evaluation": {
            "met_criteria": met_criteria,
            "confidence": confidence,
            "criteria_results": criteria_results,
            "unrecognised_criteria": unrecognised_criteria,
            "summary": summary,
        },
        "recommended_action": recommended_action,
    }


def evaluate_from_runtime_response(
    request_payload: dict[str, Any],
    runtime_response: dict[str, Any],
) -> dict[str, Any]:
    """
    Convenience wrapper that pulls target_criteria from the request and
    performance_metrics/status from an existing runtime_response object.
    """
    execution_summary = runtime_response.get("execution_summary") or {}
    return evaluate_result(
        performance_metrics=runtime_response.get("performance_metrics"),
        target_criteria=extract_target_criteria(request_payload),
        status=execution_summary.get("status", "FAILED"),
    )


def apply_evaluation(
    request_payload: dict[str, Any],
    runtime_response: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy of runtime_response with evaluation + recommended_action merged in."""
    patch = evaluate_from_runtime_response(request_payload, runtime_response)
    return {**runtime_response, **patch}
