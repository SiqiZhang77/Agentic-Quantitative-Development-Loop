#!/usr/bin/env python3
"""Create an answer-free E3V15 evaluator hash manifest.

The collector reads evaluator files only as opaque bytes for SHA-256 hashing.
It never imports or executes them, and it never writes paths, source text,
candidate answers or expected answers to the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


class PrivateBindingError(RuntimeError):
    """A required evaluator file is absent or the output is unsafe."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_file(path: Path, label: str) -> str:
    resolved = path.resolve()
    if not resolved.is_file():
        raise PrivateBindingError(f"required evaluator file is missing: {label}")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect(
    *,
    private_item_scorer: Path,
    private_base_scorer: Path,
    evaluator_coordinator: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "exp3-e3v15-private-evaluator-hashes-v1",
        "task_suite_version": "t3-quant-suite-v2",
        "private_item_scorer_sha256": _sha256_file(
            private_item_scorer, "private_item_scorer"
        ),
        "private_base_scorer_sha256": _sha256_file(
            private_base_scorer, "private_base_scorer"
        ),
        "evaluator_coordinator_sha256": _sha256_file(
            evaluator_coordinator, "evaluator_coordinator"
        ),
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "contains_private_paths": False,
        "contains_evaluator_source": False,
        "contains_candidate_or_expected_answers": False,
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    resolved = path.resolve()
    if resolved.exists():
        raise PrivateBindingError(f"refusing to overwrite existing output: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(_canonical(value) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-item-scorer", type=Path, required=True)
    parser.add_argument("--private-base-scorer", type=Path, required=True)
    parser.add_argument("--evaluator-coordinator", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(
            private_item_scorer=args.private_item_scorer,
            private_base_scorer=args.private_base_scorer,
            evaluator_coordinator=args.evaluator_coordinator,
        )
        _write(args.output, result)
    except PrivateBindingError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "provider_call_count": 0,
                    "issue": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps({"status": "hash_only_manifest_written", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
