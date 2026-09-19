#!/usr/bin/env python3
"""Create a deterministic, privacy-aware manual review package for Jira RAG documents."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any


REVIEW_PACKAGE_VERSION = "1.0.0"
EXPECTED_FIELDS = {
    "memory_id",
    "source_ticket_id",
    "source_type",
    "source_id",
    "source_timestamp",
    "text",
}
EMAIL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
URL_RE = re.compile(r"(?i)https?://")
SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|token|authorization|password|secret)"
    r"\s*[:=]\s*\S+"
)
LONG_HEX_RE = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class ReviewPackageError(ValueError):
    """Raised when a review package cannot be created safely."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_documents(path: Path) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReviewPackageError(f"cannot read documents file {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewPackageError(
                f"documents line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(value, dict) or set(value) != EXPECTED_FIELDS:
            raise ReviewPackageError(
                f"documents line {line_number} must contain exactly the six index fields"
            )
        if any(not isinstance(value[field], str) or not value[field] for field in EXPECTED_FIELDS):
            raise ReviewPackageError(
                f"documents line {line_number} contains an empty or non-string field"
            )
        documents.append(value)
    memory_ids = [item["memory_id"] for item in documents]
    if not documents or len(memory_ids) != len(set(memory_ids)):
        raise ReviewPackageError("documents must be non-empty with unique memory IDs")
    return documents


def _payload_after_label(text: str) -> str:
    if ":" not in text:
        return text.strip()
    return text.split(":", 1)[1].strip()


def _flags(text: str, source_type: str, sensitive_literals: list[str]) -> list[str]:
    flags: list[str] = []
    if EMAIL_RE.search(text):
        flags.append("email_candidate")
    if URL_RE.search(text):
        flags.append("url_candidate")
    if SECRET_RE.search(text):
        flags.append("secret_assignment_candidate")
    if LONG_HEX_RE.search(text):
        flags.append("long_hex_candidate")
    for literal in sensitive_literals:
        if re.search(rf"(?<!\w){re.escape(literal)}(?!\w)", text, re.IGNORECASE):
            flags.append("reviewed_sensitive_literal")
            break
    if "[PERSON]" in text:
        flags.append("person_placeholder_present")
    if "[TRUNCATED]" in text:
        flags.append("truncated")
    if source_type in {"attachment_metadata", "status_change"}:
        flags.append("low_value_source_type_review")
    payload = _payload_after_label(text)
    if len(payload) <= 20 and len(TOKEN_RE.findall(payload)) <= 1:
        flags.append("trivial_payload")
    return flags


def _preliminary_decision(flags: list[str]) -> str:
    if any(
        flag in flags
        for flag in {
            "email_candidate",
            "url_candidate",
            "secret_assignment_candidate",
            "long_hex_candidate",
            "reviewed_sensitive_literal",
        }
    ):
        return "requires_redaction"
    if "trivial_payload" in flags:
        return "exclude_low_information_candidate"
    if "truncated" in flags:
        return "requires_content_review"
    return "retain_candidate"


def create_review_package(
    *,
    documents_path: Path,
    output_dir: Path,
    sample_size: int,
    seed: int,
    sensitive_literals: list[str],
) -> dict[str, Any]:
    if output_dir.exists():
        raise ReviewPackageError(f"output directory already exists: {output_dir}")
    documents = _read_documents(documents_path)
    if not 1 <= sample_size <= len(documents):
        raise ReviewPackageError(
            f"sample_size must be between 1 and {len(documents)}"
        )
    normalized_literals = sorted(
        {item.strip() for item in sensitive_literals if item.strip()},
        key=str.casefold,
    )
    selected = random.Random(seed).sample(documents, sample_size)
    ticket_sample_counts = Counter(item["source_ticket_id"] for item in selected)
    rows: list[dict[str, Any]] = []
    for sample_number, document in enumerate(selected, 1):
        flags = _flags(
            document["text"], document["source_type"], normalized_literals
        )
        rows.append(
            {
                "sample_number": sample_number,
                **document,
                "automated_flags": flags,
                "assistant_preliminary_decision": _preliminary_decision(flags),
                "human_decision": "",
                "human_notes": "",
            }
        )

    temporary = output_dir.with_name(output_dir.name + ".tmp")
    if temporary.exists():
        raise ReviewPackageError(f"temporary output already exists: {temporary}")
    temporary.mkdir(parents=True, mode=0o700)
    try:
        sample_path = temporary / "sample.jsonl"
        sample_path.write_text(
            "".join(_canonical_json(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        review_path = temporary / "review.csv"
        fieldnames = [
            "sample_number",
            "memory_id",
            "source_ticket_id",
            "source_type",
            "source_id",
            "source_timestamp",
            "text",
            "automated_flags",
            "assistant_preliminary_decision",
            "human_decision",
            "human_notes",
        ]
        with review_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                csv_row = dict(row)
                csv_row["automated_flags"] = ";".join(row["automated_flags"])
                writer.writerow({field: csv_row[field] for field in fieldnames})

        flag_counts = Counter(flag for row in rows for flag in row["automated_flags"])
        decision_counts = Counter(row["assistant_preliminary_decision"] for row in rows)
        source_type_counts = Counter(row["source_type"] for row in rows)
        audit = {
            "schema_version": "jira-corpus-review-audit-v1",
            "review_package_version": REVIEW_PACKAGE_VERSION,
            "documents_sha256": _sha256_bytes(documents_path.read_bytes()),
            "document_count": len(documents),
            "sample_seed": seed,
            "sample_size": sample_size,
            "sampled_unique_memory_count": len({row["memory_id"] for row in rows}),
            "sampled_unique_ticket_count": len(ticket_sample_counts),
            "source_type_counts": dict(sorted(source_type_counts.items())),
            "ticket_concentration_top_10": ticket_sample_counts.most_common(10),
            "automated_flag_counts": dict(sorted(flag_counts.items())),
            "assistant_preliminary_decision_counts": dict(sorted(decision_counts.items())),
            "sensitive_literal_sha256s": [
                _sha256_bytes(item.casefold().encode("utf-8"))
                for item in normalized_literals
            ],
            "manual_review_status": "pending_human_confirmation",
        }
        (temporary / "audit.json").write_text(
            json.dumps(audit, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary / "README.md").write_text(
            "# Jira corpus review package\n\n"
            "This package is a deterministic simple-random sample for privacy and "
            "retrieval-quality review. `review.csv` is the human-review worksheet; "
            "the assistant decision is preliminary and the two human columns must be "
            "completed before formal corpus freeze. `sample.jsonl` preserves the exact "
            "machine-readable sample. `audit.json` records the seed, input digest, "
            "coverage, and automated flags.\n",
            encoding="utf-8",
        )
        output_names = ["README.md", "audit.json", "review.csv", "sample.jsonl"]
        manifest = {
            "schema_version": "jira-corpus-review-manifest-v1",
            "review_package_version": REVIEW_PACKAGE_VERSION,
            "documents_sha256": audit["documents_sha256"],
            "sample_seed": seed,
            "sample_size": sample_size,
            "generator_script_sha256": _sha256_bytes(Path(__file__).read_bytes()),
            "file_sha256": {
                name: _sha256_bytes((temporary / name).read_bytes())
                for name in output_names
            },
        }
        (temporary / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for path in temporary.iterdir():
            path.chmod(0o600)
        os.chmod(temporary, 0o700)
        temporary.rename(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return {"output_dir": str(output_dir), **audit}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a deterministic Jira RAG corpus review package."
    )
    parser.add_argument("--documents-jsonl", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--sensitive-literal", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = create_review_package(
        documents_path=args.documents_jsonl,
        output_dir=args.output_dir,
        sample_size=args.sample_size,
        seed=args.seed,
        sensitive_literals=args.sensitive_literal,
    )
    print(_canonical_json(result))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReviewPackageError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(2) from exc
