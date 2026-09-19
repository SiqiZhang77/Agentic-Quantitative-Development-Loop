#!/usr/bin/env python3
"""Export an immutable, read-only Jira Cloud snapshot for Experiment 2.

This exporter is intentionally separate from the production Jira pollers.  It
uses a strict method/endpoint allowlist, explicitly paginates issues, comments,
and changelogs, fetches attachment metadata without downloading content, and
writes both canonical raw response pages and convenient JSONL record views.

The raw output contains personal data and must stay outside Git.  The record
views hash structured Jira account IDs, but their free text is not yet fully
redacted; they are an intermediate source for the sanitized RAG normalizer, not
documents that may be sent directly to an agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


EXPORTER_VERSION = "1.0.1"
SNAPSHOT_SCHEMA_VERSION = "jira-api-snapshot-v1"
ISSUE_RECORD_SCHEMA_VERSION = "jira-api-issue-record-v1"
COMMENT_RECORD_SCHEMA_VERSION = "jira-api-comment-record-v1"
CHANGELOG_RECORD_SCHEMA_VERSION = "jira-api-changelog-record-v1"
ATTACHMENT_RECORD_SCHEMA_VERSION = "jira-api-attachment-record-v1"

DEFAULT_FIELDS = (
    "summary",
    "description",
    "issuetype",
    "status",
    "created",
    "updated",
    "resolution",
    "resolutiondate",
    "labels",
    "components",
    "parent",
    "attachment",
)
RETRYABLE_STATUS_CODES = {429, 502, 503, 504}
SAFE_RESPONSE_HEADERS = (
    "Retry-After",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "X-RateLimit-NearLimit",
    "RateLimit-Reason",
)
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class JiraApiExportError(RuntimeError):
    """Raised when a Jira snapshot cannot be proven complete."""


RequestFn = Callable[..., Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise JiraApiExportError(f"{label} must be a timezone-aware timestamp")
    normalized = value.strip().replace("Z", "+00:00")
    normalized = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", normalized)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise JiraApiExportError(
            f"{label} must be an ISO-8601 timestamp: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise JiraApiExportError(f"{label} must include a timezone")
    return parsed


def _is_after_cutoff(value: Any, cutoff: datetime) -> bool | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc) > cutoff
        except (OSError, OverflowError, ValueError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _parse_timestamp(value, "Jira timestamp") > cutoff
    except JiraApiExportError:
        return None


def _account_id_hash(actor: Any) -> str | None:
    if not isinstance(actor, Mapping):
        return None
    account_id = actor.get("accountId")
    if not isinstance(account_id, str) or not account_id:
        return None
    return _sha256_text(account_id)


def _adf_to_text(node: Any) -> str:
    """Render the useful text content of a Jira ADF value deterministically."""

    def join(parts: Iterable[str], separator: str) -> str:
        return separator.join(part for part in parts if part)

    def render(current: Any) -> str:
        if isinstance(current, str):
            return current
        if isinstance(current, list):
            return join((render(child) for child in current), "\n")
        if not isinstance(current, Mapping):
            return ""

        node_type = current.get("type")
        if node_type == "text":
            return str(current.get("text") or "")
        if node_type == "hardBreak":
            return "\n"
        if node_type == "mention":
            attrs = current.get("attrs") or {}
            if isinstance(attrs, Mapping):
                return str(attrs.get("text") or attrs.get("displayName") or "")
            return ""
        if node_type in {"emoji", "status"}:
            attrs = current.get("attrs") or {}
            if isinstance(attrs, Mapping):
                return str(attrs.get("text") or attrs.get("shortName") or "")
            return ""
        if node_type in {"inlineCard", "blockCard", "embedCard"}:
            attrs = current.get("attrs") or {}
            if isinstance(attrs, Mapping):
                return str(attrs.get("url") or "")
            return ""

        content = current.get("content") or []
        if not isinstance(content, list):
            content = []
        if node_type == "paragraph":
            return join((render(child) for child in content), "")
        if node_type == "codeBlock":
            return join((render(child) for child in content), "")
        if node_type in {
            "doc",
            "heading",
            "blockquote",
            "panel",
            "expand",
            "nestedExpand",
            "bodiedExtension",
            "extension",
            "bulletList",
            "orderedList",
            "taskList",
            "listItem",
            "taskItem",
            "table",
            "tableRow",
            "tableCell",
            "tableHeader",
        }:
            return join((render(child) for child in content), "\n")
        if "text" in current and not content:
            return str(current.get("text") or "")
        return join((render(child) for child in content), "")

    lines = [line.rstrip() for line in render(node).splitlines()]
    normalized: list[str] = []
    for line in lines:
        if line or (normalized and normalized[-1]):
            normalized.append(line)
    while normalized and not normalized[-1]:
        normalized.pop()
    return "\n".join(normalized)


def _write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    encoded = _pretty_json_bytes(value)
    path.write_bytes(encoded)
    path.chmod(0o600)
    return _sha256_bytes(encoded)


def _write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    encoded = "".join(_canonical_json(value) + "\n" for value in values).encode(
        "utf-8"
    )
    path.write_bytes(encoded)
    path.chmod(0o600)
    return _sha256_bytes(encoded)


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _response_header(response: Any, name: str) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value)
    return None


def _safe_response_headers(response: Any) -> dict[str, str]:
    values = {}
    for name in SAFE_RESPONSE_HEADERS:
        value = _response_header(response, name)
        if value is not None:
            values[name] = value
    return values


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception as exc:
        raise JiraApiExportError("Jira returned a non-JSON response") from exc


@dataclass
class ReadOnlyJiraSession:
    """Rate-limit-aware Jira transport with a hard read-only allowlist."""

    request_fn: RequestFn
    root: Path
    timeout_seconds: int = 60
    max_retries: int = 4
    sleep_fn: Callable[[float], None] = time.sleep
    random_fn: Callable[[], float] = random.random

    def __post_init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.retry_count = 0
        self.endpoint_counts: Counter[str] = Counter()

    @staticmethod
    def _endpoint_kind(method: str, endpoint: str) -> str:
        normalized_method = method.upper()
        allowed_exact = {
            ("GET", "/rest/api/3/serverInfo"): "server_info",
            ("GET", "/rest/api/3/myself"): "myself",
            ("POST", "/rest/api/3/search/jql"): "issue_search",
            ("POST", "/rest/api/3/changelog/bulkfetch"): "changelog_bulkfetch",
        }
        exact = allowed_exact.get((normalized_method, endpoint))
        if exact:
            return exact
        if normalized_method == "GET" and re.fullmatch(
            r"/rest/api/3/issue/[A-Z][A-Z0-9_]*-\d+/comment", endpoint
        ):
            return "issue_comments"
        if normalized_method == "GET" and re.fullmatch(
            r"/rest/api/3/attachment/\d+", endpoint
        ):
            return "attachment_metadata"
        raise JiraApiExportError(
            f"read-only exporter refuses non-allowlisted request: "
            f"{normalized_method} {endpoint}"
        )

    def get_json(
        self,
        endpoint: str,
        *,
        raw_relative_path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._request_json(
            "GET",
            endpoint,
            raw_relative_path=raw_relative_path,
            params=params,
        )

    def post_query_json(
        self,
        endpoint: str,
        *,
        raw_relative_path: str,
        body: dict[str, Any],
    ) -> Any:
        return self._request_json(
            "POST",
            endpoint,
            raw_relative_path=raw_relative_path,
            body=body,
        )

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        raw_relative_path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        endpoint_kind = self._endpoint_kind(method, endpoint)
        relative_path = Path(raw_relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise JiraApiExportError("raw response path may not escape output root")

        attempts = 0
        while True:
            kwargs: dict[str, Any] = {
                "headers": {"Accept": "application/json"},
                "timeout": self.timeout_seconds,
            }
            if params:
                kwargs["params"] = params
            if body is not None:
                kwargs["json"] = body
                kwargs["headers"]["Content-Type"] = "application/json"

            response = self.request_fn(method, endpoint, **kwargs)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code not in RETRYABLE_STATUS_CODES:
                break
            if attempts >= self.max_retries:
                raise JiraApiExportError(
                    f"Jira request exhausted retries after HTTP {status_code}: "
                    f"{method} {endpoint}"
                )

            retry_after = _response_header(response, "Retry-After")
            if retry_after is not None:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = float(2**attempts)
            else:
                delay = float(2**attempts)
            delay *= 0.7 + (0.6 * self.random_fn())
            self.sleep_fn(delay)
            attempts += 1
            self.retry_count += 1

        if status_code < 200 or status_code >= 300:
            try:
                response.raise_for_status()
            except Exception as exc:
                raise JiraApiExportError(
                    f"Jira request failed with HTTP {status_code}: {method} {endpoint}"
                ) from exc
            raise JiraApiExportError(
                f"Jira request failed with HTTP {status_code}: {method} {endpoint}"
            )

        data = _response_json(response)
        raw_path = self.root / raw_relative_path
        raw_sha256 = _write_json(raw_path, data)
        entry = {
            "sequence": len(self.requests) + 1,
            "method": method.upper(),
            "endpoint": endpoint,
            "endpoint_kind": endpoint_kind,
            "params": params or {},
            "body": body,
            "status_code": status_code,
            "response_headers": _safe_response_headers(response),
            "raw_file": raw_relative_path,
            "raw_sha256": raw_sha256,
            "attempt_count": attempts + 1,
        }
        self.requests.append(entry)
        self.endpoint_counts[endpoint_kind] += 1
        return data


def _issue_record(issue: dict[str, Any], cutoff: datetime) -> dict[str, Any]:
    fields = issue.get("fields") or {}
    if not isinstance(fields, Mapping):
        raise JiraApiExportError("Jira issue fields must be an object")
    issue_key = str(issue.get("key") or "")
    issue_id = str(issue.get("id") or "")
    if not _ISSUE_KEY_RE.fullmatch(issue_key) or not issue_id:
        raise JiraApiExportError("Jira search returned an invalid issue identity")

    attachments = fields.get("attachment") or []
    attachment_ids = []
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, Mapping) and attachment.get("id") is not None:
                attachment_ids.append(str(attachment["id"]))

    issue_type = fields.get("issuetype") or {}
    status = fields.get("status") or {}
    resolution = fields.get("resolution") or None
    parent = fields.get("parent") or None
    description = fields.get("description")
    updated = fields.get("updated")
    return {
        "schema_version": ISSUE_RECORD_SCHEMA_VERSION,
        "issue_id": issue_id,
        "ticket_key": issue_key,
        "summary": fields.get("summary"),
        "description_adf": description,
        "description_text": _adf_to_text(description),
        "issue_type": {
            "id": issue_type.get("id") if isinstance(issue_type, Mapping) else None,
            "name": issue_type.get("name") if isinstance(issue_type, Mapping) else None,
        },
        "status": {
            "id": status.get("id") if isinstance(status, Mapping) else None,
            "name": status.get("name") if isinstance(status, Mapping) else None,
        },
        "created": fields.get("created"),
        "updated": updated,
        "resolution": (
            {
                "id": resolution.get("id"),
                "name": resolution.get("name"),
            }
            if isinstance(resolution, Mapping)
            else None
        ),
        "resolution_date": fields.get("resolutiondate"),
        "labels": fields.get("labels") if isinstance(fields.get("labels"), list) else [],
        "components": [
            {"id": item.get("id"), "name": item.get("name")}
            for item in (fields.get("components") or [])
            if isinstance(item, Mapping)
        ],
        "parent": (
            {"id": parent.get("id"), "key": parent.get("key")}
            if isinstance(parent, Mapping)
            else None
        ),
        "attachment_ids": sorted(set(attachment_ids)),
        "current_text_after_cutoff": _is_after_cutoff(updated, cutoff),
    }


def _comment_record(
    issue_key: str,
    comment: dict[str, Any],
    cutoff: datetime,
) -> dict[str, Any]:
    comment_id = str(comment.get("id") or "")
    if not comment_id:
        raise JiraApiExportError(f"{issue_key} comment is missing an ID")
    body = comment.get("body")
    updated = comment.get("updated") or comment.get("created")
    return {
        "schema_version": COMMENT_RECORD_SCHEMA_VERSION,
        "ticket_key": issue_key,
        "comment_id": comment_id,
        "created": comment.get("created"),
        "updated": comment.get("updated"),
        "author_account_id_sha256": _account_id_hash(comment.get("author")),
        "update_author_account_id_sha256": _account_id_hash(
            comment.get("updateAuthor")
        ),
        "body_adf": body,
        "body_text": _adf_to_text(body),
        "visibility": comment.get("visibility"),
        "current_body_after_cutoff": _is_after_cutoff(updated, cutoff),
    }


def _changelog_records(
    issue_key_by_id: dict[str, str],
    issue_change_log: dict[str, Any],
    cutoff: datetime,
) -> list[dict[str, Any]]:
    issue_id = str(issue_change_log.get("issueId") or "")
    issue_key = issue_key_by_id.get(issue_id)
    if not issue_key:
        raise JiraApiExportError(
            f"bulk changelog returned unknown Jira issue ID: {issue_id or '<empty>'}"
        )
    histories = issue_change_log.get("changeHistories") or []
    if not isinstance(histories, list):
        raise JiraApiExportError("bulk changelog changeHistories must be an array")
    records = []
    for history in histories:
        if not isinstance(history, Mapping):
            raise JiraApiExportError("bulk changelog history must be an object")
        history_id = str(history.get("id") or "")
        if not history_id:
            raise JiraApiExportError("bulk changelog history is missing an ID")
        created = history.get("created")
        records.append(
            {
                "schema_version": CHANGELOG_RECORD_SCHEMA_VERSION,
                "ticket_key": issue_key,
                "issue_id": issue_id,
                "history_id": history_id,
                "created": created,
                "author_account_id_sha256": _account_id_hash(history.get("author")),
                "items": history.get("items") if isinstance(history.get("items"), list) else [],
                "after_cutoff": _is_after_cutoff(created, cutoff),
            }
        )
    return records


def _attachment_record(
    issue_key: str,
    attachment: dict[str, Any],
    cutoff: datetime,
) -> dict[str, Any]:
    attachment_id = str(attachment.get("id") or "")
    if not attachment_id:
        raise JiraApiExportError(f"{issue_key} attachment is missing an ID")
    created = attachment.get("created")
    return {
        "schema_version": ATTACHMENT_RECORD_SCHEMA_VERSION,
        "ticket_key": issue_key,
        "attachment_id": attachment_id,
        "filename": attachment.get("filename"),
        "created": created,
        "mime_type": attachment.get("mimeType"),
        "size": attachment.get("size"),
        "author_account_id_sha256": _account_id_hash(attachment.get("author")),
        "after_cutoff": _is_after_cutoff(created, cutoff),
        "content_downloaded": False,
    }


def _fetch_issue_search(
    session: ReadOnlyJiraSession,
    *,
    jql: str,
    fields: list[str],
    max_results: int,
    raw_prefix: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen_issue_ids: set[str] = set()
    seen_issue_keys: set[str] = set()
    seen_tokens: set[str] = set()
    token: str | None = None
    page_number = 0

    while True:
        page_number += 1
        body: dict[str, Any] = {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results,
        }
        if token:
            body["nextPageToken"] = token
        page = session.post_query_json(
            "/rest/api/3/search/jql",
            raw_relative_path=f"{raw_prefix}/page-{page_number:05d}.json",
            body=body,
        )
        if not isinstance(page, Mapping):
            raise JiraApiExportError("enhanced Jira search response must be an object")
        page_issues = page.get("issues") or []
        if not isinstance(page_issues, list):
            raise JiraApiExportError("enhanced Jira search issues must be an array")
        for issue in page_issues:
            if not isinstance(issue, dict):
                raise JiraApiExportError("enhanced Jira search issue must be an object")
            issue_id = str(issue.get("id") or "")
            issue_key = str(issue.get("key") or "")
            if not issue_id or not _ISSUE_KEY_RE.fullmatch(issue_key):
                raise JiraApiExportError("enhanced Jira search returned invalid issue identity")
            if issue_id in seen_issue_ids or issue_key in seen_issue_keys:
                raise JiraApiExportError(
                    f"enhanced Jira search returned a duplicate issue: {issue_key}"
                )
            seen_issue_ids.add(issue_id)
            seen_issue_keys.add(issue_key)
            issues.append(issue)

        if page.get("isLast") is True:
            break
        next_token = page.get("nextPageToken")
        if not isinstance(next_token, str) or not next_token:
            raise JiraApiExportError(
                "enhanced Jira search is not last but returned no nextPageToken"
            )
        if next_token in seen_tokens:
            raise JiraApiExportError("enhanced Jira search repeated nextPageToken")
        seen_tokens.add(next_token)
        token = next_token
    return issues


def _fetch_comments_once(
    session: ReadOnlyJiraSession,
    *,
    issue_key: str,
    max_results: int,
    raw_prefix: str,
) -> tuple[list[dict[str, Any]], set[int]]:
    start_at = 0
    page_number = 0
    observed_totals: set[int] = set()
    comments_by_id: dict[str, dict[str, Any]] = {}

    while True:
        page_number += 1
        page = session.get_json(
            f"/rest/api/3/issue/{issue_key}/comment",
            raw_relative_path=f"{raw_prefix}/page-{page_number:05d}.json",
            params={"startAt": start_at, "maxResults": max_results},
        )
        if not isinstance(page, Mapping):
            raise JiraApiExportError(f"{issue_key} comment page must be an object")
        page_start = page.get("startAt")
        total = page.get("total")
        comments = page.get("comments") or []
        if not isinstance(page_start, int) or not isinstance(total, int):
            raise JiraApiExportError(
                f"{issue_key} comment page lacks integer startAt/total"
            )
        if page_start != start_at:
            raise JiraApiExportError(
                f"{issue_key} comment page startAt changed unexpectedly"
            )
        if not isinstance(comments, list):
            raise JiraApiExportError(f"{issue_key} comments must be an array")
        observed_totals.add(total)
        for comment in comments:
            if not isinstance(comment, dict) or not comment.get("id"):
                raise JiraApiExportError(f"{issue_key} comment has no stable ID")
            comment_id = str(comment["id"])
            previous = comments_by_id.get(comment_id)
            if previous is not None and previous != comment:
                raise JiraApiExportError(
                    f"{issue_key} comment {comment_id} changed during pagination"
                )
            comments_by_id[comment_id] = comment

        next_start = page_start + len(comments)
        if next_start >= total:
            break
        if not comments:
            raise JiraApiExportError(
                f"{issue_key} returned an empty non-final comment page"
            )
        start_at = next_start

    comments_sorted = sorted(
        comments_by_id.values(),
        key=lambda item: (str(item.get("created") or ""), str(item.get("id") or "")),
    )
    return comments_sorted, observed_totals


def _fetch_comments(
    session: ReadOnlyJiraSession,
    *,
    issue_key: str,
    max_results: int,
    consistency_retries: int,
    quiet_seconds: float,
) -> tuple[list[dict[str, Any]], bool]:
    concurrent_update_detected = False
    for attempt in range(consistency_retries + 1):
        raw_prefix = f"raw/comments/{issue_key}/attempt-{attempt + 1:02d}"
        comments, totals = _fetch_comments_once(
            session,
            issue_key=issue_key,
            max_results=max_results,
            raw_prefix=raw_prefix,
        )
        if len(totals) <= 1:
            return comments, concurrent_update_detected
        concurrent_update_detected = True
        if attempt >= consistency_retries:
            break
        session.sleep_fn(quiet_seconds)
    raise JiraApiExportError(
        f"{issue_key} comment total changed during every consistency attempt"
    )


def _fetch_changelogs(
    session: ReadOnlyJiraSession,
    *,
    issue_keys: list[str],
    issue_key_by_id: dict[str, str],
    max_results: int,
    cutoff: datetime,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_history_keys: set[tuple[str, str]] = set()
    for batch_number, batch in enumerate(_chunked(issue_keys, 1000), start=1):
        token: str | None = None
        seen_tokens: set[str] = set()
        page_number = 0
        while True:
            page_number += 1
            body: dict[str, Any] = {
                "issueIdsOrKeys": batch,
                "maxResults": max_results,
            }
            if token:
                body["nextPageToken"] = token
            page = session.post_query_json(
                "/rest/api/3/changelog/bulkfetch",
                raw_relative_path=(
                    f"raw/changelogs/batch-{batch_number:04d}/"
                    f"page-{page_number:05d}.json"
                ),
                body=body,
            )
            if not isinstance(page, Mapping):
                raise JiraApiExportError("bulk changelog response must be an object")
            issue_logs = page.get("issueChangeLogs") or []
            if not isinstance(issue_logs, list):
                raise JiraApiExportError("bulk changelog issueChangeLogs must be an array")
            for issue_log in issue_logs:
                if not isinstance(issue_log, dict):
                    raise JiraApiExportError("bulk changelog issue log must be an object")
                for record in _changelog_records(issue_key_by_id, issue_log, cutoff):
                    unique = (record["ticket_key"], record["history_id"])
                    if unique in seen_history_keys:
                        raise JiraApiExportError(
                            f"bulk changelog repeated history {unique[1]} for {unique[0]}"
                        )
                    seen_history_keys.add(unique)
                    records.append(record)

            next_token = page.get("nextPageToken")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise JiraApiExportError("bulk changelog repeated invalid nextPageToken")
            seen_tokens.add(next_token)
            token = next_token
    return sorted(
        records,
        key=lambda item: (str(item.get("created") or ""), item["ticket_key"], item["history_id"]),
    )


def _fetch_attachments(
    session: ReadOnlyJiraSession,
    *,
    issues: list[dict[str, Any]],
    cutoff: datetime,
) -> list[dict[str, Any]]:
    discovered: dict[str, str] = {}
    for issue in issues:
        issue_key = str(issue["key"])
        fields = issue.get("fields") or {}
        attachments = fields.get("attachment") or [] if isinstance(fields, Mapping) else []
        if not isinstance(attachments, list):
            raise JiraApiExportError(f"{issue_key} attachment field must be an array")
        for attachment in attachments:
            if not isinstance(attachment, Mapping) or attachment.get("id") is None:
                raise JiraApiExportError(f"{issue_key} contains attachment without an ID")
            attachment_id = str(attachment["id"])
            if not attachment_id.isdigit():
                raise JiraApiExportError(
                    f"{issue_key} attachment ID is not numeric: {attachment_id!r}"
                )
            previous_issue = discovered.get(attachment_id)
            if previous_issue is not None and previous_issue != issue_key:
                raise JiraApiExportError(
                    f"attachment {attachment_id} appears on multiple issues"
                )
            discovered[attachment_id] = issue_key

    records = []
    for attachment_id, issue_key in sorted(
        discovered.items(), key=lambda item: int(item[0])
    ):
        metadata = session.get_json(
            f"/rest/api/3/attachment/{attachment_id}",
            raw_relative_path=f"raw/attachments/{attachment_id}.json",
        )
        if not isinstance(metadata, dict):
            raise JiraApiExportError(
                f"attachment {attachment_id} metadata must be an object"
            )
        if str(metadata.get("id") or "") != attachment_id:
            raise JiraApiExportError(
                f"attachment metadata ID mismatch for {attachment_id}"
            )
        records.append(_attachment_record(issue_key, metadata, cutoff))
    return records


def _verify_issue_snapshot(
    session: ReadOnlyJiraSession,
    *,
    jql: str,
    max_results: int,
    initial_issues: list[dict[str, Any]],
) -> None:
    verification = _fetch_issue_search(
        session,
        jql=jql,
        fields=["updated"],
        max_results=max_results,
        raw_prefix="raw/issue-verification",
    )
    initial = {
        str(issue["key"]): str((issue.get("fields") or {}).get("updated") or "")
        for issue in initial_issues
    }
    final = {
        str(issue["key"]): str((issue.get("fields") or {}).get("updated") or "")
        for issue in verification
    }
    if initial != final:
        changed = sorted(set(initial) ^ set(final))
        changed.extend(
            key for key in sorted(set(initial) & set(final)) if initial[key] != final[key]
        )
        preview = ", ".join(changed[:10])
        raise JiraApiExportError(
            "Jira issues changed while the snapshot was being exported"
            + (f": {preview}" if preview else "")
        )


def export_snapshot(
    *,
    request_fn: RequestFn,
    output_dir: Path,
    project_key: str,
    cutoff_at: str,
    jql: str | None = None,
    fields: Iterable[str] = DEFAULT_FIELDS,
    max_results: int = 100,
    timeout_seconds: int = 60,
    max_retries: int = 4,
    comment_consistency_retries: int = 1,
    comment_quiet_seconds: float = 2.0,
    include_changelog: bool = True,
    include_attachment_metadata: bool = True,
    verify_issues_after: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
    random_fn: Callable[[], float] = random.random,
) -> dict[str, Any]:
    """Export one immutable Jira API snapshot to an unused directory."""

    if not _PROJECT_KEY_RE.fullmatch(project_key):
        raise JiraApiExportError(f"invalid Jira project key: {project_key!r}")
    if not isinstance(max_results, int) or max_results < 1 or max_results > 1000:
        raise JiraApiExportError("max_results must be between 1 and 1000")
    if comment_consistency_retries < 0:
        raise JiraApiExportError("comment consistency retries may not be negative")
    if output_dir.exists():
        raise JiraApiExportError(f"refusing to overwrite output directory: {output_dir}")
    cutoff = _parse_timestamp(cutoff_at, "cutoff_at")
    field_list = [str(field).strip() for field in fields if str(field).strip()]
    if not field_list or "comment" in {field.casefold() for field in field_list}:
        raise JiraApiExportError(
            "fields must be non-empty and must not use embedded comment data"
        )
    selected_jql = jql or (
        f"project = {project_key} ORDER BY created ASC, key ASC"
    )
    if not selected_jql.strip():
        raise JiraApiExportError("JQL may not be empty")

    building = output_dir.with_name(output_dir.name + ".building")
    if building.exists():
        raise JiraApiExportError(f"temporary output directory exists: {building}")
    building.mkdir(parents=True, mode=0o700)
    building.chmod(0o700)
    started_at = _utc_now()

    session = ReadOnlyJiraSession(
        request_fn=request_fn,
        root=building,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
        random_fn=random_fn,
    )
    try:
        server_info = session.get_json(
            "/rest/api/3/serverInfo",
            raw_relative_path="raw/server_info.json",
        )
        myself = session.get_json(
            "/rest/api/3/myself",
            raw_relative_path="raw/myself.json",
        )
        if not isinstance(server_info, Mapping) or not isinstance(myself, Mapping):
            raise JiraApiExportError("serverInfo and myself responses must be objects")
        exporter_account_hash = _account_id_hash(myself)
        if exporter_account_hash is None or not _HEX64_RE.fullmatch(exporter_account_hash):
            raise JiraApiExportError("Jira myself response has no stable accountId")

        issues = _fetch_issue_search(
            session,
            jql=selected_jql,
            fields=field_list,
            max_results=max_results,
            raw_prefix="raw/issues",
        )
        issue_records = [_issue_record(issue, cutoff) for issue in issues]
        issue_key_by_id = {str(issue["id"]): str(issue["key"]) for issue in issues}
        issue_keys = [str(issue["key"]) for issue in issues]

        comments: list[dict[str, Any]] = []
        dirty_comment_tickets: list[str] = []
        for issue_key in issue_keys:
            issue_comments, dirty = _fetch_comments(
                session,
                issue_key=issue_key,
                max_results=max_results,
                consistency_retries=comment_consistency_retries,
                quiet_seconds=comment_quiet_seconds,
            )
            if dirty:
                dirty_comment_tickets.append(issue_key)
            comments.extend(
                _comment_record(issue_key, comment, cutoff)
                for comment in issue_comments
            )

        changelogs = (
            _fetch_changelogs(
                session,
                issue_keys=issue_keys,
                issue_key_by_id=issue_key_by_id,
                max_results=max_results,
                cutoff=cutoff,
            )
            if include_changelog and issue_keys
            else []
        )
        attachments = (
            _fetch_attachments(session, issues=issues, cutoff=cutoff)
            if include_attachment_metadata
            else []
        )
        if verify_issues_after:
            _verify_issue_snapshot(
                session,
                jql=selected_jql,
                max_results=max_results,
                initial_issues=issues,
            )

        _write_jsonl(building / "records/issues.jsonl", issue_records)
        _write_jsonl(building / "records/comments.jsonl", comments)
        _write_jsonl(building / "records/changelogs.jsonl", changelogs)
        _write_jsonl(building / "records/attachments.jsonl", attachments)
        _write_jsonl(building / "requests.jsonl", session.requests)

        payload_hashes = {
            str(path.relative_to(building)): _sha256_bytes(path.read_bytes())
            for path in sorted(building.rglob("*"))
            if path.is_file()
        }
        snapshot_content_sha256 = _sha256_text(_canonical_json(payload_hashes))
        snapshot_id = f"jira-api-{snapshot_content_sha256[:16]}"
        completed_at = _utc_now()
        audit = {
            "schema_version": "jira-api-export-audit-v1",
            "exporter_version": EXPORTER_VERSION,
            "status": "complete",
            "snapshot_id": snapshot_id,
            "snapshot_content_sha256": snapshot_content_sha256,
            "project_key": project_key,
            "jql": selected_jql,
            "fields": field_list,
            "cutoff_at": cutoff_at,
            "export_started_at": started_at,
            "export_completed_at": completed_at,
            "server_time": server_info.get("serverTime"),
            "server_timezone": server_info.get("serverTimeZone"),
            "exporter_timezone": myself.get("timeZone"),
            "exporter_account_id_sha256": exporter_account_hash,
            "issue_count": len(issue_records),
            "issue_current_text_after_cutoff_count": sum(
                item["current_text_after_cutoff"] is True for item in issue_records
            ),
            "comment_count": len(comments),
            "comment_current_body_after_cutoff_count": sum(
                item["current_body_after_cutoff"] is True for item in comments
            ),
            "changelog_history_count": len(changelogs),
            "changelog_after_cutoff_count": sum(
                item["after_cutoff"] is True for item in changelogs
            ),
            "attachment_metadata_count": len(attachments),
            "attachment_after_cutoff_count": sum(
                item["after_cutoff"] is True for item in attachments
            ),
            "request_count": len(session.requests),
            "retry_count": session.retry_count,
            "endpoint_request_counts": dict(sorted(session.endpoint_counts.items())),
            "comment_concurrent_update_tickets": sorted(dirty_comment_tickets),
            "issue_end_verification_enabled": verify_issues_after,
            "changelog_enabled": include_changelog,
            "attachment_metadata_enabled": include_attachment_metadata,
            "visibility_scope": "content_visible_to_exporter_account_only",
            "raw_contains_personal_data": True,
            "records_sanitization_status": (
                "intermediate_private_structured_account_ids_hashed_"
                "free_text_not_reviewed"
            ),
            "safe_for_direct_rag_ingestion": False,
            "attachment_content_downloaded": False,
        }
        _write_json(building / "audit.json", audit)

        all_hashes = {
            str(path.relative_to(building)): _sha256_bytes(path.read_bytes())
            for path in sorted(building.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        }
        manifest = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "exporter_version": EXPORTER_VERSION,
            "snapshot_id": snapshot_id,
            "snapshot_content_sha256": snapshot_content_sha256,
            "cutoff_at": cutoff_at,
            "generated_at": completed_at,
            "files": all_hashes,
            "raw_data_policy": "private_do_not_commit_or_send_to_model",
            "next_step": "normalize_and_redact_before_building_rag_index",
        }
        _write_json(building / "manifest.json", manifest)
        building.rename(output_dir)
        return {"manifest": manifest, "audit": audit}
    except Exception:
        shutil.rmtree(building, ignore_errors=True)
        raise


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a complete, immutable Jira API snapshot using only "
            "allowlisted read operations. Run in the Airflow environment so "
            "the existing jira_cloud Connection supplies credentials."
        )
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cutoff-at", required=True)
    parser.add_argument(
        "--project-key",
        help="defaults to the existing JIRA_PROJECT_KEY Airflow Variable",
    )
    parser.add_argument(
        "--jql",
        help="optional read-only JQL; defaults to the complete selected project",
    )
    parser.add_argument(
        "--field",
        action="append",
        dest="fields",
        help="repeat to override the default issue field list; do not add comment",
    )
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--comment-consistency-retries", type=int, default=1)
    parser.add_argument("--comment-quiet-seconds", type=float, default=2.0)
    parser.add_argument("--skip-changelog", action="store_true")
    parser.add_argument("--skip-attachment-metadata", action="store_true")
    parser.add_argument("--skip-end-verification", action="store_true")
    return parser.parse_args()


def _direct_jira_request(
    config: Mapping[str, Any],
    *,
    session_factory: Callable[[], Any] | None = None,
) -> RequestFn:
    """Build a Jira request function without re-entering Airflow's task SDK.

    ``get_jira_config`` already resolves and decrypts the configured Airflow
    Connection, including its metadata-database fallback.  Airflow 3's
    ``HttpHook`` performs a second Connection lookup through the task SDK; that
    lookup is unavailable when this standalone exporter runs from an SSH shell.
    Keep the resolved credentials only in this process and use a private HTTP
    session for the allowlisted requests instead.
    """

    base_url = str(config.get("base_url") or "").rstrip("/")
    email = str(config.get("email") or "")
    api_token = str(config.get("api_token") or "")
    missing = [
        name
        for name, value in {
            "base_url": base_url,
            "email": email,
            "api_token": api_token,
        }.items()
        if not value
    ]
    if missing:
        raise JiraApiExportError(
            "resolved Jira configuration is missing: " + ", ".join(missing)
        )

    if session_factory is None:
        try:
            import requests
        except ImportError as exc:  # pragma: no cover - Airflow image dependency
            raise JiraApiExportError(
                "the Airflow Python environment does not provide requests"
            ) from exc
        session_factory = requests.Session

    session = session_factory()
    session.auth = (email, api_token)

    def request(method: str, endpoint: str, **kwargs: Any):
        headers = kwargs.pop("headers", {})
        timeout = kwargs.pop("timeout", 30)
        url = f"{base_url}/{endpoint.lstrip('/')}"
        return session.request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            **kwargs,
        )

    return request


def _load_airflow_jira_request(
    project_key_override: str | None = None,
) -> tuple[RequestFn, str]:
    dags_dir = Path(__file__).resolve().parents[1] / "dags"
    if str(dags_dir) not in sys.path:
        sys.path.insert(0, str(dags_dir))

    # jira_quant_common validates JIRA_PROJECT_KEY while resolving the
    # Connection, so apply the explicit CLI override before importing/loading
    # that configuration.  The change is process-local.
    if project_key_override:
        os.environ["JIRA_PROJECT_KEY"] = project_key_override
    try:
        from jira_quant_common import get_jira_config
    except Exception as exc:
        raise JiraApiExportError(
            "could not load the existing Jira/Airflow connection helper; run "
            "this script in the Airflow Python environment"
        ) from exc
    config = get_jira_config()
    return _direct_jira_request(config), str(config["project_key"])


def main() -> int:
    args = _parse_args()
    request_fn, configured_project_key = _load_airflow_jira_request(
        args.project_key
    )
    project_key = args.project_key or configured_project_key
    result = export_snapshot(
        request_fn=request_fn,
        output_dir=args.output_dir,
        project_key=project_key,
        cutoff_at=args.cutoff_at,
        jql=args.jql,
        fields=args.fields or DEFAULT_FIELDS,
        max_results=args.max_results,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        comment_consistency_retries=args.comment_consistency_retries,
        comment_quiet_seconds=args.comment_quiet_seconds,
        include_changelog=not args.skip_changelog,
        include_attachment_metadata=not args.skip_attachment_metadata,
        verify_issues_after=not args.skip_end_verification,
    )
    audit = result["audit"]
    print(
        _canonical_json(
            {
                "snapshot_id": result["manifest"]["snapshot_id"],
                "snapshot_content_sha256": result["manifest"][
                    "snapshot_content_sha256"
                ],
                "issue_count": audit["issue_count"],
                "comment_count": audit["comment_count"],
                "changelog_history_count": audit["changelog_history_count"],
                "attachment_metadata_count": audit["attachment_metadata_count"],
                "output_dir": str(args.output_dir),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JiraApiExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
