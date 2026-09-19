"""Public, answer-free tests for the T3 per-item submission contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


SUITE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SUITE_DIR / "item_submission_schema_v1.json"
TASK_PATH = SUITE_DIR / "TASK.md"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _candidate_definitions() -> dict[str, dict]:
    variants = _schema()["properties"]["items"]["items"]["oneOf"]
    return {
        variant["properties"]["candidate_type"]["const"]: variant
        for variant in variants
    }


def _valid_five_type_batch() -> dict:
    return {
        "schema_version": "t3-quant-item-submission-v1",
        "items": [
            {
                "item_id": 1,
                "candidate_type": "numeric_vector_ref",
                "calculation_id": "calculation-1",
                "result_id": "asset_a_path",
            },
            {
                "item_id": 5,
                "candidate_type": "numeric_scalar_ref",
                "calculation_id": "calculation-2",
                "result_id": "asset_a_terminal",
            },
            {
                "item_id": 17,
                "candidate_type": "date",
                "value": "2026-01-02",
            },
            {
                "item_id": 18,
                "candidate_type": "date_list",
                "values": [
                    "2026-01-02",
                    "2026-01-03",
                    "2026-01-04",
                    "2026-01-05",
                    "2026-01-06",
                    "2026-01-07",
                ],
            },
            {
                "item_id": 19,
                "candidate_type": "rebalance_rows_ref",
                "calculation_id": "calculation-4",
                "result_id": "rebalance_matrix",
                "indices": [4, 14, 24, 34, 44, 54],
            },
        ],
    }


def test_schema_accepts_query_and_all_five_request_structures() -> None:
    validator = _validator()

    validator.validate(
        {"schema_version": "t3-quant-item-submission-v1", "items": []}
    )
    validator.validate(_valid_five_type_batch())


@pytest.mark.parametrize(
    "mutation",
    [
        # A vector item cannot inline the hidden 120-number result.
        {
            "item_id": 1,
            "candidate_type": "numeric_vector_ref",
            "value": [0.1, 0.2],
        },
        # Item 18 has a date-list structure, not a numeric scalar reference.
        {
            "item_id": 18,
            "candidate_type": "numeric_scalar_ref",
            "calculation_id": "calculation-1",
            "result_id": "wrong_shape",
        },
        # The public calculator permits only calculation-1 through calculation-4.
        {
            "item_id": 5,
            "candidate_type": "numeric_scalar_ref",
            "calculation_id": "calculation-5",
            "result_id": "answer",
        },
        # Item 19 requires six distinct, non-negative row positions.
        {
            "item_id": 19,
            "candidate_type": "rebalance_rows_ref",
            "calculation_id": "calculation-4",
            "result_id": "rebalance_matrix",
            "indices": [4, 4, 24, 34, 44, 54],
        },
    ],
)
def test_schema_rejects_wrong_structure_or_reference(mutation: dict) -> None:
    request = {
        "schema_version": "t3-quant-item-submission-v1",
        "items": [mutation],
    }

    assert list(_validator().iter_errors(request))


def test_schema_assigns_every_public_item_to_exactly_one_candidate_type() -> None:
    definitions = _candidate_definitions()
    expected = {
        "numeric_vector_ref": set(range(1, 5)) | set(range(13, 16)),
        "numeric_scalar_ref": set(range(5, 13))
        | {16}
        | set(range(20, 25)),
        "date": {17, 25},
        "date_list": {18},
        "rebalance_rows_ref": {19},
    }
    observed: set[int] = set()

    for definition_name, expected_ids in expected.items():
        item_id_schema = definitions[definition_name]["properties"]["item_id"]
        actual_ids = set(item_id_schema.get("enum", [item_id_schema.get("const")]))
        assert actual_ids == expected_ids
        assert observed.isdisjoint(actual_ids)
        observed.update(actual_ids)

    assert observed == set(range(1, 26))


def test_numeric_requests_contain_references_not_inline_values() -> None:
    definitions = _candidate_definitions()

    for definition_name in (
        "numeric_vector_ref",
        "numeric_scalar_ref",
        "rebalance_rows_ref",
    ):
        properties = definitions[definition_name]["properties"]
        assert "calculation_id" in properties
        assert "result_id" in properties
        assert "value" not in properties
        assert "rows" not in properties


def test_task_explains_store_precedence_query_and_outer_attempt_limit() -> None:
    task = TASK_PATH.read_text(encoding="utf-8")

    assert "`submit_t3_items`" in task
    assert "empty `items` list" in task
    assert "only queries which item IDs are already saved" in task
    assert "Never inline or retype a numeric array" in task
    assert "item store is the primary mathematical submission" in task
    assert "may supply only an item ID that is absent from the store" in task
    assert "at most two complete attempts" in task
    assert "do not start, authorize or add an attempt" in task


def test_task_retains_public_mathematical_requirements() -> None:
    task = TASK_PATH.read_text(encoding="utf-8")

    for requirement in (
        "treat the gross portfolio as daily rebalanced to those weights",
        "sample daily volatility",
        "one-way turnover as half of the total absolute difference",
        "Apply returns before any end-of-day rebalance",
        "Sample daily volatility uses ddof=1",
        "abs(actual - expected) <= 1e-10 + 1e-9 * abs(expected)",
    ):
        assert requirement in task
