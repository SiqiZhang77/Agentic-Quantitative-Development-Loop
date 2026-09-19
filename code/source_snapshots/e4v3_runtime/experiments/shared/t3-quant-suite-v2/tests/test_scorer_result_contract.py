"""Offline tests for the public, answer-free T3 scorer-result contract."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


SUITE_DIR = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = SUITE_DIR / "scorer_result_validator.py"
SPEC = importlib.util.spec_from_file_location("t3_scorer_result_validator", VALIDATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def _result() -> dict:
    rows = []
    for item_id in range(1, 26):
        if item_id == 1:
            state, correct = "accepted", True
        elif item_id == 2:
            state, correct = "accepted", False
        elif item_id == 3:
            state, correct = "invalid_format", False
        elif item_id == 4:
            state, correct = "explicit_abstain", False
        elif item_id == 5:
            state, correct = "tool_failure", False
        else:
            state, correct = "not_attempted", False
        rows.append(
            {"item_id": item_id, "submission_state": state, "correct": correct}
        )
    return {
        "schema_version": "t3-quant-scorer-result-v2",
        "total_items": 25,
        "correct_items": 1,
        "submitted_item_ids": [1, 2],
        "missing_item_ids": list(range(3, 26)),
        "submitted_incorrect_item_ids": [2],
        "item_results": rows,
    }


def test_valid_result_separates_missing_from_submitted_incorrect() -> None:
    result = _result()

    assert VALIDATOR.validate_scorer_result(result) == result
    assert result["correct_items"] == 1
    assert result["total_items"] == 25
    assert result["submitted_incorrect_item_ids"] == [2]
    assert 2 not in result["missing_item_ids"]
    correct_ids = {
        item["item_id"] for item in result["item_results"] if item["correct"]
    }
    assert set(result["submitted_item_ids"]) == (
        correct_ids | set(result["submitted_incorrect_item_ids"])
    )
    assert correct_ids.isdisjoint(result["submitted_incorrect_item_ids"])
    assert set(result["submitted_item_ids"]).isdisjoint(result["missing_item_ids"])
    assert set(result["submitted_item_ids"]) | set(result["missing_item_ids"]) == set(
        range(1, 26)
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda result: result.__setitem__("correct_items", 2), "correct_items"),
        (
            lambda result: result.__setitem__("submitted_item_ids", [1, 2, 3]),
            "submitted_item_ids",
        ),
        (
            lambda result: result.__setitem__("missing_item_ids", list(range(4, 26))),
            "missing_item_ids",
        ),
        (
            lambda result: result.__setitem__(
                "submitted_incorrect_item_ids", [1, 2]
            ),
            "submitted_incorrect_item_ids",
        ),
        (
            lambda result: result["item_results"][2].__setitem__("correct", True),
            "cannot be correct",
        ),
        (
            lambda result: result["item_results"][1].__setitem__("item_id", 1),
            "item IDs 1 through 25",
        ),
    ],
)
def test_relationship_mismatch_is_rejected(mutation, message: str) -> None:
    result = copy.deepcopy(_result())
    mutation(result)

    with pytest.raises(VALIDATOR.ScorerResultContractError, match=message):
        VALIDATOR.validate_scorer_result(result)


def test_schema_rejects_answer_values_or_extra_fields() -> None:
    result = _result()
    result["item_results"][0]["answer"] = 123.45

    with pytest.raises(VALIDATOR.ScorerResultContractError, match="additionalProperties"):
        VALIDATOR.validate_scorer_result(result)


def test_loader_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":"t3-quant-scorer-result-v2",'
        '"total_items":25,"total_items":25}',
        encoding="utf-8",
    )

    with pytest.raises(VALIDATOR.ScorerResultContractError, match="duplicate JSON key"):
        VALIDATOR.load_scorer_result(path)


def test_schema_is_valid_and_contains_no_candidate_value_field() -> None:
    schema = json.loads(
        (SUITE_DIR / "scorer_result_schema_v2.json").read_text(encoding="utf-8")
    )

    assert schema["properties"]["total_items"]["const"] == 25
    assert schema["properties"]["item_results"]["minItems"] == 25
    assert schema["properties"]["item_results"]["maxItems"] == 25
    assert set(schema["$defs"]["itemResult"]["properties"]) == {
        "item_id",
        "submission_state",
        "correct",
    }
