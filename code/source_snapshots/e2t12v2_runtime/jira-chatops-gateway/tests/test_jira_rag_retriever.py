from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jira_rag_retriever import (
    JiraRagIndexError,
    _canonical_sha256,
    build_frozen_index_payload,
    load_frozen_index,
    retrieve_jira_memory,
)


def _document(
    memory_id: str,
    text: str,
    *,
    ticket_id: str = "SCRUM-10",
    timestamp: str = "2026-06-01T10:00:00+00:00",
) -> dict:
    return {
        "memory_id": memory_id,
        "source_ticket_id": ticket_id,
        "source_type": "comment",
        "source_id": f"comment:{memory_id}",
        "source_timestamp": timestamp,
        "text": text,
    }


def _write_index(
    path: Path,
    documents: list[dict],
    *,
    excluded: list[str] | None = None,
    cutoff_at: str = "2026-06-30T23:59:59+00:00",
) -> str:
    excluded = sorted(excluded or [])
    normalized_documents = sorted(documents, key=lambda item: item["memory_id"])
    payload = {
        "schema_version": "1.0",
        "index_id": "e2-jira-index-v1",
        "corpus_id": "e2-jira-corpus-v1",
        "corpus_sha256": _canonical_sha256(normalized_documents),
        "exclusion_list_id": "e2-exclusions-v1",
        "exclusion_list_sha256": _canonical_sha256(excluded),
        "excluded_ticket_ids": excluded,
        "cutoff_at": cutoff_at,
        "documents": documents,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_request() -> dict:
    return {
        "jira_metadata": {
            "ticket_id": "SCRUM-99",
            "summary": "Preserve chronological Jira event history",
            "description": "Keep the history cap and skip malformed comments.",
        },
        "execution_objectives": {
            "strategy_type": "refactor",
            "resource_path": "jira-chatops-gateway/dags/jira_command_validation.py",
            "parsed_task_parameters": {
                "objective": "Refactor Jira event history without changing behaviour"
            },
        },
    }


def test_index_builder_sorts_documents_and_hashes_exclusions():
    payload = build_frozen_index_payload(
        [
            _document("MEM-02", "Second clean memory."),
            _document("MEM-01", "First clean memory."),
        ],
        index_id="e2-index-v1",
        corpus_id="e2-corpus-v1",
        exclusion_list_id="e2-exclusions-v1",
        excluded_ticket_ids=["SCRUM-30", "SCRUM-20", "SCRUM-30"],
        cutoff_at="2026-06-30T23:59:59+00:00",
    )

    assert [item["memory_id"] for item in payload["documents"]] == [
        "MEM-01",
        "MEM-02",
    ]
    assert payload["excluded_ticket_ids"] == ["SCRUM-20", "SCRUM-30"]
    assert payload["corpus_sha256"] == _canonical_sha256(payload["documents"])
    assert payload["exclusion_list_sha256"] == _canonical_sha256(
        payload["excluded_ticket_ids"]
    )


def test_retrieval_is_deterministic_ranked_and_excludes_current_ticket(tmp_path):
    documents = [
        _document("MEM-02", "Jira history stays chronological and capped."),
        _document("MEM-01", "Jira history stays chronological and capped."),
        _document("MEM-03", "Unrelated Docker memory settings."),
        _document(
            "MEM-CURRENT",
            "Jira history chronological capped malformed comments refactor.",
            ticket_id="SCRUM-99",
        ),
    ]
    path = tmp_path / "index.json"
    digest = _write_index(path, documents)

    first = retrieve_jira_memory(
        _runtime_request(),
        index_path=path,
        expected_index_sha256=digest,
        top_k=2,
    )
    second = retrieve_jira_memory(
        _runtime_request(),
        index_path=path,
        expected_index_sha256=digest,
        top_k=2,
    )

    assert first == second
    assert [item["memory_id"] for item in first["memories"]] == [
        "MEM-01",
        "MEM-02",
    ]
    assert [item["rank"] for item in first["memories"]] == [1, 2]
    assert first["candidate_count"] == 3
    assert first["max_memories_per_source_ticket"] == 2
    assert first["status"] == "ok"
    assert first["index_sha256"] == digest
    assert "MEM-CURRENT" not in json.dumps(first)


def test_zero_overlap_is_recorded_as_an_empty_c1_retrieval(tmp_path):
    path = tmp_path / "index.json"
    digest = _write_index(path, [_document("MEM-01", "volatility covariance")])

    result = retrieve_jira_memory(
        _runtime_request(),
        index_path=path,
        expected_index_sha256=digest,
        top_k=5,
    )

    assert result["enabled"] is True
    assert result["status"] == "empty"
    assert result["memories"] == []
    assert result["candidate_count"] == 1


def test_retrieval_limits_each_source_ticket_to_two_memories(tmp_path):
    documents = [
        _document("MEM-A1", "Jira history chronological refactor.", ticket_id="SCRUM-10"),
        _document("MEM-A2", "Jira history chronological refactor.", ticket_id="SCRUM-10"),
        _document("MEM-A3", "Jira history chronological refactor.", ticket_id="SCRUM-10"),
        _document("MEM-B1", "Jira history chronological refactor.", ticket_id="SCRUM-11"),
        _document("MEM-B2", "Jira history chronological refactor.", ticket_id="SCRUM-11"),
        _document("MEM-C1", "Jira history chronological refactor.", ticket_id="SCRUM-12"),
    ]
    path = tmp_path / "index.json"
    digest = _write_index(path, documents)

    result = retrieve_jira_memory(
        _runtime_request(),
        index_path=path,
        expected_index_sha256=digest,
        top_k=5,
    )

    assert [item["memory_id"] for item in result["memories"]] == [
        "MEM-A1",
        "MEM-A2",
        "MEM-B1",
        "MEM-B2",
        "MEM-C1",
    ]
    assert [item["rank"] for item in result["memories"]] == [1, 2, 3, 4, 5]
    source_counts = {}
    for item in result["memories"]:
        source_counts[item["source_ticket_id"]] = (
            source_counts.get(item["source_ticket_id"], 0) + 1
        )
    assert max(source_counts.values()) == 2
    assert result["max_memories_per_source_ticket"] == 2


def test_index_hash_mismatch_fails_closed(tmp_path):
    path = tmp_path / "index.json"
    _write_index(path, [_document("MEM-01", "Jira history")])

    with pytest.raises(JiraRagIndexError, match="SHA-256 does not match"):
        load_frozen_index(path, expected_sha256="0" * 64)


def test_document_after_cutoff_is_rejected(tmp_path):
    path = tmp_path / "index.json"
    digest = _write_index(
        path,
        [
            _document(
                "MEM-FUTURE",
                "future result",
                timestamp="2026-07-01T00:00:00+00:00",
            )
        ],
    )

    with pytest.raises(JiraRagIndexError, match="newer than the frozen cutoff"):
        load_frozen_index(path, expected_sha256=digest)


def test_index_cannot_contain_an_excluded_ticket(tmp_path):
    path = tmp_path / "index.json"
    digest = _write_index(
        path,
        [_document("MEM-01", "implementation notes", ticket_id="SCRUM-10")],
        excluded=["SCRUM-10"],
    )

    with pytest.raises(JiraRagIndexError, match="contains excluded tickets"):
        load_frozen_index(path, expected_sha256=digest)


@pytest.mark.parametrize(
    "text",
    [
        "api_key=super-secret-value",
        '{"api_key":"super-secret-value"}',
        "token: super-secret-value",
        "Authorization: Bearer super-secret-value",
        "Bearer super-secret-value",
        "Contact analyst@example.com for details",
        "x" * 4_001,
    ],
)
def test_index_rejects_unsanitized_or_oversized_memory(tmp_path, text):
    path = tmp_path / "index.json"
    digest = _write_index(path, [_document("MEM-01", text)])

    with pytest.raises(JiraRagIndexError, match="secret, email address, or exceeds"):
        load_frozen_index(path, expected_sha256=digest)


def test_index_rejects_non_rfc3339_timestamp_separator(tmp_path):
    path = tmp_path / "index.json"
    digest = _write_index(
        path,
        [
            _document(
                "MEM-01",
                "Jira history",
                timestamp="2026-06-01 10:00:00+00:00",
            )
        ],
    )

    with pytest.raises(JiraRagIndexError, match="RFC3339"):
        load_frozen_index(path, expected_sha256=digest)


@pytest.mark.parametrize("top_k", [0, 11, True, "5"])
def test_top_k_is_strictly_bounded(tmp_path, top_k):
    path = tmp_path / "index.json"
    digest = _write_index(path, [_document("MEM-01", "Jira history")])

    with pytest.raises(JiraRagIndexError, match="top_k must be an integer"):
        retrieve_jira_memory(
            _runtime_request(),
            index_path=path,
            expected_index_sha256=digest,
            top_k=top_k,
        )
