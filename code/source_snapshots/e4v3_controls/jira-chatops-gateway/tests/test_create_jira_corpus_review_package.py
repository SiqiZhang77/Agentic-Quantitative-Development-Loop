from __future__ import annotations

import json

import pytest

from scripts.create_jira_corpus_review_package import (
    ReviewPackageError,
    create_review_package,
)


def _document(memory_id: str, ticket: str, text: str) -> dict[str, str]:
    return {
        "memory_id": memory_id,
        "source_ticket_id": ticket,
        "source_type": "comment",
        "source_id": f"comment:{memory_id}",
        "source_timestamp": "2026-06-01T10:00:00+00:00",
        "text": text,
    }


def test_review_package_is_deterministic_and_flags_reviewed_literal(tmp_path):
    documents = tmp_path / "documents.jsonl"
    documents.write_text(
        "".join(
            json.dumps(item, sort_keys=True) + "\n"
            for item in [
                _document("MEM-01", "SCRUM-1", "Useful history."),
                _document("MEM-02", "SCRUM-2", "Runs as internal-user."),
                _document("MEM-03", "SCRUM-3", "Another useful result."),
            ]
        ),
        encoding="utf-8",
    )

    first = tmp_path / "review-1"
    second = tmp_path / "review-2"
    kwargs = {
        "documents_path": documents,
        "sample_size": 3,
        "seed": 20260817,
        "sensitive_literals": ["internal-user"],
    }
    create_review_package(output_dir=first, **kwargs)
    create_review_package(output_dir=second, **kwargs)

    first_rows = [json.loads(line) for line in (first / "sample.jsonl").read_text().splitlines()]
    second_rows = [json.loads(line) for line in (second / "sample.jsonl").read_text().splitlines()]
    assert first_rows == second_rows
    flagged = [
        row for row in first_rows
        if "reviewed_sensitive_literal" in row["automated_flags"]
    ]
    assert [row["memory_id"] for row in flagged] == ["MEM-02"]
    assert flagged[0]["assistant_preliminary_decision"] == "requires_redaction"
    assert json.loads((first / "audit.json").read_text())["sampled_unique_memory_count"] == 3


def test_review_package_refuses_to_overwrite_existing_output(tmp_path):
    documents = tmp_path / "documents.jsonl"
    documents.write_text(
        json.dumps(_document("MEM-01", "SCRUM-1", "Useful history.")) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "review"
    output.mkdir()

    with pytest.raises(ReviewPackageError, match="already exists"):
        create_review_package(
            documents_path=documents,
            output_dir=output,
            sample_size=1,
            seed=1,
            sensitive_literals=[],
        )
