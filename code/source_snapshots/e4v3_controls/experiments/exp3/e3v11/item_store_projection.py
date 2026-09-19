"""Answer-blind projection from the audited T3 v2 store to scorer v1 input.

The runtime v2 store adds a value-free event history around the same candidate
map used by the historical private scorer.  This module validates both the
candidate and event hashes, then copies the candidate map unchanged into the
four-field v1 envelope.  It never reads an answer key, scores a value, chooses
an item ID or alters a candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any


SOURCE_SCHEMA = "t3-quant-item-store-v2"
TARGET_SCHEMA = "t3-quant-item-store-v1"
TOP_LEVEL_FIELDS = {
    "schema_version",
    "candidate_count",
    "candidates_sha256",
    "candidates",
    "event_count",
    "events_sha256",
    "events",
}
RECORD_FIELDS = {
    "attempt",
    "candidate_type",
    "document",
    "candidate_bytes",
    "candidate_sha256",
}
EVENT_FIELDS = {
    "sequence",
    "attempt",
    "item_id",
    "action",
    "candidate_type",
    "candidate_bytes",
    "candidate_sha256",
    "previous_candidate_sha256",
}
TYPE_BY_ITEM = {
    **{item_id: "numeric_vector" for item_id in (1, 2, 3, 4, 13, 14, 15)},
    **{
        item_id: "numeric_scalar"
        for item_id in (5, 6, 7, 8, 9, 10, 11, 12, 16, 20, 21, 22, 23, 24)
    },
    17: "date",
    18: "date_list",
    19: "rebalance_rows",
    25: "date",
}
HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class ItemStoreProjectionError(ValueError):
    """The source store cannot be safely projected without changing answers."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ItemStoreProjectionError("item store contains a duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise ItemStoreProjectionError("item store contains a non-finite JSON value")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (OverflowError, TypeError, ValueError) as exc:
        raise ItemStoreProjectionError("item store is not canonical finite JSON") from exc


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ItemStoreProjectionError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ItemStoreProjectionError("item store cannot be read as JSON") from exc
    if not isinstance(value, dict):
        raise ItemStoreProjectionError("item store root is not an object")
    return value


def _valid_hash(value: Any) -> bool:
    return type(value) is str and HASH_RE.fullmatch(value) is not None


def _validate_candidates(value: dict[str, Any]) -> dict[str, Any]:
    candidates = value.get("candidates")
    count = value.get("candidate_count")
    digest = value.get("candidates_sha256")
    if (
        not isinstance(candidates, dict)
        or type(count) is not int
        or not 0 <= count <= 25
        or count != len(candidates)
        or not _valid_hash(digest)
        or _sha256_value(candidates) != digest
    ):
        raise ItemStoreProjectionError("candidate inventory or hash is invalid")
    for raw_item_id, record in candidates.items():
        if (
            type(raw_item_id) is not str
            or not raw_item_id.isascii()
            or not raw_item_id.isdecimal()
            or str(int(raw_item_id)) != raw_item_id
        ):
            raise ItemStoreProjectionError("candidate item ID is invalid")
        item_id = int(raw_item_id)
        if item_id not in TYPE_BY_ITEM or not isinstance(record, dict):
            raise ItemStoreProjectionError("candidate record is invalid")
        if set(record) != RECORD_FIELDS:
            raise ItemStoreProjectionError("candidate record fields are invalid")
        attempt = record.get("attempt")
        candidate_type = record.get("candidate_type")
        document = record.get("document")
        if (
            type(attempt) is not int
            or attempt not in {1, 2}
            or candidate_type != TYPE_BY_ITEM[item_id]
            or not isinstance(document, dict)
            or type(record.get("candidate_bytes")) is not int
            or record["candidate_bytes"] != len(_canonical(document))
            or not _valid_hash(record.get("candidate_sha256"))
        ):
            raise ItemStoreProjectionError("candidate record metadata is invalid")
        hashed = {
            "attempt": attempt,
            "candidate_type": candidate_type,
            "document": document,
            "item_id": item_id,
        }
        if record["candidate_sha256"] != _sha256_value(hashed):
            raise ItemStoreProjectionError("candidate record hash is invalid")
    return candidates


def _validate_events(value: dict[str, Any], candidates: dict[str, Any]) -> None:
    events = value.get("events")
    count = value.get("event_count")
    digest = value.get("events_sha256")
    if (
        not isinstance(events, list)
        or type(count) is not int
        or not 0 <= count <= 200
        or count != len(events)
        or not _valid_hash(digest)
        or _sha256_value(events) != digest
    ):
        raise ItemStoreProjectionError("event inventory or hash is invalid")
    latest: dict[int, dict[str, Any]] = {}
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
            raise ItemStoreProjectionError("event record fields are invalid")
        item_id = event.get("item_id")
        previous = event.get("previous_candidate_sha256")
        prior = latest.get(item_id) if type(item_id) is int else None
        if (
            event.get("sequence") != sequence
            or type(event.get("attempt")) is not int
            or event["attempt"] not in {1, 2}
            or type(item_id) is not int
            or item_id not in TYPE_BY_ITEM
            or event.get("candidate_type") != TYPE_BY_ITEM[item_id]
            or type(event.get("candidate_bytes")) is not int
            or event["candidate_bytes"] <= 0
            or not _valid_hash(event.get("candidate_sha256"))
            or (previous is not None and not _valid_hash(previous))
        ):
            raise ItemStoreProjectionError("event record metadata is invalid")
        if prior is None:
            if event.get("action") != "added" or previous is not None:
                raise ItemStoreProjectionError("first item event is not an addition")
        elif (
            event.get("action") != "replaced"
            or previous != prior["candidate_sha256"]
            or event["attempt"] < prior["attempt"]
        ):
            raise ItemStoreProjectionError("replacement event chain is invalid")
        latest[item_id] = event
    if set(latest) != {int(key) for key in candidates}:
        raise ItemStoreProjectionError("events do not cover the candidate inventory")
    for item_id, event in latest.items():
        candidate = candidates[str(item_id)]
        for field in ("attempt", "candidate_type", "candidate_bytes", "candidate_sha256"):
            if event[field] != candidate[field]:
                raise ItemStoreProjectionError("latest event does not match candidate")


def project_item_store(
    source: Path, destination: Path, receipt_path: Path
) -> dict[str, Any]:
    """Validate one v2 store and write an unchanged-candidate v1 projection."""

    source = source.resolve()
    destination = destination.resolve()
    receipt_path = receipt_path.resolve()
    if destination.exists() or receipt_path.exists():
        raise ItemStoreProjectionError("projection output already exists")
    value = _load(source)
    if set(value) != TOP_LEVEL_FIELDS or value.get("schema_version") != SOURCE_SCHEMA:
        raise ItemStoreProjectionError("item store is not the exact v2 schema")
    candidates = _validate_candidates(value)
    _validate_events(value, candidates)
    projected = {
        "schema_version": TARGET_SCHEMA,
        "candidate_count": value["candidate_count"],
        "candidates_sha256": value["candidates_sha256"],
        "candidates": candidates,
    }
    encoded = _canonical(projected) + b"\n"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    receipt = {
        "schema_version": "e3v11-item-store-projection-receipt-v1",
        "source_schema_version": SOURCE_SCHEMA,
        "target_schema_version": TARGET_SCHEMA,
        "source_file_sha256": _sha256_file(source),
        "projected_file_sha256": hashlib.sha256(encoded).hexdigest(),
        "candidate_count": value["candidate_count"],
        "candidates_sha256": value["candidates_sha256"],
        "event_count": value["event_count"],
        "events_sha256": value["events_sha256"],
        "candidate_content_modified": False,
        "answer_key_read": False,
        "provider_call_count": 0,
    }
    receipt_path.write_bytes(_canonical(receipt) + b"\n")
    return receipt
