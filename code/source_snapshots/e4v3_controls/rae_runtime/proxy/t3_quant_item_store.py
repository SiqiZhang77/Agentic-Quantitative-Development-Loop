"""Persist independently scorable T3 item candidates across model attempts.

The store is a run artifact beside the content-free MCP audit.  It keeps the
latest candidate for scoring and an append-only, value-free event record for
every accepted save.  A later attempt may therefore improve one answer without
erasing the evidence that an earlier attempt submitted a different candidate.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Final


ITEM_STORE_SCHEMA_VERSION: Final = "t3-quant-item-store-v2"
ITEM_STORE_FILENAME: Final = "t3_quant_item_candidates.json"
ITEM_STORE_SNAPSHOT_SCHEMA_VERSION: Final = "t3-quant-item-attempt-snapshot-v1"
ITEM_STORE_SNAPSHOT_TEMPLATE: Final = "t3_quant_item_candidates_attempt_{attempt}.json"
MAX_ITEM_COUNT: Final = 25
MAX_ITEM_EVENT_COUNT: Final = 200
MAX_ITEM_STORE_FILE_BYTES: Final = 256 * 1024
MAX_ABS_CANDIDATE_NUMBER: Final = 1e300

NUMERIC_VECTOR_ITEMS: Final = frozenset({1, 2, 3, 4, 13, 14, 15})
NUMERIC_SCALAR_ITEMS: Final = frozenset(
    {5, 6, 7, 8, 9, 10, 11, 12, 16, 20, 21, 22, 23, 24}
)
DATE_ITEMS: Final = frozenset({17, 25})
DATE_LIST_ITEMS: Final = frozenset({18})
REBALANCE_ROWS_ITEMS: Final = frozenset({19})

_EXPECTED_TYPE_BY_ITEM: Final = {
    **{item_id: "numeric_vector" for item_id in NUMERIC_VECTOR_ITEMS},
    **{item_id: "numeric_scalar" for item_id in NUMERIC_SCALAR_ITEMS},
    **{item_id: "date" for item_id in DATE_ITEMS},
    **{item_id: "date_list" for item_id in DATE_LIST_ITEMS},
    **{item_id: "rebalance_rows" for item_id in REBALANCE_ROWS_ITEMS},
}
_TOP_LEVEL_FIELDS: Final = frozenset(
    {
        "schema_version",
        "candidate_count",
        "candidates_sha256",
        "candidates",
        "event_count",
        "events_sha256",
        "events",
    }
)
_RECORD_FIELDS: Final = frozenset(
    {
        "attempt",
        "candidate_type",
        "document",
        "candidate_bytes",
        "candidate_sha256",
    }
)
_EVENT_FIELDS: Final = frozenset(
    {
        "sequence",
        "attempt",
        "item_id",
        "action",
        "candidate_type",
        "candidate_bytes",
        "candidate_sha256",
        "previous_candidate_sha256",
    }
)
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_STORE_LOCKS_GUARD = threading.Lock()
_STORE_LOCKS: dict[str, threading.RLock] = {}

if set(_EXPECTED_TYPE_BY_ITEM) != set(range(1, MAX_ITEM_COUNT + 1)):
    raise RuntimeError("T3 item type map must cover exactly items 1-25")


class T3QuantItemStoreError(ValueError):
    """A stable, content-free item-store failure."""

    def __init__(self, code: str):
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "item_store_failed"
        self.code = code
        super().__init__(code)


def _coerce_store_path(path: Path) -> Path:
    try:
        candidate = Path(path)
    except TypeError as exc:
        raise T3QuantItemStoreError("item_store_path_invalid") from exc
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise T3QuantItemStoreError("item_store_path_invalid")
    parent = candidate.parent
    if not parent.is_dir():
        raise T3QuantItemStoreError("item_store_parent_unavailable")
    for ancestor in (parent, *parent.parents):
        if ancestor.is_symlink():
            raise T3QuantItemStoreError("item_store_parent_symlink")
    if candidate.is_symlink():
        raise T3QuantItemStoreError("item_store_path_invalid")
    return candidate


def _process_lock_for_store(path: Path) -> threading.RLock:
    """Return the one in-process lock shared by all writers to ``path``."""

    key = os.path.normcase(str(path))
    with _STORE_LOCKS_GUARD:
        lock = _STORE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STORE_LOCKS[key] = lock
        return lock


@contextmanager
def _locked_store_update(path: Path):
    """Serialize one complete read-modify-write transaction for one store.

    The thread lock covers concurrent tool calls inside this MCP process.  The
    sibling file lock covers a second MCP process writing the same run-local
    store.  The lock file deliberately remains in place: unlinking it after one
    writer exits could let a new writer lock a different inode while another
    process still holds the original lock.
    """

    store_path = _coerce_store_path(path)
    lock_path = store_path.with_name(f".{store_path.name}.lock")
    process_lock = _process_lock_for_store(store_path)
    with process_lock:
        if lock_path.is_symlink() or not hasattr(os, "O_NOFOLLOW"):
            raise T3QuantItemStoreError("item_store_lock_unavailable")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(lock_path, flags, 0o600)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise T3QuantItemStoreError("item_store_lock_unavailable")
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield store_path
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        except T3QuantItemStoreError:
            raise
        except OSError as exc:
            raise T3QuantItemStoreError("item_store_lock_unavailable") from exc


def item_store_path_from_audit(audit_destination: str | None) -> Path:
    """Return the shared item-store path for any audit in one run directory."""

    if type(audit_destination) is not str or not audit_destination:
        raise T3QuantItemStoreError("item_store_audit_path_required")
    audit_path = Path(audit_destination)
    if not audit_path.is_absolute():
        raise T3QuantItemStoreError("item_store_audit_path_not_absolute")
    return _coerce_store_path(audit_path.parent / ITEM_STORE_FILENAME)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise T3QuantItemStoreError("item_store_non_json_or_nonfinite") from exc


def _sha256(value: Any) -> tuple[int, str]:
    encoded = _canonical_json(value).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _reject_json_constant(_value: str) -> None:
    raise T3QuantItemStoreError("item_store_non_json_or_nonfinite")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise T3QuantItemStoreError("item_store_duplicate_key")
        value[key] = item
    return value


def _valid_number(value: Any) -> int | float:
    if type(value) not in (int, float):
        raise T3QuantItemStoreError("item_store_number_invalid")
    try:
        parsed = float(value)
    except (OverflowError, ValueError) as exc:
        raise T3QuantItemStoreError("item_store_number_out_of_range") from exc
    if not math.isfinite(parsed):
        raise T3QuantItemStoreError("item_store_non_json_or_nonfinite")
    if abs(parsed) > MAX_ABS_CANDIDATE_NUMBER:
        raise T3QuantItemStoreError("item_store_number_out_of_range")
    return value


def _valid_date(value: Any) -> str:
    if type(value) is not str:
        raise T3QuantItemStoreError("item_store_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise T3QuantItemStoreError("item_store_date_invalid") from exc
    if parsed.isoformat() != value:
        raise T3QuantItemStoreError("item_store_date_invalid")
    return value


def _validate_document(candidate_type: str, document: Any) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise T3QuantItemStoreError("item_store_document_invalid")

    if candidate_type == "numeric_scalar":
        if set(document) != {"value"}:
            raise T3QuantItemStoreError("item_store_document_invalid")
        return {"value": _valid_number(document.get("value"))}

    if candidate_type == "date":
        if set(document) != {"value"}:
            raise T3QuantItemStoreError("item_store_document_invalid")
        return {"value": _valid_date(document.get("value"))}

    if candidate_type == "numeric_vector":
        values = document.get("values")
        if set(document) != {"values"} or not isinstance(values, list) or len(values) != 120:
            raise T3QuantItemStoreError("item_store_document_invalid")
        return {"values": [_valid_number(value) for value in values]}

    if candidate_type == "date_list":
        values = document.get("values")
        if set(document) != {"values"} or not isinstance(values, list) or len(values) != 6:
            raise T3QuantItemStoreError("item_store_document_invalid")
        return {"values": [_valid_date(value) for value in values]}

    if candidate_type == "rebalance_rows":
        rows = document.get("rows")
        if set(document) != {"rows"} or not isinstance(rows, list) or len(rows) != 6:
            raise T3QuantItemStoreError("item_store_document_invalid")
        checked_rows: list[list[int | float]] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != 10:
                raise T3QuantItemStoreError("item_store_document_invalid")
            checked_rows.append([_valid_number(value) for value in row])
        return {"rows": checked_rows}

    raise T3QuantItemStoreError("item_store_candidate_type_invalid")


def _candidate_record(
    *, item_id: int, attempt: int, candidate_type: str, document: Any
) -> dict[str, Any]:
    if type(item_id) is not int or item_id not in _EXPECTED_TYPE_BY_ITEM:
        raise T3QuantItemStoreError("item_store_item_id_invalid")
    if type(attempt) is not int or attempt not in {1, 2}:
        raise T3QuantItemStoreError("item_store_attempt_invalid")
    if type(candidate_type) is not str or (
        candidate_type != _EXPECTED_TYPE_BY_ITEM[item_id]
    ):
        raise T3QuantItemStoreError("item_store_candidate_type_invalid")
    checked_document = _validate_document(candidate_type, document)
    hash_value = {
        "attempt": attempt,
        "candidate_type": candidate_type,
        "document": checked_document,
        "item_id": item_id,
    }
    document_bytes = len(_canonical_json(checked_document).encode("utf-8"))
    _, digest = _sha256(hash_value)
    return {
        "attempt": attempt,
        "candidate_type": candidate_type,
        "document": checked_document,
        "candidate_bytes": document_bytes,
        "candidate_sha256": digest,
    }


def _initial_payload() -> dict[str, Any]:
    _, digest = _sha256({})
    _, events_digest = _sha256([])
    return {
        "schema_version": ITEM_STORE_SCHEMA_VERSION,
        "candidate_count": 0,
        "candidates_sha256": digest,
        "candidates": {},
        "event_count": 0,
        "events_sha256": events_digest,
        "events": [],
    }


def _validate_payload(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != _TOP_LEVEL_FIELDS
        or value.get("schema_version") != ITEM_STORE_SCHEMA_VERSION
    ):
        raise T3QuantItemStoreError("item_store_file_schema_invalid")
    candidates = value.get("candidates")
    count = value.get("candidate_count")
    digest = value.get("candidates_sha256")
    if (
        not isinstance(candidates, dict)
        or type(count) is not int
        or not 0 <= count <= MAX_ITEM_COUNT
        or count != len(candidates)
        or type(digest) is not str
        or _HASH_RE.fullmatch(digest) is None
    ):
        raise T3QuantItemStoreError("item_store_file_schema_invalid")

    for raw_item_id, record in candidates.items():
        if (
            type(raw_item_id) is not str
            or not raw_item_id.isascii()
            or not raw_item_id.isdecimal()
        ):
            raise T3QuantItemStoreError("item_store_record_invalid")
        item_id = int(raw_item_id)
        if raw_item_id != str(item_id) or item_id not in _EXPECTED_TYPE_BY_ITEM:
            raise T3QuantItemStoreError("item_store_record_invalid")
        if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
            raise T3QuantItemStoreError("item_store_record_invalid")
        expected = _candidate_record(
            item_id=item_id,
            attempt=record.get("attempt"),
            candidate_type=record.get("candidate_type"),
            document=record.get("document"),
        )
        if (
            type(record.get("candidate_bytes")) is not int
            or type(record.get("candidate_sha256")) is not str
            or record.get("candidate_bytes") != expected["candidate_bytes"]
            or record.get("candidate_sha256") != expected["candidate_sha256"]
        ):
            raise T3QuantItemStoreError("item_store_hash_mismatch")
    _, expected_digest = _sha256(candidates)
    if digest != expected_digest:
        raise T3QuantItemStoreError("item_store_hash_mismatch")

    events = value.get("events")
    event_count = value.get("event_count")
    events_digest = value.get("events_sha256")
    if (
        not isinstance(events, list)
        or type(event_count) is not int
        or not 0 <= event_count <= MAX_ITEM_EVENT_COUNT
        or event_count != len(events)
        or type(events_digest) is not str
        or _HASH_RE.fullmatch(events_digest) is None
    ):
        raise T3QuantItemStoreError("item_store_file_schema_invalid")
    latest_event_by_item: dict[int, dict[str, Any]] = {}
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
            raise T3QuantItemStoreError("item_store_event_invalid")
        previous_digest = event.get("previous_candidate_sha256")
        if (
            event.get("sequence") != sequence
            or type(event.get("attempt")) is not int
            or event.get("attempt") not in {1, 2}
            or type(event.get("item_id")) is not int
            or event.get("item_id") not in _EXPECTED_TYPE_BY_ITEM
            or event.get("action") not in {"added", "replaced"}
            or event.get("candidate_type")
            != _EXPECTED_TYPE_BY_ITEM[event.get("item_id")]
            or type(event.get("candidate_bytes")) is not int
            or event.get("candidate_bytes") <= 0
            or type(event.get("candidate_sha256")) is not str
            or _HASH_RE.fullmatch(event.get("candidate_sha256")) is None
            or (
                previous_digest is not None
                and (
                    type(previous_digest) is not str
                    or _HASH_RE.fullmatch(previous_digest) is None
                )
            )
            or (event.get("action") == "added" and previous_digest is not None)
            or (event.get("action") == "replaced" and previous_digest is None)
        ):
            raise T3QuantItemStoreError("item_store_event_invalid")
        item_id = event["item_id"]
        previous_event = latest_event_by_item.get(item_id)
        if (
            (previous_event is None and event["action"] != "added")
            or previous_event is not None
            and (
                event["action"] != "replaced"
                or event["previous_candidate_sha256"]
                != previous_event["candidate_sha256"]
                or event["attempt"] < previous_event["attempt"]
            )
        ):
            raise T3QuantItemStoreError("item_store_event_invalid")
        latest_event_by_item[item_id] = event
    if set(latest_event_by_item) != {int(key) for key in candidates}:
        raise T3QuantItemStoreError("item_store_event_invalid")
    for item_id, event in latest_event_by_item.items():
        candidate = candidates[str(item_id)]
        if (
            event["attempt"] != candidate["attempt"]
            or event["candidate_type"] != candidate["candidate_type"]
            or event["candidate_bytes"] != candidate["candidate_bytes"]
            or event["candidate_sha256"] != candidate["candidate_sha256"]
        ):
            raise T3QuantItemStoreError("item_store_hash_mismatch")
    _, expected_events_digest = _sha256(events)
    if events_digest != expected_events_digest:
        raise T3QuantItemStoreError("item_store_hash_mismatch")
    return value


def _read_payload(path: Path) -> dict[str, Any]:
    path = _coerce_store_path(path)
    if not path.exists():
        return _initial_payload()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise T3QuantItemStoreError("item_store_path_invalid")
            if metadata.st_size <= 0 or metadata.st_size > MAX_ITEM_STORE_FILE_BYTES:
                raise T3QuantItemStoreError("item_store_file_size_invalid")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, min(64 * 1024, MAX_ITEM_STORE_FILE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_ITEM_STORE_FILE_BYTES:
                    raise T3QuantItemStoreError("item_store_file_size_invalid")
        finally:
            os.close(fd)
        parsed = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except T3QuantItemStoreError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise T3QuantItemStoreError("item_store_file_unreadable") from exc
    return _validate_payload(parsed)


def load_item_store(path: Path) -> dict[str, Any]:
    """Return a verified copy of the complete item store."""

    return deepcopy(_read_payload(path))


def load_item_candidates(path: Path) -> dict[str, dict[str, Any]]:
    """Return verified candidate records keyed by the canonical item number."""

    return deepcopy(_read_payload(path)["candidates"])


def freeze_item_store_attempt(path: Path, *, attempt: int) -> dict[str, Any]:
    """Create one immutable, full-value snapshot at an attempt boundary.

    A repeat call is accepted only when it asks to write byte-identical content.
    This makes completion checks idempotent without allowing a later execution to
    rewrite what attempt 1 or attempt 2 actually left behind.
    """

    path = _coerce_store_path(path)
    if type(attempt) is not int or attempt not in {1, 2}:
        raise T3QuantItemStoreError("item_store_attempt_invalid")
    payload = _read_payload(path)
    snapshot_value = {
        "schema_version": ITEM_STORE_SNAPSHOT_SCHEMA_VERSION,
        "attempt": attempt,
        "candidate_count": payload["candidate_count"],
        "candidates_sha256": payload["candidates_sha256"],
        "candidates": deepcopy(payload["candidates"]),
        "event_count": payload["event_count"],
        "events_sha256": payload["events_sha256"],
        "events": deepcopy(payload["events"]),
    }
    rendered = (
        json.dumps(
            snapshot_value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(rendered) > MAX_ITEM_STORE_FILE_BYTES:
        raise T3QuantItemStoreError("item_store_snapshot_too_large")
    snapshot_path = path.with_name(
        ITEM_STORE_SNAPSHOT_TEMPLATE.format(attempt=attempt)
    )
    if snapshot_path.is_symlink():
        raise T3QuantItemStoreError("item_store_snapshot_path_invalid")
    if snapshot_path.exists():
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(snapshot_path, flags)
            try:
                metadata = os.fstat(fd)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size <= 0
                    or metadata.st_size > MAX_ITEM_STORE_FILE_BYTES
                ):
                    raise T3QuantItemStoreError(
                        "item_store_snapshot_file_size_invalid"
                    )
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(
                        fd,
                        min(
                            64 * 1024,
                            MAX_ITEM_STORE_FILE_BYTES + 1 - total,
                        ),
                    )
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > MAX_ITEM_STORE_FILE_BYTES:
                        raise T3QuantItemStoreError(
                            "item_store_snapshot_file_size_invalid"
                        )
                existing = b"".join(chunks)
            finally:
                os.close(fd)
        except T3QuantItemStoreError:
            raise
        except OSError as exc:
            raise T3QuantItemStoreError("item_store_snapshot_unreadable") from exc
        if existing != rendered:
            raise T3QuantItemStoreError("item_store_snapshot_already_frozen")
    else:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(snapshot_path, flags, 0o600)
            try:
                offset = 0
                while offset < len(rendered):
                    written = os.write(fd, rendered[offset:])
                    if type(written) is not int or written <= 0:
                        raise T3QuantItemStoreError(
                            "item_store_snapshot_write_incomplete"
                        )
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
        except T3QuantItemStoreError:
            raise
        except OSError as exc:
            raise T3QuantItemStoreError("item_store_snapshot_write_failed") from exc
    snapshot_sha256 = hashlib.sha256(rendered).hexdigest()
    return {
        "schema_version": ITEM_STORE_SNAPSHOT_SCHEMA_VERSION,
        "attempt": attempt,
        "candidate_count": payload["candidate_count"],
        "candidates_sha256": payload["candidates_sha256"],
        "event_count": payload["event_count"],
        "events_sha256": payload["events_sha256"],
        "snapshot_path": str(snapshot_path),
        "snapshot_bytes": len(rendered),
        "snapshot_sha256": snapshot_sha256,
    }


def _write_payload(path: Path, payload: Mapping[str, Any]) -> None:
    path = _coerce_store_path(path)
    rendered = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(rendered) > MAX_ITEM_STORE_FILE_BYTES:
        raise T3QuantItemStoreError("item_store_file_too_large")

    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    if temporary.exists() or temporary.is_symlink():
        raise T3QuantItemStoreError("item_store_temporary_path_unavailable")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temporary, flags, 0o600)
        try:
            offset = 0
            while offset < len(rendered):
                written = os.write(fd, rendered[offset:])
                if type(written) is not int or written <= 0:
                    raise T3QuantItemStoreError("item_store_write_incomplete")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        if path.is_symlink():
            raise T3QuantItemStoreError("item_store_path_invalid")
        os.replace(temporary, path)
    except T3QuantItemStoreError:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        raise
    except OSError as exc:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        raise T3QuantItemStoreError("item_store_write_failed") from exc


def save_item_candidates(
    path: Path,
    *,
    attempt: int,
    candidates: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Atomically add or replace one to 25 independently scorable candidates.

    Every submitted candidate is validated before the existing file is changed.
    Attempt 2 may replace attempt-1 work, while attempt 1 cannot overwrite an
    item already saved by attempt 2.  The same atomic file replacement also
    appends one content-free evidence event per accepted candidate.
    """

    if type(attempt) is not int or attempt not in {1, 2}:
        raise T3QuantItemStoreError("item_store_attempt_invalid")
    if (
        not isinstance(candidates, list)
        or not 1 <= len(candidates) <= MAX_ITEM_COUNT
    ):
        raise T3QuantItemStoreError("item_store_candidate_batch_invalid")

    submitted: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {
            "item_id",
            "candidate_type",
            "document",
        }:
            raise T3QuantItemStoreError("item_store_candidate_batch_invalid")
        item_id = candidate.get("item_id")
        if type(item_id) is not int or item_id not in _EXPECTED_TYPE_BY_ITEM:
            raise T3QuantItemStoreError("item_store_item_id_invalid")
        key = str(item_id)
        if key in submitted:
            raise T3QuantItemStoreError("item_store_duplicate_item")
        submitted[key] = _candidate_record(
            item_id=item_id,
            attempt=attempt,
            candidate_type=candidate.get("candidate_type"),
            document=candidate.get("document"),
        )

    # The lock must start before the read and end after the atomic replace.  A
    # lock around only _write_payload would still allow two writers to read the
    # same old file and let the last replacement erase the other submission.
    with _locked_store_update(path) as store_path:
        payload = _read_payload(store_path)
        current = payload["candidates"]
        for key in submitted:
            existing = current.get(key)
            if isinstance(existing, dict) and attempt < existing["attempt"]:
                raise T3QuantItemStoreError("item_store_attempt_regression")

        current_events = payload["events"]
        if len(current_events) + len(submitted) > MAX_ITEM_EVENT_COUNT:
            raise T3QuantItemStoreError("item_store_event_limit_exceeded")

        updated = deepcopy(current)
        added_item_ids = sorted(int(key) for key in submitted if key not in current)
        replaced_item_ids = sorted(int(key) for key in submitted if key in current)
        updated.update(deepcopy(submitted))
        _, candidates_digest = _sha256(updated)
        updated_events = deepcopy(current_events)
        for key in sorted(submitted, key=int):
            record = submitted[key]
            previous = current.get(key)
            updated_events.append(
                {
                    "sequence": len(updated_events) + 1,
                    "attempt": attempt,
                    "item_id": int(key),
                    "action": "replaced" if isinstance(previous, dict) else "added",
                    "candidate_type": record["candidate_type"],
                    "candidate_bytes": record["candidate_bytes"],
                    "candidate_sha256": record["candidate_sha256"],
                    "previous_candidate_sha256": (
                        previous.get("candidate_sha256")
                        if isinstance(previous, dict)
                        else None
                    ),
                }
            )
        _, events_digest = _sha256(updated_events)
        next_payload = {
            "schema_version": ITEM_STORE_SCHEMA_VERSION,
            "candidate_count": len(updated),
            "candidates_sha256": candidates_digest,
            "candidates": updated,
            "event_count": len(updated_events),
            "events_sha256": events_digest,
            "events": updated_events,
        }
        _validate_payload(next_payload)
        _write_payload(store_path, next_payload)

    return {
        "attempt": attempt,
        "submitted_count": len(submitted),
        "added_item_ids": added_item_ids,
        "replaced_item_ids": replaced_item_ids,
        "candidate_count": len(updated),
        "saved_item_ids": sorted(int(key) for key in updated),
        "candidates_sha256": candidates_digest,
        "event_count": len(updated_events),
        "events_sha256": events_digest,
        "candidate_sha256s": {
            key: submitted[key]["candidate_sha256"] for key in sorted(submitted, key=int)
        },
    }


def save_item_candidate(
    path: Path,
    *,
    item_id: int,
    attempt: int,
    candidate_type: str,
    document: Mapping[str, Any],
) -> dict[str, Any]:
    """Convenience wrapper for one candidate using the same atomic batch path."""

    summary = save_item_candidates(
        path,
        attempt=attempt,
        candidates=[
            {
                "item_id": item_id,
                "candidate_type": candidate_type,
                "document": document,
            }
        ],
    )
    return {
        **summary,
        "item_id": item_id,
        "candidate_type": candidate_type,
        "candidate_sha256": summary["candidate_sha256s"][str(item_id)],
    }
