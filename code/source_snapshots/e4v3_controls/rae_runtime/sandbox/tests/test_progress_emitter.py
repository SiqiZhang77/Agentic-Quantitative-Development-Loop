import json
from pathlib import Path

from progress_emitter import emit_from_request, get_progress_events_path


def _base_request(tmp_path: Path, include_progress_path: bool = True) -> dict:
    output_paths = {
        "result_path": str(tmp_path / "result.json"),
        "artifact_dir": str(tmp_path / "artifacts"),
    }

    if include_progress_path:
        output_paths["progress_events_path"] = str(tmp_path / "progress_events.jsonl")

    return {
        "schema_version": "1.0",
        "run_id": "run-001",
        "jira_metadata": {
            "ticket_id": "SCRUM-46",
        },
        "output_paths": output_paths,
    }


def test_get_progress_events_path_returns_optional_path(tmp_path):
    request_payload = _base_request(tmp_path, include_progress_path=True)

    assert get_progress_events_path(request_payload).endswith("progress_events.jsonl")


def test_get_progress_events_path_returns_none_when_omitted(tmp_path):
    request_payload = _base_request(tmp_path, include_progress_path=False)

    assert get_progress_events_path(request_payload) is None


def test_emit_from_request_writes_jsonl_when_path_present(tmp_path):
    request_payload = _base_request(tmp_path, include_progress_path=True)

    emit_from_request(
        request_payload=request_payload,
        stage="backtest",
        status="RUNNING",
        iteration=1,
        agent="backtest_agent",
        tool_call="submit_backtest",
        message="Backtest submitted",
    )

    progress_path = Path(request_payload["output_paths"]["progress_events_path"])
    assert progress_path.exists()

    lines = progress_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    event = json.loads(lines[0])
    assert event["schema_version"] == "1.0"
    assert event["run_id"] == "run-001"
    assert event["ticket_id"] == "SCRUM-46"
    assert event["stage"] == "backtest"
    assert event["status"] == "RUNNING"
    assert event["agent"] == "backtest_agent"
    assert event["tool_call"] == "submit_backtest"


def test_emit_from_request_skips_when_path_omitted(tmp_path):
    request_payload = _base_request(tmp_path, include_progress_path=False)

    emit_from_request(
        request_payload=request_payload,
        stage="backtest",
        status="RUNNING",
        message="Backtest submitted",
    )

    assert not (tmp_path / "progress_events.jsonl").exists()
