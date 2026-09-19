#!/usr/bin/env python3
"""Build the private six-observation E2 Terra T3 C0/C1 v5 control package.

The builder performs no provider call. It writes retrieval-bearing requests and
the condition map only to an operator-selected directory outside every Git
worktree. Private scorer contents are never copied into the package.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
EXP2_ROOT = HERE.parent
REPO_ROOT = EXP2_ROOT.parents[1]
PROJECT_ROOT = REPO_ROOT.parents[1]
SOURCE_TREE = PROJECT_ROOT / "01_CODE" / "e2-t3-rag-source-v5"
T3_RELATIVE_ROOT = Path("experiments/shared/t3-quant-suite-v1")
T3_ROOT = SOURCE_TREE / T3_RELATIVE_ROOT
T3_TARGET_PATH = str(
    T3_RELATIVE_ROOT / "submissions" / "quant_portfolio_analytics.json"
)
T3_SCHEMA_PATH = str(T3_RELATIVE_ROOT / "output_schema_v1.json")
REQUEST_SCHEMA_PATH = (
    SOURCE_TREE / "rae_runtime" / "sandbox" / "schemas" / "runtime_request.schema.json"
)
NEGATIVE_MANIFEST_SCHEMA_PATH = (
    SOURCE_TREE
    / "rae_runtime"
    / "proxy"
    / "exp3"
    / "schemas"
    / "negative_ref_manifest_v2.schema.json"
)
GATEWAY_DAGS = REPO_ROOT / "jira-chatops-gateway" / "dags"
PROTOCOL_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "protocols"
    / "amendments"
    / "e2-t3-rag-v5"
)
PROTOCOL_PATH = PROTOCOL_ROOT / "E2_T3_RAG_V5_PROSPECTIVE_STUDY.md"
CANONICAL_TASK_PATH = PROTOCOL_ROOT / "T3_PUBLIC_TASK_E2_T3_RAG_V5.txt"
INDEX_PATH = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "exp2-private"
    / "exp2"
    / "e2-freeze-v1"
    / "e2-jira-index-v1.json"
)
DEFAULT_SALT_PATH = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "exp2-private"
    / "exp2"
    / "formal-v2"
    / "secrets"
    / "blinding_salt_v1.txt"
)
PRIVATE_SCORER_PATH = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "e3v6-private"
    / "tools"
    / "score_t3_quant_25_two_attempt.py"
)

SOURCE_REF = "exp2/terra-t3-rag-source-v5"
SOURCE_COMMIT = "b8e1b175fe6a89003faca5e6703e513e9643dee5"
CONTROL_REF = "exp2/terra-t3-rag-control-v5"
IMAGE_DIGEST = "sha256:b2d7e0fed6ae1f71e64c815b3f8a16866399ce3b4e39d826633791376d159a31"
REPO_FULL_NAME = "bankingscience/BSLAgenticQuantDevLoop"
INDEX_ID = "e2-jira-index-v1"
INDEX_SHA256 = "72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1"
PRIVATE_SCORER_SHA256 = "08569edbfc4d9a253336bd0abdfc15ef3ad5005861a88f8a5cc9bbd4bab01919"
PROVIDER_MODE = "openai"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
MODEL_ALIAS = "gpt-5.6-terra"
EXPERIMENT_ID = "E2-terra-t3-rag-v5"
EXECUTION_EPOCH = "E2T3RAGV5"
TARGET_PREFIX = f"quant/{EXECUTION_EPOCH}-"
PAIR_ORDER = {1: ("C0", "C1"), 2: ("C1", "C0"), 3: ("C0", "C1")}
FIXED_TIMESTAMP = "2026-09-01T00:00:00Z"
AGREEMENT_SCHEMA = "exp2-terra-t3-rag-agreement-v5"
AGREEMENT_DIGEST_SCHEMA = "exp2-terra-t3-rag-agreement-digest-v5"
EVALUATOR_SCHEMA = "exp2-terra-t3-private-evaluator-hashes-v2"
ITERATION_CONTROLS = {
    "allow_iteration": True,
    "max_iterations": 2,
    "max_failed_iterations": 2,
    "max_agent_turns": 20,
    "max_token_budget_per_run": 700_000,
    "timeout_seconds": 3_600,
}
SHARED_BUDGET = {
    "max_attempts": 2,
    "max_calls_per_attempt": 20,
    "max_tokens_per_attempt": 350_000,
    "max_calls_per_observation": 40,
    "max_tokens_per_observation": 700_000,
}
PUBLIC_INPUTS = (
    T3_RELATIVE_ROOT / "input" / "synthetic_portfolio_returns_v1.csv",
    T3_RELATIVE_ROOT / "input" / "portfolio_config_v1.json",
    T3_RELATIVE_ROOT / "output_schema_v1.json",
    T3_RELATIVE_ROOT / "RUBRIC.md",
)
TOOL_IMPLEMENTATION_FILES = (
    "rae_runtime/proxy/github_mcp_server.py",
    "rae_runtime/proxy/pipeline_mcp.py",
    "rae_runtime/proxy/quant_calculator.py",
    "rae_runtime/proxy/quant_calculator_audit.py",
    "rae_runtime/proxy/t3_quant_checkpoints.py",
    "rae_runtime/proxy/exp3/architecture.py",
    "rae_runtime/proxy/exp3/attempt_control.py",
    "rae_runtime/proxy/exp3/isolation.py",
    "rae_runtime/proxy/exp3/production_provider.py",
    "rae_runtime/proxy/exp3/telemetry.py",
    "rae_runtime/proxy/exp3/schemas/negative_ref_manifest_v2.schema.json",
    "rae_runtime/sandbox/Dockerfile",
    "rae_runtime/sandbox/iteration_loop.py",
    "rae_runtime/sandbox/requirements.txt",
    "rae_runtime/sandbox/result_io.py",
    "rae_runtime/sandbox/run.py",
    "rae_runtime/sandbox/schemas/runtime_request.schema.json",
    "rae_runtime/sandbox/schemas/runtime_response.schema.json",
    "jira-chatops-gateway/schemas/runtime_response.schema.json",
)
CONTROL_FILES = (
    "experiments/exp2/formal-terra-t3-rag-v5/PROTOCOL.md",
    "experiments/exp2/formal-terra-t3-rag-v5/build_control_package.py",
    "experiments/exp2/formal-terra-t3-rag-v5/run_formal_pair.py",
    "experiments/exp2/tests/test_formal_terra_t3_rag_v5_control.py",
)


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def git_worktree_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def require_outside_git(path: Path, label: str) -> None:
    if git_worktree_root(path) is not None:
        raise ValueError(f"{label} must remain outside every Git worktree")


def provider_identity() -> dict[str, str]:
    value = {
        "schema_version": "llm-provider-identity-v1",
        "provider_mode": PROVIDER_MODE,
        "base_url": PROVIDER_BASE_URL,
        "model_alias": MODEL_ALIAS,
        "transport_model": MODEL_ALIAS,
        "adapter": "openai_chat_completions",
    }
    return {**value, "sha256": sha256_bytes(canonical_bytes(value))}


def execution_schedule() -> list[tuple[int, str]]:
    schedule = [
        (replicate, condition)
        for replicate in (1, 2, 3)
        for condition in PAIR_ORDER[replicate]
    ]
    if len(schedule) != 6 or len(set(schedule)) != 6:
        raise AssertionError("E2 T3 schedule must contain three complete C0/C1 pairs")
    return schedule


def neutral_submission_id(salt: bytes, replicate: int, condition: str) -> str:
    message = f"{EXPERIMENT_ID}|T3|R{replicate}|{condition}".encode("utf-8")
    digest = hmac.new(salt, message, hashlib.sha256).hexdigest()
    return "E2T3S-" + digest[:12].upper()


def target_ref(submission_id: str) -> str:
    return TARGET_PREFIX + submission_id


def neutral_ticket_id(submission_id: str) -> str:
    """Return a schema-valid synthetic Jira key without exposing the arm."""

    digest = hashlib.sha256(submission_id.encode("utf-8")).hexdigest()
    return f"E2T3-{int(digest[:12], 16)}"


def remote_heads() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-remote", "--heads", "github"],
        check=True,
        capture_output=True,
        text=True,
    )
    heads: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if line.strip():
            commit, ref = line.split()
            heads[ref.removeprefix("refs/heads/")] = commit
    return heads


def retrieve_for_c1(request: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(GATEWAY_DAGS))
    try:
        from jira_rag_retriever import retrieve_jira_memory
    finally:
        sys.path.pop(0)
    return retrieve_jira_memory(
        request,
        index_path=INDEX_PATH,
        expected_index_sha256=INDEX_SHA256,
        top_k=5,
    )


def _request(
    *,
    replicate: int,
    condition: str,
    submission_id: str,
    target: str,
) -> dict[str, Any]:
    if condition not in {"C0", "C1"}:
        raise ValueError("condition must be C0 or C1")
    task_text = (T3_ROOT / "TASK.md").read_text(encoding="utf-8").strip()
    provider = provider_identity()
    request = {
        "schema_version": "1.0",
        "run_id": submission_id,
        "architecture_mode": "single_agent",
        "repository_details": [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                "source_branch": SOURCE_REF,
                "target_branch": target,
                "runtime_role": "default",
                "allowed_directories": [str(T3_RELATIVE_ROOT)],
            }
        ],
        "jira_metadata": {
            "ticket_id": neutral_ticket_id(submission_id),
            "current_status": "Approved direct-local formal execution",
            "summary": "Experiment 2 frozen T3 portfolio analytics task",
            "description": task_text,
            "triggering_comment": {
                "comment_id": submission_id,
                "timestamp": FIXED_TIMESTAMP,
                "author": "experiment-operator",
                "text": "Execute the complete frozen T3 task using only the declared source, target and tools.",
            },
            "events_history": [
                {
                    "event_type": "comment",
                    "timestamp": FIXED_TIMESTAMP,
                    "author": "experiment-operator",
                    "text": "Execute the complete frozen T3 task using only the declared source, target and tools.",
                }
            ],
        },
        "execution_objectives": {
            "strategy_type": "other",
            "target_date_range": {},
            "parsed_task_parameters": {
                "objective": task_text,
                "model": MODEL_ALIAS,
                "provider_mode": PROVIDER_MODE,
                "provider_base_url": PROVIDER_BASE_URL,
                "provider_identity_sha256": provider["sha256"],
                "formal_execution_contract": "exp2_single_agent_rag_v1",
                "rag_enabled": condition == "C1",
                "rag_top_k": 5,
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": T3_SCHEMA_PATH,
            },
            "system_instruction_override": None,
            "resource_path": str(T3_RELATIVE_ROOT / "TASK.md"),
            "target_path": T3_TARGET_PATH,
        },
        "iteration_controls": copy.deepcopy(ITERATION_CONTROLS),
        "resource_requirements": {
            "cpu_vcpus": 2,
            "memory_mb": 4_096,
            "gpu_count": 0,
            "execution_timeout_seconds": 3_900,
            "pids_limit": 256,
            "yarn_queue": "root.default",
        },
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    validator = Draft202012Validator(
        json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    validator.validate(request)
    return request


def pair_projection(request: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(request)
    value.pop("run_id", None)
    value.pop("retrieval_context", None)
    value["repository_details"][0].pop("target_branch", None)
    value["execution_objectives"]["parsed_task_parameters"].pop("rag_enabled", None)
    metadata = value["jira_metadata"]
    metadata.pop("ticket_id", None)
    metadata["triggering_comment"].pop("comment_id", None)
    return value


def _validate_committed_control_files() -> str:
    """Return HEAD only when every V5 control file is committed at that HEAD."""

    control_commit = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *CONTROL_FILES],
        check=True,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        raise RuntimeError("V5 control files are not committed at local HEAD")
    for path in CONTROL_FILES:
        present = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "-e", f"{control_commit}:{path}"],
            capture_output=True,
            text=True,
        )
        if present.returncode != 0:
            raise RuntimeError(f"V5 control file is absent from local HEAD: {path}")
    return control_commit


def _validate_static_inputs() -> dict[str, Any]:
    if SOURCE_TREE.resolve() != SOURCE_TREE:
        raise RuntimeError("source tree path is not canonical")
    completed = subprocess.run(
        ["git", "-C", str(SOURCE_TREE), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stdout.strip() != SOURCE_COMMIT:
        raise RuntimeError("local mature T3 source does not match the frozen commit")
    dirty = subprocess.run(
        ["git", "-C", str(SOURCE_TREE), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if dirty.stdout.strip():
        raise RuntimeError("local mature T3 source worktree is not clean")
    if not CANONICAL_TASK_PATH.is_file() or not PROTOCOL_PATH.is_file():
        raise RuntimeError("prospective study control files are missing")
    if CANONICAL_TASK_PATH.read_bytes() != (T3_ROOT / "TASK.md").read_bytes():
        raise RuntimeError("control and model-visible T3 task bytes differ")
    if sha256_file(PRIVATE_SCORER_PATH) != PRIVATE_SCORER_SHA256:
        raise RuntimeError("private T3 scorer hash drifted")
    if sha256_file(INDEX_PATH) != INDEX_SHA256:
        raise RuntimeError("frozen Experiment 2 RAG index hash drifted")
    public_hashes = {
        str(path): sha256_file(SOURCE_TREE / path) for path in PUBLIC_INPUTS
    }
    tool_hashes = {
        path: sha256_file(SOURCE_TREE / path) for path in TOOL_IMPLEMENTATION_FILES
    }
    control_hashes = {
        path: sha256_file(REPO_ROOT / path) for path in CONTROL_FILES
    }
    control_commit = _validate_committed_control_files()
    return {
        "task_sha256": sha256_file(T3_ROOT / "TASK.md"),
        "canonical_task_sha256": sha256_file(CANONICAL_TASK_PATH),
        "public_inputs_sha256": public_hashes,
        "tool_implementation_files_sha256": tool_hashes,
        "tool_implementation_sha256": sha256_bytes(canonical_bytes(tool_hashes)),
        "control_files_sha256": control_hashes,
        "control_commit": control_commit,
    }


def build_negative_ref_manifest(
    *,
    submission_id: str,
    current_target: str,
    paired_target: str,
    all_targets: set[str],
    heads: dict[str, str],
) -> tuple[dict[str, Any], str, str]:
    """Create the runtime-native ref deny list and its two frozen hashes."""

    prior_study_refs = sorted(
        set(heads)
        - {SOURCE_REF, CONTROL_REF, current_target, paired_target, "main"}
        - all_targets
    )
    if not prior_study_refs:
        raise RuntimeError("negative-ref manifest requires prior study refs")
    other_result_refs = sorted(all_targets - {current_target, paired_target})
    deny_refs = {
        "prior_study_refs": prior_study_refs,
        "other_result_refs": other_result_refs,
        "protected_refs": ["main"],
        "arbitrary_probe_refs": [
            f"exp2/probe/{EXECUTION_EPOCH}/{submission_id}/a",
            f"exp2/probe/{EXECUTION_EPOCH}/{submission_id}/b",
        ],
    }
    manifest = {
        "schema_version": "rae-negative-ref-manifest-v2",
        "experiment_id": EXPERIMENT_ID,
        "run_id": submission_id,
        "repositories": [
            {
                "repo_full_name": REPO_FULL_NAME,
                "source_ref": SOURCE_REF,
                "current_target_ref": current_target,
                "paired_target_ref": paired_target,
                "deny_refs": deny_refs,
            }
        ],
    }
    Draft202012Validator(
        json.loads(NEGATIVE_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    ).validate(manifest)
    complete_deny_set = sorted(
        {paired_target}
        | {ref for refs in deny_refs.values() for ref in refs}
    )
    deny_identity = [
        {"repo_full_name": REPO_FULL_NAME, "deny_refs": complete_deny_set}
    ]
    return (
        manifest,
        sha256_bytes(canonical_bytes(manifest)),
        sha256_bytes(canonical_bytes(deny_identity)),
    )


def build_package(
    *,
    salt: bytes,
    output: Path,
    heads: dict[str, str],
    retrieve: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    stripped_salt = salt.strip()
    if len(stripped_salt) < 32:
        raise ValueError("blinding salt must contain at least 32 non-whitespace bytes")
    output = output.resolve()
    require_outside_git(output, "private control output")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    if heads.get(SOURCE_REF) != SOURCE_COMMIT:
        raise RuntimeError("published E2 T3 source ref is absent or drifted")

    static = _validate_static_inputs()
    if heads.get(CONTROL_REF) != static["control_commit"]:
        raise RuntimeError("published E2 T3 control ref is absent or drifted")
    schedule = execution_schedule()
    submission_ids = {
        (replicate, condition): neutral_submission_id(
            stripped_salt, replicate, condition
        )
        for replicate, condition in schedule
    }
    targets = {key: target_ref(value) for key, value in submission_ids.items()}
    existing_targets = sorted(set(targets.values()).intersection(heads))
    if existing_targets:
        raise RuntimeError(f"formal target already exists: {existing_targets[0]}")
    remote_inventory = [
        {"ref": ref, "commit": heads[ref]} for ref in sorted(heads)
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    run_entries: list[dict[str, Any]] = []
    private_map: dict[str, Any] = {}
    pair_requests: dict[int, dict[str, dict[str, Any]]] = {}
    retrieval_hashes: set[str] = set()
    try:
        for sequence, (replicate, condition) in enumerate(schedule, start=1):
            submission_id = submission_ids[(replicate, condition)]
            current_target = targets[(replicate, condition)]
            paired_condition = "C1" if condition == "C0" else "C0"
            paired_target = targets[(replicate, paired_condition)]
            request = _request(
                replicate=replicate,
                condition=condition,
                submission_id=submission_id,
                target=current_target,
            )
            retrieval_sha256 = None
            if condition == "C1":
                retrieval = retrieve(request)
                if (
                    retrieval.get("index_id") != INDEX_ID
                    or retrieval.get("index_sha256") != INDEX_SHA256
                    or retrieval.get("enabled") is not True
                    or len(retrieval.get("memories") or []) > 5
                ):
                    raise RuntimeError("C1 retrieval identity or size drifted")
                request["retrieval_context"] = retrieval
                retrieval_sha256 = sha256_bytes(canonical_bytes(retrieval))
                retrieval_hashes.add(retrieval_sha256)
            Draft202012Validator(
                json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
            ).validate(request)
            pair_requests.setdefault(replicate, {})[condition] = request

            (
                negative_manifest,
                negative_manifest_sha256,
                deny_ref_set_sha256,
            ) = build_negative_ref_manifest(
                submission_id=submission_id,
                current_target=current_target,
                paired_target=paired_target,
                all_targets=set(targets.values()),
                heads=heads,
            )
            identity = {
                "schema_version": "exp2-terra-t3-run-identity-v2",
                "experiment_id": EXPERIMENT_ID,
                "execution_epoch": EXECUTION_EPOCH,
                "submission_id": submission_id,
                "task_id": "T3",
                "replicate": replicate,
                "condition": condition,
                "architecture_mode": "single_agent",
                "rag_enabled": condition == "C1",
                "retrieval_context_sha256": retrieval_sha256,
                "source_ref": SOURCE_REF,
                "source_commit": SOURCE_COMMIT,
                "control_ref": CONTROL_REF,
                "control_commit": static["control_commit"],
                "target_ref": current_target,
                "paired_target_ref": paired_target,
                "provider_identity_sha256": provider_identity()["sha256"],
                "image_digest": IMAGE_DIGEST,
                "index_sha256": INDEX_SHA256,
                "task_sha256": static["task_sha256"],
                "tool_implementation_sha256": static["tool_implementation_sha256"],
                "iteration_controls": copy.deepcopy(ITERATION_CONTROLS),
                "shared_budget": copy.deepcopy(SHARED_BUDGET),
                "negative_ref_manifest_sha256": negative_manifest_sha256,
                "deny_ref_set_sha256": deny_ref_set_sha256,
            }
            identity_sha256 = sha256_bytes(canonical_bytes(identity))
            run_dir = staging / "runs" / submission_id
            write_json(run_dir / "request.json", request)
            write_json(run_dir / "identity.json", identity)
            write_json(run_dir / "negative_ref_manifest.json", negative_manifest)
            run_entries.append(
                {
                    "sequence": sequence,
                    "submission_id": submission_id,
                    "task_id": "T3",
                    "replicate": replicate,
                    "pair_id": f"T3-R{replicate}",
                    "target_ref": current_target,
                    "paired_target_ref": paired_target,
                    "identity_sha256": identity_sha256,
                    "negative_ref_manifest_sha256": negative_manifest_sha256,
                    "deny_ref_set_sha256": deny_ref_set_sha256,
                    "files": {
                        name: f"runs/{submission_id}/{name}.json"
                        for name in ("request", "identity", "negative_ref_manifest")
                    },
                    "file_sha256": {
                        name: sha256_file(run_dir / f"{name}.json")
                        for name in ("request", "identity", "negative_ref_manifest")
                    },
                }
            )
            private_map[submission_id] = {
                "task_id": "T3",
                "replicate": replicate,
                "condition": condition,
                "rag_enabled": condition == "C1",
            }

        for replicate, requests in pair_requests.items():
            if set(requests) != {"C0", "C1"}:
                raise AssertionError(f"pair is incomplete: T3-R{replicate}")
            if pair_projection(requests["C0"]) != pair_projection(requests["C1"]):
                raise AssertionError(
                    f"pair differs beyond run, target and RAG treatment: T3-R{replicate}"
                )
        if len(retrieval_hashes) != 1:
            raise RuntimeError("the three C1 requests did not freeze one identical retrieval")

        private_condition_map = {
            "schema_version": "exp2-terra-t3-private-condition-map-v2",
            "salt_sha256": sha256_bytes(stripped_salt),
            "submissions": private_map,
        }
        evaluator_hashes = {
            "schema_version": EVALUATOR_SCHEMA,
            "private_t3_scorer_sha256": PRIVATE_SCORER_SHA256,
        }
        write_json(staging / "blinding" / "private_condition_map.json", private_condition_map)
        write_json(staging / "private_evaluator_hashes.json", evaluator_hashes)
        agreement = {
            "schema_version": AGREEMENT_SCHEMA,
            "experiment_id": EXPERIMENT_ID,
            "execution_epoch": EXECUTION_EPOCH,
            "status": "user_agreed_private_control_ready",
            "approval_authority": "user",
            "scope": "T3_only_RAG_vs_no_RAG",
            "formal_run_count": 6,
            "pair_count": 3,
            "replicates": 3,
            "pair_order": {str(key): list(value) for key, value in PAIR_ORDER.items()},
            "source_ref": SOURCE_REF,
            "source_commit": SOURCE_COMMIT,
            "control_ref": CONTROL_REF,
            "control_commit": static["control_commit"],
            "image_digest": IMAGE_DIGEST,
            "provider_identity": provider_identity(),
            "architecture_mode": "single_agent",
            "rag_enabled": {"C0": False, "C1": True},
            "index_id": INDEX_ID,
            "index_sha256": INDEX_SHA256,
            "retrieval_context_sha256": next(iter(retrieval_hashes)),
            "private_t3_scorer_sha256": PRIVATE_SCORER_SHA256,
            "evaluator_hashes_schema": EVALUATOR_SCHEMA,
            "evaluator_hashes_sha256": sha256_bytes(canonical_bytes(evaluator_hashes)),
            "prospective_study_control_sha256": sha256_file(PROTOCOL_PATH),
            "task_text_sha256": static["task_sha256"],
            "canonical_task_sha256": static["canonical_task_sha256"],
            "public_inputs_sha256": static["public_inputs_sha256"],
            "tool_implementation_files_sha256": static[
                "tool_implementation_files_sha256"
            ],
            "tool_implementation_sha256": static["tool_implementation_sha256"],
            "control_files_sha256": static["control_files_sha256"],
            "iteration_controls": copy.deepcopy(ITERATION_CONTROLS),
            "shared_budget": copy.deepcopy(SHARED_BUDGET),
            "blinding_salt_sha256": sha256_bytes(stripped_salt),
            "private_condition_map_sha256": sha256_bytes(
                canonical_bytes(private_condition_map)
            ),
            "remote_ref_inventory_sha256": sha256_bytes(
                canonical_bytes(remote_inventory)
            ),
            "execution_order": [item["submission_id"] for item in run_entries],
            "runs": run_entries,
        }
        write_json(staging / "agreement.json", agreement)
        agreement_sha256 = sha256_file(staging / "agreement.json")
        write_json(
            staging / "agreement.sha256.json",
            {
                "schema_version": AGREEMENT_DIGEST_SCHEMA,
                "agreement_sha256": agreement_sha256,
            },
        )
        checksum_rows = [
            f"{sha256_file(path)}  {path.relative_to(staging)}"
            for path in sorted(item for item in staging.rglob("*") if item.is_file())
            if path.name != "checksums.sha256"
        ]
        (staging / "checksums.sha256").write_text(
            "\n".join(checksum_rows) + "\n", encoding="utf-8"
        )
        os.replace(staging, output)
        return {
            "status": "built",
            "formal_run_count": 6,
            "pair_count": 3,
            "agreement_sha256": agreement_sha256,
            "output": str(output),
            "provider_call_count": 0,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blinding-salt-file", type=Path, default=DEFAULT_SALT_PATH)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_outside_git(args.blinding_salt_file, "blinding salt")
    result = build_package(
        salt=args.blinding_salt_file.read_bytes(),
        output=args.output,
        heads=remote_heads(),
        retrieve=retrieve_for_c1,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
