#!/usr/bin/env python3
"""Convert private 25-item scorer output into the answer-free v2 contract.

Mathematical correctness remains owned by the separately hashed private scorer.
This adapter never receives an answer key. It uses only value-free per-item
statuses and model-visible tool results, and never copies candidate values,
expected answers, prompts or model output into its result.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping


TOTAL_ITEMS = 25
MISSING_STATES = frozenset(
    {"invalid_format", "explicit_abstain", "tool_failure", "not_attempted"}
)


class ResultAdapterError(ValueError):
    """Input evidence is malformed or internally inconsistent."""


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_missing_states(trace_path: Path | None) -> dict[int, str]:
    """Extract value-free failure states from captured submit-tool results."""

    if trace_path is None or not trace_path.is_file():
        return {}
    states: dict[int, str] = {}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, Mapping):
            raise ResultAdapterError("visible trace record must be an object")
        if (
            record.get("record_type") != "model_visible_tool_call"
            or record.get("tool") != "submit_t3_items"
        ):
            continue
        result = record.get("result")
        if not isinstance(result, Mapping):
            continue
        rejections = result.get("rejections")
        if isinstance(rejections, list):
            for rejection in rejections:
                if not isinstance(rejection, Mapping):
                    continue
                item_id = rejection.get("item_id")
                if type(item_id) is int and 1 <= item_id <= TOTAL_ITEMS:
                    states[item_id] = "invalid_format"
        item_id = result.get("item_id")
        status = result.get("status")
        if (
            type(item_id) is int
            and 1 <= item_id <= TOTAL_ITEMS
            and status in {"failed", "error", "tool_failure"}
            and item_id not in states
        ):
            states[item_id] = "tool_failure"
    return states


def adapt_result(
    legacy: Mapping[str, Any], trace_path: Path | None = None
) -> dict[str, Any]:
    items = legacy.get("items")
    if not isinstance(items, list) or len(items) != TOTAL_ITEMS:
        raise ResultAdapterError("private scorer must provide exactly 25 item rows")
    by_id: dict[int, Mapping[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ResultAdapterError("private scorer item row must be an object")
        item_id = item.get("item_id")
        if type(item_id) is not int or item_id not in range(1, TOTAL_ITEMS + 1):
            raise ResultAdapterError("private scorer item ID is invalid")
        if item_id in by_id:
            raise ResultAdapterError("private scorer item IDs must be unique")
        if item.get("status") not in {"correct", "incorrect", "missing"}:
            raise ResultAdapterError("private scorer item status is invalid")
        by_id[item_id] = item
    if set(by_id) != set(range(1, TOTAL_ITEMS + 1)):
        raise ResultAdapterError("private scorer item IDs must cover 1 through 25")

    observed_missing = _tool_missing_states(trace_path)
    item_results: list[dict[str, Any]] = []
    for item_id in range(1, TOTAL_ITEMS + 1):
        status = by_id[item_id]["status"]
        if status == "correct":
            state, correct = "accepted", True
        elif status == "incorrect":
            state, correct = "accepted", False
        else:
            state, correct = observed_missing.get(item_id, "not_attempted"), False
            if state not in MISSING_STATES:
                raise ResultAdapterError("derived missing state is invalid")
        item_results.append(
            {"item_id": item_id, "submission_state": state, "correct": correct}
        )

    submitted = [
        row["item_id"]
        for row in item_results
        if row["submission_state"] == "accepted"
    ]
    missing = [
        row["item_id"]
        for row in item_results
        if row["submission_state"] != "accepted"
    ]
    incorrect = [
        row["item_id"]
        for row in item_results
        if row["submission_state"] == "accepted" and row["correct"] is False
    ]
    return {
        "schema_version": "t3-quant-scorer-result-v2",
        "total_items": TOTAL_ITEMS,
        "correct_items": sum(row["correct"] is True for row in item_results),
        "submitted_item_ids": submitted,
        "missing_item_ids": missing,
        "submitted_incorrect_item_ids": incorrect,
        "item_results": item_results,
    }


def _validate(path: Path, value: Mapping[str, Any]) -> None:
    spec = importlib.util.spec_from_file_location("t3_scorer_result_validator", path)
    if spec is None or spec.loader is None:
        raise ResultAdapterError("public scorer-result validator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_scorer_result(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-score", type=Path, required=True)
    parser.add_argument("--visible-trace", type=Path)
    parser.add_argument("--validator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite existing scorer result")
    legacy = _load_json(args.private_score)
    if not isinstance(legacy, Mapping):
        raise SystemExit("private scorer result must be an object")
    result = adapt_result(legacy, args.visible_trace)
    _validate(args.validator, result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "converted",
                "correct_items": result["correct_items"],
                "total_items": TOTAL_ITEMS,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
