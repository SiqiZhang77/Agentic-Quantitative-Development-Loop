#!/usr/bin/env python3
"""Build an immutable Jira RAG index from approved normalized JSONL chunks."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


DAGS_DIR = Path(__file__).resolve().parents[1] / "dags"
if str(DAGS_DIR) not in sys.path:
    sys.path.insert(0, str(DAGS_DIR))

from jira_rag_retriever import (  # noqa: E402
    JiraRagIndexError,
    MAX_INDEX_BYTES,
    build_frozen_index_payload,
)


def _read_documents(path: Path) -> list[dict]:
    documents = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JiraRagIndexError(
                f"{path}:{line_number} is not valid JSON: {exc.msg}"
            ) from exc
        if not isinstance(item, dict):
            raise JiraRagIndexError(f"{path}:{line_number} must be a JSON object")
        documents.append(item)
    return documents


def _read_exclusions(path: Path) -> list[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise JiraRagIndexError(f"{path} is not valid JSON: {exc.msg}") from exc
    if not isinstance(value, list):
        raise JiraRagIndexError(f"{path} must contain a JSON array of Jira keys")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate normalized Jira-memory chunks and write a frozen lexical "
            "index. The output path must not already exist."
        )
    )
    parser.add_argument("--documents-jsonl", type=Path, required=True)
    parser.add_argument("--excluded-ticket-ids", type=Path, required=True)
    parser.add_argument("--cutoff-at", required=True)
    parser.add_argument("--index-id", required=True)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--exclusion-list-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists():
        raise JiraRagIndexError(
            f"refusing to overwrite frozen index output: {args.output}"
        )
    payload = build_frozen_index_payload(
        _read_documents(args.documents_jsonl),
        index_id=args.index_id,
        corpus_id=args.corpus_id,
        exclusion_list_id=args.exclusion_list_id,
        excluded_ticket_ids=_read_exclusions(args.excluded_ticket_ids),
        cutoff_at=args.cutoff_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_INDEX_BYTES:
        raise JiraRagIndexError(
            f"frozen index output exceeds the {MAX_INDEX_BYTES}-byte loader limit"
        )
    args.output.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    print(
        json.dumps(
            {
                "index_path": str(args.output),
                "index_sha256": digest,
                "index_id": payload["index_id"],
                "corpus_id": payload["corpus_id"],
                "corpus_sha256": payload["corpus_sha256"],
                "exclusion_list_id": payload["exclusion_list_id"],
                "exclusion_list_sha256": payload["exclusion_list_sha256"],
                "cutoff_at": payload["cutoff_at"],
                "document_count": len(payload["documents"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JiraRagIndexError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
