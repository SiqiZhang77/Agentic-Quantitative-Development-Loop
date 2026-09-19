from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import time

import pytest

from research_transcript import (
    ResearchTranscriptError,
    append_model_visible_output,
    append_model_visible_tool_call,
    capture_model_visible_output,
    capture_model_visible_tool_call,
)
import research_transcript


def test_visible_output_is_saved_without_prompt_or_secret(tmp_path: Path) -> None:
    audit = tmp_path / "mcp_tool_audit_attempt_1.jsonl"

    evidence = append_model_visible_output(
        audit,
        architecture_mode="manager_star",
        role="developer",
        phase="developer_to_manager",
        attempt=1,
        output={
            "summary": "I calculated and submitted items 1 and 2.",
            "api_key": "sk-do-not-store-this-value",
        },
    )

    path = tmp_path / "model_visible_transcript.jsonl"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["output"] == {
        "api_key": "[REDACTED_SECRET_FIELD]",
        "summary": "I calculated and submitted items 1 and 2.",
    }
    assert record["output_sha256"] == evidence["output_sha256"]
    assert "prompt" not in record
    assert "sk-do-not-store" not in path.read_text(encoding="utf-8")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_malformed_raw_output_redacts_named_secret_assignment(tmp_path: Path) -> None:
    audit = tmp_path / "mcp_tool_audit_attempt_1.jsonl"
    secret = "company-private-token-value"

    append_model_visible_output(
        audit,
        architecture_mode="manager_star",
        role="manager",
        phase="manager_to_architect",
        attempt=1,
        output='{"instruction":"unfinished","api_key":"' + secret,
    )

    path = tmp_path / "model_visible_transcript.jsonl"
    rendered = path.read_text(encoding="utf-8")
    record = json.loads(rendered)
    assert secret not in rendered
    assert "[REDACTED_SECRET_VALUE]" in record["output"]
    assert "prompt" not in record


def test_transcript_rejects_relative_or_oversized_output(tmp_path: Path) -> None:
    with pytest.raises(ResearchTranscriptError, match="audit path"):
        append_model_visible_output(
            "relative-audit.jsonl",
            architecture_mode="single_agent",
            role="developer",
            phase="single_agent_developer",
            attempt=1,
            output="visible",
        )

    with pytest.raises(ResearchTranscriptError, match="exceeds"):
        append_model_visible_output(
            tmp_path / "audit.jsonl",
            architecture_mode="single_agent",
            role="developer",
            phase="single_agent_developer",
            attempt=1,
            output="x" * (3 * 1024 * 1024),
        )


def test_tool_call_keeps_visible_mapping_and_rejection_without_secret(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"

    append_model_visible_tool_call(
        audit,
        architecture_mode="single_agent",
        role="developer",
        tool="submit_t3_items",
        attempt=1,
        arguments={"request": {"items": [{"item_id": 7, "result_id": "asset_c"}]}},
        result={
            "status": "failed",
            "rejected_items": [
                {"item_id": 7, "error": "item_store_number_invalid"}
            ],
            "authorization": "Bearer must-not-survive",
        },
    )

    record = json.loads(
        (tmp_path / "model_visible_transcript.jsonl").read_text(encoding="utf-8")
    )
    assert record["record_type"] == "model_visible_tool_call"
    assert record["arguments"]["request"]["items"][0]["item_id"] == 7
    assert record["result"]["rejected_items"][0]["error"] == (
        "item_store_number_invalid"
    )
    assert record["result"]["authorization"] == "[REDACTED_SECRET_FIELD]"


def test_existing_transcript_permissions_are_tightened_before_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "model_visible_transcript.jsonl"
    path.write_text("", encoding="utf-8")
    path.chmod(0o644)

    append_model_visible_output(
        tmp_path / "audit.jsonl",
        architecture_mode="single_agent",
        role="developer",
        phase="single_agent_developer",
        attempt=1,
        output="visible",
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_safe_capture_failure_keeps_content_free_status_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "company-private-token-value"

    def fail(*_args, **_kwargs):
        raise ResearchTranscriptError("model-visible transcript write failed")

    monkeypatch.setattr(research_transcript, "_append_record", fail)
    evidence = capture_model_visible_output(
        tmp_path / "audit.jsonl",
        architecture_mode="manager_star",
        role="manager",
        phase="manager_final",
        attempt=1,
        output={"summary": "complete", "api_key": secret},
    )

    assert evidence["capture_status"] == "failed"
    assert evidence["capture_error"] == "model_visible_transcript_write_failed"
    assert evidence["capture_evidence_status"] == "persisted"
    rendered = (tmp_path / "model_visible_transcript_capture.jsonl").read_text(
        encoding="utf-8"
    )
    record = json.loads(rendered)
    assert record["capture_status"] == "failed"
    assert record["capture_error"] == "model_visible_transcript_write_failed"
    assert "output" not in record
    assert "prompt" not in record
    assert secret not in rendered


def test_safe_tool_capture_failure_returns_status_instead_of_raising(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        research_transcript,
        "append_model_visible_tool_call",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ResearchTranscriptError("model-visible transcript is full")
        ),
    )

    evidence = capture_model_visible_tool_call(
        tmp_path / "audit.jsonl",
        architecture_mode="single_agent",
        role="developer",
        tool="submit_t3_items",
        attempt=2,
        arguments={"request": {"items": [{"item_id": 5}]}},
        result={"status": "success"},
    )

    assert evidence["capture_status"] == "failed"
    assert evidence["capture_error"] == "model_visible_transcript_is_full"
    assert evidence["capture_evidence_status"] == "persisted"


def test_parallel_partial_writes_remain_separate_json_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_write = research_transcript.os.write

    def partial_write(fd: int, payload: bytes) -> int:
        time.sleep(0.001)
        return original_write(fd, payload[:7])

    monkeypatch.setattr(research_transcript.os, "write", partial_write)
    audit = tmp_path / "audit.jsonl"

    def capture(index: int) -> None:
        append_model_visible_output(
            audit,
            architecture_mode="manager_star",
            role="developer",
            phase="developer_to_manager",
            attempt=1,
            output={"index": index, "summary": "visible"},
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(capture, range(16)))

    records = [
        json.loads(line)
        for line in (tmp_path / "model_visible_transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 16
    assert {record["output"]["index"] for record in records} == set(range(16))
