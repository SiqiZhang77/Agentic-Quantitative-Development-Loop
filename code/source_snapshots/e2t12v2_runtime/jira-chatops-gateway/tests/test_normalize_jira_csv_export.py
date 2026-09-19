from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from jira_rag_retriever import build_frozen_index_payload
from scripts.normalize_jira_csv_export import (
    JiraCsvNormalizerError,
    _is_low_information_document,
    _parse_local_timestamp,
    normalize_snapshot,
)
from zoneinfo import ZoneInfo


COMMIT = "0123456789abcdef0123456789abcdef01234567"
BOT_AUTHOR = "ari:0123456789abcdef0123456789abcdef"
BOT_AUTHOR_SHA256 = hashlib.sha256(BOT_AUTHOR.encode("utf-8")).hexdigest()


def test_low_information_filter_is_narrow_and_deterministic():
    base = {
        "memory_id": "MEM-SCRUM-1-SUMMARY-abc",
        "source_ticket_id": "SCRUM-1",
        "source_id": "issue:SCRUM-1:summary",
        "source_timestamp": "2026-07-01T00:00:00Z",
    }
    assert _is_low_information_document({
        **base,
        "source_type": "summary",
        "text": "Ticket SCRUM-1 summary: Test",
    })
    assert _is_low_information_document({
        **base,
        "source_type": "comment",
        "text": "Ticket SCRUM-1 historical comment:\n104",
    })
    assert not _is_low_information_document({
        **base,
        "source_type": "summary",
        "text": "Ticket SCRUM-1 summary: Momentum strategy",
    })
    assert not _is_low_information_document({
        **base,
        "source_type": "description",
        "text": "Ticket SCRUM-1 description:\nTest",
    })


def _bot_running(run_id: str) -> str:
    return "\n".join(
        [
            "[quant-loop-bot]",
            "h2. Automated quant workflow report: running workflow",
            f"Run ID: {run_id}",
            "Final Status: not available",
        ]
    )


def _bot_terminal(run_id: str, outcome: str = "succeeded") -> str:
    return "\n".join(
        [
            "[quant-loop-bot]",
            "h2. Automated quant workflow report: successful completion",
            f"Run ID: {run_id}",
            f"Final Status: {outcome}",
            "Error Code: not provided",
            "Branch Name: quant/SCRUM-10",
            f"Commit SHA: {COMMIT}",
            "Last event: 2026-07-01T08:32:00Z",
        ]
    )


def _write_fixture(
    path: Path,
    *,
    comments: list[str],
    second_ticket: bool = True,
    comment_authors: list[str] | None = None,
    description: str = "Contact alice@example.com; account id: acct-123; api_key=secret-value",
) -> None:
    header = [
        "Issue key",
        "Summary",
        "Description",
        "Issue Type",
        "Status",
        "Created",
        "Updated",
        "Reporter",
        "Reporter Id",
        *(["Comment"] * len(comments)),
        "Attachment",
    ]
    authors = comment_authors or [
        BOT_AUTHOR if body.startswith("[quant-loop-bot]") else "Alice Analyst"
        for body in comments
    ]
    assert len(authors) == len(comments)
    first_comments = [
        f"01/Jul/26 {2 + index // 60}:{index % 60:02d} PM;{authors[index]};{body}"
        for index, body in enumerate(comments)
    ]
    rows = [
        [
            "SCRUM-10",
            "Build parser for Alice",
            description,
            "Task",
            "Done",
            "01/07/2026 13:00",
            "01/07/2026 15:00",
            "Alice Analyst",
            "acct-123",
            *first_comments,
            "01/Jul/26 3:00 PM;Alice Analyst;result.csv;https://jira.invalid/private?id=1",
        ]
    ]
    if second_ticket:
        rows.append(
            [
                "SCRUM-20",
                "Excluded private task",
                "Should never become a document.",
                "Task",
                "Done",
                "01/07/2026 13:00",
                "01/07/2026 15:00",
                "Alice Analyst",
                "acct-123",
                *([""] * len(comments)),
                "",
            ]
        )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.writer(handle).writerows([header, *rows])


def _run(tmp_path: Path, comments: list[str], output_name: str = "normalized"):
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    output = tmp_path / output_name
    _write_fixture(input_csv, comments=comments)
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    result = normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption_conflicts_with_embedded_utc",
        cutoff_at="2026-07-02T00:00:00Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )
    return output, result


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_normalizes_duplicate_comment_columns_redacts_and_builds_documents(tmp_path):
    run_id = "run_SCRUM-10_abc123"
    output, result = _run(
        tmp_path,
        [
            "/quant\nImplement parser; preserve semicolons",
            _bot_running(run_id),
            _bot_terminal(run_id),
        ],
    )

    events = _jsonl(output / "events.jsonl")
    episodes = _jsonl(output / "episodes.jsonl")
    documents = _jsonl(output / "documents.jsonl")
    attachments = _jsonl(output / "attachments.jsonl")
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    ).casefold()

    assert [event["event_type"] for event in events] == [
        "quant_request",
        "bot_progress",
        "bot_terminal",
    ]
    assert [event["actor_role"] for event in events] == ["human", "bot", "bot"]
    assert "preserve semicolons" in events[0]["text"]
    assert events[0]["timestamp_utc_candidate"] == "2026-07-01T13:00:00Z"
    assert episodes[0]["pairing_confidence"] == "strict_modern"
    assert episodes[0]["primary_eligible"] is True
    assert episodes[0]["outcome"] == "succeeded"
    assert result["audit"]["primary_episode_count"] == 1
    assert result["audit"]["timezone_conflict_detected"] is True
    assert attachments[0]["url_retained"] is False
    assert "https://" not in combined
    assert "@" not in combined
    assert "alice" not in combined
    assert "alice@example.com" not in combined
    assert "acct-123" not in combined
    assert "secret-value" not in combined
    assert all(set(document) == {
        "memory_id",
        "source_ticket_id",
        "source_type",
        "source_id",
        "source_timestamp",
        "text",
    } for document in documents)
    assert all(document["source_ticket_id"] != "SCRUM-20" for document in documents)
    assert any(document["source_type"] == "result_summary" for document in documents)
    assert next(
        document for document in documents if document["source_type"] == "summary"
    )["source_timestamp"] == "2026-07-01T14:00:00Z"
    assert all(len(document["text"]) <= 4_000 for document in documents)
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert "source_filename" not in manifest
    assert len(manifest["source_filename_sha256"]) == 64
    assert len(manifest["normalizer_script_sha256"]) == 64
    assert manifest["trusted_bot_author_sha256s"] == [BOT_AUTHOR_SHA256]

    payload = build_frozen_index_payload(
        documents,
        index_id="test-index",
        corpus_id="test-corpus",
        exclusion_list_id="test-exclusions",
        excluded_ticket_ids=["SCRUM-20"],
        cutoff_at="2026-07-02T00:00:00Z",
    )
    assert len(payload["documents"]) == len(documents)


def test_same_run_conflicting_terminals_never_uses_last_wins(tmp_path):
    run_id = "run_SCRUM-10_conflict1"
    output, result = _run(
        tmp_path,
        [
            "/quant\nImplement parser",
            _bot_running(run_id),
            _bot_terminal(run_id, "succeeded"),
            _bot_terminal(run_id, "failed"),
        ],
    )

    run = _jsonl(output / "runs.jsonl")[0]
    episode = _jsonl(output / "episodes.jsonl")[0]
    assert run["terminal_conflict"] is True
    assert run["canonical_terminal_event_id"] is None
    assert episode["primary_eligible"] is False
    assert "terminal_conflict" in episode["quality_flags"]
    assert result["audit"]["conflicting_run_count"] == 1


def test_duplicate_same_outcome_terminal_is_canonical_and_audited(tmp_path):
    run_id = "run_SCRUM-10_duplicate1"
    output, _ = _run(
        tmp_path,
        [
            "/quant\nImplement parser",
            _bot_terminal(run_id, "succeeded"),
            _bot_terminal(run_id, "succeeded"),
        ],
    )
    run = _jsonl(output / "runs.jsonl")[0]
    episode = _jsonl(output / "episodes.jsonl")[0]
    assert run["terminal_conflict"] is False
    assert "duplicate_terminal_same_outcome" in run["quality_flags"]
    assert run["canonical_terminal_event_id"] == run["terminal_event_ids"][-1]
    assert episode["primary_eligible"] is True


def test_non_strict_quant_forms_are_diagnostics_not_primary_requests(tmp_path):
    output, result = _run(
        tmp_path,
        [
            "* /quant\nlegacy bullet",
            "/Quant\nwrong case",
            "attachment marker\n/quant\nembedded",
        ],
    )
    events = _jsonl(output / "events.jsonl")
    assert all(event["event_type"] == "human_comment" for event in events)
    assert "legacy_bullet_quant_candidate" in events[0]["quality_flags"]
    assert "embedded_quant_candidate" in events[2]["quality_flags"]
    assert result["audit"]["episode_count"] == 0


def test_outputs_are_byte_deterministic_and_never_overwritten(tmp_path):
    comments = [
        "/quant\nImplement parser",
        _bot_terminal("run_SCRUM-10_stable1"),
    ]
    first, _ = _run(tmp_path, comments, "first")
    second = tmp_path / "second"
    normalize_snapshot(
        input_csv=tmp_path / "Jira.csv",
        exclusions_path=tmp_path / "excluded.json",
        output_dir=second,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption_conflicts_with_embedded_utc",
        cutoff_at="2026-07-02T00:00:00Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )
    assert {path.name: path.read_bytes() for path in first.iterdir()} == {
        path.name: path.read_bytes() for path in second.iterdir()
    }
    with pytest.raises(JiraCsvNormalizerError, match="refusing to overwrite"):
        normalize_snapshot(
            input_csv=tmp_path / "Jira.csv",
            exclusions_path=tmp_path / "excluded.json",
            output_dir=first,
            source_timezone_name="Europe/London",
            timezone_status="operator_assumption",
            cutoff_at="2026-07-02T00:00:00Z",
            bot_author_sha256s=[BOT_AUTHOR_SHA256],
        )


def test_after_cutoff_records_remain_auditable_but_never_enter_documents(tmp_path):
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    output = tmp_path / "normalized"
    _write_fixture(
        input_csv,
        comments=[
            "/quant\nImplement parser",
            _bot_terminal("run_SCRUM-10_future1"),
        ],
    )
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    result = normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption",
        cutoff_at="2026-06-30T23:59:59Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )

    assert _jsonl(output / "documents.jsonl") == []
    episode = _jsonl(output / "episodes.jsonl")[0]
    assert episode["primary_eligible"] is False
    assert "after_cutoff" in episode["quality_flags"]
    assert result["audit"]["documents_skipped_after_cutoff"] > 0


def test_post_cutoff_events_cannot_change_pre_cutoff_episode(tmp_path):
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    output = tmp_path / "normalized"
    primary_run_id = "run_SCRUM-10_boundary1"
    future_run_id = "run_SCRUM-10_boundary2"
    _write_fixture(
        input_csv,
        comments=[
            "/quant\nImplement parser",
            _bot_running(primary_run_id),
            _bot_terminal(primary_run_id, "succeeded"),
            _bot_terminal(primary_run_id, "failed"),
            _bot_terminal(future_run_id, "failed"),
        ],
    )
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    result = normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption",
        cutoff_at="2026-07-01T13:02:30Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )

    runs = {run["run_id"]: run for run in _jsonl(output / "runs.jsonl")}
    primary = runs[primary_run_id]
    future = runs[future_run_id]
    episode = _jsonl(output / "episodes.jsonl")[0]
    assert primary["terminal_conflict"] is False
    assert primary["all_terminal_conflict"] is True
    assert len(primary["pre_cutoff_terminal_event_ids"]) == 1
    assert len(primary["post_cutoff_event_ids"]) == 1
    assert "post_cutoff_terminal_conflict" in primary["quality_flags"]
    assert future["canonical_terminal_event_id"] is None
    assert future["pre_cutoff_terminal_event_ids"] == []
    assert episode["primary_eligible"] is True
    assert episode["outcome"] == "succeeded"
    assert episode["run_record_ids"] == [primary["run_record_id"]]
    assert result["audit"]["conflicting_run_count"] == 0
    assert result["audit"]["all_snapshot_conflicting_run_count"] == 1


def test_marker_from_untrusted_author_is_not_a_bot_report(tmp_path):
    run_id = "run_SCRUM-10_forged1"
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    output = tmp_path / "normalized"
    _write_fixture(
        input_csv,
        comments=[_bot_terminal(run_id)],
        comment_authors=["Alice Analyst"],
    )
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    result = normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption",
        cutoff_at="2026-07-02T00:00:00Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )

    event = _jsonl(output / "events.jsonl")[0]
    assert event["event_type"] == "human_comment"
    assert event["actor_role"] == "human"
    assert "untrusted_bot_marker" in event["quality_flags"]
    assert _jsonl(output / "runs.jsonl") == []
    assert result["audit"]["untrusted_bot_marker_count"] == 1


def test_operator_identity_lexicon_redacts_free_text_name(tmp_path):
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    identities = tmp_path / "reviewed-identities.json"
    output = tmp_path / "normalized"
    _write_fixture(
        input_csv,
        comments=[],
        description=(
            "Escalate this result to Bob Smith and Max Garner before release. "
            "Max value must remain searchable."
        ),
    )
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    identities.write_text('["Bob Smith", "Max Garner"]\n', encoding="utf-8")
    normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption",
        cutoff_at="2026-07-02T00:00:00Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
        additional_identities_path=identities,
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert "Bob Smith" not in combined
    assert "Max Garner" not in combined
    assert "Max value must remain searchable" in combined
    assert "reviewed-identities.json" not in combined
    assert manifest["additional_identity_source_sha256"] == hashlib.sha256(
        identities.read_bytes()
    ).hexdigest()


def test_contextual_project_framework_author_is_redacted_without_lexicon(tmp_path):
    input_csv = tmp_path / "Jira.csv"
    exclusions = tmp_path / "excluded.json"
    output = tmp_path / "normalized"
    _write_fixture(
        input_csv,
        comments=[],
        description=(
            "Extend the 2025 / Bob Smith Fatberg framework. "
            "Max value must remain searchable."
        ),
    )
    exclusions.write_text('["SCRUM-20"]\n', encoding="utf-8")
    normalize_snapshot(
        input_csv=input_csv,
        exclusions_path=exclusions,
        output_dir=output,
        source_timezone_name="Europe/London",
        timezone_status="operator_assumption",
        cutoff_at="2026-07-02T00:00:00Z",
        bot_author_sha256s=[BOT_AUTHOR_SHA256],
    )

    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir() if path.is_file()
    )
    assert "Bob Smith" not in combined
    assert "Max value must remain searchable" in combined


def test_london_dst_ambiguity_fails_closed():
    with pytest.raises(JiraCsvNormalizerError, match="ambiguous or nonexistent"):
        _parse_local_timestamp(
            "25/Oct/26 1:30 AM",
            ZoneInfo("Europe/London"),
            "ambiguous",
        )
