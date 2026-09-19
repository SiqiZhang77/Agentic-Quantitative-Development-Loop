from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.normalize_jira_api_snapshot import (
    JiraApiNormalizerError,
    _api_utc,
    _field_state_at_cutoff,
    _identity_fingerprint_in_text,
    normalize_api_snapshot,
)
from scripts.normalize_jira_csv_export import _parse_rfc3339


BOT_RAW = "ari:bot-account-private"
BOT_HASH = hashlib.sha256(BOT_RAW.encode()).hexdigest()
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value, sort_keys=True) + "\n" for value in values), encoding="utf-8")


def _comment(comment_id: str, created: str, updated: str, body: str, author_hash: str) -> dict:
    return {
        "schema_version": "jira-api-comment-record-v1",
        "ticket_key": "SCRUM-10",
        "comment_id": comment_id,
        "created": created,
        "updated": updated,
        "author_account_id_sha256": author_hash,
        "update_author_account_id_sha256": author_hash,
        "body_text": body,
        "body_adf": None,
        "visibility": None,
        "current_body_after_cutoff": False,
    }


def _snapshot(root: Path) -> Path:
    snapshot = root / "snapshot"
    cutoff = "2026-07-02T00:00:00Z"
    request = _comment("1", "2026-07-01T09:00:00+00:00", "2026-07-01T09:00:00+00:00", "/quant\nImplement parser", hashlib.sha256(b"human").hexdigest())
    terminal = _comment(
        "2",
        "2026-07-01T10:00:00+00:00",
        "2026-07-01T10:00:00+00:00",
        "\n".join([
            "[quant-loop-bot]", "Run ID: run_SCRUM-10_test001",
            "Final Status: succeeded", "Branch Name: quant/SCRUM-10",
            f"Commit SHA: {COMMIT}",
        ]),
        BOT_HASH,
    )
    edited_after = _comment("3", "2026-07-01T11:00:00+00:00", "2026-07-03T11:00:00+00:00", "Future private text", hashlib.sha256(b"human").hexdigest())
    empty = _comment("4", "2026-07-01T12:00:00+00:00", "2026-07-01T12:00:00+00:00", "", hashlib.sha256(b"human").hexdigest())
    issue = {
        "schema_version": "jira-api-issue-record-v1",
        "ticket_key": "SCRUM-10", "issue_id": "10010",
        "summary": "Future Summary by Alice", "description_text": "Contact alice@example.com",
        "description_adf": None, "issue_type": {"name": "Task"},
        "status": {"name": "Done"}, "created": "2026-07-01T08:00:00.000+0000",
        "updated": "2026-07-03T12:00:00.000+0000", "resolution": None,
        "resolution_date": None, "labels": [], "components": [], "parent": None,
        "attachment_ids": ["501"], "current_text_after_cutoff": True,
    }
    # This post-cutoff history lets the adapter reconstruct the pre-cutoff summary.
    change = {
        "schema_version": "jira-api-changelog-record-v1", "ticket_key": "SCRUM-10",
        "issue_id": "10010", "history_id": "9001", "created": 1783076400000,
        "author_account_id_sha256": hashlib.sha256(b"human").hexdigest(),
        "after_cutoff": None,
        "items": [{"field":"summary","fieldId":"summary","fromString":"Original Summary by Alice","toString":"Future Summary by Alice"}],
    }
    attachment = {
        "schema_version": "jira-api-attachment-record-v1", "ticket_key": "SCRUM-10",
        "attachment_id": "501", "created": "2026-07-01T12:00:00+00:00",
        "filename": "Alice-result.csv", "mime_type": "text/csv", "size": 20,
        "author_account_id_sha256": hashlib.sha256(b"human").hexdigest(),
        "after_cutoff": False, "content_downloaded": False,
    }
    _write_jsonl(snapshot / "records/issues.jsonl", [issue])
    _write_jsonl(snapshot / "records/comments.jsonl", [request, terminal, edited_after, empty])
    _write_jsonl(snapshot / "records/changelogs.jsonl", [change])
    _write_jsonl(snapshot / "records/attachments.jsonl", [attachment])
    _write_json(snapshot / "raw/myself.json", {"accountId": BOT_RAW, "displayName": "Quant Bot", "emailAddress": "bot@example.invalid"})
    _write_json(snapshot / "raw/comments/SCRUM-10/attempt-01/page-00001.json", {
        "comments": [{
            "id": "1",
            "author": {
                "accountId": "ari:human-account-private",
                "displayName": "Alice",
                "emailAddress": "alice@example.com",
            },
        }]
    })
    audit = {
        "status":"complete", "cutoff_at":cutoff, "issue_count":1, "comment_count":4,
        "changelog_history_count":1, "attachment_metadata_count":1,
        "issue_end_verification_enabled":True, "changelog_enabled":True,
        "attachment_metadata_enabled":True, "comment_concurrent_update_tickets":[],
        "exporter_timezone":"Asia/Calcutta", "server_timezone":"Etc/UTC",
    }
    _write_json(snapshot / "audit.json", audit)
    files = {
        str(path.relative_to(snapshot)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(snapshot.rglob("*")) if path.is_file()
    }
    _write_json(snapshot / "manifest.json", {
        "schema_version":"jira-api-snapshot-v1", "snapshot_id":"jira-api-test",
        "snapshot_content_sha256":"a"*64, "cutoff_at":cutoff, "files":files,
    })
    return snapshot


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_api_timestamp_supports_offsets_and_epoch_milliseconds() -> None:
    assert _api_utc("2026-08-13T22:31:26.061+0530", "value") == "2026-08-13T17:01:26.061000Z"
    assert _api_utc(1783076400000, "value") == "2026-07-03T11:00:00Z"


def test_identity_residual_audit_is_chunked_and_privacy_safe() -> None:
    replacements = [(f"Person{index}", True) for index in range(450)]
    assert _identity_fingerprint_in_text("hello Person449", replacements) is not None
    assert _identity_fingerprint_in_text("hello nobody", replacements) is None


def test_field_state_rewinds_post_cutoff_change() -> None:
    value, timestamp = _field_state_at_cutoff(
        current="future", histories=[{"history_id":"1","created":1783076400000,"items":[{"field":"summary","fromString":"past","toString":"future"}]}],
        field="summary", created_utc="2026-07-01T08:00:00Z",
        cutoff=_parse_rfc3339("2026-07-02T00:00:00Z", "cutoff"),
    )
    assert value == "past"
    assert timestamp == "2026-07-01T08:00:00Z"


def test_normalizes_verified_api_snapshot_with_cutoff_and_redaction(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text("[]\n")
    output = tmp_path / "corpus"
    result = normalize_api_snapshot(
        snapshot_dir=snapshot, exclusions_path=exclusions, output_dir=output,
        cutoff_at="2026-07-02T00:00:00Z", bot_author_sha256s=[BOT_HASH],
        bot_author_trust_status="verified",
    )
    tickets = _read_jsonl(output / "tickets.jsonl")
    events = _read_jsonl(output / "events.jsonl")
    documents = _read_jsonl(output / "documents.jsonl")
    combined = "\n".join(path.read_text() for path in output.iterdir() if path.is_file()).casefold()
    assert tickets[0]["summary"] == "Original Summary by [PERSON]"
    assert [event["event_type"] for event in events] == ["quant_request", "bot_terminal", "human_comment", "human_comment"]
    assert events[2]["after_cutoff"] is True
    assert result["audit"]["changelog_after_cutoff_count_corrected"] == 1
    assert result["audit"]["primary_episode_count"] == 1
    assert not any("future private text" in document["text"].casefold() for document in documents)
    assert all(not document["text"].endswith("\n") for document in documents)
    assert "alice" not in combined
    assert "@" not in combined
    assert all(set(document) == {"memory_id","source_ticket_id","source_type","source_id","source_timestamp","text"} for document in documents)


def test_refuses_tampered_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    with (snapshot / "records/issues.jsonl").open("a") as handle:
        handle.write("{}\n")
    exclusions = tmp_path / "exclusions.json"
    exclusions.write_text("[]\n")
    with pytest.raises(JiraApiNormalizerError, match="hash mismatch"):
        normalize_api_snapshot(
            snapshot_dir=snapshot, exclusions_path=exclusions,
            output_dir=tmp_path / "corpus", cutoff_at="2026-07-02T00:00:00Z",
            bot_author_sha256s=[BOT_HASH],
        )
