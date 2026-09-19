from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from experiments.exp3.e3v11 import item_store_projection as projection


def _sha(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _store() -> dict:
    document = {"value": 0.5}
    record = {
        "attempt": 1,
        "candidate_type": "numeric_scalar",
        "document": document,
        "candidate_bytes": len(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
        ),
        "candidate_sha256": _sha(
            {
                "attempt": 1,
                "candidate_type": "numeric_scalar",
                "document": document,
                "item_id": 5,
            }
        ),
    }
    candidates = {"5": record}
    event = {
        "sequence": 1,
        "attempt": 1,
        "item_id": 5,
        "action": "added",
        "candidate_type": "numeric_scalar",
        "candidate_bytes": record["candidate_bytes"],
        "candidate_sha256": record["candidate_sha256"],
        "previous_candidate_sha256": None,
    }
    events = [event]
    return {
        "schema_version": "t3-quant-item-store-v2",
        "candidate_count": 1,
        "candidates_sha256": _sha(candidates),
        "candidates": candidates,
        "event_count": 1,
        "events_sha256": _sha(events),
        "events": events,
    }


def test_projection_changes_only_the_outer_store_schema(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    destination = tmp_path / "projected.json"
    receipt = tmp_path / "receipt.json"
    source.write_text(json.dumps(_store()), encoding="utf-8")

    result = projection.project_item_store(source, destination, receipt)
    projected = json.loads(destination.read_text(encoding="utf-8"))
    original = _store()

    assert projected == {
        "schema_version": "t3-quant-item-store-v1",
        "candidate_count": original["candidate_count"],
        "candidates_sha256": original["candidates_sha256"],
        "candidates": original["candidates"],
    }
    assert result["candidate_content_modified"] is False
    assert result["answer_key_read"] is False
    assert result["candidates_sha256"] == original["candidates_sha256"]
    assert json.loads(receipt.read_text(encoding="utf-8")) == result


@pytest.mark.parametrize("field", ["candidates_sha256", "events_sha256"])
def test_projection_rejects_a_changed_candidate_or_event_hash(
    tmp_path: Path, field: str
) -> None:
    value = _store()
    value[field] = "0" * 64
    source = tmp_path / "source.json"
    source.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(projection.ItemStoreProjectionError):
        projection.project_item_store(
            source,
            tmp_path / "projected.json",
            tmp_path / "receipt.json",
        )

def test_projection_refuses_unknown_outer_fields(tmp_path: Path) -> None:
    value = _store()
    value["answer_hint"] = "not allowed"
    source = tmp_path / "source.json"
    source.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(projection.ItemStoreProjectionError, match="exact v2"):
        projection.project_item_store(
            source,
            tmp_path / "projected.json",
            tmp_path / "receipt.json",
        )
