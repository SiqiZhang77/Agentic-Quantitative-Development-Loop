from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "build_scorer_result_v2.py"
SPEC = importlib.util.spec_from_file_location("build_scorer_result_v2", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


def _legacy() -> dict:
    rows = []
    for item_id in range(1, 26):
        status = (
            "correct" if item_id == 1 else "incorrect" if item_id == 2 else "missing"
        )
        rows.append(
            {
                "item_id": item_id,
                "name": f"item-{item_id}",
                "source": "item_store" if item_id < 3 else "missing",
                "status": status,
            }
        )
    return {"items": rows}


def test_adapter_preserves_math_and_refines_missing_state(tmp_path: Path) -> None:
    trace = tmp_path / "visible.jsonl"
    trace.write_text(
        json.dumps(
            {
                "schema_version": "model-visible-transcript-v1",
                "record_type": "model_visible_tool_call",
                "architecture_mode": "single_agent",
                "role": "developer",
                "tool": "submit_t3_items",
                "attempt": 1,
                "exchange_sha256": "a" * 64,
                "arguments": {"items": [{"item_id": 3, "value": "not copied"}]},
                "result": {
                    "status": "rejected",
                    "rejections": [{"item_id": 3, "error": "shape"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = adapter.adapt_result(_legacy(), trace)
    assert result["correct_items"] == 1
    assert result["submitted_item_ids"] == [1, 2]
    assert result["submitted_incorrect_item_ids"] == [2]
    assert result["item_results"][2] == {
        "item_id": 3,
        "submission_state": "invalid_format",
        "correct": False,
    }
    assert result["item_results"][3] == {
        "item_id": 4,
        "submission_state": "not_attempted",
        "correct": False,
    }
    rendered = json.dumps(result)
    assert "not copied" not in rendered
    assert "expected" not in rendered


def test_adapter_rejects_duplicate_item_ids() -> None:
    legacy = _legacy()
    legacy["items"][24]["item_id"] = 1
    with pytest.raises(adapter.ResultAdapterError, match="unique"):
        adapter.adapt_result(legacy)
