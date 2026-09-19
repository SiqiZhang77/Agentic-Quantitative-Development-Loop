from __future__ import annotations

import json

import pytest

from quant_calculator import CALCULATION_ERROR_DETAILS
from quant_calculator_audit import (
    QuantCalculatorAuditError,
    read_quant_calculate_records,
)


def _record(**overrides):
    return {
        "schema_version": "exp3-mcp-tool-audit-v1",
        "record_type": "tool_call",
        "tool": "quant_calculate",
        "role": "developer",
        "status": "succeeded",
        "input_sha256": "a" * 64,
        "output_sha256": "b" * 64,
        "latency_seconds": 0.1,
        "operation_count": 2,
        **overrides,
    }


def test_reader_filters_repository_records_and_returns_only_safe_fields(tmp_path):
    path = tmp_path / "audit.jsonl"
    repository_record = {
        "schema_version": "exp3-mcp-tool-audit-v1",
        "record_type": "tool_call",
        "tool": "read_file",
        "role": "developer",
        "status": "success",
        "path": "public/input.csv",
    }
    calculator_record = _record(
        error=None,
        timestamp="2026-08-28T00:00:00Z",
    )
    path.write_text(
        "\n".join(
            [json.dumps(repository_record), json.dumps(calculator_record), ""]
        ),
        encoding="utf-8",
    )

    assert read_quant_calculate_records(path) == [
        {
            "tool": "quant_calculate",
            "role": "developer",
            "status": "succeeded",
            "input_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "latency_seconds": 0.1,
            "operation_count": 2,
        }
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "manager"},
        {"input_sha256": "bad"},
        {"latency_seconds": -1},
        {"operation_count": 65},
        {"content": "PRIVATE"},
    ],
)
def test_reader_rejects_identity_hash_limit_or_content_drift(tmp_path, overrides):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps(_record(**overrides)) + "\n", encoding="utf-8")

    with pytest.raises(QuantCalculatorAuditError):
        read_quant_calculate_records(path)


def test_reader_rejects_more_than_four_calls(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        "".join(json.dumps(_record()) + "\n" for _ in range(5)),
        encoding="utf-8",
    )

    with pytest.raises(QuantCalculatorAuditError, match="call cap"):
        read_quant_calculate_records(path)


def test_reader_preserves_rejected_oversized_request(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            _record(
                status="failed",
                error="invalid_operation_count",
                operation_count=69,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    records = read_quant_calculate_records(path)

    assert records[0]["status"] == "failed"
    assert records[0]["operation_count"] == 69


def test_reader_rejects_oversized_request_without_matching_error_code(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            _record(
                status="failed",
                error="nonempty_vector_required",
                operation_count=69,
            )
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(QuantCalculatorAuditError, match="exceeds the cap"):
        read_quant_calculate_records(path)


def test_reader_preserves_only_the_content_free_stored_results_hash(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(_record(stored_results_sha256="c" * 64)) + "\n",
        encoding="utf-8",
    )

    records = read_quant_calculate_records(path)

    assert records[0]["stored_results_sha256"] == "c" * 64


def test_reader_preserves_safe_failure_location_and_rule_without_values(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            _record(
                status="failed",
                error="invalid_operand_object",
                error_detail=CALCULATION_ERROR_DETAILS["invalid_operand_object"],
                failed_operation_index=4,
                failed_operation="subtract",
                failed_argument="left",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    records = read_quant_calculate_records(path)

    assert records[0]["error"] == "invalid_operand_object"
    assert records[0]["error_detail"] == CALCULATION_ERROR_DETAILS[
        "invalid_operand_object"
    ]
    assert records[0]["failed_operation_index"] == 4
    assert records[0]["failed_operation"] == "subtract"
    assert records[0]["failed_argument"] == "left"
    assert "request" not in records[0]


def test_reader_preserves_content_free_partial_result_evidence(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(
        json.dumps(
            _record(
                status="partial",
                operation_count=3,
                completed_operation_count=2,
                stored_results_sha256="c" * 64,
                stored_result_shapes=[
                    {"kind": "array", "shape": [120]},
                    {"kind": "scalar", "shape": []},
                ],
                error="nonempty_vector_required",
                failed_operation_index=2,
                failed_operation="sum",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    records = read_quant_calculate_records(path)

    assert records[0]["status"] == "partial"
    assert records[0]["completed_operation_count"] == 2
    assert records[0]["stored_results_sha256"] == "c" * 64
    assert records[0]["stored_result_shapes"] == [
        {"kind": "array", "shape": [120]},
        {"kind": "scalar", "shape": []},
    ]
    assert "result_id" not in json.dumps(records[0])


@pytest.mark.parametrize(
    "overrides",
    [
        {"completed_operation_count": 0},
        {"completed_operation_count": 3},
        {"stored_results_sha256": None},
        {"stored_result_shapes": []},
        {"stored_result_shapes": [{"kind": "array", "shape": []}]},
        {"error": None},
        {"failed_operation_index": None},
    ],
)
def test_reader_rejects_incomplete_or_invalid_partial_evidence(tmp_path, overrides):
    path = tmp_path / "audit.jsonl"
    partial = {
        "status": "partial",
        "operation_count": 3,
        "completed_operation_count": 2,
        "stored_results_sha256": "c" * 64,
        "stored_result_shapes": [{"kind": "array", "shape": [3]}],
        "error": "nonempty_vector_required",
        "failed_operation_index": 2,
        "failed_operation": "sum",
        **overrides,
    }
    path.write_text(json.dumps(_record(**partial)) + "\n", encoding="utf-8")

    with pytest.raises(QuantCalculatorAuditError):
        read_quant_calculate_records(path)
