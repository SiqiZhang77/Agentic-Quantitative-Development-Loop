#!/usr/bin/env python3
"""Resolve E4V1 against an already frozen shared runtime and RAG context.

This control performs no model or evaluator call.  It copies only the
model-visible retrieval context and hash-only evaluator manifest into a new
control package outside Git, then binds their exact bytes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import identity
except ImportError:  # pragma: no cover
    import identity  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[1]
REMOTE_NAME = "github"


class FreezeConfigError(RuntimeError):
    """The candidate E4 freeze cannot be reproduced from fixed inputs."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeConfigError(f"cannot read required JSON: {path}") from exc
    if not isinstance(value, dict):
        raise FreezeConfigError(f"required JSON root is not an object: {path}")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value) + b"\n")


def _git(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=CONTROL_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise FreezeConfigError(f"control Git check failed: {detail}")
    return completed.stdout.strip()


def _remote_head(ref: str) -> str | None:
    completed = subprocess.run(
        ["git", "ls-remote", "--heads", REMOTE_NAME, f"refs/heads/{ref}"],
        cwd=CONTROL_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise FreezeConfigError("remote control/source identity check failed")
    return completed.stdout.split()[0] if completed.stdout.strip() else None


def _outside_worktree(path: Path) -> None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            raise FreezeConfigError("formal freeze output must be outside every Git worktree")


def _copy_runtime_bindings(config: dict[str, Any], runtime: dict[str, Any]) -> None:
    source = runtime.get("source") or {}
    runtime_binding = runtime.get("runtime") or {}
    suite = runtime.get("task_suite") or {}
    if source.get("repository") != "bankingscience/BSLAgenticQuantDevLoop":
        raise FreezeConfigError("shared runtime repository identity differs")
    config["source"] = copy.deepcopy(source)
    for field in (
        "image_ref",
        "image_digest",
        "runtime_request_schema_sha256",
        "runtime_response_schema_sha256",
        "tool_implementation_sha256",
        "model_visible_tool_fingerprint_sha256",
        "provider_identity_sha256",
        "provider_mode",
        "provider_base_url",
        "model_alias",
    ):
        config["runtime"][field] = runtime_binding.get(field)
    config["runtime"]["item_submission_schema_sha256"] = suite.get(
        "item_submission_schema_sha256"
    )

    t3 = config["task_bindings"]["T3"]
    mapping = {
        "task_text_sha256": "task_sha256",
        "rubric_sha256": "rubric_sha256",
        "item_submission_schema_sha256": "item_submission_schema_sha256",
        "output_schema_sha256": "output_schema_sha256",
    }
    for target, source_name in mapping.items():
        t3[target] = suite.get(source_name)
    source_inputs = {row.get("path"): row.get("sha256") for row in suite.get("public_inputs", [])}
    for row in t3["public_inputs"]:
        row["sha256"] = source_inputs.get(row["path"])
    contract = t3["result_contract"]
    contract["scorer_result_schema_sha256"] = suite.get(
        "scorer_result_schema_sha256"
    )
    contract["scorer_result_validator_sha256"] = suite.get(
        "scorer_result_validator_sha256"
    )

    if config["shared_budget"] != runtime.get("shared_budget"):
        raise FreezeConfigError("E4 budget differs from the frozen shared runtime budget")
    if config["manager_star_role_budget"] != runtime.get("manager_star_role_budget"):
        raise FreezeConfigError("E4 manager-star role budget differs from the shared runtime")


def _copy_evaluator_bindings(
    config: dict[str, Any],
    evaluator: dict[str, Any],
    evaluator_path: Path,
    runtime_evaluation: dict[str, Any],
) -> None:
    required = {
        "private_item_scorer_sha256",
        "private_base_scorer_sha256",
        "evaluator_coordinator_sha256",
    }
    if not required.issubset(evaluator):
        raise FreezeConfigError("private evaluator hash manifest is incomplete")
    target = config["evaluator_bindings"]
    target["T3"] = evaluator["private_item_scorer_sha256"]
    target["private_base_scorer_sha256"] = evaluator[
        "private_base_scorer_sha256"
    ]
    target["coordinator_sha256"] = evaluator["evaluator_coordinator_sha256"]
    target["visible_response_trace_schema_sha256"] = runtime_evaluation.get(
        "visible_response_trace_schema_sha256"
    )
    target["item_store_projection_sha256"] = runtime_evaluation.get(
        "item_store_projection_sha256"
    )
    target["private_evaluator_manifest_sha256"] = _sha256_file(evaluator_path)


def _bind_retrieval(
    config: dict[str, Any], request: dict[str, Any], staging: Path, output: Path
) -> None:
    parameters = (
        (request.get("execution_objectives") or {}).get("parsed_task_parameters") or {}
    )
    context = request.get("retrieval_context")
    if parameters.get("rag_enabled") is not True or not isinstance(context, dict):
        raise FreezeConfigError("retrieval source request is not a RAG-on request")
    memories = context.get("memories")
    query = context.get("query")
    if (
        context.get("enabled") is not True
        or not isinstance(memories, list)
        or not 1 <= len(memories) <= 5
        or not isinstance(query, str)
        or not query
    ):
        raise FreezeConfigError("retrieval context identity, query or memory count is invalid")
    repositories = request.get("repository_details") or []
    task = config["task_bindings"]["T3"]
    if (
        len(repositories) != 1
        or repositories[0].get("source_branch") != config["source"]["source_ref"]
        or parameters.get("model") != config["runtime"]["model_alias"]
        or parameters.get("answer_capture_profile") != task["answer_capture_profile"]
        or context.get("query_sha256") != _sha256_bytes(query.encode("utf-8"))
    ):
        raise FreezeConfigError("retrieval source request differs from the shared T3 boundary")
    context_bytes = _canonical(context)
    query_bytes = query.encode("utf-8")
    (staging / "retrieval_context.json").write_bytes(context_bytes)
    (staging / "retrieval_query.txt").write_bytes(query_bytes)

    retrieval = config["retrieval_binding"]
    for field in (
        "retriever_name",
        "retriever_version",
        "corpus_id",
        "corpus_sha256",
        "index_id",
        "index_sha256",
        "exclusion_list_id",
        "exclusion_list_sha256",
        "cutoff_at",
        "requested_top_k",
        "max_memories_per_source_ticket",
    ):
        retrieval[field] = context.get(field)
    if retrieval["requested_top_k"] != 5 or retrieval[
        "max_memories_per_source_ticket"
    ] != 2:
        raise FreezeConfigError("retrieval size policy differs from E4V1")
    retrieval["task_query_context"]["T3"] = {
        "query_path": str((output / "retrieval_query.txt").resolve()),
        "query_sha256": _sha256_bytes(query_bytes),
        "retrieval_context_path": str((output / "retrieval_context.json").resolve()),
        "retrieval_context_sha256": _sha256_bytes(context_bytes),
        "retrieval_context_byte_count": len(context_bytes),
        "memory_count": len(memories),
    }


def build(
    *,
    template_path: Path,
    shared_runtime_config: Path,
    public_bindings: Path,
    private_evaluator_manifest: Path,
    retrieval_request: Path,
    control_ref: str,
    control_commit: str,
    output: Path,
) -> dict[str, Any]:
    output = output.resolve()
    _outside_worktree(output)
    if output.exists():
        raise FreezeConfigError(f"refusing to overwrite freeze package: {output}")
    if _git(["rev-parse", "HEAD"]) != control_commit:
        raise FreezeConfigError("control checkout HEAD differs from --control-commit")
    if _git(["status", "--porcelain"]):
        raise FreezeConfigError("control checkout must be clean before formal binding")

    config = identity.load_config(template_path)
    runtime = _load_json(shared_runtime_config)
    public = _load_json(public_bindings)
    evaluator = _load_json(private_evaluator_manifest)
    request = _load_json(retrieval_request)
    _copy_runtime_bindings(config, runtime)
    runtime_evaluation = runtime.get("evaluation") or {}
    _copy_evaluator_bindings(
        config,
        evaluator,
        private_evaluator_manifest,
        runtime_evaluation,
    )
    runtime_control = runtime.get("control") or {}
    if _sha256_file(public_bindings) != runtime_control.get(
        "public_binding_inventory_sha256"
    ):
        raise FreezeConfigError("public binding inventory differs from shared runtime")
    if _sha256_file(private_evaluator_manifest) != runtime_evaluation.get(
        "private_evaluator_manifest_sha256"
    ):
        raise FreezeConfigError("private evaluator manifest differs from shared runtime")
    if not public:
        raise FreezeConfigError("public binding inventory is empty")
    if _remote_head(config["source"]["source_ref"]) != config["source"]["source_commit"]:
        raise FreezeConfigError("remote shared runtime source ref is absent or drifted")
    if _remote_head(control_ref) != control_commit:
        raise FreezeConfigError("remote E4 control ref is absent or drifted")

    config["control"]["control_ref"] = control_ref
    config["control"]["control_commit"] = control_commit
    for row in config["control"]["control_files"]:
        path = CONTROL_ROOT / row["path"]
        if not path.is_file():
            raise FreezeConfigError(f"E4 control file is missing: {row['path']}")
        row["sha256"] = _sha256_file(path)
    config["freeze_status"] = "ready_for_freeze"
    config["execution"]["agreement_generation_enabled"] = True

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        _bind_retrieval(config, request, staging, output)
        identity.validate_config(config)
        _write_json(staging / "freeze_config.json", config)
        evidence = staging / "evidence"
        evidence.mkdir()
        shutil.copy2(public_bindings, evidence / "public_bindings.json")
        shutil.copy2(
            private_evaluator_manifest, evidence / "private_evaluator_hashes.json"
        )
        receipt = {
            "schema_version": "exp4-e4v1-freeze-receipt-v1",
            "status": "ready_for_agreement",
            "provider_call_count": 0,
            "formal_observation_count": 0,
            "source_commit": config["source"]["source_commit"],
            "control_commit": control_commit,
            "image_digest": config["runtime"]["image_digest"],
            "retrieval_context_sha256": config["retrieval_binding"][
                "task_query_context"
            ]["T3"]["retrieval_context_sha256"],
            "public_bindings_sha256": _sha256_file(public_bindings),
            "private_evaluator_manifest_sha256": _sha256_file(
                private_evaluator_manifest
            ),
        }
        _write_json(staging / "freeze_receipt.json", receipt)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    resolved = identity.load_config(output / "freeze_config.json")
    issues = identity.readiness_issues(resolved)
    if issues:
        raise FreezeConfigError("resolved E4 freeze is not ready: " + "; ".join(issues))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, default=identity.CONFIG_PATH)
    parser.add_argument("--shared-runtime-config", type=Path, required=True)
    parser.add_argument("--public-bindings", type=Path, required=True)
    parser.add_argument("--private-evaluator-manifest", type=Path, required=True)
    parser.add_argument("--retrieval-request", type=Path, required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(
            template_path=args.template.resolve(),
            shared_runtime_config=args.shared_runtime_config.resolve(),
            public_bindings=args.public_bindings.resolve(),
            private_evaluator_manifest=args.private_evaluator_manifest.resolve(),
            retrieval_request=args.retrieval_request.resolve(),
            control_ref=args.control_ref,
            control_commit=args.control_commit,
            output=args.output,
        )
    except Exception as exc:
        print(json.dumps({
            "schema_version": "exp4-e4v1-freeze-status-v1",
            "status": "blocked",
            "provider_call_count": 0,
            "formal_observation_count": 0,
            "issue": str(exc),
        }, sort_keys=True))
        return 2
    print(json.dumps({**result, "output": str(args.output.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
