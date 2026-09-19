"""Public, answer-free tests for the T3 immediate item-save contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


SUITE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = SUITE_DIR / "item_submission_schema_v2.json"
TASK_PATH = SUITE_DIR / "TASK.md"


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _candidate_definitions() -> dict[str, dict]:
    variants = _schema()["properties"]["items"]["items"]["oneOf"]
    return {
        variant["properties"]["candidate_type"]["const"]: variant
        for variant in variants
    }


def _valid_candidates() -> list[dict]:
    return [
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
        {"item_id": 17, "candidate_type": "date", "value": "2026/01/02"},
        {
            "item_id": 18,
            "candidate_type": "date_list",
            "values": [
                "2026-01-02",
                "2026/01/03",
                "2026-01-04",
                "2026/01/05",
                "2026-01-06",
                "2026/01/07",
            ],
        },
        {
            "item_id": 19,
            "candidate_type": "rebalance_rows_ref",
            "calculation_id": "calculation-4",
            "result_id": "rebalance_matrix",
            "indices": [4, 14, 24, 34, 44, 54],
        },
    ]


def test_schema_accepts_query_and_each_of_five_single_item_structures() -> None:
    validator = _validator()
    validator.validate(
        {"schema_version": "t3-quant-item-submission-v2", "items": []}
    )
    for candidate in _valid_candidates():
        validator.validate(
            {
                "schema_version": "t3-quant-item-submission-v2",
                "items": [candidate],
            }
        )


def test_schema_rejects_batch_even_when_each_item_is_valid() -> None:
    request = {
        "schema_version": "t3-quant-item-submission-v2",
        "items": _valid_candidates()[:2],
    }

    assert list(_validator().iter_errors(request))


@pytest.mark.parametrize(
    "mutation",
    [
        {
            "item_id": 1,
            "candidate_type": "numeric_vector_ref",
            "value": [0.1, 0.2],
        },
        {
            "item_id": 18,
            "candidate_type": "numeric_scalar_ref",
            "calculation_id": "calculation-1",
            "result_id": "wrong_shape",
        },
        {
            "item_id": 5,
            "candidate_type": "numeric_scalar_ref",
            "calculation_id": "calculation-5",
            "result_id": "answer",
        },
        {
            "item_id": 19,
            "candidate_type": "rebalance_rows_ref",
            "calculation_id": "calculation-4",
            "result_id": "rebalance_matrix",
            "indices": [4, 4, 24, 34, 44, 54],
        },
        {"item_id": 17, "candidate_type": "date", "value": "2026.01.02"},
    ],
)
def test_schema_rejects_wrong_structure_or_reference(mutation: dict) -> None:
    request = {
        "schema_version": "t3-quant-item-submission-v2",
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


def test_task_explains_immediate_save_normalization_evidence_and_attempt_limit() -> None:
    task = TASK_PATH.read_text(encoding="utf-8")

    for statement in (
        "As soon as one requested mathematical result is available",
        "exactly one item candidate",
        "do not wait to assemble a batch",
        "only queries which item IDs are already saved",
        "Never inline or retype a numeric array",
        "one atomic file replacement",
        "never returns an expected answer",
        "only three deterministic, answer-blind normalizations",
        "never pads or truncates an array",
        "append-only, value-free event",
        "item store is the primary mathematical submission",
        "at most two complete attempts",
        "do not start, authorize or add an attempt",
    ):
        assert statement in task


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


def test_rubric_separates_missing_from_submitted_incorrect_items() -> None:
    rubric = " ".join(
        (SUITE_DIR / "RUBRIC.md").read_text(encoding="utf-8").split()
    )

    for field in (
        "`correct_items`",
        "`total_items: 25`",
        "`submitted_item_ids`",
        "`missing_item_ids`",
        "`submitted_incorrect_item_ids`",
    ):
        assert field in rubric
    assert "A missing item is not correct" in rubric
    assert (
        "distinguishable from a saved candidate whose mathematics is wrong" in rubric
    )
