"""Persist model-visible outputs for restricted qualitative analysis.

This artifact is deliberately separate from public, content-free telemetry.  It
contains only text or JSON that the model actually returned.  It never copies
the input prompt, retrieval input, an evaluator key, or hidden reasoning into
the file.  If the model itself quotes supplied context in its visible answer,
that quotation remains part of the visible answer and is therefore retained.
"""

from __future__ import annotations

from collections.abc import Mapping
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Final


TRANSCRIPT_SCHEMA_VERSION: Final = "model-visible-transcript-v1"
TRANSCRIPT_FILENAME: Final = "model_visible_transcript.jsonl"
CAPTURE_STATUS_SCHEMA_VERSION: Final = "model-visible-transcript-capture-v1"
CAPTURE_STATUS_FILENAME: Final = "model_visible_transcript_capture.jsonl"
# The formal observation budget can produce a visible answer larger than
# 512 KiB.  Keep a finite bound, but do not turn a valid high-token answer into
# a framework failure merely because the qualitative evidence file was sized
# for a much smaller response.
MAX_RECORD_BYTES: Final = 2 * 1024 * 1024
MAX_FILE_BYTES: Final = 32 * 1024 * 1024
_SAFE_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_SECRET_FIELD_RE: Final = re.compile(
    r"(?i)(?:api[_-]?key|authorization|access[_-]?token|password|secret)"
)
_SECRET_VALUE_RE: Final = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{12,}|sk-[A-Za-z0-9_-]{12,})"
)
_SECRET_ASSIGNMENT_RE: Final = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|access[_-]?token|password|secret)\b"
    r"[\"']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]\r\n]+)"
)
_APPEND_LOCKS_GUARD = threading.Lock()
_APPEND_LOCKS: dict[str, threading.RLock] = {}


class ResearchTranscriptError(RuntimeError):
    """The restricted model-visible output artifact could not be persisted."""


def transcript_path_from_audit(audit_path: str | Path) -> Path:
    path = Path(audit_path)
    if not path.is_absolute() or ".." in path.parts or not path.parent.is_dir():
        raise ResearchTranscriptError("transcript audit path is invalid")
    if path.parent.is_symlink():
        raise ResearchTranscriptError("transcript parent must not be a symlink")
    destination = path.parent / TRANSCRIPT_FILENAME
    if destination.is_symlink():
        raise ResearchTranscriptError("transcript path must not be a symlink")
    return destination


def _capture_status_path_from_audit(audit_path: str | Path) -> Path:
    path = Path(audit_path)
    if not path.is_absolute() or ".." in path.parts or not path.parent.is_dir():
        raise ResearchTranscriptError("transcript audit path is invalid")
    if path.parent.is_symlink():
        raise ResearchTranscriptError("transcript parent must not be a symlink")
    destination = path.parent / CAPTURE_STATUS_FILENAME
    if destination.is_symlink():
        raise ResearchTranscriptError("transcript capture status path is invalid")
    return destination


def _redact(value: Any, *, field_name: str | None = None) -> Any:
    if field_name is not None and _SECRET_FIELD_RE.search(field_name):
        return "[REDACTED_SECRET_FIELD]"
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if type(value) is str:
        redacted = _SECRET_ASSIGNMENT_RE.sub(
            lambda match: match.group(1) + "[REDACTED_SECRET_VALUE]",
            value,
        )
        return _SECRET_VALUE_RE.sub("[REDACTED_SECRET_VALUE]", redacted)
    if value is None or type(value) in {bool, int, float}:
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _redact(model_dump(mode="json"))
    return _redact(str(value))


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (OverflowError, TypeError, ValueError) as exc:
        raise ResearchTranscriptError("model-visible output is not JSON safe") from exc


def _append_lock(destination: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(os.fspath(destination)))
    with _APPEND_LOCKS_GUARD:
        lock = _APPEND_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _APPEND_LOCKS[key] = lock
        return lock


def _append_bytes(destination: Path, rendered: bytes, *, max_file_bytes: int) -> None:
    if destination.is_symlink() or not hasattr(os, "O_NOFOLLOW"):
        raise ResearchTranscriptError("transcript path must not be a symlink")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    with _append_lock(destination):
        try:
            fd = os.open(destination, flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ResearchTranscriptError("transcript is not a regular file")
                os.fchmod(fd, 0o600)
                if metadata.st_size + len(rendered) > max_file_bytes:
                    raise ResearchTranscriptError("model-visible transcript is full")
                offset = 0
                while offset < len(rendered):
                    written = os.write(fd, rendered[offset:])
                    if type(written) is not int or written <= 0:
                        raise ResearchTranscriptError(
                            "transcript write was incomplete"
                        )
                    offset += written
                os.fsync(fd)
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        except ResearchTranscriptError:
            raise
        except OSError as exc:
            raise ResearchTranscriptError(
                "model-visible transcript write failed"
            ) from exc


def _append_record(audit_path: str | Path, record: Mapping[str, Any]) -> None:
    rendered = (_canonical(record) + "\n").encode("utf-8")
    if len(rendered) > MAX_RECORD_BYTES:
        raise ResearchTranscriptError("model-visible output exceeds transcript limit")
    _append_bytes(
        transcript_path_from_audit(audit_path),
        rendered,
        max_file_bytes=MAX_FILE_BYTES,
    )


def _capture_error_code(error: ResearchTranscriptError) -> str:
    words = re.sub(r"[^a-z0-9]+", "_", str(error).lower()).strip("_")
    return (words or "transcript_capture_failed")[:96]


def _record_capture_status(
    audit_path: str | Path,
    evidence: Mapping[str, Any],
) -> bool:
    """Best-effort content-free status; never changes the completed model action."""

    record = {
        "schema_version": CAPTURE_STATUS_SCHEMA_VERSION,
        "record_type": evidence.get("record_type"),
        "architecture_mode": evidence.get("architecture_mode"),
        "role": evidence.get("role"),
        "phase": evidence.get("phase"),
        "tool": evidence.get("tool"),
        "attempt": evidence.get("attempt"),
        "capture_status": evidence.get("capture_status"),
        "capture_error": evidence.get("capture_error"),
        "output_sha256": evidence.get("output_sha256"),
        "output_char_count": evidence.get("output_char_count"),
        "exchange_sha256": evidence.get("exchange_sha256"),
    }
    try:
        rendered = (_canonical(record) + "\n").encode("utf-8")
        _append_bytes(
            _capture_status_path_from_audit(audit_path),
            rendered,
            max_file_bytes=4 * 1024 * 1024,
        )
    except ResearchTranscriptError:
        return False
    return True


def append_model_visible_output(
    audit_path: str | Path,
    *,
    architecture_mode: str,
    role: str,
    phase: str,
    attempt: int | None,
    output: Any,
) -> dict[str, Any]:
    """Append one sanitized visible output and return content-free evidence."""

    for name, value in {
        "architecture_mode": architecture_mode,
        "role": role,
        "phase": phase,
    }.items():
        if type(value) is not str or _SAFE_NAME_RE.fullmatch(value) is None:
            raise ResearchTranscriptError(f"transcript {name} is invalid")
    if attempt is not None and (type(attempt) is not int or attempt not in {1, 2}):
        raise ResearchTranscriptError("transcript attempt is invalid")

    sanitized = _redact(output)
    output_json = _canonical(sanitized)
    record = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "record_type": "model_visible_output",
        "architecture_mode": architecture_mode,
        "role": role,
        "phase": phase,
        "attempt": attempt,
        "output_sha256": hashlib.sha256(output_json.encode("utf-8")).hexdigest(),
        "output_char_count": len(output_json),
        "output": sanitized,
    }
    _append_record(audit_path, record)

    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "record_type": "model_visible_output",
        "architecture_mode": architecture_mode,
        "role": role,
        "phase": phase,
        "attempt": attempt,
        "output_sha256": record["output_sha256"],
        "output_char_count": record["output_char_count"],
    }


def append_model_visible_tool_call(
    audit_path: str | Path,
    *,
    architecture_mode: str,
    role: str,
    tool: str,
    attempt: int | None,
    arguments: Any,
    result: Any,
) -> dict[str, Any]:
    """Append one model-issued tool request and its visible result.

    These are external actions the model actually requested, not hidden thought.
    Secret-looking fields and values are redacted before persistence.
    """

    for name, value in {
        "architecture_mode": architecture_mode,
        "role": role,
        "tool": tool,
    }.items():
        if type(value) is not str or _SAFE_NAME_RE.fullmatch(value) is None:
            raise ResearchTranscriptError(f"transcript {name} is invalid")
    if attempt is not None and (type(attempt) is not int or attempt not in {1, 2}):
        raise ResearchTranscriptError("transcript attempt is invalid")
    safe_arguments = _redact(arguments)
    safe_result = _redact(result)
    canonical_exchange = _canonical(
        {"arguments": safe_arguments, "result": safe_result}
    )
    record = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "record_type": "model_visible_tool_call",
        "architecture_mode": architecture_mode,
        "role": role,
        "tool": tool,
        "attempt": attempt,
        "exchange_sha256": hashlib.sha256(
            canonical_exchange.encode("utf-8")
        ).hexdigest(),
        "arguments": safe_arguments,
        "result": safe_result,
    }
    _append_record(audit_path, record)
    return {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "record_type": "model_visible_tool_call",
        "architecture_mode": architecture_mode,
        "role": role,
        "tool": tool,
        "attempt": attempt,
        "exchange_sha256": record["exchange_sha256"],
    }


def capture_model_visible_output(
    audit_path: str | Path,
    *,
    architecture_mode: str,
    role: str,
    phase: str,
    attempt: int | None,
    output: Any,
) -> dict[str, Any]:
    """Capture output without turning an evidence-write failure into a run failure."""

    try:
        evidence = append_model_visible_output(
            audit_path,
            architecture_mode=architecture_mode,
            role=role,
            phase=phase,
            attempt=attempt,
            output=output,
        )
    except ResearchTranscriptError as exc:
        evidence = {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "record_type": "model_visible_output",
            "architecture_mode": architecture_mode,
            "role": role,
            "phase": phase,
            "attempt": attempt,
            "capture_status": "failed",
            "capture_error": _capture_error_code(exc),
        }
    else:
        evidence.update(capture_status="captured", capture_error=None)
    evidence["capture_evidence_status"] = (
        "persisted" if _record_capture_status(audit_path, evidence) else "unavailable"
    )
    return evidence


def capture_model_visible_tool_call(
    audit_path: str | Path,
    *,
    architecture_mode: str,
    role: str,
    tool: str,
    attempt: int | None,
    arguments: Any,
    result: Any,
) -> dict[str, Any]:
    """Capture a completed tool exchange without changing its returned result."""

    try:
        evidence = append_model_visible_tool_call(
            audit_path,
            architecture_mode=architecture_mode,
            role=role,
            tool=tool,
            attempt=attempt,
            arguments=arguments,
            result=result,
        )
    except ResearchTranscriptError as exc:
        evidence = {
            "schema_version": TRANSCRIPT_SCHEMA_VERSION,
            "record_type": "model_visible_tool_call",
            "architecture_mode": architecture_mode,
            "role": role,
            "tool": tool,
            "attempt": attempt,
            "capture_status": "failed",
            "capture_error": _capture_error_code(exc),
        }
    else:
        evidence.update(capture_status="captured", capture_error=None)
    evidence["capture_evidence_status"] = (
        "persisted" if _record_capture_status(audit_path, evidence) else "unavailable"
    )
    return evidence
