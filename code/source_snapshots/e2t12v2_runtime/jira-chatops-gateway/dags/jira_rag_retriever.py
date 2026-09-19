"""Deterministic retrieval from a frozen, content-addressed Jira memory index.

Formal Experiment 2 runs must never query mutable Jira state at execution time.
This module therefore accepts only a local read-only index whose SHA-256 is
supplied independently by Airflow configuration.  The implementation uses a
small standard-library BM25 scorer so the deployed Airflow image needs no new
machine-learning dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

RETRIEVER_NAME = "jira_lexical_bm25"
RETRIEVER_VERSION = "1.1.0"
INDEX_SCHEMA_VERSION = "1.0"
DEFAULT_TOP_K = 5
MAX_TOP_K = 10
MAX_MEMORIES_PER_SOURCE_TICKET = 2
MAX_QUERY_CHARS = 8_000
MAX_MEMORY_CHARS = 4_000
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_DOCUMENTS = 100_000
BM25_K1 = 1.5
BM25_B = 0.75

_HASH_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_EMAIL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
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
_SOURCE_TYPES = {
    "summary",
    "description",
    "comment",
    "status_change",
    "description_update",
    "bot_result",
    "error_summary",
    "result_summary",
    "attachment_metadata",
    "other",
}


class JiraRagIndexError(ValueError):
    """Raised when a frozen index cannot be trusted or parsed."""


@dataclass(frozen=True)
class FrozenJiraIndex:
    index_id: str
    index_sha256: str
    corpus_id: str
    corpus_sha256: str
    exclusion_list_id: str
    exclusion_list_sha256: str
    cutoff_at: str
    documents: tuple[dict[str, Any], ...]


def build_frozen_index_payload(
    documents: list[dict[str, Any]],
    *,
    index_id: str,
    corpus_id: str,
    exclusion_list_id: str,
    excluded_ticket_ids: list[str],
    cutoff_at: str,
) -> dict[str, Any]:
    """Validate normalized chunks and construct a content-addressed index object."""

    cutoff = _parse_timestamp(cutoff_at, "cutoff_at")
    excluded = sorted(
        {
            _require_string(item, "excluded_ticket_ids item", pattern=_TICKET_RE)
            for item in excluded_ticket_ids
        }
    )
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS:
        raise JiraRagIndexError(
            f"documents must be an array with at most {MAX_DOCUMENTS} entries"
        )
    normalized = [
        _normalise_document(item, cutoff, position)
        for position, item in enumerate(documents)
    ]
    normalized.sort(key=lambda item: item["memory_id"])
    memory_ids = [item["memory_id"] for item in normalized]
    if len(memory_ids) != len(set(memory_ids)):
        raise JiraRagIndexError("memory_id values must be unique")
    prohibited = sorted(
        {item["source_ticket_id"] for item in normalized}.intersection(excluded)
    )
    if prohibited:
        raise JiraRagIndexError(
            "frozen index contains excluded tickets: " + ", ".join(prohibited)
        )

    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "index_id": _require_string(index_id, "index_id", pattern=_SAFE_ID_RE),
        "corpus_id": _require_string(corpus_id, "corpus_id", pattern=_SAFE_ID_RE),
        "corpus_sha256": _canonical_sha256(normalized),
        "exclusion_list_id": _require_string(
            exclusion_list_id,
            "exclusion_list_id",
            pattern=_SAFE_ID_RE,
        ),
        "exclusion_list_sha256": _canonical_sha256(excluded),
        "excluded_ticket_ids": excluded,
        "cutoff_at": cutoff_at,
        "documents": normalized,
    }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_string(
    value: Any,
    label: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str) or not value:
        raise JiraRagIndexError(f"{label} must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise JiraRagIndexError(f"{label} has an invalid format")
    return value


def _require_hash(value: Any, label: str) -> str:
    return _require_string(value, label, pattern=_HASH_RE).lower()


def _parse_timestamp(value: Any, label: str) -> datetime:
    text = _require_string(value, label)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
        text,
    ):
        raise JiraRagIndexError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JiraRagIndexError(f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise JiraRagIndexError(f"{label} must include a timezone")
    return parsed


def _sanitize_text(value: Any, max_chars: int) -> str:
    lines = [line.rstrip() for line in str(value or "").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    text = "\n".join(lines)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    marker = "\n[TRUNCATED]"
    if len(text) > max_chars:
        text = text[: max_chars - len(marker)].rstrip() + marker
    return text


def _sanitize_memory_text(value: Any) -> str:
    return _sanitize_text(value, MAX_MEMORY_CHARS)


def _normalise_document(raw: Any, cutoff: datetime, index: int) -> dict[str, Any]:
    label = f"documents[{index}]"
    if not isinstance(raw, dict):
        raise JiraRagIndexError(f"{label} must be an object")
    expected_fields = {
        "memory_id",
        "source_ticket_id",
        "source_type",
        "source_id",
        "source_timestamp",
        "text",
    }
    unknown = set(raw) - expected_fields
    missing = expected_fields - set(raw)
    if unknown:
        raise JiraRagIndexError(
            f"{label} contains unsupported fields: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise JiraRagIndexError(
            f"{label} is missing fields: {', '.join(sorted(missing))}"
        )

    source_type = _require_string(raw["source_type"], f"{label}.source_type")
    if source_type not in _SOURCE_TYPES:
        raise JiraRagIndexError(f"{label}.source_type is unsupported")
    source_timestamp = _require_string(
        raw["source_timestamp"], f"{label}.source_timestamp"
    )
    if _parse_timestamp(source_timestamp, f"{label}.source_timestamp") > cutoff:
        raise JiraRagIndexError(f"{label} is newer than the frozen cutoff")

    original_text = _require_string(raw["text"], f"{label}.text")
    sanitized_text = _sanitize_memory_text(original_text)
    if sanitized_text != original_text:
        raise JiraRagIndexError(
            f"{label}.text contains a secret, email address, or exceeds the size cap"
        )

    return {
        "memory_id": _require_string(
            raw["memory_id"], f"{label}.memory_id", pattern=_SAFE_ID_RE
        ),
        "source_ticket_id": _require_string(
            raw["source_ticket_id"],
            f"{label}.source_ticket_id",
            pattern=_TICKET_RE,
        ),
        "source_type": source_type,
        "source_id": _require_string(
            raw["source_id"], f"{label}.source_id", pattern=_SAFE_ID_RE
        ),
        "source_timestamp": source_timestamp,
        "text": sanitized_text,
    }


def load_frozen_index(
    index_path: str | Path,
    *,
    expected_sha256: str,
) -> FrozenJiraIndex:
    """Load and fully verify a frozen index, failing closed on any drift."""

    expected_hash = _require_hash(expected_sha256, "expected index SHA-256")
    path = Path(index_path)
    if not path.is_file():
        raise JiraRagIndexError(f"frozen Jira RAG index does not exist: {path}")
    size = path.stat().st_size
    if size <= 0 or size > MAX_INDEX_BYTES:
        raise JiraRagIndexError(
            f"frozen Jira RAG index size must be between 1 and {MAX_INDEX_BYTES} bytes"
        )

    raw_bytes = path.read_bytes()
    actual_hash = hashlib.sha256(raw_bytes).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise JiraRagIndexError(
            "frozen Jira RAG index SHA-256 does not match JIRA_RAG_INDEX_SHA256"
        )
    try:
        payload = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JiraRagIndexError("frozen Jira RAG index is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise JiraRagIndexError("frozen Jira RAG index must be a JSON object")

    required_fields = {
        "schema_version",
        "index_id",
        "corpus_id",
        "corpus_sha256",
        "exclusion_list_id",
        "exclusion_list_sha256",
        "excluded_ticket_ids",
        "cutoff_at",
        "documents",
    }
    unknown = set(payload) - required_fields
    missing = required_fields - set(payload)
    if unknown:
        raise JiraRagIndexError(
            "frozen Jira RAG index contains unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    if missing:
        raise JiraRagIndexError(
            "frozen Jira RAG index is missing fields: " + ", ".join(sorted(missing))
        )
    if payload["schema_version"] != INDEX_SCHEMA_VERSION:
        raise JiraRagIndexError(
            f"unsupported Jira RAG index schema_version: {payload['schema_version']}"
        )

    cutoff_text = _require_string(payload["cutoff_at"], "cutoff_at")
    cutoff = _parse_timestamp(cutoff_text, "cutoff_at")
    excluded_raw = payload["excluded_ticket_ids"]
    if not isinstance(excluded_raw, list):
        raise JiraRagIndexError("excluded_ticket_ids must be an array")
    excluded = sorted(
        {
            _require_string(item, "excluded_ticket_ids item", pattern=_TICKET_RE)
            for item in excluded_raw
        }
    )
    exclusion_hash = _require_hash(
        payload["exclusion_list_sha256"], "exclusion_list_sha256"
    )
    if _canonical_sha256(excluded) != exclusion_hash:
        raise JiraRagIndexError("exclusion_list_sha256 does not match excluded_ticket_ids")

    raw_documents = payload["documents"]
    if not isinstance(raw_documents, list) or len(raw_documents) > MAX_DOCUMENTS:
        raise JiraRagIndexError(
            f"documents must be an array with at most {MAX_DOCUMENTS} entries"
        )
    documents = [
        _normalise_document(item, cutoff, index)
        for index, item in enumerate(raw_documents)
    ]
    documents.sort(key=lambda item: item["memory_id"])
    memory_ids = [item["memory_id"] for item in documents]
    if len(memory_ids) != len(set(memory_ids)):
        raise JiraRagIndexError("memory_id values must be unique")
    prohibited = sorted(
        {item["source_ticket_id"] for item in documents}.intersection(excluded)
    )
    if prohibited:
        raise JiraRagIndexError(
            "frozen index contains excluded tickets: " + ", ".join(prohibited)
        )
    corpus_hash = _require_hash(payload["corpus_sha256"], "corpus_sha256")
    if _canonical_sha256(documents) != corpus_hash:
        raise JiraRagIndexError("corpus_sha256 does not match normalized documents")

    return FrozenJiraIndex(
        index_id=_require_string(payload["index_id"], "index_id", pattern=_SAFE_ID_RE),
        index_sha256=actual_hash,
        corpus_id=_require_string(
            payload["corpus_id"], "corpus_id", pattern=_SAFE_ID_RE
        ),
        corpus_sha256=corpus_hash,
        exclusion_list_id=_require_string(
            payload["exclusion_list_id"],
            "exclusion_list_id",
            pattern=_SAFE_ID_RE,
        ),
        exclusion_list_sha256=exclusion_hash,
        cutoff_at=cutoff_text,
        documents=tuple(documents),
    )


def build_retrieval_query(runtime_request: dict[str, Any]) -> str:
    """Build the versioned canonical query from current-ticket context only."""

    jira = runtime_request.get("jira_metadata") or {}
    objectives = runtime_request.get("execution_objectives") or {}
    parameters = objectives.get("parsed_task_parameters") or {}
    parts = [
        jira.get("summary"),
        jira.get("description"),
        parameters.get("objective"),
        objectives.get("strategy_type"),
        objectives.get("resource_path"),
    ]
    query = _sanitize_text(
        "\n".join(str(part).strip() for part in parts if str(part or "").strip()),
        MAX_QUERY_CHARS,
    )
    if not _tokenize(query):
        raise JiraRagIndexError("canonical Jira RAG query contains no searchable terms")
    return query


def _tokenize(text: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(text)]


def _bm25_scores(
    query_tokens: list[str],
    documents: list[dict[str, Any]],
) -> list[tuple[float, dict[str, Any]]]:
    tokenized = [_tokenize(item["text"]) for item in documents]
    if not tokenized:
        return []
    average_length = sum(len(tokens) for tokens in tokenized) / len(tokenized)
    average_length = average_length or 1.0
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))

    query_terms = Counter(query_tokens)
    scored: list[tuple[float, dict[str, Any]]] = []
    document_count = len(documents)
    for item, tokens in zip(documents, tokenized):
        term_frequency = Counter(tokens)
        document_length = len(tokens)
        score = 0.0
        for term, query_frequency in query_terms.items():
            frequency = term_frequency.get(term, 0)
            if not frequency:
                continue
            frequency_in_documents = document_frequency[term]
            inverse_document_frequency = math.log(
                1.0
                + (document_count - frequency_in_documents + 0.5)
                / (frequency_in_documents + 0.5)
            )
            denominator = frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * document_length / average_length
            )
            score += (
                query_frequency
                * inverse_document_frequency
                * frequency
                * (BM25_K1 + 1.0)
                / denominator
            )
        if score > 0:
            scored.append((score, item))
    return scored


def retrieve_jira_memory(
    runtime_request: dict[str, Any],
    *,
    index_path: str | Path,
    expected_index_sha256: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Return deterministic top-k Jira evidence plus complete safe provenance."""

    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        raise JiraRagIndexError(f"top_k must be an integer between 1 and {MAX_TOP_K}")
    current_ticket = _require_string(
        (runtime_request.get("jira_metadata") or {}).get("ticket_id"),
        "current ticket_id",
        pattern=_TICKET_RE,
    )
    query = build_retrieval_query(runtime_request)
    frozen = load_frozen_index(
        index_path,
        expected_sha256=expected_index_sha256,
    )
    candidates = [
        item for item in frozen.documents if item["source_ticket_id"] != current_ticket
    ]
    scored = _bm25_scores(_tokenize(query), candidates)
    scored.sort(key=lambda item: (-item[0], item[1]["memory_id"]))

    memories = []
    source_ticket_counts: Counter[str] = Counter()
    for score, item in scored:
        source_ticket_id = item["source_ticket_id"]
        if source_ticket_counts[source_ticket_id] >= MAX_MEMORIES_PER_SOURCE_TICKET:
            continue
        rank = len(memories) + 1
        memories.append(
            {
                **item,
                "rank": rank,
                "score": round(score, 8),
            }
        )
        source_ticket_counts[source_ticket_id] += 1
        if len(memories) >= top_k:
            break

    return {
        "enabled": True,
        "status": "ok" if memories else "empty",
        "query": query,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "retriever_name": RETRIEVER_NAME,
        "retriever_version": RETRIEVER_VERSION,
        "corpus_id": frozen.corpus_id,
        "corpus_sha256": frozen.corpus_sha256,
        "index_id": frozen.index_id,
        "index_sha256": frozen.index_sha256,
        "exclusion_list_id": frozen.exclusion_list_id,
        "exclusion_list_sha256": frozen.exclusion_list_sha256,
        "cutoff_at": frozen.cutoff_at,
        "requested_top_k": top_k,
        "max_memories_per_source_ticket": MAX_MEMORIES_PER_SOURCE_TICKET,
        "candidate_count": len(candidates),
        "memories": memories,
    }
