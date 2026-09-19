from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

import github_mcp_server as server
import t3_quant_item_submission as submission
from quant_calculator import CALCULATION_ERROR_DETAILS
from t3_quant_item_store import load_item_store
from t3_quant_item_submission import (
    T3ItemSubmissionError,
    submit_t3_item_request,
    t3_item_submission_request_json_schema,
)


PUBLIC_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "experiments"
    / "shared"
    / "t3-quant-suite-v2"
    / "item_submission_schema_v2.json"
)


class RecordingFastMCP:
    def __init__(self, _name: str) -> None:
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


def _request(item: dict) -> dict:
    return {"schema_version": "t3-quant-item-submission-v2", "items": [item]}


def test_runtime_schema_is_exactly_the_public_v2_schema() -> None:
    public = json.loads(PUBLIC_SCHEMA.read_text(encoding="utf-8"))

    assert t3_item_submission_request_json_schema() == public
    assert server.T3ItemSubmissionRequest.__get_pydantic_json_schema__(None, None) == public
    assert public["properties"]["items"]["maxItems"] == 1


def test_one_item_is_saved_immediately_and_empty_request_only_queries(
    tmp_path: Path,
) -> None:
    audit = tmp_path / "mcp_attempt_1.jsonl"
    result = submit_t3_item_request(
        _request(
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": "calculation-1",
                "result_id": "path",
            }
        ),
        stored_calculations={
            "calculation-1": {"path": [float(index) / 1000 for index in range(120)]}
        },
        audit_destination=str(audit),
        attempt=1,
    )

    assert result["status"] == "success"
    assert result["submitted_count"] == result["accepted_count"] == 1
    assert result["saved_item_ids"] == [1]
    assert result["event_count"] == 1
    assert result["normalizations"] == []
    assert (tmp_path / "t3_quant_item_candidates.json").is_file()

    query = submit_t3_item_request(
        {"schema_version": "t3-quant-item-submission-v2", "items": []},
        stored_calculations={},
        audit_destination=str(tmp_path / "mcp_attempt_2.jsonl"),
        attempt=2,
    )
    assert query["mode"] == "query"
    assert query["saved_item_ids"] == [1]
    assert query["event_count"] == 1


def test_parallel_submit_receipt_uses_its_own_locked_save_summary(
    tmp_path: Path, monkeypatch
) -> None:
    audit = tmp_path / "mcp_attempt_1.jsonl"
    real_save = submission.save_item_candidate
    both_saves_finished = threading.Barrier(2)

    def save_then_wait(*args, **kwargs):
        summary = real_save(*args, **kwargs)
        both_saves_finished.wait(timeout=5)
        return summary

    monkeypatch.setattr(submission, "save_item_candidate", save_then_wait)

    def submit(item_id: int) -> dict:
        return submit_t3_item_request(
            _request(
                {
                    "item_id": item_id,
                    "candidate_type": "numeric_vector_ref",
                    "calculation_id": "calculation-1",
                    "result_id": f"path_{item_id}",
                }
            ),
            stored_calculations={
                "calculation-1": {
                    f"path_{item_id}": [
                        float(index) / 1000 for index in range(120)
                    ]
                }
            },
            audit_destination=str(audit),
            attempt=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(executor.map(submit, (1, 2)))

    # Both saves have finished before either caller continues.  A post-save
    # global read would therefore report candidate_count=2 to both callers.
    # Returning each locked save summary instead preserves the actual sequence:
    # one receipt observed the first accepted item, the other the second.
    assert sorted(receipt["candidate_count"] for receipt in receipts) == [1, 2]
    assert sorted(receipt["event_count"] for receipt in receipts) == [1, 2]
    assert {tuple(receipt["accepted_item_ids"]) for receipt in receipts} == {
        (1,),
        (2,),
    }
    final = load_item_store(tmp_path / "t3_quant_item_candidates.json")
    assert final["candidate_count"] == 2
    assert sorted(int(key) for key in final["candidates"]) == [1, 2]


def test_v2_rejects_a_multi_item_call_before_saving_any_item(tmp_path: Path) -> None:
    request = {
        "schema_version": "t3-quant-item-submission-v2",
        "items": [
            {"item_id": 17, "candidate_type": "date", "value": "2026-01-17"},
            {"item_id": 25, "candidate_type": "date", "value": "2026-01-25"},
        ],
    }

    with pytest.raises(
        T3ItemSubmissionError, match="item_submission_items_invalid"
    ):
        submit_t3_item_request(
            request,
            stored_calculations={},
            audit_destination=str(tmp_path / "audit.jsonl"),
            attempt=1,
        )

    assert not (tmp_path / "t3_quant_item_candidates.json").exists()


def test_second_attempt_replacement_keeps_content_free_first_attempt_evidence(
    tmp_path: Path,
) -> None:
    first = submit_t3_item_request(
        _request(
            {"item_id": 17, "candidate_type": "date", "value": "2026-01-17"}
        ),
        stored_calculations={},
        audit_destination=str(tmp_path / "mcp_attempt_1.jsonl"),
        attempt=1,
    )
    replacement = submit_t3_item_request(
        _request(
            {"item_id": 17, "candidate_type": "date", "value": "2026-02-17"}
        ),
        stored_calculations={},
        audit_destination=str(tmp_path / "mcp_attempt_2.jsonl"),
        attempt=2,
    )

    assert first["event_count"] == 1
    assert replacement["event_count"] == 2
    store = load_item_store(tmp_path / "t3_quant_item_candidates.json")
    assert store["candidates"]["17"]["attempt"] == 2
    assert store["candidates"]["17"]["document"] == {"value": "2026-02-17"}
    assert [event["attempt"] for event in store["events"]] == [1, 2]
    assert store["events"][0]["candidate_sha256"] == store["events"][1][
        "previous_candidate_sha256"
    ]
    assert all("document" not in event for event in store["events"])


def test_unambiguous_numeric_and_date_formats_are_normalized_before_save(
    tmp_path: Path,
) -> None:
    vector = submit_t3_item_request(
        _request(
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": "calculation-1",
                "result_id": "wrapped_path",
            }
        ),
        stored_calculations={
            "calculation-1": {
                "wrapped_path": {
                    "only_value": [str(index / 1000) for index in range(120)]
                }
            }
        },
        audit_destination=str(tmp_path / "audit.jsonl"),
        attempt=1,
    )
    scalar = submit_t3_item_request(
        _request(
            {
                "item_id": 5,
                "candidate_type": "numeric_scalar_ref",
                "calculation_id": "calculation-1",
                "result_id": "wrapped_scalar",
            }
        ),
        stored_calculations={
            "calculation-1": {"wrapped_scalar": {"answer": "-1.25e-2"}}
        },
        audit_destination=str(tmp_path / "audit.jsonl"),
        attempt=1,
    )
    date_result = submit_t3_item_request(
        _request(
            {"item_id": 17, "candidate_type": "date", "value": "2026/01/17"}
        ),
        stored_calculations={},
        audit_destination=str(tmp_path / "audit.jsonl"),
        attempt=1,
    )
    dates_result = submit_t3_item_request(
        _request(
            {
                "item_id": 18,
                "candidate_type": "date_list",
                "values": [
                    "2026/01/01",
                    "2026/01/02",
                    "2026/01/03",
                    "2026/01/04",
                    "2026/01/05",
                    "2026/01/06",
                ],
            }
        ),
        stored_calculations={},
        audit_destination=str(tmp_path / "audit.jsonl"),
        attempt=1,
    )

    assert vector["normalizations"] == [
        "single_key_wrapper",
        "finite_numeric_strings",
    ]
    assert scalar["normalizations"] == [
        "single_key_wrapper",
        "finite_numeric_string",
    ]
    assert date_result["normalizations"] == ["slash_date_to_iso"]
    assert dates_result["normalizations"] == ["slash_dates_to_iso"]
    store = load_item_store(tmp_path / "t3_quant_item_candidates.json")
    assert store["candidates"]["1"]["document"]["values"][119] == 0.119
    assert store["candidates"]["5"]["document"] == {"value": -0.0125}
    assert store["candidates"]["17"]["document"] == {"value": "2026-01-17"}
    assert store["candidates"]["18"]["document"]["values"][0] == "2026-01-01"


@pytest.mark.parametrize(
    ("item", "stored", "expected_rejection"),
    [
        (
            {
                "item_id": 5,
                "candidate_type": "numeric_scalar_ref",
                "calculation_id": "calculation-1",
                "result_id": "bad",
            },
            {"calculation-1": {"bad": [1.0, 2.0]}},
            {
                "item_id": 5,
                "error": "item_store_number_invalid",
                "expected_kind": "number",
                "actual_kind": "array",
                "actual_length": 2,
            },
        ),
        (
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": "calculation-1",
                "result_id": "bad",
            },
            {"calculation-1": {"bad": [1.0] * 119}},
            {
                "item_id": 1,
                "error": "item_store_document_invalid",
                "expected_kind": "array",
                "actual_kind": "array",
                "expected_length": 120,
                "actual_length": 119,
            },
        ),
        (
            {"item_id": 17, "candidate_type": "date", "value": "2026/02/30"},
            {},
            {
                "item_id": 17,
                "error": "item_store_date_invalid",
                "expected_kind": "date_string",
                "actual_kind": "string",
            },
        ),
    ],
)
def test_rejection_feedback_reports_only_expected_and_actual_shape(
    tmp_path: Path,
    item: dict,
    stored: dict,
    expected_rejection: dict,
) -> None:
    result = submit_t3_item_request(
        _request(item),
        stored_calculations=stored,
        audit_destination=str(tmp_path / "audit.jsonl"),
        attempt=1,
    )

    assert result["status"] == "failed"
    assert result["rejected_items"] == [expected_rejection]
    rendered = json.dumps(result, sort_keys=True)
    assert "1.0" not in rendered
    assert "2026/02/30" not in rendered


def test_real_server_path_shares_results_and_writes_content_free_audit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(server, "FastMCP", RecordingFastMCP)
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("E3_REF_SCOPE_PREFLIGHT_PASSED", "true")
    monkeypatch.setenv("E3_NEGATIVE_REF_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("E3_NEGATIVE_REF_SET_SHA256", "b" * 64)
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", str(audit))
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setenv("MAX_QUANT_CALCULATE_CALLS", "4")
    monkeypatch.setenv("RAE_EXPERIMENT_ATTEMPT", "1")
    tools = server.create_mcp_server().tools

    calculation = tools["quant_calculate"](
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "candidate_path",
                    "op": "add",
                    "args": {
                        "left": [987654.125 + index for index in range(120)],
                        "right": 0.0,
                    },
                }
            ],
            "return_ids": ["candidate_path"],
        }
    )
    submitted = tools["submit_t3_items"](
        _request(
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": calculation["calculation_id"],
                "result_id": "candidate_path",
            }
        )
    )

    assert calculation["status"] == "succeeded"
    assert submitted["status"] == "success"
    assert submitted["saved_item_ids"] == [1]
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [record["tool"] for record in records] == [
        "quant_calculate",
        "submit_t3_items",
    ]
    assert records[1]["attempt"] == 1
    assert records[1]["accepted_item_ids"] == [1]
    assert "987654.125" not in audit.read_text(encoding="utf-8")
    assert "arguments" not in records[1]


def test_empty_vector_error_can_be_corrected_and_saved_in_the_same_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit = tmp_path / "mcp_attempt_1.jsonl"
    monkeypatch.setattr(server, "FastMCP", RecordingFastMCP)
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("E3_REF_SCOPE_PREFLIGHT_PASSED", "true")
    monkeypatch.setenv("E3_NEGATIVE_REF_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("E3_NEGATIVE_REF_SET_SHA256", "b" * 64)
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", str(audit))
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setenv("MAX_QUANT_CALCULATE_CALLS", "4")
    monkeypatch.setenv("RAE_EXPERIMENT_ATTEMPT", "1")
    tools = server.create_mcp_server().tools

    rejected = tools["quant_calculate"](
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {"id": "bad_path", "op": "sum", "args": {"values": []}}
            ],
            "return_ids": ["bad_path"],
        }
    )
    corrected = tools["quant_calculate"](
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "candidate_path",
                    "op": "add",
                    "args": {"left": [0.001] * 120, "right": 0.0},
                }
            ],
            "return_ids": ["candidate_path"],
        }
    )
    submitted = tools["submit_t3_items"](
        _request(
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": corrected["calculation_id"],
                "result_id": "candidate_path",
            }
        )
    )

    assert rejected == {
        "status": "failed",
        "error": "nonempty_vector_required",
        "error_detail": CALCULATION_ERROR_DETAILS["nonempty_vector_required"],
        "failed_operation_index": 0,
        "failed_operation_id": "bad_path",
        "failed_operation": "sum",
        "failed_argument": "values",
    }
    assert "expected minimum length 1" in rejected["error_detail"]
    assert "actual length 0" in rejected["error_detail"]
    assert corrected["status"] == "succeeded"
    assert submitted["status"] == "success"
    assert submitted["saved_item_ids"] == [1]
    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    assert [record["tool"] for record in records] == [
        "quant_calculate",
        "quant_calculate",
        "submit_t3_items",
    ]
