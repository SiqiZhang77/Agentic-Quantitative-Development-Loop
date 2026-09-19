from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


SCHEMA_VERSION = "1.0"

VALID_STAGES = {
    "fetch",
    "modify",
    "commit",
    "backtest",
    "report",
}

# SUCCESS / FAILED / SKIPPED align with runtime_response.execution_summary.iteration_traces.
# PENDING / RUNNING are live-only progress states.
VALID_STATUSES = {
    "PENDING",
    "RUNNING",
    "SUCCESS",
    "FAILED",
    "SKIPPED",
}


def utc_now() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def get_progress_events_path(request_payload: dict[str, Any]) -> Optional[str]:
    """
    Return the optional JSONL progress channel path from the runtime request.

    IW controls this through:
        request_payload["output_paths"]["progress_events_path"]

    If omitted, null, or empty, RAE should skip progress reporting.
    """
    output_paths = request_payload.get("output_paths") or {}
    progress_events_path = output_paths.get("progress_events_path")
    return progress_events_path or None


def build_progress_event(
    *,
    run_id: str,
    ticket_id: str,
    stage: str,
    status: str,
    message: Optional[str] = None,
    iteration: Optional[int] = None,
    agent: Optional[str] = None,
    tool_call: Optional[str] = None,
) -> dict[str, Any]:
    """Build and validate a minimal progress event dictionary."""
    if not run_id:
        raise ValueError("run_id must not be empty")

    if not ticket_id:
        raise ValueError("ticket_id must not be empty")

    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage: {stage}")

    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    if iteration is not None and iteration < 1:
        raise ValueError("iteration must be >= 1 when provided")

    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "ticket_id": ticket_id,
        "stage": stage,
        "status": status,
        "timestamp": utc_now(),
        "iteration": iteration,
        "agent": agent,
        "tool_call": tool_call,
        "message": message,
    }


def emit_progress_event(
    *,
    run_id: str,
    ticket_id: str,
    stage: str,
    status: str,
    channel_path: Optional[str],
    message: Optional[str] = None,
    iteration: Optional[int] = None,
    agent: Optional[str] = None,
    tool_call: Optional[str] = None,
) -> None:
    """
    Append one progress event to the mounted JSONL side-channel.

    If channel_path is None or empty, no event is written. This follows IW's
    requested behaviour: progress reporting only happens when IW supplies
    output_paths.progress_events_path.

    This function is intentionally fail-safe. Any exception during progress
    reporting is swallowed so the actual RAE task can continue.
    """
    if not channel_path:
        return

    try:
        event = build_progress_event(
            run_id=run_id,
            ticket_id=ticket_id,
            stage=stage,
            status=status,
            message=message,
            iteration=iteration,
            agent=agent,
            tool_call=tool_call,
        )

        path = Path(channel_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")

    except Exception:
        # Progress reporting must not slow down, interrupt, or crash RAE execution.
        pass


def emit_from_request(
    *,
    request_payload: dict[str, Any],
    stage: str,
    status: str,
    message: Optional[str] = None,
    iteration: Optional[int] = None,
    agent: Optional[str] = None,
    tool_call: Optional[str] = None,
) -> None:
    """
    Convenience wrapper that extracts run_id, ticket_id, and progress_events_path
    from the runtime request payload.
    """
    try:
        emit_progress_event(
            run_id=request_payload["run_id"],
            ticket_id=request_payload["jira_metadata"]["ticket_id"],
            stage=stage,
            status=status,
            channel_path=get_progress_events_path(request_payload),
            message=message,
            iteration=iteration,
            agent=agent,
            tool_call=tool_call,
        )
    except Exception:
        # Keep progress reporting non-blocking even if request shape is unexpected.
        pass
