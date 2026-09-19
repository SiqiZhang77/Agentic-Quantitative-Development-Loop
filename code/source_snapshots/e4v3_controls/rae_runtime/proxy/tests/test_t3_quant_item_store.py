from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import stat
import threading
import time

import pytest

import t3_quant_item_store as store


def _document(item_id: int) -> tuple[str, dict]:
    if item_id in store.NUMERIC_VECTOR_ITEMS:
        return "numeric_vector", {"values": [float(index) for index in range(120)]}
    if item_id in store.NUMERIC_SCALAR_ITEMS:
        return "numeric_scalar", {"value": float(item_id) / 10.0}
    if item_id in store.DATE_ITEMS:
        return "date", {"value": "2026-08-31"}
    if item_id in store.DATE_LIST_ITEMS:
        return "date_list", {
            "values": [
                "2026-01-02",
                "2026-02-02",
                "2026-03-02",
                "2026-04-02",
                "2026-05-04",
                "2026-06-01",
            ]
        }
    if item_id in store.REBALANCE_ROWS_ITEMS:
        return "rebalance_rows", {
            "rows": [
                [float(row * 10 + column) for column in range(10)]
                for row in range(6)
            ]
        }
    raise AssertionError(f"missing test document for item {item_id}")


def _candidate(item_id: int) -> dict:
    candidate_type, document = _document(item_id)
    return {
        "item_id": item_id,
        "candidate_type": candidate_type,
        "document": document,
    }


def test_attempt_audits_in_one_directory_resolve_to_one_store(tmp_path: Path) -> None:
    first = store.item_store_path_from_audit(str(tmp_path / "attempt_1_mcp.jsonl"))
    second = store.item_store_path_from_audit(str(tmp_path / "attempt_2_mcp.jsonl"))

    assert first == second == tmp_path / store.ITEM_STORE_FILENAME
    assert store.load_item_store(first) == {
        "schema_version": "t3-quant-item-store-v2",
        "candidate_count": 0,
        "candidates_sha256": store._sha256({})[1],
        "candidates": {},
        "event_count": 0,
        "events_sha256": store._sha256([])[1],
        "events": [],
    }


def test_all_25_item_types_are_saved_with_verified_hashes(tmp_path: Path) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME

    summary = store.save_item_candidates(
        path,
        attempt=1,
        candidates=[_candidate(item_id) for item_id in range(1, 26)],
    )

    assert summary["submitted_count"] == 25
    assert summary["candidate_count"] == 25
    assert summary["added_item_ids"] == list(range(1, 26))
    assert summary["replaced_item_ids"] == []
    assert summary["saved_item_ids"] == list(range(1, 26))
    assert set(summary["candidate_sha256s"]) == {str(item) for item in range(1, 26)}
    payload = store.load_item_store(path)
    assert payload["candidate_count"] == 25
    assert payload["candidates_sha256"] == summary["candidates_sha256"]
    assert set(payload["candidates"]) == {str(item) for item in range(1, 26)}
    assert payload["candidates"]["13"]["candidate_type"] == "numeric_vector"
    assert payload["candidates"]["19"]["candidate_type"] == "rebalance_rows"
    assert payload["candidates"]["25"]["candidate_type"] == "date"
    assert payload["event_count"] == 25
    assert payload["events_sha256"] == summary["events_sha256"]
    assert [event["sequence"] for event in payload["events"]] == list(range(1, 26))
    assert all(event["action"] == "added" for event in payload["events"])
    assert all(
        event["previous_candidate_sha256"] is None
        and "document" not in event
        for event in payload["events"]
    )
    assert all(
        len(record["candidate_sha256"]) == 64
        and record["candidate_bytes"] > 0
        and record["attempt"] == 1
        for record in payload["candidates"].values()
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_parallel_item_saves_preserve_every_accepted_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Concurrent MCP calls must not replace one another's older reads."""

    path = tmp_path / store.ITEM_STORE_FILENAME
    item_ids = [5, 6, 7, 8, 9, 10, 11, 12]
    start = threading.Barrier(len(item_ids))
    original_read = store._read_payload

    def slow_read(store_path):
        payload = original_read(store_path)
        # Without a lock that begins before this read, all workers observe the
        # empty store before any of them performs its atomic replacement.
        time.sleep(0.03)
        return payload

    monkeypatch.setattr(store, "_read_payload", slow_read)

    def save(item_id: int) -> dict:
        start.wait(timeout=2)
        return store.save_item_candidate(
            path,
            item_id=item_id,
            attempt=1,
            candidate_type="numeric_scalar",
            document={"value": float(item_id)},
        )

    with ThreadPoolExecutor(max_workers=len(item_ids)) as pool:
        summaries = list(pool.map(save, item_ids))

    payload = store.load_item_store(path)
    assert len(summaries) == len(item_ids)
    assert payload["candidate_count"] == len(item_ids)
    assert set(payload["candidates"]) == {str(item_id) for item_id in item_ids}
    assert payload["event_count"] == len(item_ids)
    assert [event["sequence"] for event in payload["events"]] == list(
        range(1, len(item_ids) + 1)
    )
    lock_path = path.with_name(f".{path.name}.lock")
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_later_valid_candidate_replaces_old_and_invalid_candidate_cannot_erase_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    first = store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )

    replacement = store.save_item_candidate(
        path,
        item_id=5,
        attempt=2,
        candidate_type="numeric_scalar",
        document={"value": 2.0},
    )

    assert replacement["added_item_ids"] == []
    assert replacement["replaced_item_ids"] == [5]
    payload = store.load_item_store(path)
    assert payload["candidates"]["5"]["document"] == {"value": 2.0}
    assert payload["events"] == [
        {
            "sequence": 1,
            "attempt": 1,
            "item_id": 5,
            "action": "added",
            "candidate_type": "numeric_scalar",
            "candidate_bytes": len('{"value":1.0}'.encode("utf-8")),
            "candidate_sha256": first["candidate_sha256"],
            "previous_candidate_sha256": None,
        },
        {
            "sequence": 2,
            "attempt": 2,
            "item_id": 5,
            "action": "replaced",
            "candidate_type": "numeric_scalar",
            "candidate_bytes": len('{"value":2.0}'.encode("utf-8")),
            "candidate_sha256": replacement["candidate_sha256"],
            "previous_candidate_sha256": first["candidate_sha256"],
        },
    ]
    stable_bytes = path.read_bytes()

    with pytest.raises(
        store.T3QuantItemStoreError, match="item_store_non_json_or_nonfinite"
    ):
        store.save_item_candidate(
            path,
            item_id=5,
            attempt=2,
            candidate_type="numeric_scalar",
            document={"value": float("nan")},
        )
    assert path.read_bytes() == stable_bytes

    with pytest.raises(
        store.T3QuantItemStoreError, match="item_store_attempt_regression"
    ):
        store.save_item_candidate(
            path,
            item_id=5,
            attempt=1,
            candidate_type="numeric_scalar",
            document={"value": 3.0},
        )
    assert path.read_bytes() == stable_bytes

    store.save_item_candidate(
        path,
        item_id=5,
        attempt=2,
        candidate_type="numeric_scalar",
        document={"value": 4.0},
    )
    payload = store.load_item_store(path)
    assert payload["candidates"]["5"]["document"] == {"value": 4.0}
    assert payload["event_count"] == 3
    assert payload["events"][1]["candidate_sha256"] != payload["events"][2]["candidate_sha256"]


def test_attempt_snapshots_preserve_the_answer_state_from_each_attempt(
    tmp_path: Path,
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )

    first = store.freeze_item_store_attempt(path, attempt=1)
    first_path = Path(first["snapshot_path"])
    first_bytes = first_path.read_bytes()
    first_payload = json.loads(first_bytes)
    assert first_payload["attempt"] == 1
    assert first_payload["candidates"]["5"]["document"] == {"value": 1.0}
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600

    # Repeating the attempt-1 boundary with unchanged bytes is idempotent.
    assert store.freeze_item_store_attempt(path, attempt=1) == first

    store.save_item_candidate(
        path,
        item_id=5,
        attempt=2,
        candidate_type="numeric_scalar",
        document={"value": 2.0},
    )
    second = store.freeze_item_store_attempt(path, attempt=2)
    second_payload = json.loads(Path(second["snapshot_path"]).read_bytes())

    assert first_path.read_bytes() == first_bytes
    assert first_payload["candidates"]["5"]["document"] == {"value": 1.0}
    assert second_payload["attempt"] == 2
    assert second_payload["candidates"]["5"]["document"] == {"value": 2.0}

    # A later execution cannot rewrite the already-frozen attempt-1 evidence.
    with pytest.raises(
        store.T3QuantItemStoreError,
        match="item_store_snapshot_already_frozen",
    ):
        store.freeze_item_store_attempt(path, attempt=1)
    assert first_path.read_bytes() == first_bytes


def test_existing_oversized_attempt_snapshot_fails_without_unbounded_read(
    tmp_path: Path,
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    snapshot_path = tmp_path / store.ITEM_STORE_SNAPSHOT_TEMPLATE.format(attempt=1)
    snapshot_path.write_bytes(b"x" * (store.MAX_ITEM_STORE_FILE_BYTES + 1))

    with pytest.raises(
        store.T3QuantItemStoreError,
        match="item_store_snapshot_file_size_invalid",
    ):
        store.freeze_item_store_attempt(path, attempt=1)


def test_batch_is_all_or_nothing_and_rejects_duplicate_items(tmp_path: Path) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    stable_bytes = path.read_bytes()

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_duplicate_item"):
        store.save_item_candidates(
            path,
            attempt=1,
            candidates=[_candidate(6), _candidate(6)],
        )

    assert path.read_bytes() == stable_bytes


@pytest.mark.parametrize(
    ("item_id", "attempt", "candidate_type", "document", "code"),
    [
        (0, 1, "numeric_scalar", {"value": 1.0}, "item_store_item_id_invalid"),
        (5, 0, "numeric_scalar", {"value": 1.0}, "item_store_attempt_invalid"),
        (13, 1, "numeric_scalar", {"value": 1.0}, "item_store_candidate_type_invalid"),
        (
            1,
            1,
            "numeric_vector",
            {"values": [1.0] * 119},
            "item_store_document_invalid",
        ),
        (5, 1, "numeric_scalar", {"value": True}, "item_store_number_invalid"),
        (
            5,
            1,
            "numeric_scalar",
            {"value": 1e301},
            "item_store_number_out_of_range",
        ),
        (
            17,
            1,
            "date",
            {"value": "2026-02-30"},
            "item_store_date_invalid",
        ),
        (
            18,
            1,
            "date_list",
            {"values": ["2026-01-01"] * 5},
            "item_store_document_invalid",
        ),
        (
            19,
            1,
            "rebalance_rows",
            {"rows": [[1.0] * 9 for _ in range(6)]},
            "item_store_document_invalid",
        ),
        (
            25,
            1,
            "date",
            {"value": "2026-08-31", "extra": 1},
            "item_store_document_invalid",
        ),
    ],
)
def test_item_boundaries_fail_before_creating_a_store(
    tmp_path: Path,
    item_id: int,
    attempt: int,
    candidate_type: str,
    document: dict,
    code: str,
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME

    with pytest.raises(store.T3QuantItemStoreError, match=code):
        store.save_item_candidate(
            path,
            item_id=item_id,
            attempt=attempt,
            candidate_type=candidate_type,
            document=document,
        )

    assert not path.exists()


def test_tampered_candidate_event_or_top_level_hash_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["candidates"]["5"]["document"]["value"] = 999.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_hash_mismatch"):
        store.load_item_store(path)

    top_level_path = tmp_path / f"top-level-{store.ITEM_STORE_FILENAME}"
    store.save_item_candidate(
        top_level_path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    payload = json.loads(top_level_path.read_text(encoding="utf-8"))
    payload["candidates_sha256"] = "0" * 64
    top_level_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_hash_mismatch"):
        store.load_item_store(top_level_path)

    event_path = tmp_path / f"event-{store.ITEM_STORE_FILENAME}"
    store.save_item_candidate(
        event_path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    payload = json.loads(event_path.read_text(encoding="utf-8"))
    payload["events"][0]["candidate_sha256"] = "0" * 64
    event_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_hash_mismatch"):
        store.load_item_store(event_path)


def test_duplicate_json_keys_and_oversized_files_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    empty_digest = store._sha256({})[1]
    path.write_text(
        "{"
        '"schema_version":"t3-quant-item-store-v2",'
        '"candidate_count":0,'
        f'"candidates_sha256":"{empty_digest}",'
        '"candidates":{},"candidates":{},'
        '"event_count":0,'
        f'"events_sha256":"{store._sha256([])[1]}",'
        '"events":[]'
        "}",
        encoding="utf-8",
    )

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_duplicate_key"):
        store.load_item_store(path)

    path.write_bytes(b"x" * (store.MAX_ITEM_STORE_FILE_BYTES + 1))
    with pytest.raises(
        store.T3QuantItemStoreError, match="item_store_file_size_invalid"
    ):
        store.load_item_store(path)


def test_oversized_new_store_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    monkeypatch.setattr(store, "MAX_ITEM_STORE_FILE_BYTES", 100)

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_file_too_large"):
        store.save_item_candidate(
            path,
            item_id=5,
            attempt=1,
            candidate_type="numeric_scalar",
            document={"value": 1.0},
        )

    assert not path.exists()


def test_store_and_parent_symlinks_fail_closed(tmp_path: Path) -> None:
    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    linked_store = tmp_path / store.ITEM_STORE_FILENAME
    linked_store.symlink_to(real_file)

    with pytest.raises(store.T3QuantItemStoreError, match="item_store_path_invalid"):
        store.load_item_store(linked_store)
    with pytest.raises(store.T3QuantItemStoreError, match="item_store_path_invalid"):
        store.save_item_candidate(
            linked_store,
            item_id=5,
            attempt=1,
            candidate_type="numeric_scalar",
            document={"value": 1.0},
        )

    real_directory = tmp_path / "real-directory"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(store.T3QuantItemStoreError, match="item_store_parent_symlink"):
        store.item_store_path_from_audit(str(linked_directory / "audit.jsonl"))


def test_failed_atomic_replace_preserves_previous_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / store.ITEM_STORE_FILENAME
    store.save_item_candidate(
        path,
        item_id=5,
        attempt=1,
        candidate_type="numeric_scalar",
        document={"value": 1.0},
    )
    stable_bytes = path.read_bytes()

    def fail_replace(_source, _destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    with pytest.raises(store.T3QuantItemStoreError, match="item_store_write_failed"):
        store.save_item_candidate(
            path,
            item_id=6,
            attempt=1,
            candidate_type="numeric_scalar",
            document={"value": 2.0},
        )

    assert path.read_bytes() == stable_bytes
    assert not list(tmp_path.glob(".*.tmp"))
