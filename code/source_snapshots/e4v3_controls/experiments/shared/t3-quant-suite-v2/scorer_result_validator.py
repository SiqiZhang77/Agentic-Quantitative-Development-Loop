"""Validate public, answer-free T3 scorer-result relationships.

JSON Schema fixes the shape of the result.  This module adds the relationships
that JSON Schema cannot express cleanly: submitted and missing IDs partition all
25 items, submitted-incorrect IDs are a subset of submitted IDs, and missing
work must never be counted as correct.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Final, Mapping

from jsonschema import Draft202012Validator


SCHEMA_PATH: Final = Path(__file__).with_name("scorer_result_schema_v2.json")
TOTAL_ITEMS: Final = 25
ACCEPTED_STATE: Final = "accepted"


class ScorerResultContractError(ValueError):
    """A scorer result is structurally invalid or internally inconsistent."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ScorerResultContractError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ScorerResultContractError(f"non-finite JSON constant: {value}")


def _schema() -> dict[str, Any]:
    value = json.loads(
        SCHEMA_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    Draft202012Validator.check_schema(value)
    return value


def load_scorer_result(path: Path) -> dict[str, Any]:
    """Load one result without silently accepting duplicate keys or NaN."""

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ScorerResultContractError("scorer result root must be an object")
    return validate_scorer_result(value)


def _canonical_id_list(value: object, field: str) -> list[int]:
    if not isinstance(value, list):
        raise ScorerResultContractError(f"{field} must be an item-ID list")
    if value != sorted(value):
        raise ScorerResultContractError(f"{field} must be in ascending order")
    return value


def validate_scorer_result(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a defensive copy after structural and relationship validation."""

    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.absolute_path) or "<root>"
        raise ScorerResultContractError(
            f"scorer result {location} violates {first.validator or 'schema'}"
        )

    item_results = value["item_results"]
    observed_ids = [item["item_id"] for item in item_results]
    expected_ids = list(range(1, TOTAL_ITEMS + 1))
    if observed_ids != expected_ids:
        raise ScorerResultContractError(
            "item_results must contain item IDs 1 through 25 in ascending order"
        )

    submitted = _canonical_id_list(
        value["submitted_item_ids"], "submitted_item_ids"
    )
    missing = _canonical_id_list(value["missing_item_ids"], "missing_item_ids")
    submitted_incorrect = _canonical_id_list(
        value["submitted_incorrect_item_ids"],
        "submitted_incorrect_item_ids",
    )

    expected_submitted = [
        item["item_id"]
        for item in item_results
        if item["submission_state"] == ACCEPTED_STATE
    ]
    expected_missing = [
        item["item_id"]
        for item in item_results
        if item["submission_state"] != ACCEPTED_STATE
    ]
    expected_incorrect = [
        item["item_id"]
        for item in item_results
        if item["submission_state"] == ACCEPTED_STATE and item["correct"] is False
    ]
    expected_correct_ids = [
        item["item_id"]
        for item in item_results
        if item["submission_state"] == ACCEPTED_STATE and item["correct"] is True
    ]
    expected_correct = sum(item["correct"] is True for item in item_results)

    if any(
        item["correct"] is True and item["submission_state"] != ACCEPTED_STATE
        for item in item_results
    ):
        raise ScorerResultContractError(
            "an item without an accepted candidate cannot be correct"
        )
    if submitted != expected_submitted:
        raise ScorerResultContractError(
            "submitted_item_ids do not match accepted item_results"
        )
    if missing != expected_missing:
        raise ScorerResultContractError(
            "missing_item_ids do not match item_results without accepted candidates"
        )
    if submitted_incorrect != expected_incorrect:
        raise ScorerResultContractError(
            "submitted_incorrect_item_ids do not match accepted incorrect items"
        )
    if value["correct_items"] != expected_correct:
        raise ScorerResultContractError(
            "correct_items does not equal the number of correct per-item rows"
        )
    if set(submitted) & set(missing) or set(submitted) | set(missing) != set(expected_ids):
        raise ScorerResultContractError(
            "submitted and missing item IDs must be disjoint and cover 1 through 25"
        )
    if not set(submitted_incorrect).issubset(submitted):
        raise ScorerResultContractError(
            "submitted_incorrect_item_ids must be a subset of submitted_item_ids"
        )
    if (
        set(expected_correct_ids) & set(submitted_incorrect)
        or set(expected_correct_ids) | set(submitted_incorrect) != set(submitted)
    ):
        raise ScorerResultContractError(
            "submitted items must partition into correct and submitted-incorrect IDs"
        )

    return deepcopy(dict(value))
