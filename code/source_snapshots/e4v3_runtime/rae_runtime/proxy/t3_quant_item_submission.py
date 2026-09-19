"""Resolve and persist public T3 item submissions without scoring them.

The model-visible request contains either public dates or references to exact
results retained by ``quant_calculate`` in the current MCP process.  This
module copies those values into the run-local item store.  It never reads an
answer key, chooses a formula, or reports whether a candidate is correct.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from typing import Any, Final

from jsonschema import Draft202012Validator

from t3_quant_item_store import (
    T3QuantItemStoreError,
    item_store_path_from_audit,
    load_item_store,
    save_item_candidate,
)


REQUEST_SCHEMA_VERSION: Final = "t3-quant-item-submission-v2"
_SCHEMA_PATH: Final = (
    Path(__file__).resolve().parent
    / "exp3"
    / "schemas"
    / "t3_item_submission_v2.schema.json"
)
_CALCULATION_ID_RE: Final = re.compile(r"^calculation-[1-4]$")
_RESULT_ID_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FINITE_NUMBER_STRING_RE: Final = re.compile(
    r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$"
)
_SLASH_DATE_RE: Final = re.compile(r"^[0-9]{4}/[0-9]{2}/[0-9]{2}$")

_REQUEST_TYPE_BY_ITEM: Final = {
    **{item_id: "numeric_vector_ref" for item_id in (1, 2, 3, 4, 13, 14, 15)},
    **{
        item_id: "numeric_scalar_ref"
        for item_id in (5, 6, 7, 8, 9, 10, 11, 12, 16, 20, 21, 22, 23, 24)
    },
    17: "date",
    18: "date_list",
    19: "rebalance_rows_ref",
    25: "date",
}
_INTERNAL_TYPE_BY_REQUEST_TYPE: Final = {
    "numeric_vector_ref": "numeric_vector",
    "numeric_scalar_ref": "numeric_scalar",
    "date": "date",
    "date_list": "date_list",
    "rebalance_rows_ref": "rebalance_rows",
}
_FIELDS_BY_REQUEST_TYPE: Final = {
    "numeric_vector_ref": {
        "item_id",
        "candidate_type",
        "calculation_id",
        "result_id",
    },
    "numeric_scalar_ref": {
        "item_id",
        "candidate_type",
        "calculation_id",
        "result_id",
    },
    "date": {"item_id", "candidate_type", "value"},
    "date_list": {"item_id", "candidate_type", "values"},
    "rebalance_rows_ref": {
        "item_id",
        "candidate_type",
        "calculation_id",
        "result_id",
        "indices",
    },
}


class T3ItemSubmissionError(ValueError):
    """A stable, content-free submission failure."""

    def __init__(self, code: str, *, detail: Mapping[str, Any] | None = None):
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "item_submission_failed"
        self.code = code
        self.detail = _safe_detail(detail)
        super().__init__(code)


def _safe_detail(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only shape/type diagnostics that cannot contain answer values."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "expected_kind",
        "actual_kind",
        "expected_length",
        "actual_length",
        "expected_row_length",
        "actual_row_length",
    ):
        item = value.get(key)
        if key.endswith("kind"):
            if type(item) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item):
                result[key] = item
        elif type(item) is int and 0 <= item <= 10000:
            result[key] = item
    return result


def _actual_kind(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is str:
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "other"


def _shape_detail(
    value: Any,
    *,
    expected_kind: str,
    expected_length: int | None = None,
    expected_row_length: int | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "expected_kind": expected_kind,
        "actual_kind": _actual_kind(value),
    }
    if expected_length is not None:
        detail["expected_length"] = expected_length
    if isinstance(value, (list, Mapping)):
        detail["actual_length"] = len(value)
    if expected_row_length is not None:
        detail["expected_row_length"] = expected_row_length
        if isinstance(value, list) and value:
            row_lengths = {
                len(row) for row in value if isinstance(row, (list, Mapping))
            }
            if len(row_lengths) == 1:
                detail["actual_row_length"] = next(iter(row_lengths))
    return _safe_detail(detail)


def _unwrap_single_key(value: Any) -> tuple[Any, bool]:
    """Unwrap exactly one mapping layer when it contains only one value."""

    if isinstance(value, Mapping) and len(value) == 1:
        return deepcopy(next(iter(value.values()))), True
    return value, False


def _normalize_number(value: Any) -> tuple[Any, bool]:
    if type(value) is not str or _FINITE_NUMBER_STRING_RE.fullmatch(value) is None:
        return value, False
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        return value, False
    if not math.isfinite(parsed):
        return value, False
    if "." not in value and "e" not in value.lower():
        try:
            return int(value), True
        except ValueError:
            return parsed, True
    return parsed, True


def _normalize_numeric_tree(value: Any) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        normalized: list[Any] = []
        for item in value:
            checked, item_changed = _normalize_numeric_tree(item)
            normalized.append(checked)
            changed = changed or item_changed
        return normalized, changed
    return _normalize_number(value)


def _normalize_date(value: Any) -> tuple[Any, bool]:
    if type(value) is not str or _SLASH_DATE_RE.fullmatch(value) is None:
        return value, False
    candidate = value.replace("/", "-")
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError:
        return value, False
    return parsed.isoformat(), True


@lru_cache(maxsize=1)
def _cached_schema() -> dict[str, Any]:
    try:
        value = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(value)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("T3 item submission schema is unavailable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("T3 item submission schema is invalid")
    return value


def t3_item_submission_request_json_schema() -> dict[str, Any]:
    """Return a defensive copy of the exact public model-facing contract."""

    return deepcopy(_cached_schema())


def _stored_result(
    candidate: Mapping[str, Any],
    stored_calculations: Mapping[str, Mapping[str, Any]],
) -> Any:
    calculation_id = candidate.get("calculation_id")
    result_id = candidate.get("result_id")
    if (
        type(calculation_id) is not str
        or _CALCULATION_ID_RE.fullmatch(calculation_id) is None
        or type(result_id) is not str
        or _RESULT_ID_RE.fullmatch(result_id) is None
    ):
        raise T3ItemSubmissionError("item_reference_invalid")
    calculation = stored_calculations.get(calculation_id)
    if not isinstance(calculation, Mapping) or result_id not in calculation:
        raise T3ItemSubmissionError("item_reference_not_found")
    return deepcopy(calculation[result_id])


def _resolve_candidate(
    candidate: Any,
    stored_calculations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(candidate, Mapping):
        raise T3ItemSubmissionError("item_candidate_object_required")
    item_id = candidate.get("item_id")
    candidate_type = candidate.get("candidate_type")
    if type(item_id) is not int or item_id not in _REQUEST_TYPE_BY_ITEM:
        raise T3ItemSubmissionError("item_id_invalid")
    expected_type = _REQUEST_TYPE_BY_ITEM[item_id]
    if type(candidate_type) is not str or candidate_type != expected_type:
        raise T3ItemSubmissionError("item_candidate_type_invalid")
    if set(candidate) != _FIELDS_BY_REQUEST_TYPE[candidate_type]:
        raise T3ItemSubmissionError("item_candidate_fields_invalid")

    normalizations: list[str] = []
    if candidate_type == "numeric_vector_ref":
        result = _stored_result(candidate, stored_calculations)
        result, unwrapped = _unwrap_single_key(result)
        result, numeric_strings = _normalize_numeric_tree(result)
        if unwrapped:
            normalizations.append("single_key_wrapper")
        if numeric_strings:
            normalizations.append("finite_numeric_strings")
        document = {"values": result}
    elif candidate_type == "numeric_scalar_ref":
        result = _stored_result(candidate, stored_calculations)
        result, unwrapped = _unwrap_single_key(result)
        result, numeric_string = _normalize_number(result)
        if unwrapped:
            normalizations.append("single_key_wrapper")
        if numeric_string:
            normalizations.append("finite_numeric_string")
        document = {"value": result}
    elif candidate_type == "date":
        value, slash_date = _normalize_date(candidate.get("value"))
        if slash_date:
            normalizations.append("slash_date_to_iso")
        document = {"value": value}
    elif candidate_type == "date_list":
        values = deepcopy(candidate.get("values"))
        normalized_values: list[Any] = []
        slash_dates = False
        if isinstance(values, list):
            for value in values:
                checked, changed = _normalize_date(value)
                normalized_values.append(checked)
                slash_dates = slash_dates or changed
            values = normalized_values
        if slash_dates:
            normalizations.append("slash_dates_to_iso")
        document = {"values": values}
    else:
        result = _stored_result(candidate, stored_calculations)
        result, unwrapped = _unwrap_single_key(result)
        if unwrapped:
            normalizations.append("single_key_wrapper")
        indices = candidate.get("indices")
        if (
            not isinstance(result, list)
            or not isinstance(indices, list)
            or len(indices) != 6
            or any(type(index) is not int or index < 0 for index in indices)
            or len(set(indices)) != len(indices)
        ):
            raise T3ItemSubmissionError(
                "item_rebalance_selection_invalid",
                detail=_shape_detail(
                    result,
                    expected_kind="array",
                    expected_row_length=10,
                ),
            )
        try:
            selected = [deepcopy(result[index]) for index in indices]
        except IndexError as exc:
            raise T3ItemSubmissionError(
                "item_rebalance_selection_invalid",
                detail=_shape_detail(
                    result,
                    expected_kind="array",
                    expected_row_length=10,
                ),
            ) from exc
        selected, numeric_strings = _normalize_numeric_tree(selected)
        if numeric_strings:
            normalizations.append("finite_numeric_strings")
        document = {"rows": selected}

    return {
        "item_id": item_id,
        "candidate_type": _INTERNAL_TYPE_BY_REQUEST_TYPE[candidate_type],
        "document": document,
        "normalizations": normalizations,
    }


def _document_shape_detail(candidate_type: str, document: Any) -> dict[str, Any]:
    value: Any = document
    if isinstance(document, Mapping):
        if candidate_type in {"numeric_scalar", "date"}:
            value = document.get("value")
        elif candidate_type in {"numeric_vector", "date_list"}:
            value = document.get("values")
        elif candidate_type == "rebalance_rows":
            value = document.get("rows")
    if candidate_type == "numeric_scalar":
        return _shape_detail(value, expected_kind="number")
    if candidate_type == "date":
        return _shape_detail(value, expected_kind="date_string")
    if candidate_type == "numeric_vector":
        return _shape_detail(value, expected_kind="array", expected_length=120)
    if candidate_type == "date_list":
        return _shape_detail(value, expected_kind="array", expected_length=6)
    if candidate_type == "rebalance_rows":
        return _shape_detail(
            value,
            expected_kind="array",
            expected_length=6,
            expected_row_length=10,
        )
    return {}


def _safe_item_id(value: Any) -> int | None:
    if isinstance(value, Mapping):
        item_id = value.get("item_id")
        if type(item_id) is int and 1 <= item_id <= 25:
            return item_id
    return None


def submit_t3_item_request(
    request: Any,
    *,
    stored_calculations: Mapping[str, Mapping[str, Any]],
    audit_destination: str | None,
    attempt: int,
) -> dict[str, Any]:
    """Resolve a query or one item and atomically save that item immediately."""

    if not isinstance(request, Mapping):
        raise T3ItemSubmissionError("item_submission_object_required")
    if set(request) != {"schema_version", "items"}:
        raise T3ItemSubmissionError("item_submission_fields_invalid")
    if request.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise T3ItemSubmissionError("item_submission_schema_version_invalid")
    items = request.get("items")
    if not isinstance(items, list) or len(items) > 1:
        raise T3ItemSubmissionError("item_submission_items_invalid")
    if type(attempt) is not int or attempt not in {1, 2}:
        raise T3ItemSubmissionError("item_submission_attempt_invalid")
    if not isinstance(stored_calculations, Mapping):
        raise T3ItemSubmissionError("item_submission_calculations_invalid")

    try:
        store_path = item_store_path_from_audit(audit_destination)
        before = load_item_store(store_path)
    except T3QuantItemStoreError as exc:
        raise T3ItemSubmissionError(exc.code) from exc

    if not items:
        return {
            "status": "success",
            "mode": "query",
            "attempt": attempt,
            "submitted_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "accepted_item_ids": [],
            "rejected_items": [],
            "candidate_count": before["candidate_count"],
            "saved_item_ids": sorted(int(key) for key in before["candidates"]),
            "candidates_sha256": before["candidates_sha256"],
            "event_count": before["event_count"],
            "events_sha256": before["events_sha256"],
        }

    raw_candidate = items[0]
    safe_item_id = _safe_item_id(raw_candidate)
    accepted_item_ids: list[int] = []
    rejected_items: list[dict[str, Any]] = []
    normalizations: list[str] = []
    save_summary: dict[str, Any] | None = None
    try:
        candidate = _resolve_candidate(raw_candidate, stored_calculations)
        normalizations = candidate["normalizations"]
        save_summary = save_item_candidate(
            store_path,
            item_id=candidate["item_id"],
            attempt=attempt,
            candidate_type=candidate["candidate_type"],
            document=candidate["document"],
        )
    except T3ItemSubmissionError as exc:
        rejected_items.append(
            {"item_id": safe_item_id, "error": exc.code, **exc.detail}
        )
    except T3QuantItemStoreError as exc:
        detail = (
            _document_shape_detail(candidate["candidate_type"], candidate["document"])
            if "candidate" in locals()
            else {}
        )
        rejected_items.append(
            {"item_id": safe_item_id, "error": exc.code, **detail}
        )
    else:
        accepted_item_ids.append(candidate["item_id"])

    accepted_item_ids.sort()
    rejected_items.sort(
        key=lambda item: (
            item["item_id"] is None,
            item["item_id"] if item["item_id"] is not None else 0,
            item["error"],
        )
    )
    status = "success" if accepted_item_ids else "failed"
    # A successful submit must report the exact store state created while its
    # read-modify-write lock was still held.  Reading the shared file again here
    # would allow another concurrent submit to change the totals before this
    # caller receives its receipt.  A rejected submit made no change, so its
    # pre-submit snapshot remains the relevant content-free store summary.
    store_summary = save_summary if save_summary is not None else before
    saved_item_ids = (
        list(store_summary["saved_item_ids"])
        if "saved_item_ids" in store_summary
        else sorted(int(key) for key in store_summary["candidates"])
    )
    return {
        "status": status,
        "mode": "submit",
        "attempt": attempt,
        "submitted_count": len(items),
        "accepted_count": len(accepted_item_ids),
        "rejected_count": len(rejected_items),
        "accepted_item_ids": accepted_item_ids,
        "rejected_items": rejected_items,
        "candidate_count": store_summary["candidate_count"],
        "saved_item_ids": saved_item_ids,
        "candidates_sha256": store_summary["candidates_sha256"],
        "event_count": store_summary["event_count"],
        "events_sha256": store_summary["events_sha256"],
        "normalizations": normalizations,
    }
