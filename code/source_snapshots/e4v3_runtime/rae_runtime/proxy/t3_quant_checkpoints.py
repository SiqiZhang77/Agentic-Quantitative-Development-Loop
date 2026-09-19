"""Persist bounded, schema-validated T3 section answers between agent attempts.

The checkpoint file is a run artifact, not a repository write.  It exists so a
later model or evaluator can observe a completed Level A, B or C even when the
full GitHub artifact has not yet been committed.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Final


CHECKPOINT_SCHEMA_VERSION: Final = "t3-quant-section-checkpoints-v1"
CHECKPOINT_FILENAME: Final = "t3_quant_checkpoints.json"
CHECKPOINT_SECTIONS: Final = frozenset({"level_a", "level_b", "level_c"})
MAX_CHECKPOINT_FILE_BYTES: Final = 3 * 1024 * 1024
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class T3QuantCheckpointError(ValueError):
    """A stable, content-free checkpoint persistence failure."""

    def __init__(self, code: str):
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "checkpoint_failed"
        self.code = code
        super().__init__(code)


def checkpoint_path_from_audit(audit_destination: str | None) -> Path:
    if type(audit_destination) is not str or not audit_destination:
        raise T3QuantCheckpointError("checkpoint_audit_path_required")
    audit_path = Path(audit_destination)
    if not audit_path.is_absolute():
        raise T3QuantCheckpointError("checkpoint_audit_path_not_absolute")
    if not audit_path.parent.is_dir():
        raise T3QuantCheckpointError("checkpoint_parent_unavailable")
    if audit_path.parent.is_symlink():
        raise T3QuantCheckpointError("checkpoint_parent_must_not_be_symlink")
    return audit_path.parent / CHECKPOINT_FILENAME


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise T3QuantCheckpointError("checkpoint_non_json_or_nonfinite") from exc


def _reject_json_constant(_value: str) -> None:
    raise T3QuantCheckpointError("checkpoint_non_json_or_nonfinite")


def _read_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": CHECKPOINT_SCHEMA_VERSION, "sections": {}}
    if path.is_symlink() or not path.is_file():
        raise T3QuantCheckpointError("checkpoint_path_invalid")
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_CHECKPOINT_FILE_BYTES:
            raise T3QuantCheckpointError("checkpoint_file_size_invalid")
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except T3QuantCheckpointError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise T3QuantCheckpointError("checkpoint_file_unreadable") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "sections"}
        or payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
        or not isinstance(payload.get("sections"), dict)
    ):
        raise T3QuantCheckpointError("checkpoint_file_schema_invalid")
    for section, record in payload["sections"].items():
        if section not in CHECKPOINT_SECTIONS or not isinstance(record, dict):
            raise T3QuantCheckpointError("checkpoint_file_schema_invalid")
        if set(record) != {
            "artifact_bytes",
            "artifact_sha256",
            "calculation_reference_count",
            "document",
        }:
            raise T3QuantCheckpointError("checkpoint_file_schema_invalid")
        document = record.get("document")
        digest = record.get("artifact_sha256")
        byte_count = record.get("artifact_bytes")
        reference_count = record.get("calculation_reference_count")
        canonical = _canonical_json(document).encode("utf-8")
        if (
            not isinstance(document, dict)
            or type(digest) is not str
            or _HASH_RE.fullmatch(digest) is None
            or digest != hashlib.sha256(canonical).hexdigest()
            or type(byte_count) is not int
            or byte_count != len(canonical)
            or type(reference_count) is not int
            or reference_count < 1
            or reference_count > 512
        ):
            raise T3QuantCheckpointError("checkpoint_record_invalid")
    return payload


def load_checkpoint_sections(path: Path) -> dict[str, dict[str, Any]]:
    """Return verified section documents, or an empty mapping before first save."""

    payload = _read_payload(path)
    return {
        section: dict(record["document"])
        for section, record in payload["sections"].items()
    }


def save_checkpoint_section(
    path: Path,
    *,
    section: str,
    document: Mapping[str, Any],
    calculation_reference_count: int,
) -> dict[str, Any]:
    """Atomically add or replace one validated section in the run artifact."""

    if section not in CHECKPOINT_SECTIONS or not isinstance(document, Mapping):
        raise T3QuantCheckpointError("checkpoint_section_invalid")
    if (
        type(calculation_reference_count) is not int
        or calculation_reference_count < 1
        or calculation_reference_count > 512
    ):
        raise T3QuantCheckpointError("checkpoint_reference_count_invalid")
    payload = _read_payload(path)
    canonical_document = _canonical_json(dict(document)).encode("utf-8")
    payload["sections"][section] = {
        "artifact_bytes": len(canonical_document),
        "artifact_sha256": hashlib.sha256(canonical_document).hexdigest(),
        "calculation_reference_count": calculation_reference_count,
        "document": dict(document),
    }
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
    if len(rendered) > MAX_CHECKPOINT_FILE_BYTES:
        raise T3QuantCheckpointError("checkpoint_file_too_large")

    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise T3QuantCheckpointError("checkpoint_temporary_path_unavailable")
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
                    raise T3QuantCheckpointError("checkpoint_write_incomplete")
                offset += written
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    except T3QuantCheckpointError:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    except OSError as exc:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise T3QuantCheckpointError("checkpoint_write_failed") from exc

    return {
        "section": section,
        "artifact_sha256": payload["sections"][section]["artifact_sha256"],
        "artifact_bytes": payload["sections"][section]["artifact_bytes"],
        "calculation_reference_count": calculation_reference_count,
        "saved_sections": sorted(payload["sections"]),
    }
