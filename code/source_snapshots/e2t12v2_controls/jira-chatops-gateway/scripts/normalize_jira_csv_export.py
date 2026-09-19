#!/usr/bin/env python3
"""Normalize a Jira all-fields CSV into sanitized Experiment 2 corpus files.

The raw export is deliberately kept outside Git.  This offline normalizer reads
duplicate Jira CSV columns positionally, removes identity and secret material,
builds deterministic request/run episodes, and emits both a rich audit corpus
and the six-field JSONL consumed by ``build_jira_rag_index.py``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NORMALIZER_VERSION = "1.2.0"
CORPUS_SCHEMA_VERSION = "jira-normalized-corpus-v1"
PAIRING_RULE_VERSION = "jira-episode-pairing-v2"
REDACTION_RULE_VERSION = "jira-redaction-v3"
DOCUMENT_FILTER_RULE_VERSION = "jira-document-filter-v1"
MAX_INDEX_TEXT_CHARS = 4_000

_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_VALID_RUN_ID_RE = re.compile(r"^run_[A-Z][A-Z0-9]+-\d+_[0-9A-Za-z]+$")
_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>'\"]+")
_ACCOUNT_LABEL_RE = re.compile(
    r"(?i)\b(account(?:\s+id)?|atlassian(?:\s+account)?(?:\s+id)?)"
    r"(\s*[:=]\s*)[^\s,;]+"
)
_ASSIGNMENT_LINE_RE = re.compile(
    r"(?im)^(\s*\**\s*(?:assigned\s+to|done\s+by|contact|owner|author|"
    r"reporter|reviewer|supervisor|mentor)\s*\**\s*:\s*)[^\r\n]+"
)
_ROLE_IDENTITY_RE = re.compile(
    r"(?:(?i:\b(?:cluster\s+admin|administrator|supervisor|mentor|reviewer|"
    r"owner|author|contact|handled\s+separately\s+by|done\s+by)\s*"
    r"(?:[:=(]\s*)?))([A-Z][a-z]{2,20}(?:[ -][A-Z][a-z]{2,20}){0,2})"
)
_PROJECT_FRAMEWORK_IDENTITY_RE = re.compile(
    r"\b(?:19|20)\d{2}\s*/\s*"
    r"([A-Z][a-z]{2,20}\s+[A-Z][a-z]{2,20})"
    r"(?=\s+[A-Z][A-Za-z-]{2,30}\s+framework\b)"
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|auth(?:orization)?|password|secret)"
        r"([\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s,;\"'}]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)
_RFC3339_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\b"
)
_COMMIT_RE = re.compile(r"(?i)\b[0-9a-f]{40}\b")
_BOT_MARKER_RE = re.compile(r"(?i)^\s*\[quant-loop-bot\]")
_BULLET_QUANT_RE = re.compile(r"^\s*[-*•]\s+/quant(?:\s|$)")
_EMBEDDED_QUANT_RE = re.compile(r"(?m)^\s*/quant(?:\s|$)")
_UNKNOWN_VALUES = {
    "", "unknown", "none", "null", "n/a", "na", "unavailable", "not available",
    "not provided", "not_provided",
}
_IDENTITY_HEADERS = (
    "Assignee", "Assignee Id", "Creator", "Creator Id", "Reporter", "Reporter Id",
    "Project lead", "Project lead id", "Watchers", "Watchers Id",
)
_IDENTITY_TOKEN_STOPLIST = {
    "admin", "agent", "analyst", "and", "banking", "bot", "docker", "for",
    "from", "github", "jira", "loop", "mark", "max", "may", "quant", "student",
    "support", "team", "test", "the", "user", "will", "with",
}


class JiraCsvNormalizerError(ValueError):
    """Raised when the snapshot cannot be normalized deterministically."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _stable_suffix(*parts: Any, size: int = 16) -> str:
    return _sha256_text(_canonical_json(parts))[:size]


def _normalise_whitespace(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()


def _is_low_information_document(document: dict[str, str]) -> bool:
    """Identify one-token summaries/comments that add lexical noise only.

    The rich ticket, event, and episode records remain untouched.  This rule is
    applied only to retrieval documents, where a one-word payload such as
    ``Test`` or ``104`` can be over-promoted by BM25 length normalisation.
    """

    if document.get("source_type") not in {"summary", "comment"}:
        return False
    text = document.get("text", "")
    payload = text.split(":", 1)[1].strip() if ":" in text else text.strip()
    tokens = re.findall(r"\w+", payload, flags=re.UNICODE)
    return len(payload) <= 20 and len(tokens) <= 1


def _parse_rfc3339(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value,
    ):
        raise JiraCsvNormalizerError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JiraCsvNormalizerError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise JiraCsvNormalizerError(f"{label} must include a timezone")
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_local_timestamp(raw: str, source_timezone: ZoneInfo, label: str) -> str:
    value = (raw or "").strip()
    formats = (
        "%d/%b/%y %I:%M %p",
        "%d/%m/%Y %H:%M",
        "%d/%m/%y %H:%M",
        "%d/%b/%Y %H:%M",
        "%d/%b/%y %H:%M",
        "%d/%b/%Y %I:%M %p",
        "%d/%b/%y, %I:%M %p",
        "%d/%b/%Y, %I:%M %p",
    )
    naive = None
    for fmt in formats:
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        raise JiraCsvNormalizerError(f"{label} has unsupported Jira timestamp: {value!r}")

    fold_zero = naive.replace(tzinfo=source_timezone, fold=0)
    fold_one = naive.replace(tzinfo=source_timezone, fold=1)
    if fold_zero.utcoffset() != fold_one.utcoffset():
        raise JiraCsvNormalizerError(
            f"{label} is ambiguous or nonexistent in {source_timezone.key}: {value!r}"
        )
    round_trip = fold_zero.astimezone(timezone.utc).astimezone(source_timezone).replace(tzinfo=None)
    if round_trip != naive:
        raise JiraCsvNormalizerError(
            f"{label} is nonexistent in {source_timezone.key}: {value!r}"
        )
    return _utc_text(fold_zero)


def _split_comment_envelope(raw: str, label: str) -> tuple[str, str, str]:
    parts = (raw or "").lstrip("\ufeff").split(";", 2)
    if len(parts) != 3:
        raise JiraCsvNormalizerError(f"{label} must use 'timestamp;author;body'")
    timestamp_raw, author_raw, body_raw = (part.strip() for part in parts)
    if not timestamp_raw or not body_raw:
        raise JiraCsvNormalizerError(f"{label} has an empty timestamp or body")
    return timestamp_raw, author_raw, body_raw


def _split_attachment_envelope(raw: str, label: str) -> tuple[str, str, str, str]:
    parts = (raw or "").lstrip("\ufeff").split(";", 3)
    if len(parts) != 4:
        raise JiraCsvNormalizerError(
            f"{label} must use 'timestamp;author;filename;url'"
        )
    return tuple(part.strip() for part in parts)  # type: ignore[return-value]


def _strict_quant_request(text: str) -> bool:
    parts = (text or "").strip().split(maxsplit=1)
    return len(parts) == 2 and parts[0] == "/quant" and bool(parts[1].strip())


def _field(text: str, label: str) -> str | None:
    match = re.search(
        rf"(?im)^\s*\**\s*{re.escape(label)}:\s*\**\s*([^\r\n]+)",
        text,
    )
    if not match:
        return None
    value = match.group(1).strip().strip("* ")
    return None if value.casefold() in _UNKNOWN_VALUES else value


def _normalise_outcome(value: str | None, *, legacy: bool) -> str | None:
    normalized = (value or "").strip().strip("* ").casefold().replace(" ", "_")
    mapping = {
        "success": "succeeded",
        "succeeded": "succeeded",
        "failed": "failed",
        "failure": "failed",
        "timeout": "timeout",
        "timed_out": "timeout",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "validation_failed": "validation_failed",
    }
    outcome = mapping.get(normalized)
    if legacy and outcome in {"succeeded", "failed"}:
        return f"{outcome}_legacy"
    return outcome


def _parse_bot_fields(text: str) -> dict[str, Any]:
    run_id_raw = _field(text, "Run ID")
    run_id = run_id_raw if run_id_raw and _VALID_RUN_ID_RE.fullmatch(run_id_raw) else None
    workflow_id = _field(text, "Workflow ID")
    command = _field(text, "Command")
    final_status = _field(text, "Final Status")
    legacy_status = _field(text, "Status") if final_status is None else None
    outcome = _normalise_outcome(final_status, legacy=False)
    if outcome is None and legacy_status is not None:
        outcome = _normalise_outcome(legacy_status, legacy=True)

    branch = (
        _field(text, "Branch Name")
        or _field(text, "Target Branch")
        or _field(text, "Git Branch")
    )
    commit_value = (
        _field(text, "Commit SHA")
        or _field(text, "Commit")
        or _field(text, "Head Commit")
    )
    commit_match = _COMMIT_RE.search(commit_value or "")
    commit_sha = commit_match.group(0).lower() if commit_match else None
    error_type = _field(text, "Error Code")

    embedded_timestamps = []
    for raw_timestamp in _RFC3339_RE.findall(text):
        try:
            embedded_timestamps.append(_utc_text(_parse_rfc3339(raw_timestamp, "embedded timestamp")))
        except JiraCsvNormalizerError:
            continue

    lower = text.casefold()
    is_progress = (
        "running workflow" in lower
        or "partial progress" in lower
        or (final_status is None and outcome is None)
    )
    if final_status is not None and final_status.casefold() in _UNKNOWN_VALUES:
        is_progress = True
    return {
        "run_id": run_id,
        "run_id_raw_valid": run_id_raw is None or run_id is not None,
        "workflow_id": workflow_id,
        "legacy_command": command if command and command.startswith("run_") else None,
        "outcome": outcome,
        "error_type": error_type,
        "branch": branch,
        "commit_sha": commit_sha,
        "embedded_event_timestamps_utc": embedded_timestamps,
        "terminal": outcome is not None,
        "progress": is_progress and outcome is None,
    }


def _identity_literals(
    rows: list[list[str]],
    indices: dict[str, list[int]],
    additional_identities: Iterable[str] = (),
) -> tuple[list[tuple[str, bool]], set[str]]:
    display_values: set[str] = set(additional_identities)
    account_values: set[str] = set()
    for row in rows:
        for header in _IDENTITY_HEADERS:
            for index in indices.get(header, []):
                value = row[index].strip()
                if not value:
                    continue
                if header.casefold().endswith("id") or _looks_like_account_id(value):
                    account_values.add(value)
                else:
                    display_values.add(value)
        for index in indices.get("Comment", []):
            value = row[index].strip()
            if value:
                _, author, _ = _split_comment_envelope(value, "comment identity envelope")
                if author:
                    (account_values if _looks_like_account_id(author) else display_values).add(author)
        for index in indices.get("Attachment", []):
            value = row[index].strip()
            if value:
                _, author, _, _ = _split_attachment_envelope(value, "attachment identity envelope")
                if author:
                    (account_values if _looks_like_account_id(author) else display_values).add(author)

    title_aliases: set[str] = set()
    summary_index = indices.get("Summary", [None])[0]
    if summary_index is not None:
        for row in rows:
            summary = row[summary_index]
            for match in re.finditer(
                r"(?i)\bexp(?:eriment)?\s*[23](?:\s*/\s*[23])?\s*\(([^)]{1,40})\)",
                summary,
            ):
                title_aliases.add(match.group(1).strip())
            for match in re.finditer(r"\b([A-Z][A-Za-z]{2,20})-Style\b", summary):
                title_aliases.add(match.group(1))
            for match in re.finditer(
                r"(?i)\bexp[23](?:-rule\d+)?-([a-z][a-z0-9_]{2,20})(?:\b|\s)",
                summary,
            ):
                candidate = match.group(1)
                if candidate.casefold() not in {
                    "momentum", "volatility", "sanctum", "rule", "factor",
                }:
                    title_aliases.add(candidate)

    # Jira descriptions sometimes carry names outside structured fields.  A
    # conservative possessive-name detector adds those aliases.  Assignment
    # lines are redacted in-place instead of adding arbitrary role text to the
    # global identity lexicon.
    description_index = indices.get("Description", [None])[0]
    if description_index is not None:
        for row in rows:
            description = row[description_index]
            for match in re.finditer(r"\b([A-Z][a-z]{2,20})['’]s\b", description):
                candidate = match.group(1)
                if candidate.casefold() not in {
                    "airflow", "atlassian", "docker", "github", "jira", "python", "rae", "team",
                }:
                    title_aliases.add(candidate)

    # Pick up names that are explicitly introduced by a human-role phrase even
    # when that person is not a Jira actor. This is deliberately narrower than
    # generic capitalized-word matching, which would erase technical terms.
    free_text_fields = [
        *(indices.get("Summary", [])),
        *(indices.get("Description", [])),
        *(indices.get("Comment", [])),
    ]
    comment_field_indices = set(indices.get("Comment", []))
    for row in rows:
        for field_index in free_text_fields:
            value = row[field_index]
            if not value:
                continue
            if field_index in comment_field_indices:
                try:
                    _, _, value = _split_comment_envelope(
                        value, "role identity comment envelope"
                    )
                except JiraCsvNormalizerError:
                    continue
            for match in _ROLE_IDENTITY_RE.finditer(value):
                title_aliases.add(match.group(1).strip())
            for match in _PROJECT_FRAMEWORK_IDENTITY_RE.finditer(value):
                title_aliases.add(match.group(1).strip())

    replacements: dict[tuple[str, bool], None] = {}
    for value in display_values | title_aliases:
        normalized = _normalise_whitespace(value)
        if len(normalized) >= 4 or (
            len(normalized) == 3 and (normalized[:1].isupper() or normalized.isupper())
        ):
            replacements[(normalized, len(normalized) == 3)] = None
        for token in re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE):
            if len(token) >= 4 and token.casefold() not in _IDENTITY_TOKEN_STOPLIST:
                # Single name tokens are high-value for privacy but can also be
                # ordinary English words. Match their original capitalization
                # instead of replacing them case-insensitively everywhere.
                replacements[(token, True)] = None
            elif (
                len(token) == 3
                and token.casefold() not in _IDENTITY_TOKEN_STOPLIST
                and (token[:1].isupper() or token.isupper())
            ):
                replacements[(token, True)] = None
    ordered = sorted(replacements, key=lambda item: (-len(item[0]), item[0].casefold()))
    return ordered, account_values


def _looks_like_account_id(value: str) -> bool:
    normalized = (value or "").strip()
    return bool(
        re.fullmatch(r"(?i)(?:[a-z0-9]+:)?[0-9a-f]{6,}(?:-[0-9a-f]{4,}){2,}", normalized)
        or re.fullmatch(r"(?i)[a-z0-9]+:[0-9a-f-]{20,}", normalized)
    )


def _sanitize_text(
    value: str,
    identity_replacements: list[tuple[str, bool]],
    account_values: set[str],
    *,
    redact_urls: bool = True,
) -> tuple[str, Counter[str]]:
    text = _normalise_whitespace(value)
    counts: Counter[str] = Counter()
    text, substitutions = _ASSIGNMENT_LINE_RE.subn(r"\1[PERSON]", text)
    counts["person"] += substitutions
    # Email must be removed before person-name substitutions; otherwise an
    # address such as ``name@example.com`` can become ``[PERSON]@example.com``
    # and evade the complete-address detector.
    text, substitutions = _EMAIL_RE.subn("[EMAIL]", text)
    counts["email"] += substitutions
    for account_value in sorted(account_values, key=len, reverse=True):
        if account_value and account_value in text:
            occurrences = text.count(account_value)
            text = text.replace(account_value, "[ACCOUNT_ID]")
            counts["account_id"] += occurrences
    for identity, case_sensitive in identity_replacements:
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(rf"(?<!\w){re.escape(identity)}(?!\w)", flags=flags)
        text, substitutions = pattern.subn("[PERSON]", text)
        counts["person"] += substitutions
    text, substitutions = _ACCOUNT_LABEL_RE.subn(r"\1\2[ACCOUNT_ID]", text)
    counts["account_id"] += substitutions
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text, substitutions = pattern.subn(r"\1\2[REDACTED]", text)
        else:
            text, substitutions = pattern.subn("[REDACTED]", text)
        counts["secret"] += substitutions
    if redact_urls:
        text, substitutions = _URL_RE.subn("[URL]", text)
        counts["url"] += substitutions
    return text, counts


def _bounded_text(text: str, max_chars: int = MAX_INDEX_TEXT_CHARS) -> str:
    marker = "\n[TRUNCATED]"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - len(marker)].rstrip() + marker


def _read_exclusions(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JiraCsvNormalizerError(f"cannot read exclusion list {path}: {exc}") from exc
    if not isinstance(value, list):
        raise JiraCsvNormalizerError("exclusion list must be a JSON array")
    keys = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            key = item
        elif isinstance(item, dict) and isinstance(item.get("ticket_key"), str):
            if item.get("decision", "exclude") == "keep":
                continue
            key = item["ticket_key"]
        else:
            raise JiraCsvNormalizerError(
                f"exclusion list item {index} must be a Jira key or decision object"
            )
        if not _TICKET_RE.fullmatch(key):
            raise JiraCsvNormalizerError(f"invalid excluded Jira key: {key!r}")
        keys.append(key)
    return sorted(set(keys))


def _read_additional_identities(path: Path | None) -> tuple[list[str], str | None]:
    """Read an operator-reviewed, local-only JSON list of extra identity strings."""

    if path is None:
        return [], None
    raw_bytes = path.read_bytes()
    try:
        value = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JiraCsvNormalizerError(
            f"additional identity file is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise JiraCsvNormalizerError(
            "additional identity file must be a JSON array of non-empty strings"
        )
    return sorted({_normalise_whitespace(item) for item in value}), _sha256_bytes(raw_bytes)


def _validate_bot_author_hashes(values: Iterable[str]) -> list[str]:
    hashes = sorted({str(value).strip().casefold() for value in values})
    if not hashes or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise JiraCsvNormalizerError(
            "at least one bot author SHA-256 must be supplied as 64 lowercase hex characters"
        )
    return hashes


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    encoded = "".join(_canonical_json(value) + "\n" for value in values).encode("utf-8")
    path.write_bytes(encoded)


def _output_hash(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _document(
    *,
    ticket_key: str,
    source_type: str,
    source_id: str,
    timestamp: str,
    text: str,
) -> dict[str, str]:
    bounded = _bounded_text(text)
    suffix = _stable_suffix(ticket_key, source_type, source_id, timestamp, bounded, size=12)
    return {
        "memory_id": f"MEM-{ticket_key}-{source_type.upper()}-{suffix}",
        "source_ticket_id": ticket_key,
        "source_type": source_type,
        "source_id": source_id,
        "source_timestamp": timestamp,
        "text": bounded,
    }


def normalize_snapshot(
    *,
    input_csv: Path,
    exclusions_path: Path,
    output_dir: Path,
    source_timezone_name: str,
    timezone_status: str,
    cutoff_at: str,
    bot_author_sha256s: Iterable[str],
    bot_author_trust_status: str = "snapshot_candidate_unverified",
    additional_identities_path: Path | None = None,
    normalizer_repository_revision: str | None = None,
) -> dict[str, Any]:
    cutoff = _parse_rfc3339(cutoff_at, "cutoff_at")
    try:
        source_timezone = ZoneInfo(source_timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise JiraCsvNormalizerError(f"unknown IANA timezone: {source_timezone_name}") from exc
    if output_dir.exists():
        raise JiraCsvNormalizerError(f"refusing to overwrite output directory: {output_dir}")
    trusted_bot_author_hashes = _validate_bot_author_hashes(bot_author_sha256s)
    if bot_author_trust_status not in {"verified", "snapshot_candidate_unverified"}:
        raise JiraCsvNormalizerError("unsupported bot author trust status")
    if normalizer_repository_revision is not None and not re.fullmatch(
        r"[0-9a-f]{40}", normalizer_repository_revision.casefold()
    ):
        raise JiraCsvNormalizerError(
            "normalizer repository revision must be a full 40-character Git SHA"
        )
    normalizer_script_sha256 = _sha256_bytes(Path(__file__).resolve().read_bytes())
    additional_identities, additional_identity_source_sha256 = (
        _read_additional_identities(additional_identities_path)
    )

    raw_bytes = input_csv.read_bytes()
    snapshot_sha256 = _sha256_bytes(raw_bytes)
    snapshot_id = f"jira-csv-{snapshot_sha256[:16]}"
    with input_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise JiraCsvNormalizerError("Jira CSV is empty") from exc
        rows = list(reader)
    if not rows:
        raise JiraCsvNormalizerError("Jira CSV contains no ticket rows")
    width = len(header)
    if any(len(row) != width for row in rows):
        raise JiraCsvNormalizerError("Jira CSV contains ragged rows")

    indices: dict[str, list[int]] = defaultdict(list)
    for index, name in enumerate(header):
        indices[name].append(index)
    required = {"Issue key", "Summary", "Description", "Issue Type", "Status", "Created", "Updated"}
    missing = sorted(required - set(indices))
    if missing:
        raise JiraCsvNormalizerError("Jira CSV is missing columns: " + ", ".join(missing))

    exclusions = _read_exclusions(exclusions_path)
    excluded_set = set(exclusions)
    identity_replacements, account_values = _identity_literals(
        rows,
        indices,
        additional_identities,
    )
    redaction_counts: Counter[str] = Counter()

    def first(row: list[str], name: str) -> str:
        return row[indices[name][0]].strip()

    tickets: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    attachments: list[dict[str, Any]] = []
    events_by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_ticket_keys: set[str] = set()

    for source_row_number, row in enumerate(rows, start=2):
        ticket_key = first(row, "Issue key")
        if not _TICKET_RE.fullmatch(ticket_key):
            raise JiraCsvNormalizerError(f"row {source_row_number} has invalid Issue key")
        if ticket_key in seen_ticket_keys:
            raise JiraCsvNormalizerError(f"duplicate ticket row: {ticket_key}")
        seen_ticket_keys.add(ticket_key)
        created_raw = first(row, "Created")
        updated_raw = first(row, "Updated")
        created_utc = _parse_local_timestamp(created_raw, source_timezone, f"{ticket_key}.Created")
        updated_utc = _parse_local_timestamp(updated_raw, source_timezone, f"{ticket_key}.Updated")
        summary, counts = _sanitize_text(first(row, "Summary"), identity_replacements, account_values)
        redaction_counts.update(counts)
        description, counts = _sanitize_text(first(row, "Description"), identity_replacements, account_values)
        redaction_counts.update(counts)

        comment_ids = []
        ticket_events = []
        ordinal = 0
        for column_index in indices.get("Comment", []):
            raw_comment = row[column_index]
            if not raw_comment.strip():
                continue
            ordinal += 1
            timestamp_raw, author_raw, body_raw = _split_comment_envelope(
                raw_comment,
                f"{ticket_key}.Comment[{ordinal}]",
            )
            timestamp_utc = _parse_local_timestamp(
                timestamp_raw,
                source_timezone,
                f"{ticket_key}.Comment[{ordinal}].timestamp",
            )
            body, counts = _sanitize_text(body_raw, identity_replacements, account_values)
            redaction_counts.update(counts)
            # A marker alone is forgeable. Authenticate the raw Jira author by
            # its approved SHA-256 before discarding the identity value.
            has_bot_marker = bool(_BOT_MARKER_RE.match(body_raw))
            author_sha256 = _sha256_text(author_raw.strip())
            is_bot = has_bot_marker and author_sha256 in trusted_bot_author_hashes
            bot_fields = _parse_bot_fields(body) if is_bot else None
            strict_request = not is_bot and _strict_quant_request(body)
            quality_flags = []
            if has_bot_marker and not is_bot:
                quality_flags.append("untrusted_bot_marker")
            if is_bot and bot_author_trust_status != "verified":
                quality_flags.append("bot_author_identity_unverified")
            if not strict_request and not is_bot and _BULLET_QUANT_RE.match(body):
                quality_flags.append("legacy_bullet_quant_candidate")
            if (
                not strict_request
                and not is_bot
                and _EMBEDDED_QUANT_RE.search(body)
                and "legacy_bullet_quant_candidate" not in quality_flags
            ):
                quality_flags.append("embedded_quant_candidate")
            if is_bot and bot_fields and bot_fields["terminal"]:
                event_type = "bot_terminal"
            elif is_bot and bot_fields and bot_fields["progress"]:
                event_type = "bot_progress"
            elif is_bot:
                event_type = "bot_other"
            elif strict_request:
                event_type = "quant_request"
            else:
                event_type = "human_comment"
            event_id = (
                f"EVT-{ticket_key}-{ordinal:04d}-"
                f"{_stable_suffix(ticket_key, ordinal, timestamp_raw, body)}"
            )
            event = {
                "schema_version": CORPUS_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "event_id": event_id,
                "ticket_key": ticket_key,
                "event_type": event_type,
                "source_row_number": source_row_number,
                "source_column_index": column_index + 1,
                "source_ordinal": ordinal,
                "timestamp_raw": timestamp_raw,
                "source_timezone": source_timezone_name,
                "timezone_status": timezone_status,
                "timestamp_utc_candidate": timestamp_utc,
                "after_cutoff": _parse_rfc3339(timestamp_utc, "comment timestamp") > cutoff,
                "timestamp_precision": "minute",
                "actor_role": "bot" if is_bot else "human",
                "text": body,
                "text_sha256": _sha256_text(body),
                "bot_fields": bot_fields,
                "quality_flags": quality_flags,
                "excluded_ticket": ticket_key in excluded_set,
            }
            events.append(event)
            events_by_ticket[ticket_key].append(event)
            ticket_events.append(event_id)
            comment_ids.append(event_id)

        attachment_ids = []
        attachment_ordinal = 0
        for column_index in indices.get("Attachment", []):
            raw_attachment = row[column_index]
            if not raw_attachment.strip():
                continue
            attachment_ordinal += 1
            timestamp_raw, _author_raw, filename_raw, _url_raw = _split_attachment_envelope(
                raw_attachment,
                f"{ticket_key}.Attachment[{attachment_ordinal}]",
            )
            timestamp_utc = _parse_local_timestamp(
                timestamp_raw,
                source_timezone,
                f"{ticket_key}.Attachment[{attachment_ordinal}].timestamp",
            )
            filename, counts = _sanitize_text(
                filename_raw,
                identity_replacements,
                account_values,
                redact_urls=True,
            )
            redaction_counts.update(counts)
            attachment_id = (
                f"ATT-{ticket_key}-{attachment_ordinal:04d}-"
                f"{_stable_suffix(ticket_key, attachment_ordinal, timestamp_raw, filename)}"
            )
            attachment_ids.append(attachment_id)
            attachments.append({
                "schema_version": CORPUS_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "attachment_id": attachment_id,
                "ticket_key": ticket_key,
                "source_row_number": source_row_number,
                "source_column_index": column_index + 1,
                "source_ordinal": attachment_ordinal,
                "timestamp_raw": timestamp_raw,
                "source_timezone": source_timezone_name,
                "timezone_status": timezone_status,
                "timestamp_utc_candidate": timestamp_utc,
                "after_cutoff": _parse_rfc3339(timestamp_utc, "attachment timestamp") > cutoff,
                "filename": filename,
                "extension": Path(filename).suffix.casefold(),
                "url_retained": False,
                "excluded_ticket": ticket_key in excluded_set,
            })

        tickets.append({
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "ticket_key": ticket_key,
            "issue_type": first(row, "Issue Type"),
            "status": first(row, "Status"),
            "created": {
                "raw": created_raw,
                "source_timezone": source_timezone_name,
                "timezone_status": timezone_status,
                "utc_candidate": created_utc,
            },
            "updated": {
                "raw": updated_raw,
                "source_timezone": source_timezone_name,
                "timezone_status": timezone_status,
                "utc_candidate": updated_utc,
            },
            "created_after_cutoff": _parse_rfc3339(created_utc, "issue created") > cutoff,
            "updated_after_cutoff": _parse_rfc3339(updated_utc, "issue updated") > cutoff,
            "summary": summary,
            "description": description,
            "summary_description_timestamp_basis": "issue_updated_conservative",
            "comment_event_ids": comment_ids,
            "attachment_ids": attachment_ids,
            "excluded": ticket_key in excluded_set,
        })

    if not excluded_set.issubset(seen_ticket_keys):
        raise JiraCsvNormalizerError(
            "exclusion list references missing tickets: "
            + ", ".join(sorted(excluded_set - seen_ticket_keys))
        )

    for ticket_key, ticket_events in events_by_ticket.items():
        timestamps = [event["timestamp_utc_candidate"] for event in ticket_events]
        if timestamps != sorted(timestamps):
            raise JiraCsvNormalizerError(f"comment timeline is not chronological for {ticket_key}")

    run_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        fields = event.get("bot_fields") or {}
        if fields.get("run_id"):
            run_groups[fields["run_id"]].append(event)

    runs: list[dict[str, Any]] = []
    for run_id, run_events in sorted(run_groups.items()):
        ticket_keys = {event["ticket_key"] for event in run_events}
        if len(ticket_keys) != 1:
            raise JiraCsvNormalizerError(f"run ID is reused across tickets: {run_id}")
        ticket_key = next(iter(ticket_keys))
        run_events.sort(key=lambda event: event["source_ordinal"])
        pre_cutoff_events = [event for event in run_events if not event["after_cutoff"]]
        terminal_events = [
            event for event in run_events if (event.get("bot_fields") or {}).get("terminal")
        ]
        pre_cutoff_terminals = [
            event for event in pre_cutoff_events
            if (event.get("bot_fields") or {}).get("terminal")
        ]
        pre_cutoff_outcomes = sorted({
            event["bot_fields"]["outcome"] for event in pre_cutoff_terminals
            if event["bot_fields"].get("outcome")
        })
        all_outcomes = sorted({
            event["bot_fields"]["outcome"] for event in terminal_events
            if event["bot_fields"].get("outcome")
        })
        conflict = len(pre_cutoff_outcomes) > 1
        all_terminal_conflict = len(all_outcomes) > 1
        canonical = (
            pre_cutoff_terminals[-1]
            if pre_cutoff_terminals and not conflict
            else None
        )
        first_pre_cutoff_ordinal = (
            pre_cutoff_events[0]["source_ordinal"] if pre_cutoff_events else None
        )
        preceding = [] if first_pre_cutoff_ordinal is None else [
            event for event in events_by_ticket[ticket_key]
            if event["event_type"] == "quant_request"
            and not event["after_cutoff"]
            and event["source_ordinal"] < first_pre_cutoff_ordinal
        ]
        linked_request = preceding[-1]["event_id"] if preceding else None
        flags = []
        if conflict:
            flags.append("terminal_conflict")
        if len(pre_cutoff_terminals) > 1 and not conflict:
            flags.append("duplicate_terminal_same_outcome")
        if any(event["after_cutoff"] for event in run_events):
            flags.append("contains_post_cutoff_events")
        if all_terminal_conflict and not conflict:
            flags.append("post_cutoff_terminal_conflict")
        if linked_request is None:
            flags.append("orphan_run_as_of_cutoff")
        run_record = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "run_record_id": f"RUNREC-{_stable_suffix(run_id, ticket_key, size=20)}",
            "ticket_key": ticket_key,
            "run_id": run_id,
            "run_id_kind": "explicit",
            "event_ids": [event["event_id"] for event in run_events],
            "pre_cutoff_event_ids": [event["event_id"] for event in pre_cutoff_events],
            "post_cutoff_event_ids": [
                event["event_id"] for event in run_events if event["after_cutoff"]
            ],
            "terminal_event_ids": [event["event_id"] for event in terminal_events],
            "pre_cutoff_terminal_event_ids": [
                event["event_id"] for event in pre_cutoff_terminals
            ],
            "canonical_terminal_event_id": canonical["event_id"] if canonical else None,
            "linked_request_event_id": linked_request,
            "outcome": canonical["bot_fields"]["outcome"] if canonical else None,
            "error_type": canonical["bot_fields"].get("error_type") if canonical else None,
            "branch": canonical["bot_fields"].get("branch") if canonical else None,
            "commit_sha": canonical["bot_fields"].get("commit_sha") if canonical else None,
            "terminal_conflict": conflict,
            "all_terminal_conflict": all_terminal_conflict,
            "after_cutoff": any(event["after_cutoff"] for event in run_events),
            "quality_flags": flags,
            "excluded_ticket": ticket_key in excluded_set,
        }
        runs.append(run_record)

    event_lookup = {event["event_id"]: event for event in events}
    runs_by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["linked_request_event_id"]:
            runs_by_request[run["linked_request_event_id"]].append(run)

    episodes: list[dict[str, Any]] = []
    for ticket_key, ticket_events in sorted(events_by_ticket.items()):
        requests = [event for event in ticket_events if event["event_type"] == "quant_request"]
        for position, request in enumerate(requests):
            next_ordinal = (
                requests[position + 1]["source_ordinal"] if position + 1 < len(requests) else 10**9
            )
            segment = [
                event for event in ticket_events
                if request["source_ordinal"] < event["source_ordinal"] < next_ordinal
                and not event["after_cutoff"]
            ]
            segment_terminals = [event for event in segment if event["event_type"] == "bot_terminal"]
            modern_runs = sorted(
                [
                    run for run in runs_by_request.get(request["event_id"], [])
                    if run["pre_cutoff_terminal_event_ids"]
                ],
                key=lambda run: run["run_id"],
            )
            modern_terminal_ids = {
                event_id
                for run in modern_runs
                for event_id in run["pre_cutoff_terminal_event_ids"]
            }
            legacy_terminals = [
                event for event in segment_terminals if event["event_id"] not in modern_terminal_ids
            ]
            primary_run = None
            pairing_confidence = "incomplete"
            quality_flags = []
            if (
                len(modern_runs) == 1
                and not modern_runs[0]["terminal_conflict"]
                and not legacy_terminals
            ):
                primary_run = modern_runs[0]
                pairing_confidence = "strict_modern"
            elif not modern_runs and len(legacy_terminals) == 1:
                pairing_confidence = "legacy_singleton"
                quality_flags.append("sensitivity_only")
            else:
                pairing_confidence = "ambiguous" if segment_terminals else "incomplete"
                if len(modern_runs) > 1:
                    quality_flags.append("multiple_run_candidates")
                if any(run["terminal_conflict"] for run in modern_runs):
                    quality_flags.append("terminal_conflict")
                if len(legacy_terminals) > 1:
                    quality_flags.append("ambiguous_legacy_terminals")
            if ticket_key in excluded_set:
                quality_flags.append("excluded_ticket")
            terminal_for_cutoff = (
                event_lookup[primary_run["canonical_terminal_event_id"]]
                if primary_run and primary_run["canonical_terminal_event_id"]
                else None
            )
            within_cutoff = (
                not request["after_cutoff"]
                and (terminal_for_cutoff is None or not terminal_for_cutoff["after_cutoff"])
            )
            if not within_cutoff:
                quality_flags.append("after_cutoff")
            episode_id = f"EP-{ticket_key}-{_stable_suffix(request['event_id'], size=16)}"
            episodes.append({
                "schema_version": CORPUS_SCHEMA_VERSION,
                "snapshot_id": snapshot_id,
                "episode_id": episode_id,
                "ticket_key": ticket_key,
                "request_event_id": request["event_id"],
                "request_text_sha256": request["text_sha256"],
                "run_record_ids": [run["run_record_id"] for run in modern_runs],
                "legacy_terminal_event_ids": [event["event_id"] for event in legacy_terminals],
                "primary_run_record_id": primary_run["run_record_id"] if primary_run else None,
                "outcome": primary_run["outcome"] if primary_run else None,
                "pairing_rule_version": PAIRING_RULE_VERSION,
                "pairing_confidence": pairing_confidence,
                "primary_eligible": (
                    primary_run is not None
                    and ticket_key not in excluded_set
                    and within_cutoff
                ),
                "quality_flags": sorted(set(quality_flags)),
            })

    documents: list[dict[str, str]] = []
    skipped_after_cutoff = 0
    skipped_low_information = 0

    def add_document(document: dict[str, str]) -> None:
        nonlocal skipped_after_cutoff, skipped_low_information
        if _parse_rfc3339(document["source_timestamp"], "document source_timestamp") > cutoff:
            skipped_after_cutoff += 1
            return
        if _is_low_information_document(document):
            skipped_low_information += 1
            return
        documents.append(document)

    for ticket in tickets:
        if ticket["excluded"]:
            continue
        ticket_key = ticket["ticket_key"]
        # The CSV does not include summary/description changelog timestamps.
        # Using issue Updated is conservative: it can exclude an old field that
        # was untouched by a later comment, but it cannot silently backdate a
        # field that may have changed after the cutoff.
        field_timestamp = ticket["updated"]["utc_candidate"]
        if ticket["summary"]:
            add_document(_document(
                ticket_key=ticket_key,
                source_type="summary",
                source_id=f"issue:{ticket_key}:summary",
                timestamp=field_timestamp,
                text=f"Ticket {ticket_key} summary: {ticket['summary']}",
            ))
        if ticket["description"]:
            add_document(_document(
                ticket_key=ticket_key,
                source_type="description",
                source_id=f"issue:{ticket_key}:description",
                timestamp=field_timestamp,
                text=f"Ticket {ticket_key} description:\n{ticket['description']}",
            ))

    for event in events:
        if event["excluded_ticket"] or event["event_type"] != "human_comment":
            continue
        add_document(_document(
            ticket_key=event["ticket_key"],
            source_type="comment",
            source_id=f"comment:{event['event_id']}",
            timestamp=event["timestamp_utc_candidate"],
            text=f"Ticket {event['ticket_key']} historical comment:\n{event['text']}",
        ))

    run_record_lookup = {run["run_record_id"]: run for run in runs}
    for episode in episodes:
        if not episode["primary_eligible"]:
            continue
        request = event_lookup[episode["request_event_id"]]
        run = run_record_lookup[episode["primary_run_record_id"]]
        terminal = event_lookup[run["canonical_terminal_event_id"]]
        fields = [
            f"Ticket: {episode['ticket_key']}",
            f"Request:\n{request['text']}",
            f"Outcome: {run['outcome']}",
            f"Run ID: {run['run_id']}",
        ]
        if run["error_type"]:
            fields.append(f"Error Type: {run['error_type']}")
        if run["branch"]:
            fields.append(f"Branch: {run['branch']}")
        if run["commit_sha"]:
            fields.append(f"Commit: {run['commit_sha']}")
        add_document(_document(
            ticket_key=episode["ticket_key"],
            source_type="result_summary",
            source_id=f"episode:{episode['episode_id']}",
            timestamp=terminal["timestamp_utc_candidate"],
            text="\n".join(fields),
        ))

    for attachment in attachments:
        if attachment["excluded_ticket"] or not attachment["filename"]:
            continue
        add_document(_document(
            ticket_key=attachment["ticket_key"],
            source_type="attachment_metadata",
            source_id=f"attachment:{attachment['attachment_id']}",
            timestamp=attachment["timestamp_utc_candidate"],
            text=(
                f"Ticket {attachment['ticket_key']} attachment metadata: "
                f"filename={attachment['filename']}; extension={attachment['extension'] or '[none]'}"
            ),
        ))

    documents.sort(key=lambda item: item["memory_id"])
    if len({document["memory_id"] for document in documents}) != len(documents):
        raise JiraCsvNormalizerError("generated duplicate memory IDs")
    expected_document_fields = {
        "memory_id", "source_ticket_id", "source_type", "source_id", "source_timestamp", "text",
    }
    if any(set(document) != expected_document_fields for document in documents):
        raise JiraCsvNormalizerError("adapter emitted a document with unsupported fields")

    comparison_deltas = []
    raw_clock_deltas = []
    for event in events:
        fields = event.get("bot_fields") or {}
        embedded = fields.get("embedded_event_timestamps_utc") or []
        if not embedded:
            continue
        envelope = _parse_rfc3339(event["timestamp_utc_candidate"], "event timestamp")
        embedded_last = _parse_rfc3339(embedded[-1], "embedded event timestamp")
        comparison_deltas.append((envelope - embedded_last).total_seconds() / 60.0)
        try:
            raw_clock = datetime.strptime(event["timestamp_raw"], "%d/%b/%y %I:%M %p")
            raw_clock_deltas.append(
                (raw_clock - embedded_last.replace(tzinfo=None)).total_seconds() / 60.0
            )
        except ValueError:
            pass
    median_delta = statistics.median(comparison_deltas) if comparison_deltas else None
    median_raw_clock_delta = statistics.median(raw_clock_deltas) if raw_clock_deltas else None
    timezone_conflict = median_delta is not None and abs(median_delta) > 120

    event_type_counts = Counter(event["event_type"] for event in events)
    document_type_counts = Counter(document["source_type"] for document in documents)
    episode_counts = Counter(episode["pairing_confidence"] for episode in episodes)
    outcome_counts = Counter(
        episode["outcome"] for episode in episodes if episode["primary_eligible"]
    )
    audit = {
        "schema_version": "jira-normalizer-audit-v1",
        "snapshot_id": snapshot_id,
        "source_sha256": snapshot_sha256,
        "source_row_count": len(rows),
        "source_column_count": width,
        "repeated_comment_columns": len(indices.get("Comment", [])),
        "repeated_attachment_columns": len(indices.get("Attachment", [])),
        "ticket_count": len(tickets),
        "excluded_ticket_count": len(exclusions),
        "event_count": len(events),
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "attachment_count": len(attachments),
        "explicit_run_count": len(runs),
        "explicit_runs_with_terminal": sum(bool(run["terminal_event_ids"]) for run in runs),
        "explicit_runs_with_pre_cutoff_terminal": sum(
            bool(run["pre_cutoff_terminal_event_ids"]) for run in runs
        ),
        "canonical_nonconflicting_run_count": sum(
            bool(run["canonical_terminal_event_id"]) for run in runs
        ),
        "conflicting_run_count": sum(run["terminal_conflict"] for run in runs),
        "all_snapshot_conflicting_run_count": sum(
            run["all_terminal_conflict"] for run in runs
        ),
        "authorized_bot_event_count": sum(
            event["actor_role"] == "bot" for event in events
        ),
        "untrusted_bot_marker_count": sum(
            "untrusted_bot_marker" in event["quality_flags"] for event in events
        ),
        "episode_count": len(episodes),
        "episode_pairing_counts": dict(sorted(episode_counts.items())),
        "primary_episode_count": sum(episode["primary_eligible"] for episode in episodes),
        "primary_episode_outcomes": dict(sorted(outcome_counts.items(), key=lambda item: str(item[0]))),
        "document_count": len(documents),
        "document_source_type_counts": dict(sorted(document_type_counts.items())),
        "documents_skipped_after_cutoff": skipped_after_cutoff,
        "documents_skipped_low_information": skipped_low_information,
        "redaction_counts": dict(sorted(redaction_counts.items())),
        "embedded_timestamp_comparison_count": len(comparison_deltas),
        "embedded_timestamp_median_delta_minutes": (
            round(median_delta, 3) if median_delta is not None else None
        ),
        "embedded_timestamp_median_raw_clock_delta_minutes": (
            round(median_raw_clock_delta, 3)
            if median_raw_clock_delta is not None
            else None
        ),
        "timezone_conflict_detected": timezone_conflict,
        "timezone_note": (
            "Candidate UTC values use the requested IANA timezone. Embedded bot UTC "
            "timestamps disagree materially; do not mark this snapshot formally frozen "
            "until Jira's export timezone is verified."
            if timezone_conflict
            else "No material embedded timestamp conflict was detected."
        ),
    }

    temporary = output_dir.with_name(output_dir.name + ".building")
    if temporary.exists():
        raise JiraCsvNormalizerError(f"temporary output path already exists: {temporary}")
    temporary.mkdir(parents=True)
    try:
        _write_jsonl(temporary / "tickets.jsonl", sorted(tickets, key=lambda item: item["ticket_key"]))
        _write_jsonl(temporary / "events.jsonl", sorted(events, key=lambda item: item["event_id"]))
        _write_jsonl(temporary / "attachments.jsonl", sorted(attachments, key=lambda item: item["attachment_id"]))
        _write_jsonl(temporary / "runs.jsonl", sorted(runs, key=lambda item: item["run_record_id"]))
        _write_jsonl(temporary / "episodes.jsonl", sorted(episodes, key=lambda item: item["episode_id"]))
        _write_jsonl(temporary / "documents.jsonl", documents)
        (temporary / "excluded_ticket_ids.json").write_text(
            json.dumps(exclusions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (temporary / "audit.json").write_text(
            json.dumps(audit, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        hashed_files = [
            "tickets.jsonl", "events.jsonl", "attachments.jsonl", "runs.jsonl",
            "episodes.jsonl", "documents.jsonl", "excluded_ticket_ids.json", "audit.json",
        ]
        manifest = {
            "schema_version": "jira-corpus-manifest-v1",
            "status": "draft_not_formally_frozen",
            "snapshot_id": snapshot_id,
            "source_format": "jira_csv_all_fields",
            "source_filename_sha256": _sha256_text(input_csv.name),
            "source_file_extension": input_csv.suffix.casefold(),
            "source_sha256": snapshot_sha256,
            "normalizer_version": NORMALIZER_VERSION,
            "normalizer_script_sha256": normalizer_script_sha256,
            "normalizer_repository_revision": (
                normalizer_repository_revision.casefold()
                if normalizer_repository_revision is not None
                else None
            ),
            "redaction_rule_version": REDACTION_RULE_VERSION,
            "document_filter_rule_version": DOCUMENT_FILTER_RULE_VERSION,
            "pairing_rule_version": PAIRING_RULE_VERSION,
            "source_timezone_requested": source_timezone_name,
            "timezone_status": timezone_status,
            "timezone_conflict_detected": timezone_conflict,
            "cutoff_at": cutoff_at,
            "exclusion_list_sha256": _sha256_text(_canonical_json(exclusions)),
            "trusted_bot_author_sha256s": trusted_bot_author_hashes,
            "bot_author_trust_status": bot_author_trust_status,
            "structured_identity_values_retained": False,
            "free_text_identity_redaction_status": (
                "heuristic_and_operator_lexicon_manual_review_required"
            ),
            "identity_lexicon_sha256": _sha256_text(_canonical_json(
                sorted(identity for identity, _ in identity_replacements)
            )),
            "additional_identity_source_sha256": additional_identity_source_sha256,
            "attachment_urls_retained": False,
            "record_counts": {
                "tickets": len(tickets),
                "events": len(events),
                "attachments": len(attachments),
                "runs": len(runs),
                "episodes": len(episodes),
                "primary_episodes": sum(episode["primary_eligible"] for episode in episodes),
                "documents": len(documents),
            },
            "file_sha256": {
                name: _output_hash(temporary / name) for name in hashed_files
            },
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        combined_output = "".join(
            (temporary / name).read_text(encoding="utf-8")
            for name in [
                "tickets.jsonl", "events.jsonl", "attachments.jsonl", "runs.jsonl",
                "episodes.jsonl", "documents.jsonl", "audit.json", "manifest.json",
            ]
        )
        if _EMAIL_RE.search(combined_output):
            raise JiraCsvNormalizerError("sanitized outputs still contain an email address")
        for account_value in account_values:
            if account_value and account_value in combined_output:
                raise JiraCsvNormalizerError("sanitized outputs still contain an account ID")
        for identity, case_sensitive in identity_replacements:
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(rf"(?<!\w){re.escape(identity)}(?!\w)", combined_output, flags=flags):
                raise JiraCsvNormalizerError(
                    "sanitized outputs still contain identity fingerprint "
                    f"{_sha256_text(identity.casefold())[:12]}"
                )
        for document in documents:
            if len(document["text"]) > MAX_INDEX_TEXT_CHARS:
                raise JiraCsvNormalizerError("index document exceeds text limit")
        temporary.rename(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"manifest": manifest, "audit": audit}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize a Jira all-fields CSV into a sanitized RAG corpus."
    )
    parser.add_argument("--input-csv", type=Path, required=True)
    parser.add_argument("--excluded-ticket-ids", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-timezone", required=True)
    parser.add_argument(
        "--bot-author-sha256",
        action="append",
        required=True,
        help=(
            "SHA-256 of an authorized raw Jira bot-author account ID; repeat for "
            "rotated bot accounts"
        ),
    )
    parser.add_argument(
        "--bot-author-trust-status",
        choices=("verified", "snapshot_candidate_unverified"),
        default="snapshot_candidate_unverified",
        help="whether the supplied hashes were independently verified by Jira administration/API",
    )
    parser.add_argument(
        "--additional-identities",
        type=Path,
        help="local-only JSON array of extra identity literals to redact",
    )
    parser.add_argument(
        "--normalizer-repository-revision",
        help="optional full Git commit SHA containing the normalizer",
    )
    parser.add_argument(
        "--timezone-status",
        default="operator_assumption",
        choices=(
            "verified",
            "operator_assumption",
            "operator_assumption_conflicts_with_embedded_utc",
        ),
    )
    parser.add_argument("--cutoff-at", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = normalize_snapshot(
        input_csv=args.input_csv,
        exclusions_path=args.excluded_ticket_ids,
        output_dir=args.output_dir,
        source_timezone_name=args.source_timezone,
        timezone_status=args.timezone_status,
        cutoff_at=args.cutoff_at,
        bot_author_sha256s=args.bot_author_sha256,
        bot_author_trust_status=args.bot_author_trust_status,
        additional_identities_path=args.additional_identities,
        normalizer_repository_revision=args.normalizer_repository_revision,
    )
    print(_canonical_json({
        "snapshot_id": result["manifest"]["snapshot_id"],
        "source_sha256": result["manifest"]["source_sha256"],
        "document_count": result["audit"]["document_count"],
        "primary_episode_count": result["audit"]["primary_episode_count"],
        "timezone_conflict_detected": result["audit"]["timezone_conflict_detected"],
        "output_dir": str(args.output_dir),
    }))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JiraCsvNormalizerError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2) from exc
