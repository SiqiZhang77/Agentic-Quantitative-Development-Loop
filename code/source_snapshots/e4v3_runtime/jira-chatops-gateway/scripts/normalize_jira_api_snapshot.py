#!/usr/bin/env python3
"""Normalize a private Jira API snapshot into a sanitized RAG corpus.

The raw API snapshot remains private.  This adapter verifies its manifest,
normalizes offset-aware timestamps (including Jira changelog epoch values),
applies a strict cutoff, redacts structured identities and free text, pairs
requests with bot runs, and emits the six-field document contract accepted by
the frozen lexical-index builder.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from normalize_jira_csv_export import (
        CORPUS_SCHEMA_VERSION,
        DOCUMENT_FILTER_RULE_VERSION,
        PAIRING_RULE_VERSION,
        REDACTION_RULE_VERSION,
        JiraCsvNormalizerError,
        _BOT_MARKER_RE,
        _BULLET_QUANT_RE,
        _EMBEDDED_QUANT_RE,
        _EMAIL_RE,
        _TICKET_RE,
        _canonical_json,
        _document,
        _identity_literals,
        _is_low_information_document,
        _parse_bot_fields,
        _parse_rfc3339,
        _read_additional_identities,
        _read_exclusions,
        _sanitize_text,
        _sha256_bytes,
        _sha256_text,
        _stable_suffix,
        _strict_quant_request,
        _utc_text,
        _validate_bot_author_hashes,
        _write_jsonl,
    )
except ImportError:  # package import used by pytest
    from scripts.normalize_jira_csv_export import (
    CORPUS_SCHEMA_VERSION,
    DOCUMENT_FILTER_RULE_VERSION,
    PAIRING_RULE_VERSION,
    REDACTION_RULE_VERSION,
    JiraCsvNormalizerError,
    _BOT_MARKER_RE,
    _BULLET_QUANT_RE,
    _EMBEDDED_QUANT_RE,
    _EMAIL_RE,
    _TICKET_RE,
    _canonical_json,
    _document,
    _identity_literals,
    _is_low_information_document,
    _parse_bot_fields,
    _parse_rfc3339,
    _read_additional_identities,
    _read_exclusions,
    _sanitize_text,
    _sha256_bytes,
    _sha256_text,
    _stable_suffix,
    _strict_quant_request,
    _utc_text,
    _validate_bot_author_hashes,
    _write_jsonl,
    )


NORMALIZER_VERSION = "1.1.0"
API_NORMALIZER_SCHEMA_VERSION = "jira-api-normalizer-audit-v1"


class JiraApiNormalizerError(JiraCsvNormalizerError):
    """Raised when a Jira API snapshot cannot be safely normalized."""


def _identity_fingerprint_in_text(
    text: str,
    replacements: Iterable[tuple[str, bool]],
) -> str | None:
    """Return a privacy-safe fingerprint for a residual identity, if any."""

    grouped: dict[bool, list[str]] = {True: [], False: []}
    for identity, case_sensitive in replacements:
        grouped[case_sensitive].append(identity)
    # Chunking avoids both one enormous regular expression and the previous
    # O(identity_count * corpus_size) final audit.
    for case_sensitive, identities in grouped.items():
        flags = 0 if case_sensitive else re.IGNORECASE
        for offset in range(0, len(identities), 200):
            chunk = identities[offset : offset + 200]
            if not chunk:
                continue
            pattern = re.compile(
                r"(?<!\w)(?:" + "|".join(re.escape(item) for item in chunk) + r")(?!\w)",
                flags=flags,
            )
            match = pattern.search(text)
            if match:
                return _sha256_text(match.group(0).casefold())[:12]
    return None


def _api_utc(value: Any, label: str) -> str:
    if isinstance(value, bool):
        raise JiraApiNormalizerError(f"{label} has an invalid timestamp")
    if isinstance(value, (int, float)):
        try:
            return _utc_text(datetime.fromtimestamp(value / 1000.0, tz=timezone.utc))
        except (OSError, OverflowError, ValueError) as exc:
            raise JiraApiNormalizerError(f"{label} has an invalid epoch timestamp") from exc
    if not isinstance(value, str) or not value.strip():
        raise JiraApiNormalizerError(f"{label} is missing a timestamp")
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", value.strip())
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JiraApiNormalizerError(f"{label} has an invalid timestamp") from exc
    if parsed.tzinfo is None:
        raise JiraApiNormalizerError(f"{label} timestamp has no UTC offset")
    return _utc_text(parsed)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise JiraApiNormalizerError(
                        f"{path.name}:{line_number} must contain a JSON object"
                    )
                records.append(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise JiraApiNormalizerError(f"cannot read {path}: {exc}") from exc
    return records


def _load_and_verify_snapshot(snapshot_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        manifest = json.loads((snapshot_dir / "manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((snapshot_dir / "audit.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JiraApiNormalizerError(f"cannot read API snapshot metadata: {exc}") from exc
    if manifest.get("schema_version") != "jira-api-snapshot-v1":
        raise JiraApiNormalizerError("unsupported Jira API snapshot schema")
    if audit.get("status") != "complete":
        raise JiraApiNormalizerError("Jira API snapshot is not complete")
    for required_flag in (
        "issue_end_verification_enabled",
        "changelog_enabled",
        "attachment_metadata_enabled",
    ):
        if audit.get(required_flag) is not True:
            raise JiraApiNormalizerError(f"formal source requires {required_flag}=true")
    if audit.get("comment_concurrent_update_tickets"):
        raise JiraApiNormalizerError("snapshot contains concurrently changing comments")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise JiraApiNormalizerError("snapshot manifest has no file ledger")
    for relative, expected in files.items():
        path = snapshot_dir / relative
        if not path.is_file() or _sha256_bytes(path.read_bytes()) != expected:
            raise JiraApiNormalizerError(f"snapshot hash mismatch: {relative}")
    return manifest, audit


def _collect_raw_identities(raw_dir: Path) -> tuple[set[str], set[str]]:
    display_values: set[str] = set()
    account_values: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in {"displayName", "publicName", "emailAddress"} and isinstance(item, str):
                if item.strip():
                    display_values.add(item.strip())
            elif key in {"accountId", "tmpFromAccountId", "tmpToAccountId"} and isinstance(item, str):
                if item.strip():
                    account_values.add(item.strip())
            walk(item)

    for path in sorted(raw_dir.rglob("*.json")):
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise JiraApiNormalizerError(f"cannot inspect private identities in {path}: {exc}") from exc
    return display_values, account_values


def _identity_replacements(
    issues: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    attachments: list[dict[str, Any]],
    display_values: Iterable[str],
    account_values: Iterable[str],
    additional: Iterable[str],
) -> tuple[list[tuple[str, bool]], set[str]]:
    # Reuse the CSV normalizer's conservative identity heuristics by presenting
    # equivalent synthetic rows in memory.  No synthetic file is written.
    header = ["Summary", "Description", "Comment", "Attachment", "Reporter", "Reporter Id"]
    indices = {name: [index] for index, name in enumerate(header)}
    rows: list[list[str]] = []
    for issue in issues:
        rows.append([
            str(issue.get("summary") or ""),
            str(issue.get("description_text") or ""),
            "", "", "", "",
        ])
    for comment in comments:
        body = str(comment.get("body_text") or "").strip()
        if body:
            rows.append(["", "", f"01/Jan/26 01:00 PM;;{body}", "", "", ""])
    for attachment in attachments:
        filename = str(attachment.get("filename") or "").strip()
        if filename:
            rows.append(["", "", "", f"01/Jan/26 01:00 PM;;{filename};", "", ""])
    for value in sorted(set(display_values)):
        rows.append(["", "", "", "", value, ""])
    for value in sorted(set(account_values)):
        rows.append(["", "", "", "", "", value])
    replacements, discovered_accounts = _identity_literals(rows, indices, additional)
    return replacements, discovered_accounts | set(account_values)


def _field_state_at_cutoff(
    *,
    current: str,
    histories: list[dict[str, Any]],
    field: str,
    created_utc: str,
    cutoff: datetime,
) -> tuple[str, str]:
    field_key = field.casefold()
    dated = sorted(
        ((_api_utc(history.get("created"), "changelog.created"), history) for history in histories),
        key=lambda item: (item[0], str(item[1].get("history_id") or "")),
    )
    value = current
    for timestamp, history in reversed(dated):
        if _parse_rfc3339(timestamp, "changelog timestamp") <= cutoff:
            continue
        for item in reversed(history.get("items") or []):
            if str(item.get("field") or "").casefold() == field_key:
                value = str(item.get("fromString") or "")
    eligible_changes = [
        timestamp
        for timestamp, history in dated
        if _parse_rfc3339(timestamp, "changelog timestamp") <= cutoff
        and any(str(item.get("field") or "").casefold() == field_key for item in history.get("items") or [])
    ]
    return value, (eligible_changes[-1] if eligible_changes else created_utc)


def normalize_api_snapshot(
    *,
    snapshot_dir: Path,
    exclusions_path: Path,
    output_dir: Path,
    cutoff_at: str,
    bot_author_sha256s: Iterable[str],
    bot_author_trust_status: str = "snapshot_candidate_unverified",
    additional_identities_path: Path | None = None,
    normalizer_repository_revision: str | None = None,
) -> dict[str, Any]:
    cutoff = _parse_rfc3339(cutoff_at, "cutoff_at")
    if output_dir.exists():
        raise JiraApiNormalizerError(f"refusing to overwrite output directory: {output_dir}")
    trusted_bots = _validate_bot_author_hashes(bot_author_sha256s)
    if bot_author_trust_status not in {"verified", "snapshot_candidate_unverified"}:
        raise JiraApiNormalizerError("unsupported bot author trust status")
    if normalizer_repository_revision is not None and not re.fullmatch(
        r"[0-9a-f]{40}", normalizer_repository_revision.casefold()
    ):
        raise JiraApiNormalizerError("normalizer repository revision must be a full Git SHA")

    source_manifest, source_audit = _load_and_verify_snapshot(snapshot_dir)
    if source_audit.get("cutoff_at") != cutoff_at:
        raise JiraApiNormalizerError("normalizer cutoff does not match snapshot cutoff")
    records_dir = snapshot_dir / "records"
    raw_issues = _read_jsonl(records_dir / "issues.jsonl")
    raw_comments = _read_jsonl(records_dir / "comments.jsonl")
    raw_changes = _read_jsonl(records_dir / "changelogs.jsonl")
    raw_attachments = _read_jsonl(records_dir / "attachments.jsonl")
    expected_counts = {
        "issue_count": len(raw_issues),
        "comment_count": len(raw_comments),
        "changelog_history_count": len(raw_changes),
        "attachment_metadata_count": len(raw_attachments),
    }
    for key, actual in expected_counts.items():
        if source_audit.get(key) != actual:
            raise JiraApiNormalizerError(f"source audit count mismatch: {key}")

    exclusions = _read_exclusions(exclusions_path)
    excluded_set = set(exclusions)
    ticket_keys = {str(issue.get("ticket_key") or "") for issue in raw_issues}
    if any(not _TICKET_RE.fullmatch(key) for key in ticket_keys):
        raise JiraApiNormalizerError("API snapshot contains an invalid ticket key")
    if not excluded_set.issubset(ticket_keys):
        raise JiraApiNormalizerError(
            "exclusion list references missing tickets: " + ", ".join(sorted(excluded_set - ticket_keys))
        )
    if len(ticket_keys) != len(raw_issues):
        raise JiraApiNormalizerError("API snapshot contains duplicate tickets")

    additional, additional_hash = _read_additional_identities(additional_identities_path)
    display_values, raw_account_values = _collect_raw_identities(snapshot_dir / "raw")
    identity_replacements, account_values = _identity_replacements(
        raw_issues, raw_comments, raw_attachments,
        display_values, raw_account_values, additional,
    )
    redaction_counts: Counter[str] = Counter()

    def sanitize(value: Any) -> str:
        text, counts = _sanitize_text(str(value or ""), identity_replacements, account_values)
        redaction_counts.update(counts)
        return text

    changes_by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    normalized_changes: list[dict[str, Any]] = []
    corrected_change_after_cutoff = 0
    for change in raw_changes:
        key = str(change.get("ticket_key") or "")
        timestamp = _api_utc(change.get("created"), f"{key}.changelog.created")
        after = _parse_rfc3339(timestamp, "changelog timestamp") > cutoff
        corrected_change_after_cutoff += int(after)
        sanitized_items = []
        for item in change.get("items") or []:
            sanitized_items.append({
                "field": sanitize(item.get("field")),
                "field_id": sanitize(item.get("fieldId")),
                "from_string": sanitize(item.get("fromString")),
                "to_string": sanitize(item.get("toString")),
            })
        record = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": source_manifest["snapshot_id"],
            "history_id": str(change.get("history_id") or ""),
            "ticket_key": key,
            "timestamp_raw": change.get("created"),
            "timestamp_utc": timestamp,
            "after_cutoff": after,
            "author_account_id_sha256": change.get("author_account_id_sha256"),
            "items": sanitized_items,
            "excluded_ticket": key in excluded_set,
        }
        normalized_changes.append(record)
        changes_by_ticket[key].append(change)

    tickets: list[dict[str, Any]] = []
    for issue in raw_issues:
        key = str(issue["ticket_key"])
        created = _api_utc(issue.get("created"), f"{key}.created")
        updated = _api_utc(issue.get("updated"), f"{key}.updated")
        summary_at_cutoff, summary_timestamp = _field_state_at_cutoff(
            current=str(issue.get("summary") or ""), histories=changes_by_ticket[key],
            field="summary", created_utc=created, cutoff=cutoff,
        )
        description_at_cutoff, description_timestamp = _field_state_at_cutoff(
            current=str(issue.get("description_text") or ""), histories=changes_by_ticket[key],
            field="description", created_utc=created, cutoff=cutoff,
        )
        tickets.append({
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": source_manifest["snapshot_id"],
            "ticket_key": key,
            "issue_id": str(issue.get("issue_id") or ""),
            "issue_type": sanitize((issue.get("issue_type") or {}).get("name")),
            "status": sanitize((issue.get("status") or {}).get("name")),
            "created": {"raw": issue.get("created"), "timezone_status": "api_offset_verified", "utc": created},
            "updated": {"raw": issue.get("updated"), "timezone_status": "api_offset_verified", "utc": updated},
            "created_after_cutoff": _parse_rfc3339(created, "issue created") > cutoff,
            "updated_after_cutoff": _parse_rfc3339(updated, "issue updated") > cutoff,
            "summary": sanitize(summary_at_cutoff),
            "description": sanitize(description_at_cutoff),
            "summary_source_timestamp": summary_timestamp,
            "description_source_timestamp": description_timestamp,
            "summary_description_timestamp_basis": "field_changelog_at_or_before_cutoff_or_created",
            "comment_event_ids": [],
            "attachment_ids": [],
            "excluded": key in excluded_set,
        })
    ticket_lookup = {ticket["ticket_key"]: ticket for ticket in tickets}

    events: list[dict[str, Any]] = []
    events_by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    grouped_comments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comment in raw_comments:
        grouped_comments[str(comment.get("ticket_key") or "")].append(comment)
    for key, comments in sorted(grouped_comments.items()):
        comments.sort(key=lambda item: (
            _api_utc(item.get("created"), f"{key}.comment.created"),
            str(item.get("comment_id") or ""),
        ))
        for ordinal, comment in enumerate(comments, 1):
            created = _api_utc(comment.get("created"), f"{key}.comment.created")
            updated = _api_utc(comment.get("updated"), f"{key}.comment.updated")
            body_raw = str(comment.get("body_text") or "")
            body = sanitize(body_raw)
            author_hash = str(comment.get("author_account_id_sha256") or "")
            has_marker = bool(_BOT_MARKER_RE.match(body_raw))
            is_bot = has_marker and author_hash in trusted_bots
            bot_fields = _parse_bot_fields(body) if is_bot else None
            strict_request = not is_bot and _strict_quant_request(body)
            flags: list[str] = []
            if has_marker and not is_bot:
                flags.append("untrusted_bot_marker")
            if is_bot and bot_author_trust_status != "verified":
                flags.append("bot_author_identity_unverified")
            if not strict_request and not is_bot and _BULLET_QUANT_RE.match(body):
                flags.append("legacy_bullet_quant_candidate")
            if not strict_request and not is_bot and _EMBEDDED_QUANT_RE.search(body) and not flags:
                flags.append("embedded_quant_candidate")
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
            event_id = f"EVT-{key}-C{comment['comment_id']}-{_stable_suffix(key, comment['comment_id'], updated, body, size=12)}"
            event = {
                "schema_version": CORPUS_SCHEMA_VERSION,
                "snapshot_id": source_manifest["snapshot_id"],
                "event_id": event_id,
                "ticket_key": key,
                "event_type": event_type,
                "source_comment_id": str(comment["comment_id"]),
                "source_ordinal": ordinal,
                "timestamp_raw": comment.get("created"),
                "source_timezone": "api_offset",
                "timezone_status": "verified",
                "timestamp_utc_candidate": created,
                "content_updated_utc": updated,
                "after_cutoff": _parse_rfc3339(updated, "comment updated") > cutoff,
                "timestamp_precision": "api",
                "actor_role": "bot" if is_bot else "human",
                "actor_account_id_sha256": author_hash or None,
                "text": body,
                "text_sha256": _sha256_text(body),
                "bot_fields": bot_fields,
                "quality_flags": flags,
                "excluded_ticket": key in excluded_set,
            }
            events.append(event)
            events_by_ticket[key].append(event)
            ticket_lookup[key]["comment_event_ids"].append(event_id)

    attachments: list[dict[str, Any]] = []
    for attachment in raw_attachments:
        key = str(attachment.get("ticket_key") or "")
        created = _api_utc(attachment.get("created"), f"{key}.attachment.created")
        attachment_id = str(attachment.get("attachment_id") or "")
        filename = sanitize(attachment.get("filename"))
        record = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": source_manifest["snapshot_id"],
            "attachment_id": attachment_id,
            "ticket_key": key,
            "timestamp_raw": attachment.get("created"),
            "source_timezone": "api_offset",
            "timezone_status": "verified",
            "timestamp_utc_candidate": created,
            "after_cutoff": _parse_rfc3339(created, "attachment timestamp") > cutoff,
            "filename": filename,
            "extension": Path(filename).suffix.casefold(),
            "mime_type": sanitize(attachment.get("mime_type")),
            "size": attachment.get("size"),
            "author_account_id_sha256": attachment.get("author_account_id_sha256"),
            "url_retained": False,
            "content_downloaded": False,
            "excluded_ticket": key in excluded_set,
        }
        attachments.append(record)
        ticket_lookup[key]["attachment_ids"].append(attachment_id)

    for key, ticket_events in events_by_ticket.items():
        timestamps = [event["timestamp_utc_candidate"] for event in ticket_events]
        if timestamps != sorted(timestamps):
            raise JiraApiNormalizerError(f"comment timeline is not chronological for {key}")

    run_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        run_id = (event.get("bot_fields") or {}).get("run_id")
        if run_id:
            run_groups[run_id].append(event)
    runs: list[dict[str, Any]] = []
    for run_id, run_events in sorted(run_groups.items()):
        keys = {event["ticket_key"] for event in run_events}
        if len(keys) != 1:
            raise JiraApiNormalizerError(f"run ID is reused across tickets: {run_id}")
        key = next(iter(keys))
        run_events.sort(key=lambda event: event["source_ordinal"])
        pre = [event for event in run_events if not event["after_cutoff"]]
        terminals = [event for event in run_events if (event.get("bot_fields") or {}).get("terminal")]
        pre_terminals = [event for event in pre if (event.get("bot_fields") or {}).get("terminal")]
        pre_outcomes = sorted({event["bot_fields"]["outcome"] for event in pre_terminals if event["bot_fields"].get("outcome")})
        all_outcomes = sorted({event["bot_fields"]["outcome"] for event in terminals if event["bot_fields"].get("outcome")})
        conflict = len(pre_outcomes) > 1
        canonical = pre_terminals[-1] if pre_terminals and not conflict else None
        first_ordinal = pre[0]["source_ordinal"] if pre else None
        preceding = [] if first_ordinal is None else [
            event for event in events_by_ticket[key]
            if event["event_type"] == "quant_request" and not event["after_cutoff"]
            and event["source_ordinal"] < first_ordinal
        ]
        linked = preceding[-1]["event_id"] if preceding else None
        flags = []
        if conflict: flags.append("terminal_conflict")
        if len(pre_terminals) > 1 and not conflict: flags.append("duplicate_terminal_same_outcome")
        if any(event["after_cutoff"] for event in run_events): flags.append("contains_post_cutoff_events")
        if len(all_outcomes) > 1 and not conflict: flags.append("post_cutoff_terminal_conflict")
        if linked is None: flags.append("orphan_run_as_of_cutoff")
        runs.append({
            "schema_version": CORPUS_SCHEMA_VERSION,
            "snapshot_id": source_manifest["snapshot_id"],
            "run_record_id": f"RUNREC-{_stable_suffix(run_id, key, size=20)}",
            "ticket_key": key,
            "run_id": run_id,
            "run_id_kind": "explicit",
            "event_ids": [event["event_id"] for event in run_events],
            "pre_cutoff_event_ids": [event["event_id"] for event in pre],
            "post_cutoff_event_ids": [event["event_id"] for event in run_events if event["after_cutoff"]],
            "terminal_event_ids": [event["event_id"] for event in terminals],
            "pre_cutoff_terminal_event_ids": [event["event_id"] for event in pre_terminals],
            "canonical_terminal_event_id": canonical["event_id"] if canonical else None,
            "linked_request_event_id": linked,
            "outcome": canonical["bot_fields"]["outcome"] if canonical else None,
            "error_type": canonical["bot_fields"].get("error_type") if canonical else None,
            "branch": canonical["bot_fields"].get("branch") if canonical else None,
            "commit_sha": canonical["bot_fields"].get("commit_sha") if canonical else None,
            "terminal_conflict": conflict,
            "all_terminal_conflict": len(all_outcomes) > 1,
            "after_cutoff": any(event["after_cutoff"] for event in run_events),
            "quality_flags": flags,
            "excluded_ticket": key in excluded_set,
        })

    event_lookup = {event["event_id"]: event for event in events}
    runs_by_request: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["linked_request_event_id"]:
            runs_by_request[run["linked_request_event_id"]].append(run)
    episodes: list[dict[str, Any]] = []
    for key, ticket_events in sorted(events_by_ticket.items()):
        requests = [event for event in ticket_events if event["event_type"] == "quant_request"]
        for position, request in enumerate(requests):
            next_ordinal = requests[position + 1]["source_ordinal"] if position + 1 < len(requests) else 10**9
            segment = [event for event in ticket_events if request["source_ordinal"] < event["source_ordinal"] < next_ordinal and not event["after_cutoff"]]
            segment_terminals = [event for event in segment if event["event_type"] == "bot_terminal"]
            modern_runs = sorted([
                run for run in runs_by_request.get(request["event_id"], [])
                if run["pre_cutoff_terminal_event_ids"]
            ], key=lambda run: run["run_id"])
            modern_terminal_ids = {event_id for run in modern_runs for event_id in run["pre_cutoff_terminal_event_ids"]}
            legacy = [event for event in segment_terminals if event["event_id"] not in modern_terminal_ids]
            primary = None
            confidence = "incomplete"
            flags: list[str] = []
            if len(modern_runs) == 1 and not modern_runs[0]["terminal_conflict"] and not legacy:
                primary = modern_runs[0]; confidence = "strict_modern"
            elif not modern_runs and len(legacy) == 1:
                confidence = "legacy_singleton"; flags.append("sensitivity_only")
            else:
                confidence = "ambiguous" if segment_terminals else "incomplete"
                if len(modern_runs) > 1: flags.append("multiple_run_candidates")
                if any(run["terminal_conflict"] for run in modern_runs): flags.append("terminal_conflict")
                if len(legacy) > 1: flags.append("ambiguous_legacy_terminals")
            if key in excluded_set: flags.append("excluded_ticket")
            terminal = event_lookup[primary["canonical_terminal_event_id"]] if primary and primary["canonical_terminal_event_id"] else None
            within = not request["after_cutoff"] and (terminal is None or not terminal["after_cutoff"])
            if not within: flags.append("after_cutoff")
            episode_id = f"EP-{key}-{_stable_suffix(request['event_id'], size=16)}"
            episodes.append({
                "schema_version": CORPUS_SCHEMA_VERSION,
                "snapshot_id": source_manifest["snapshot_id"],
                "episode_id": episode_id,
                "ticket_key": key,
                "request_event_id": request["event_id"],
                "request_text_sha256": request["text_sha256"],
                "run_record_ids": [run["run_record_id"] for run in modern_runs],
                "legacy_terminal_event_ids": [event["event_id"] for event in legacy],
                "primary_run_record_id": primary["run_record_id"] if primary else None,
                "outcome": primary["outcome"] if primary else None,
                "pairing_rule_version": PAIRING_RULE_VERSION,
                "pairing_confidence": confidence,
                "primary_eligible": primary is not None and key not in excluded_set and within,
                "quality_flags": sorted(set(flags)),
            })

    documents: list[dict[str, str]] = []
    skipped_after_cutoff = 0
    skipped_low_information = 0
    def add_document(document: dict[str, str]) -> None:
        nonlocal skipped_after_cutoff, skipped_low_information
        if _parse_rfc3339(document["source_timestamp"], "document timestamp") > cutoff:
            skipped_after_cutoff += 1; return
        if _is_low_information_document(document):
            skipped_low_information += 1; return
        documents.append(document)
    for ticket in tickets:
        if ticket["excluded"] or ticket["created_after_cutoff"]: continue
        key = ticket["ticket_key"]
        if ticket["summary"]:
            add_document(_document(ticket_key=key, source_type="summary", source_id=f"issue:{key}:summary", timestamp=ticket["summary_source_timestamp"], text=f"Ticket {key} summary: {ticket['summary']}"))
        if ticket["description"]:
            add_document(_document(ticket_key=key, source_type="description", source_id=f"issue:{key}:description", timestamp=ticket["description_source_timestamp"], text=f"Ticket {key} description:\n{ticket['description']}"))
    for event in events:
        if (
            event["excluded_ticket"]
            or event["event_type"] != "human_comment"
            or event["after_cutoff"]
            or not event["text"]
        ):
            continue
        add_document(_document(ticket_key=event["ticket_key"], source_type="comment", source_id=f"comment:{event['source_comment_id']}", timestamp=event["content_updated_utc"], text=f"Ticket {event['ticket_key']} historical comment:\n{event['text']}"))
    run_lookup = {run["run_record_id"]: run for run in runs}
    for episode in episodes:
        if not episode["primary_eligible"]: continue
        request = event_lookup[episode["request_event_id"]]
        run = run_lookup[episode["primary_run_record_id"]]
        terminal = event_lookup[run["canonical_terminal_event_id"]]
        fields = [f"Ticket: {episode['ticket_key']}", f"Request:\n{request['text']}", f"Outcome: {run['outcome']}", f"Run ID: {run['run_id']}"]
        if run["error_type"]: fields.append(f"Error Type: {run['error_type']}")
        if run["branch"]: fields.append(f"Branch: {run['branch']}")
        if run["commit_sha"]: fields.append(f"Commit: {run['commit_sha']}")
        add_document(_document(ticket_key=episode["ticket_key"], source_type="result_summary", source_id=f"episode:{episode['episode_id']}", timestamp=terminal["content_updated_utc"], text="\n".join(fields)))
    for attachment in attachments:
        if attachment["excluded_ticket"] or attachment["after_cutoff"] or not attachment["filename"]: continue
        add_document(_document(ticket_key=attachment["ticket_key"], source_type="attachment_metadata", source_id=f"attachment:{attachment['attachment_id']}", timestamp=attachment["timestamp_utc_candidate"], text=f"Ticket {attachment['ticket_key']} attachment metadata: filename={attachment['filename']}; extension={attachment['extension'] or '[none]'}"))
    for change in normalized_changes:
        if change["excluded_ticket"] or change["after_cutoff"]: continue
        for item_index, item in enumerate(change["items"]):
            field = item["field"].casefold()
            if field == "status":
                text = f"Ticket {change['ticket_key']} status changed: {item['from_string'] or '[none]'} -> {item['to_string'] or '[none]'}"
                source_type = "status_change"
            elif field == "description" and item["to_string"]:
                text = f"Ticket {change['ticket_key']} description update:\n{item['to_string']}"
                source_type = "description_update"
            else:
                continue
            add_document(_document(ticket_key=change["ticket_key"], source_type=source_type, source_id=f"changelog:{change['history_id']}:{item_index}", timestamp=change["timestamp_utc"], text=text))
    documents.sort(key=lambda item: item["memory_id"])
    if len({item["memory_id"] for item in documents}) != len(documents):
        raise JiraApiNormalizerError("generated duplicate memory IDs")

    event_types = Counter(event["event_type"] for event in events)
    episode_types = Counter(episode["pairing_confidence"] for episode in episodes)
    outcomes = Counter(episode["outcome"] for episode in episodes if episode["primary_eligible"])
    document_types = Counter(document["source_type"] for document in documents)
    audit = {
        "schema_version": API_NORMALIZER_SCHEMA_VERSION,
        "snapshot_id": source_manifest["snapshot_id"],
        "source_snapshot_content_sha256": source_manifest["snapshot_content_sha256"],
        "ticket_count": len(tickets),
        "excluded_ticket_count": len(exclusions),
        "event_count": len(events),
        "event_type_counts": dict(sorted(event_types.items())),
        "attachment_count": len(attachments),
        "changelog_history_count": len(normalized_changes),
        "changelog_after_cutoff_count_corrected": corrected_change_after_cutoff,
        "explicit_run_count": len(runs),
        "conflicting_run_count": sum(run["terminal_conflict"] for run in runs),
        "authorized_bot_event_count": sum(event["actor_role"] == "bot" for event in events),
        "untrusted_bot_marker_count": sum("untrusted_bot_marker" in event["quality_flags"] for event in events),
        "episode_count": len(episodes),
        "episode_pairing_counts": dict(sorted(episode_types.items())),
        "primary_episode_count": sum(episode["primary_eligible"] for episode in episodes),
        "primary_episode_outcomes": dict(sorted(outcomes.items(), key=lambda item: str(item[0]))),
        "document_count": len(documents),
        "document_source_type_counts": dict(sorted(document_types.items())),
        "documents_skipped_after_cutoff": skipped_after_cutoff,
        "documents_skipped_low_information": skipped_low_information,
        "redaction_counts": dict(sorted(redaction_counts.items())),
        "timezone_status": "api_offsets_verified",
        "source_exporter_timezone": source_audit.get("exporter_timezone"),
        "source_server_timezone": source_audit.get("server_timezone"),
    }

    temporary = output_dir.with_name(output_dir.name + ".building")
    if temporary.exists():
        raise JiraApiNormalizerError(f"temporary output path already exists: {temporary}")
    temporary.mkdir(parents=True, mode=0o700)
    try:
        outputs = {
            "tickets.jsonl": sorted(tickets, key=lambda item: item["ticket_key"]),
            "events.jsonl": sorted(events, key=lambda item: item["event_id"]),
            "attachments.jsonl": sorted(attachments, key=lambda item: item["attachment_id"]),
            "changelogs.jsonl": sorted(normalized_changes, key=lambda item: (item["ticket_key"], item["timestamp_utc"], item["history_id"])),
            "runs.jsonl": sorted(runs, key=lambda item: item["run_record_id"]),
            "episodes.jsonl": sorted(episodes, key=lambda item: item["episode_id"]),
            "documents.jsonl": documents,
        }
        for name, values in outputs.items(): _write_jsonl(temporary / name, values)
        (temporary / "excluded_ticket_ids.json").write_text(json.dumps(exclusions, indent=2) + "\n", encoding="utf-8")
        (temporary / "audit.json").write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        normalizer_hash = _sha256_bytes(Path(__file__).resolve().read_bytes())
        hashed_names = [*outputs, "excluded_ticket_ids.json", "audit.json"]
        manifest = {
            "schema_version": "jira-corpus-manifest-v1",
            "status": "candidate_not_formally_frozen",
            "snapshot_id": source_manifest["snapshot_id"],
            "source_format": "jira_api_snapshot_v1",
            "source_snapshot_content_sha256": source_manifest["snapshot_content_sha256"],
            "source_manifest_sha256": _sha256_bytes((snapshot_dir / "manifest.json").read_bytes()),
            "normalizer_version": NORMALIZER_VERSION,
            "normalizer_script_sha256": normalizer_hash,
            "normalizer_repository_revision": normalizer_repository_revision,
            "redaction_rule_version": REDACTION_RULE_VERSION,
            "document_filter_rule_version": DOCUMENT_FILTER_RULE_VERSION,
            "pairing_rule_version": PAIRING_RULE_VERSION,
            "timezone_status": "api_offsets_verified",
            "cutoff_at": cutoff_at,
            "exclusion_list_sha256": _sha256_text(_canonical_json(exclusions)),
            "trusted_bot_author_sha256s": trusted_bots,
            "bot_author_trust_status": bot_author_trust_status,
            "structured_identity_values_retained": False,
            "free_text_identity_redaction_status": "heuristic_and_operator_lexicon_manual_review_required",
            "identity_lexicon_sha256": _sha256_text(_canonical_json(sorted(identity for identity, _ in identity_replacements))),
            "additional_identity_source_sha256": additional_hash,
            "attachment_urls_retained": False,
            "record_counts": {"tickets":len(tickets),"events":len(events),"attachments":len(attachments),"changelogs":len(normalized_changes),"runs":len(runs),"episodes":len(episodes),"primary_episodes":audit["primary_episode_count"],"documents":len(documents)},
            "file_sha256": {name:_sha256_bytes((temporary/name).read_bytes()) for name in hashed_names},
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        combined = "".join((temporary/name).read_text(encoding="utf-8") for name in [*outputs, "audit.json", "manifest.json"])
        if _EMAIL_RE.search(combined): raise JiraApiNormalizerError("sanitized outputs still contain an email address")
        for account in account_values:
            if account and account in combined: raise JiraApiNormalizerError("sanitized outputs still contain an account ID")
        residual_identity = _identity_fingerprint_in_text(combined, identity_replacements)
        if residual_identity:
            raise JiraApiNormalizerError(
                "sanitized outputs still contain identity fingerprint "
                + residual_identity
            )
        for path in temporary.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        temporary.rename(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"manifest": manifest, "audit": audit}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize a private Jira API snapshot into a sanitized RAG corpus.")
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--excluded-ticket-ids", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cutoff-at", required=True)
    parser.add_argument("--bot-author-sha256", action="append", required=True)
    parser.add_argument("--bot-author-trust-status", choices=("verified","snapshot_candidate_unverified"), default="snapshot_candidate_unverified")
    parser.add_argument("--additional-identities", type=Path)
    parser.add_argument("--normalizer-repository-revision")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = normalize_api_snapshot(
        snapshot_dir=args.snapshot_dir,
        exclusions_path=args.excluded_ticket_ids,
        output_dir=args.output_dir,
        cutoff_at=args.cutoff_at,
        bot_author_sha256s=args.bot_author_sha256,
        bot_author_trust_status=args.bot_author_trust_status,
        additional_identities_path=args.additional_identities,
        normalizer_repository_revision=args.normalizer_repository_revision,
    )
    print(_canonical_json({"snapshot_id":result["manifest"]["snapshot_id"],"document_count":result["audit"]["document_count"],"primary_episode_count":result["audit"]["primary_episode_count"],"output_dir":str(args.output_dir)}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JiraCsvNormalizerError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2) from exc
