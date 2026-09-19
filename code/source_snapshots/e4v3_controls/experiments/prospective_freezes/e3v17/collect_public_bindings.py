#!/usr/bin/env python3
"""Collect only the public, reproducible E3V17 bindings.

This is deliberately *not* an agreement builder.  It proves the source tree,
local immutable image and model-visible public files that a later agreement
would use, while leaving evaluator commitments unresolved.  It never reads an
evaluator, RAG corpus, API key, answer key or model output, and it never calls
a model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


PUBLIC_FILES = (
    "experiments/shared/t3-quant-suite-v2/TASK.md",
    "experiments/shared/t3-quant-suite-v2/RUBRIC.md",
    "experiments/shared/t3-quant-suite-v2/input/synthetic_portfolio_returns_v1.csv",
    "experiments/shared/t3-quant-suite-v2/input/portfolio_config_v1.json",
    "experiments/shared/t3-quant-suite-v2/item_submission_schema_v2.json",
    "experiments/shared/t3-quant-suite-v2/output_schema_v1.json",
    "experiments/shared/t3-quant-suite-v2/scorer_result_schema_v2.json",
    "experiments/shared/t3-quant-suite-v2/scorer_result_validator.py",
)
RUNTIME_FILES = (
    "rae_runtime/sandbox/schemas/runtime_request.schema.json",
    "rae_runtime/sandbox/schemas/runtime_response.schema.json",
    "rae_runtime/proxy/quant_calculator.py",
    "rae_runtime/proxy/t3_quant_item_submission.py",
    "rae_runtime/proxy/github_mcp_server.py",
    "rae_runtime/proxy/pipeline_mcp.py",
    "rae_runtime/proxy/exp3/agents_runtime.py",
    "rae_runtime/proxy/exp3/policy.py",
    "rae_runtime/proxy/exp3/role_adapters.py",
    "rae_runtime/proxy/research_transcript.py",
)
EXECUTION_CONTROL_FILES = (
    "rae_runtime/proxy/exp3/attempt_control.py",
    "rae_runtime/proxy/exp3/budget.py",
    "rae_runtime/proxy/exp3/telemetry.py",
    "rae_runtime/proxy/exp3/router.py",
    "rae_runtime/proxy/exp3/agents_runtime.py",
    "rae_runtime/proxy/exp3/production_provider.py",
    "rae_runtime/sandbox/iteration_loop.py",
    "rae_runtime/sandbox/run.py",
)
TRACE_SCHEMA_PATH = "experiments/prospective_freezes/e3v17/model_visible_trace_schema_v1.json"
CONTROL_ROOT = Path(__file__).resolve().parents[3]


class BindingError(RuntimeError):
    """The local evidence is not the source/image identity claimed by the user."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_hashes(root: Path, paths: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise BindingError(f"required public file is missing: {relative}")
        result[relative] = _sha256_bytes(path.read_bytes())
    return result


def _run(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise BindingError(f"command failed ({' '.join(args[:3])}): {detail}")
    return completed.stdout.strip()


def _clean_commit(source_tree: Path, expected: str) -> None:
    actual = _run(["git", "rev-parse", "HEAD"], cwd=source_tree)
    if actual != expected:
        raise BindingError(f"source commit mismatch: expected {expected}, found {actual}")
    if _run(["git", "status", "--porcelain"], cwd=source_tree):
        raise BindingError("source tree is dirty; bind a clean published source tree instead")


def _image_id(image_ref: str) -> str:
    value = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image_ref], cwd=Path.cwd())
    if not value.startswith("sha256:") or len(value) != 71:
        raise BindingError("Docker did not return one immutable sha256 image ID")
    return value


def _expected_digest(image_ref: str) -> str | None:
    """Return the digest embedded in a canonical repository@digest reference."""

    marker = "@sha256:"
    if marker not in image_ref:
        return None
    digest = "sha256:" + image_ref.rsplit(marker, 1)[1]
    if len(digest) != 71 or any(char not in "0123456789abcdef" for char in digest[7:]):
        raise BindingError("image_ref contains an invalid sha256 digest")
    return digest


def collect(
    *,
    source_tree: Path,
    source_ref: str,
    source_commit: str,
    image_ref: str,
    image_lookup_ref: str | None = None,
) -> dict[str, Any]:
    source_tree = source_tree.resolve()
    _clean_commit(source_tree, source_commit)
    public = _file_hashes(source_tree, PUBLIC_FILES)
    runtime = _file_hashes(source_tree, RUNTIME_FILES)
    execution_control_files = _file_hashes(source_tree, EXECUTION_CONTROL_FILES)
    trace_schema = _file_hashes(CONTROL_ROOT, (TRACE_SCHEMA_PATH,))[TRACE_SCHEMA_PATH]
    request_schema = runtime["rae_runtime/sandbox/schemas/runtime_request.schema.json"]
    response_schema = runtime["rae_runtime/sandbox/schemas/runtime_response.schema.json"]
    tool_files = {path: runtime[path] for path in RUNTIME_FILES[2:]}
    tool_implementation = _sha256_bytes(_canonical(tool_files))
    tool_fingerprint = {
        "contract_version": "shared-t3-tool-contract-v2",
        "tools": ["quant_calculate", "submit_t3_items", "submit_calculation_checkpoint", "commit_calculation_artifact"],
        "tool_implementation_sha256": tool_implementation,
        "item_submission_schema_sha256": public[
            "experiments/shared/t3-quant-suite-v2/item_submission_schema_v2.json"
        ],
        "task_sha256": public["experiments/shared/t3-quant-suite-v2/TASK.md"],
    }
    provider = {
        "schema_version": "llm-provider-identity-v1",
        "provider_mode": "openai",
        "base_url": "https://api.openai.com/v1",
        "model_alias": "gpt-5.6-terra",
        "transport_model": "gpt-5.6-terra",
        "adapter": "openai_chat_completions",
    }
    lookup_ref = image_lookup_ref or image_ref
    actual_image_id = _image_id(lookup_ref)
    expected_image_id = _expected_digest(image_ref)
    if expected_image_id is not None and actual_image_id != expected_image_id:
        raise BindingError(
            "Docker image ID mismatch: "
            f"canonical reference requires {expected_image_id}, found {actual_image_id}"
        )
    return {
        "schema_version": "shared-t3-e3v17-public-binding-inventory-v1",
        "status": "public_bindings_verified_private_bindings_unresolved",
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "source": {"source_ref": source_ref, "source_commit": source_commit},
        "runtime": {
            "image_ref": image_ref,
            "image_digest": actual_image_id,
            "image_lookup_ref": lookup_ref,
            "runtime_request_schema_sha256": request_schema,
            "runtime_response_schema_sha256": response_schema,
            "tool_implementation_sha256": tool_implementation,
            "execution_control_sha256": _sha256_bytes(
                _canonical(execution_control_files)
            ),
            "model_visible_tool_fingerprint_sha256": _sha256_bytes(_canonical(tool_fingerprint)),
            "provider_identity_sha256": _sha256_bytes(_canonical(provider)),
            "tool_implementation_files": tool_files,
            "execution_control_files": execution_control_files,
            "model_visible_tool_fingerprint": tool_fingerprint,
            "provider_identity": provider,
        },
        "public_files": public,
        "control": {
            "visible_response_trace_schema_path": TRACE_SCHEMA_PATH,
            "visible_response_trace_schema_sha256": trace_schema,
        },
        "still_required_before_agreement": [
            "private evaluator hash-only manifest for t3-quant-suite-v2",
            "reviewed agreement-generation control package",
        ],
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise BindingError(f"refusing to overwrite existing output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-tree", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image-ref", required=True)
    parser.add_argument(
        "--image-lookup-ref",
        help=(
            "optional local Docker lookup key, normally the full raw sha256 image ID; "
            "the returned ID must still match the digest embedded in --image-ref"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(
            source_tree=args.source_tree,
            source_ref=args.source_ref,
            source_commit=args.source_commit,
            image_ref=args.image_ref,
            image_lookup_ref=args.image_lookup_ref,
        )
        _write(args.output.resolve(), result)
    except BindingError as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
