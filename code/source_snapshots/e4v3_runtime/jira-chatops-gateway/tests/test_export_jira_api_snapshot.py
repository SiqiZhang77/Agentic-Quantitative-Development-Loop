from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from scripts.export_jira_api_snapshot import (
    JiraApiExportError,
    ReadOnlyJiraSession,
    _direct_jira_request,
    _fetch_comments,
    _is_after_cutoff,
    _load_airflow_jira_request,
    export_snapshot,
)


def _adf(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ],
    }


def _issue(
    issue_id: str,
    key: str,
    *,
    updated: str,
    attachment_id: str | None = None,
) -> dict[str, Any]:
    attachments = []
    if attachment_id:
        attachments.append({"id": attachment_id, "filename": "run.log"})
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "summary": f"Summary {key}",
            "description": _adf(f"Description {key}"),
            "issuetype": {"id": "10001", "name": "Task"},
            "status": {"id": "3", "name": "Done"},
            "created": "2026-07-01T09:00:00.000+0000",
            "updated": updated,
            "resolution": {"id": "1", "name": "Done"},
            "resolutiondate": "2026-07-01T10:00:00.000+0000",
            "labels": ["experiment"],
            "components": [],
            "parent": None,
            "attachment": attachments,
        },
    }


def _comment(comment_id: str, text: str, account_id: str) -> dict[str, Any]:
    return {
        "id": comment_id,
        "created": f"2026-07-01T09:0{comment_id}.000+0000",
        "updated": f"2026-07-01T09:0{comment_id}.000+0000",
        "author": {"accountId": account_id, "displayName": "Private Person"},
        "updateAuthor": {"accountId": account_id, "displayName": "Private Person"},
        "body": _adf(text),
    }


class FakeResponse:
    def __init__(
        self,
        data: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if not 200 <= self.status_code < 300:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeDirectSession:
    def __init__(self) -> None:
        self.auth: tuple[str, str] | None = None
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return FakeResponse({"ok": True})


class CompleteFakeJira:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.updated = "2026-07-01T10:00:00.000+0000"
        self.issue_one = _issue("101", "SCRUM-1", updated=self.updated, attachment_id="501")
        self.issue_two = _issue("102", "SCRUM-2", updated=self.updated)

    def __call__(self, method: str, endpoint: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, endpoint, kwargs))
        if endpoint == "/rest/api/3/serverInfo":
            return FakeResponse(
                {
                    "serverTime": "2026-08-10T08:00:00.000+0000",
                    "serverTimeZone": "Europe/London",
                    "version": "1001.0.0",
                }
            )
        if endpoint == "/rest/api/3/myself":
            return FakeResponse(
                {
                    "accountId": "bot-account-private",
                    "displayName": "Quant Bot",
                    "emailAddress": "bot@example.invalid",
                    "timeZone": "Europe/London",
                }
            )
        if endpoint == "/rest/api/3/search/jql":
            body = kwargs["json"]
            token = body.get("nextPageToken")
            verification = body.get("fields") == ["updated"]
            first = self.issue_one if not verification else {
                "id": "101",
                "key": "SCRUM-1",
                "fields": {"updated": self.updated},
            }
            second = self.issue_two if not verification else {
                "id": "102",
                "key": "SCRUM-2",
                "fields": {"updated": self.updated},
            }
            if token is None:
                return FakeResponse(
                    {"issues": [first], "isLast": False, "nextPageToken": "next-2"}
                )
            assert token == "next-2"
            return FakeResponse({"issues": [second], "isLast": True})
        if endpoint == "/rest/api/3/issue/SCRUM-1/comment":
            start = kwargs["params"]["startAt"]
            if start == 0:
                return FakeResponse(
                    {
                        "startAt": 0,
                        "maxResults": 1,
                        "total": 2,
                        "comments": [_comment("1", "/quant build parser", "human-one")],
                    }
                )
            assert start == 1
            return FakeResponse(
                {
                    "startAt": 1,
                    "maxResults": 1,
                    "total": 2,
                    "comments": [_comment("2", "[quant-loop-bot] done", "bot-account-private")],
                }
            )
        if endpoint == "/rest/api/3/issue/SCRUM-2/comment":
            return FakeResponse(
                {"startAt": 0, "maxResults": 100, "total": 0, "comments": []}
            )
        if endpoint == "/rest/api/3/changelog/bulkfetch":
            token = kwargs["json"].get("nextPageToken")
            if token is None:
                return FakeResponse(
                    {
                        "issueChangeLogs": [
                            {
                                "issueId": "101",
                                "changeHistories": [
                                    {
                                        "id": "9001",
                                        "created": "2026-07-01T09:30:00.000+0000",
                                        "author": {"accountId": "human-one"},
                                        "items": [
                                            {
                                                "field": "status",
                                                "fromString": "To Do",
                                                "toString": "Done",
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                        "nextPageToken": "changes-2",
                    }
                )
            assert token == "changes-2"
            return FakeResponse(
                {
                    "issueChangeLogs": [
                        {
                            "issueId": "102",
                            "changeHistories": [
                                {
                                    "id": "9002",
                                    "created": "2026-07-01T09:40:00.000+0000",
                                    "author": {"accountId": "human-two"},
                                    "items": [],
                                }
                            ],
                        }
                    ]
                }
            )
        if endpoint == "/rest/api/3/attachment/501":
            return FakeResponse(
                {
                    "id": "501",
                    "filename": "run.log",
                    "created": "2026-07-01T09:45:00.000+0000",
                    "mimeType": "text/plain",
                    "size": 200,
                    "author": {"accountId": "human-one"},
                    "content": "https://jira.invalid/attachment/content/501",
                }
            )
        raise AssertionError(f"unexpected request: {method} {endpoint} {kwargs}")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_direct_request_reuses_resolved_connection_without_airflow_hook() -> None:
    session = FakeDirectSession()
    request = _direct_jira_request(
        {
            "base_url": "https://jira.invalid/base/",
            "email": "service@example.invalid",
            "api_token": "private-token",
        },
        session_factory=lambda: session,
    )

    response = request(
        "GET",
        "/rest/api/3/serverInfo",
        headers={"Accept": "application/json"},
        timeout=9,
    )

    assert response.json() == {"ok": True}
    assert session.auth == ("service@example.invalid", "private-token")
    assert session.calls == [
        (
            "GET",
            "https://jira.invalid/base/rest/api/3/serverInfo",
            {"headers": {"Accept": "application/json"}, "timeout": 9},
        )
    ]


def test_loader_applies_cli_project_before_airflow_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str | None] = {}
    fake_module = types.ModuleType("jira_quant_common")

    def get_jira_config() -> dict[str, str]:
        observed["project"] = os.environ.get("JIRA_PROJECT_KEY")
        return {
            "base_url": "https://jira.invalid",
            "email": "service@example.invalid",
            "api_token": "private-token",
            "project_key": observed["project"] or "",
        }

    fake_module.get_jira_config = get_jira_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jira_quant_common", fake_module)
    monkeypatch.delenv("JIRA_PROJECT_KEY", raising=False)

    _, project_key = _load_airflow_jira_request("SCRUM")

    assert observed["project"] == "SCRUM"
    assert project_key == "SCRUM"


def test_cutoff_check_supports_jira_changelog_epoch_milliseconds() -> None:
    from datetime import datetime, timezone

    cutoff = datetime(2026, 7, 2, tzinfo=timezone.utc)
    assert _is_after_cutoff(1783076400000, cutoff) is True
    assert _is_after_cutoff(1782907200000, cutoff) is False


def test_exports_complete_paginated_snapshot_with_safe_records(tmp_path: Path) -> None:
    fake = CompleteFakeJira()
    output = tmp_path / "jira-api-snapshot"
    result = export_snapshot(
        request_fn=fake,
        output_dir=output,
        project_key="SCRUM",
        cutoff_at="2026-08-09T00:00:00Z",
        sleep_fn=lambda _: None,
        random_fn=lambda: 0.5,
    )

    issues = _jsonl(output / "records/issues.jsonl")
    comments = _jsonl(output / "records/comments.jsonl")
    changelogs = _jsonl(output / "records/changelogs.jsonl")
    attachments = _jsonl(output / "records/attachments.jsonl")
    audit = json.loads((output / "audit.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert [item["ticket_key"] for item in issues] == ["SCRUM-1", "SCRUM-2"]
    assert [item["comment_id"] for item in comments] == ["1", "2"]
    assert comments[0]["body_text"] == "/quant build parser"
    assert comments[0]["author_account_id_sha256"] == hashlib.sha256(
        b"human-one"
    ).hexdigest()
    assert [item["history_id"] for item in changelogs] == ["9001", "9002"]
    assert attachments == [
        {
            "after_cutoff": False,
            "attachment_id": "501",
            "author_account_id_sha256": hashlib.sha256(b"human-one").hexdigest(),
            "content_downloaded": False,
            "created": "2026-07-01T09:45:00.000+0000",
            "filename": "run.log",
            "mime_type": "text/plain",
            "schema_version": "jira-api-attachment-record-v1",
            "size": 200,
            "ticket_key": "SCRUM-1",
        }
    ]
    assert audit["issue_count"] == 2
    assert audit["comment_count"] == 2
    assert audit["changelog_history_count"] == 2
    assert audit["attachment_metadata_count"] == 1
    assert audit["server_timezone"] == "Europe/London"
    assert audit["safe_for_direct_rag_ingestion"] is False
    assert result["manifest"] == manifest
    assert manifest["snapshot_id"].startswith("jira-api-")
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "raw/myself.json").stat().st_mode) == 0o600

    raw_myself = (output / "raw/myself.json").read_text(encoding="utf-8")
    safe_files = (output / "audit.json").read_text(encoding="utf-8") + (
        output / "manifest.json"
    ).read_text(encoding="utf-8")
    assert "bot-account-private" in raw_myself
    assert "bot-account-private" not in safe_files
    assert "bot@example.invalid" not in safe_files
    assert not any("/attachment/content/" in endpoint for _, endpoint, _ in fake.calls)
    assert {
        (method, endpoint)
        for method, endpoint, _ in fake.calls
        if method == "POST"
    } <= {
        ("POST", "/rest/api/3/search/jql"),
        ("POST", "/rest/api/3/changelog/bulkfetch"),
    }
    comment_starts = [
        kwargs["params"]["startAt"]
        for _, endpoint, kwargs in fake.calls
        if endpoint == "/rest/api/3/issue/SCRUM-1/comment"
    ]
    assert comment_starts == [0, 1]

    for relative_path, expected_hash in manifest["files"].items():
        assert hashlib.sha256((output / relative_path).read_bytes()).hexdigest() == expected_hash


def test_read_only_allowlist_refuses_comment_write(tmp_path: Path) -> None:
    calls = []

    def unexpected(*args: Any, **kwargs: Any) -> FakeResponse:
        calls.append((args, kwargs))
        return FakeResponse({})

    session = ReadOnlyJiraSession(unexpected, tmp_path)
    with pytest.raises(JiraApiExportError, match="non-allowlisted"):
        session.post_query_json(
            "/rest/api/3/issue/SCRUM-1/comment",
            raw_relative_path="raw/forbidden.json",
            body={"body": _adf("write")},
        )
    assert calls == []


def test_repeated_search_token_fails_and_leaves_no_final_output(tmp_path: Path) -> None:
    calls = 0

    def repeated(method: str, endpoint: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        if endpoint == "/rest/api/3/serverInfo":
            return FakeResponse({"serverTimeZone": "UTC"})
        if endpoint == "/rest/api/3/myself":
            return FakeResponse({"accountId": "bot", "timeZone": "UTC"})
        if endpoint == "/rest/api/3/search/jql":
            calls += 1
            return FakeResponse(
                {"issues": [], "isLast": False, "nextPageToken": "same-token"}
            )
        raise AssertionError((method, endpoint, kwargs))

    output = tmp_path / "incomplete"
    with pytest.raises(JiraApiExportError, match="repeated nextPageToken"):
        export_snapshot(
            request_fn=repeated,
            output_dir=output,
            project_key="SCRUM",
            cutoff_at="2026-08-09T00:00:00Z",
        )
    assert calls == 2
    assert not output.exists()
    assert not output.with_name(output.name + ".building").exists()


def test_comment_total_change_retries_whole_ticket(tmp_path: Path) -> None:
    starts: list[int] = []
    attempt = 0

    def changing(method: str, endpoint: str, **kwargs: Any) -> FakeResponse:
        nonlocal attempt
        assert endpoint == "/rest/api/3/issue/SCRUM-1/comment"
        start = kwargs["params"]["startAt"]
        starts.append(start)
        if start == 0:
            attempt += 1
        if attempt == 1 and start == 0:
            return FakeResponse(
                {"startAt": 0, "total": 2, "comments": [_comment("1", "one", "a")]}
            )
        if attempt == 1 and start == 1:
            return FakeResponse(
                {
                    "startAt": 1,
                    "total": 3,
                    "comments": [
                        _comment("2", "two", "a"),
                        _comment("3", "three", "a"),
                    ],
                }
            )
        if attempt == 2 and start == 0:
            return FakeResponse(
                {
                    "startAt": 0,
                    "total": 3,
                    "comments": [
                        _comment("1", "one", "a"),
                        _comment("2", "two", "a"),
                    ],
                }
            )
        if attempt == 2 and start == 2:
            return FakeResponse(
                {"startAt": 2, "total": 3, "comments": [_comment("3", "three", "a")]}
            )
        raise AssertionError((attempt, start))

    session = ReadOnlyJiraSession(changing, tmp_path, sleep_fn=lambda _: None)
    comments, dirty = _fetch_comments(
        session,
        issue_key="SCRUM-1",
        max_results=100,
        consistency_retries=1,
        quiet_seconds=0,
    )
    assert [comment["id"] for comment in comments] == ["1", "2", "3"]
    assert dirty is True
    assert starts == [0, 1, 0, 2]


def test_429_respects_retry_after_without_logging_credentials(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls = 0

    def rate_limited(method: str, endpoint: str, **kwargs: Any) -> FakeResponse:
        nonlocal calls
        calls += 1
        assert "Authorization" not in kwargs.get("headers", {})
        if calls == 1:
            return FakeResponse({}, status_code=429, headers={"Retry-After": "2"})
        return FakeResponse({"serverTimeZone": "UTC"})

    session = ReadOnlyJiraSession(
        rate_limited,
        tmp_path,
        sleep_fn=sleeps.append,
        random_fn=lambda: 0.5,
    )
    value = session.get_json(
        "/rest/api/3/serverInfo",
        raw_relative_path="raw/server_info.json",
    )
    assert value == {"serverTimeZone": "UTC"}
    assert sleeps == [2.0]
    assert session.retry_count == 1
    assert session.requests[0]["attempt_count"] == 2


def test_refuses_embedded_comments_and_existing_output(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(JiraApiExportError, match="refusing to overwrite"):
        export_snapshot(
            request_fn=lambda *args, **kwargs: FakeResponse({}),
            output_dir=output,
            project_key="SCRUM",
            cutoff_at="2026-08-09T00:00:00Z",
        )

    with pytest.raises(JiraApiExportError, match="embedded comment"):
        export_snapshot(
            request_fn=lambda *args, **kwargs: FakeResponse({}),
            output_dir=tmp_path / "new",
            project_key="SCRUM",
            cutoff_at="2026-08-09T00:00:00Z",
            fields=["summary", "comment"],
        )
