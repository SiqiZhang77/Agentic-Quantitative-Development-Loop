"""Strict, content-free reader for quant_calculate MCP audit records."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Final

from quant_calculator import (
    CALCULATION_ERROR_DETAILS,
    MAX_CALLS_PER_RUN,
    MAX_OPERATIONS,
    MAX_RESULT_CELLS,
    SUPPORTED_OPERATIONS,
    TOOL_NAME,
)


AUDIT_SCHEMA_VERSION: Final = "exp3-mcp-tool-audit-v1"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "cap_exceeded", "blocked"}
)


class QuantCalculatorAuditError(ValueError):
    """The persisted calculation-tool evidence is missing or unsafe."""


def read_quant_calculate_records(
    path: str | Path,
    *,
    required: bool = False,
) -> list[dict[str, Any]]:
    """Return sanitized records; never return numeric request/output content."""

    audit_path = Path(path)
    if not audit_path.exists():
        if required:
            raise QuantCalculatorAuditError("quant_calculate audit file is missing")
        return []
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise QuantCalculatorAuditError("quant_calculate audit file is unreadable") from exc

    records: list[dict[str, Any]] = []
    for raw in lines:
        if not raw.strip():
            raise QuantCalculatorAuditError("quant_calculate audit contains an empty record")
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QuantCalculatorAuditError(
                "quant_calculate audit contains malformed JSON"
            ) from exc
        if not isinstance(item, dict) or item.get("tool") != TOOL_NAME:
            continue
        if item.get("schema_version") != AUDIT_SCHEMA_VERSION:
            raise QuantCalculatorAuditError("quant_calculate audit schema is invalid")
        if item.get("record_type") != "tool_call":
            raise QuantCalculatorAuditError("quant_calculate audit record type is invalid")
        if item.get("role") != "developer":
            raise QuantCalculatorAuditError("quant_calculate audit role is invalid")
        status = item.get("status")
        if status not in _STATUSES:
            raise QuantCalculatorAuditError("quant_calculate audit status is invalid")
        error_code = item.get("error")
        if error_code is not None and (
            type(error_code) is not str or _SAFE_NAME_RE.fullmatch(error_code) is None
        ):
            raise QuantCalculatorAuditError("quant_calculate error code is invalid")
        error_detail = item.get("error_detail")
        if error_detail is not None and error_detail != CALCULATION_ERROR_DETAILS.get(
            error_code
        ):
            raise QuantCalculatorAuditError("quant_calculate error detail is invalid")
        failed_operation_index = item.get("failed_operation_index")
        if failed_operation_index is not None and (
            type(failed_operation_index) is not int
            or not 0 <= failed_operation_index < MAX_OPERATIONS
        ):
            raise QuantCalculatorAuditError(
                "quant_calculate failed operation index is invalid"
            )
        failed_operation = item.get("failed_operation")
        if failed_operation is not None and failed_operation not in SUPPORTED_OPERATIONS:
            raise QuantCalculatorAuditError(
                "quant_calculate failed operation is invalid"
            )
        failed_argument = item.get("failed_argument")
        if failed_argument is not None and (
            type(failed_argument) is not str
            or _SAFE_NAME_RE.fullmatch(failed_argument) is None
        ):
            raise QuantCalculatorAuditError(
                "quant_calculate failed argument is invalid"
            )
        input_sha256 = item.get("input_sha256")
        output_sha256 = item.get("output_sha256")
        if type(input_sha256) is not str or _HASH_RE.fullmatch(input_sha256) is None:
            raise QuantCalculatorAuditError("quant_calculate input hash is invalid")
        if output_sha256 is not None and (
            type(output_sha256) is not str
            or _HASH_RE.fullmatch(output_sha256) is None
        ):
            raise QuantCalculatorAuditError("quant_calculate output hash is invalid")
        stored_results_sha256 = item.get("stored_results_sha256")
        if stored_results_sha256 is not None and (
            type(stored_results_sha256) is not str
            or _HASH_RE.fullmatch(stored_results_sha256) is None
        ):
            raise QuantCalculatorAuditError(
                "quant_calculate stored-results hash is invalid"
            )
        latency = item.get("latency_seconds")
        if type(latency) not in (int, float) or not math.isfinite(float(latency)):
            raise QuantCalculatorAuditError("quant_calculate latency is invalid")
        if float(latency) < 0:
            raise QuantCalculatorAuditError("quant_calculate latency is invalid")
        operation_count = item.get("operation_count")
        if type(operation_count) is not int or operation_count < 0:
            raise QuantCalculatorAuditError(
                "quant_calculate operation count is invalid"
            )
        # The calculator rejects an oversized request before performing any
        # arithmetic, but the audit must still preserve that rejected attempt.
        # The only legitimate over-limit record is the calculator's explicit
        # rejection code. Successful or differently failed records remain
        # invalid, so a malformed audit cannot use this exception as a bypass.
        rejected_oversized_request = (
            status == "failed"
            and item.get("error") == "invalid_operation_count"
            and operation_count > MAX_OPERATIONS
        )
        if operation_count > MAX_OPERATIONS and not rejected_oversized_request:
            raise QuantCalculatorAuditError(
                "quant_calculate operation count exceeds the cap"
            )
        completed_operation_count = item.get("completed_operation_count")
        if completed_operation_count is not None and (
            type(completed_operation_count) is not int
            or not 0 <= completed_operation_count <= min(
                operation_count, MAX_OPERATIONS
            )
        ):
            raise QuantCalculatorAuditError(
                "quant_calculate completed operation count is invalid"
            )
        stored_result_shapes = item.get("stored_result_shapes")
        if stored_result_shapes is not None:
            if (
                not isinstance(stored_result_shapes, list)
                or not 1 <= len(stored_result_shapes) <= MAX_OPERATIONS
            ):
                raise QuantCalculatorAuditError(
                    "quant_calculate stored result shapes are invalid"
                )
            for shape_record in stored_result_shapes:
                if (
                    not isinstance(shape_record, dict)
                    or set(shape_record) != {"kind", "shape"}
                    or shape_record.get("kind") not in {"scalar", "array"}
                    or not isinstance(shape_record.get("shape"), list)
                    or len(shape_record["shape"]) > 5
                    or any(
                        type(size) is not int or not 0 <= size <= MAX_RESULT_CELLS
                        for size in shape_record["shape"]
                    )
                    or (
                        shape_record["kind"] == "scalar"
                        and shape_record["shape"] != []
                    )
                    or (
                        shape_record["kind"] == "array"
                        and not shape_record["shape"]
                    )
                ):
                    raise QuantCalculatorAuditError(
                        "quant_calculate stored result shapes are invalid"
                    )
        if status == "partial" and (
            stored_results_sha256 is None
            or completed_operation_count is None
            or completed_operation_count <= 0
            or completed_operation_count >= operation_count
            or not stored_result_shapes
            or error_code is None
            or failed_operation_index is None
        ):
            raise QuantCalculatorAuditError(
                "quant_calculate partial record is incomplete"
            )
        forbidden = {"arguments", "content", "request", "response", "result"}
        if forbidden.intersection(item):
            raise QuantCalculatorAuditError(
                "quant_calculate audit contains forbidden content"
            )
        record = {
            "tool": TOOL_NAME,
            "role": "developer",
            "status": status,
            "input_sha256": input_sha256,
            "output_sha256": output_sha256,
            "latency_seconds": round(float(latency), 6),
            "operation_count": operation_count,
        }
        if stored_results_sha256 is not None:
            record["stored_results_sha256"] = stored_results_sha256
        if completed_operation_count is not None:
            record["completed_operation_count"] = completed_operation_count
        if stored_result_shapes is not None:
            record["stored_result_shapes"] = stored_result_shapes
        for name, value in (
            ("error", error_code),
            ("error_detail", error_detail),
            ("failed_operation_index", failed_operation_index),
            ("failed_operation", failed_operation),
            ("failed_argument", failed_argument),
        ):
            if value is not None:
                record[name] = value
        records.append(record)
    if len(records) > MAX_CALLS_PER_RUN:
        raise QuantCalculatorAuditError("quant_calculate audit exceeds the call cap")
    if required and not records:
        raise QuantCalculatorAuditError("quant_calculate audit contains no tool call")
    return records
