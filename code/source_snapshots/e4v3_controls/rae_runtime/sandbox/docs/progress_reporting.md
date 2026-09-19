# RAE-07 Progress Reporting

## Purpose

RAE emits minimal structured live progress events during execution so Team IW can show stage-level progress in Jira instead of waiting for only the final runtime response.

This is a temporary progress side-channel only. The canonical final output remains:

```text
/workspace/output/result.json
```

or whichever path IW passes as:

```text
runtime_request.output_paths.result_path
```

Progress events must not be written to stdout because stdout's last line is reserved for the final result JSON that Airflow reads for XCom.

## Relationship with runtime request and response

The runtime request controls whether progress events are written:

```json
{
  "output_paths": {
    "result_path": "/workspace/output/result.json",
    "artifact_dir": "/workspace/output/artifacts",
    "progress_events_path": "/workspace/output/progress_events.jsonl"
  }
}
```

If `output_paths.progress_events_path` is omitted or null, RAE skips progress reporting.

The runtime response remains the final contract. Progress events are a live subset of the final trace, not a competing trace format.

## Event shape

Required fields:

```json
{
  "schema_version": "1.0",
  "run_id": "run-001",
  "ticket_id": "SCRUM-46",
  "stage": "backtest",
  "status": "RUNNING",
  "timestamp": "2026-06-22T10:00:00+00:00"
}
```

Optional fields:

```json
{
  "iteration": 1,
  "agent": "backtest_agent",
  "tool_call": "submit_backtest",
  "message": "Backtest submitted"
}
```

## Allowed stages

```text
fetch
modify
commit
backtest
report
```

Stages are modular and independent. A workflow does not need to emit every stage. For example, a backtest-only run may emit only `backtest` and `report`.

## Allowed statuses

```text
PENDING
RUNNING
SUCCESS
FAILED
SKIPPED
```

`SUCCESS`, `FAILED`, and `SKIPPED` align with `runtime_response.execution_summary.iteration_traces`.

`PENDING` and `RUNNING` are live-only progress states.

## Example usage

```python
from rae_runtime.progress_emitter import emit_from_request

emit_from_request(
    request_payload=request_payload,
    stage="backtest",
    status="RUNNING",
    iteration=1,
    agent="backtest_agent",
    tool_call="submit_backtest",
    message="Backtest submitted",
)
```

## Example lifecycle

```python
emit_from_request(
    request_payload=request_payload,
    stage="fetch",
    status="RUNNING",
    message="Fetching repository",
)

emit_from_request(
    request_payload=request_payload,
    stage="fetch",
    status="SUCCESS",
    message="Repository fetched",
)

emit_from_request(
    request_payload=request_payload,
    stage="backtest",
    status="RUNNING",
    message="Backtest submitted",
)

emit_from_request(
    request_payload=request_payload,
    stage="backtest",
    status="SUCCESS",
    message="Backtest completed",
)
```

## Failure behaviour

Progress event emission is fail-safe and non-blocking. If the JSONL path is unavailable, malformed, or unwritable, the actual RAE execution continues.

## HDFS scope

Progress events do not include HDFS fields.

HDFS upload configuration belongs to the runtime request, and final HDFS paths belong to the runtime response. Progress events should only report stage-level execution state.
