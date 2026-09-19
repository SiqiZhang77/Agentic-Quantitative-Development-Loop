#!/usr/bin/env python3
"""Preflight or run one non-formal Experiment 2 Terra C0/C1 smoke pair.

This launcher deliberately bypasses Jira and Airflow.  It uses one selected
public T1/T2/T3 task, the frozen Experiment 2 BM25 index for C1, and the same
source/image/model for both conditions.  Smoke outputs are never formal samples.
"""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
_default_project_root = REPO_ROOT.parents[1] if len(REPO_ROOT.parents) > 1 else REPO_ROOT
PROJECT_ROOT = Path(os.getenv("BSL_PROJECT_ROOT", str(_default_project_root))).resolve()
GATEWAY_DAGS = REPO_ROOT / "jira-chatops-gateway" / "dags"
PROXY_ROOT = REPO_ROOT / "rae_runtime" / "proxy"
SCHEMA_PATH = REPO_ROOT / "rae_runtime" / "sandbox" / "schemas" / "runtime_request.schema.json"
TASK_ROOT = HERE / "formal-v2"
TASK_REGISTRY_PATH = TASK_ROOT / "task_registry.json"
INDEX_PATH = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "exp2-private"
    / "exp2"
    / "e2-freeze-v1"
    / "e2-jira-index-v1.json"
)
OUTPUT_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
    / "exp2-terra-smoke"
)

SOURCE_REF = "exp2/terra-smoke-source-v1"
SOURCE_COMMIT = "6e7d29b3f220dd4cc639219aa7e765aee88e3988"
IMAGE_REF = (
    "exp2-terra-smoke-candidate@"
    "sha256:8f135afdd816ffeac21f67834d10af91dda6509b4c8f993e33c65413b128465a"
)
INDEX_SHA256 = "72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1"
PROVIDER_MODE = "openai"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
MODEL_ALIAS = "gpt-5.6-terra"
PAIR_AUTHORIZATION = "AUTHORIZE_ONE_NONFORMAL_C0_C1_PAIR"


class SmokeLaunchError(RuntimeError):
    pass


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider_identity() -> dict[str, str]:
    value = {
        "schema_version": "llm-provider-identity-v1",
        "provider_mode": PROVIDER_MODE,
        "base_url": PROVIDER_BASE_URL,
        "model_alias": MODEL_ALIAS,
        "transport_model": MODEL_ALIAS,
        "adapter": "openai_chat_completions",
    }
    return {**value, "sha256": _canonical_sha256(value)}


def _safe_pair_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", value):
        raise SmokeLaunchError("pair-id must use only letters, digits, '.', '_' or '-'")
    return value


def _target_ref(pair_id: str, condition: str) -> str:
    return f"quant/E2-TERRA-SMOKE-{pair_id}-{condition}"


def _task_config(task_id: str) -> dict[str, Any]:
    registry = json.loads(TASK_REGISTRY_PATH.read_text(encoding="utf-8"))
    for task in registry.get("tasks", []):
        if task.get("task_id") == task_id:
            return task
    raise SmokeLaunchError("task-id must be T1, T2 or T3")


def _request(pair_id: str, condition: str, *, task_id: str = "T1") -> dict[str, Any]:
    if condition not in {"C0", "C1"}:
        raise ValueError("condition must be C0 or C1")
    task = _task_config(task_id)
    task_text = (TASK_ROOT / task["task_file"]).read_text(encoding="utf-8").strip()
    provider = _provider_identity()
    rag_enabled = condition == "C1"
    target_ref = _target_ref(pair_id, condition)
    request = {
        "schema_version": "1.0",
        "run_id": f"E2-{pair_id}-{condition}",
        "repository_details": [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                "source_branch": SOURCE_REF,
                "target_branch": target_ref,
                "runtime_role": "default",
                "allowed_directories": list(task["allowed_directories"]),
            }
        ],
        "jira_metadata": {
            "ticket_id": f"E2SMOKE-{task_id.removeprefix('T')}",
            "current_status": "Local smoke",
            "summary": f"Experiment 2 {task_id} non-formal smoke task",
            "description": task_text,
            "triggering_comment": {
                "comment_id": "local-smoke",
                "timestamp": "2026-08-27T00:00:00Z",
                "author": "local-operator",
                "text": task_text,
            },
            "events_history": [
                {
                    "event_type": "comment",
                    "timestamp": "2026-08-27T00:00:00Z",
                    "author": "local-operator",
                    "text": task_text,
                }
            ],
        },
        "execution_objectives": {
            "strategy_type": task["strategy_type"],
            "stock_type": None,
            "target_date_range": {},
            "parsed_task_parameters": {
                "objective": task_text,
                "model": MODEL_ALIAS,
                "provider_mode": PROVIDER_MODE,
                "provider_base_url": PROVIDER_BASE_URL,
                "provider_identity_sha256": provider["sha256"],
                "allow_iteration": False,
                "max_agent_turns": 15,
                "max_commits_per_run": 3,
                "rag_enabled": rag_enabled,
                "rag_top_k": 5,
            },
            "system_instruction_override": None,
            "zero_code_modifications": False,
            "resource_path": task["resource_path"],
            "target_path": task["target_path"],
        },
        "iteration_controls": {
            "allow_iteration": False,
            "max_iterations": 1,
            "max_failed_iterations": 1,
            "max_agent_turns": 15,
            "max_commits_per_run": 3,
            "timeout_seconds": 1800,
            "max_token_budget_per_run": 200000,
        },
        "resource_requirements": {
            "cpu_vcpus": 2,
            "memory_mb": 4096,
            "gpu_count": 0,
            "execution_timeout_seconds": 1800,
            "pids_limit": 256,
            "yarn_queue": "root.default",
        },
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    jsonschema.validate(request, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return request


def _common_pair_projection(request: dict[str, Any]) -> dict[str, Any]:
    projected = copy.deepcopy(request)
    projected.pop("run_id", None)
    projected.pop("retrieval_context", None)
    projected["repository_details"][0].pop("target_branch", None)
    parameters = projected["execution_objectives"]["parsed_task_parameters"]
    parameters.pop("rag_enabled", None)
    return projected


def _verify_pair(c0: dict[str, Any], c1: dict[str, Any]) -> str:
    if c0["execution_objectives"]["parsed_task_parameters"]["rag_enabled"] is not False:
        raise SmokeLaunchError("C0 must set rag_enabled=false")
    if c1["execution_objectives"]["parsed_task_parameters"]["rag_enabled"] is not True:
        raise SmokeLaunchError("C1 must set rag_enabled=true")
    left = _common_pair_projection(c0)
    right = _common_pair_projection(c1)
    if left != right:
        raise SmokeLaunchError("C0/C1 differ beyond run, target and RAG treatment fields")
    return _canonical_sha256(left)


def _retrieve_for_c1(request: dict[str, Any]) -> dict[str, Any]:
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


def _remote_head(ref: str) -> str | None:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-remote",
            "--heads",
            "github",
            f"refs/heads/{ref}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not completed.stdout.strip():
        return None
    return completed.stdout.split()[0]


def _image_available() -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", IMAGE_REF],
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        return

    # Do not collapse a stopped/unreachable Docker engine into a misleading
    # "missing image" diagnosis.  Keep the detail short because this message is
    # copied into operator logs.
    raw_detail = completed.stderr or completed.stdout or "unspecified Docker error"
    detail = " ".join(raw_detail.split())[:500]
    normalized = detail.casefold()
    engine_markers = (
        "cannot connect to the docker daemon",
        "error during connect",
        "is the docker daemon running",
        "permission denied while trying to connect",
        "docker context",
    )
    if any(marker in normalized for marker in engine_markers):
        raise SmokeLaunchError(
            "Docker engine/context is unavailable while checking the pinned "
            f"local image: {detail}"
        )
    if "no such image" in normalized or "not found" in normalized:
        raise SmokeLaunchError(f"pinned local image is unavailable: {IMAGE_REF}")
    raise SmokeLaunchError(
        f"pinned local image check failed for {IMAGE_REF}: {detail}"
    )


def _credentials() -> tuple[str, str, str | None]:
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        openai_key = getpass.getpass("Paste OPENAI_API_KEY (hidden): ").strip()
    if not openai_key.startswith("sk-") or any(ch.isspace() for ch in openai_key):
        raise SmokeLaunchError("OPENAI_API_KEY shape is invalid")

    github_username = os.getenv("GITHUB_USERNAME")
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
        )
        values: dict[str, str] = {}
        if completed.returncode == 0:
            for line in completed.stdout.splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
        github_token = values.get("password")
    if not github_token:
        raise SmokeLaunchError("GitHub credential helper returned no token")
    return openai_key, github_token, github_username


def preflight(pair_id: str, *, task_id: str = "T1") -> dict[str, Any]:
    c0 = _request(pair_id, "C0", task_id=task_id)
    c1 = _request(pair_id, "C1", task_id=task_id)
    pair_hash = _verify_pair(c0, c1)
    _image_available()
    if _remote_head(SOURCE_REF) != SOURCE_COMMIT:
        raise SmokeLaunchError("remote source ref is absent or has drifted")
    for condition in ("C0", "C1"):
        if _remote_head(_target_ref(pair_id, condition)) is not None:
            raise SmokeLaunchError(f"target already exists for {condition}; choose a new pair-id")

    # C0 deliberately never calls the retriever.  C1 validates the index digest
    # and prepares the only treatment-specific context.
    retrieval = _retrieve_for_c1(c1)
    c1["retrieval_context"] = retrieval
    jsonschema.validate(c1, json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return {
        "pair_id": pair_id,
        "task_id": task_id,
        "requests": {"C0": c0, "C1": c1},
        "pair_common_sha256": pair_hash,
        "retrieval_audit": {
            "index_id": retrieval["index_id"],
            "index_sha256": retrieval["index_sha256"],
            "query_sha256": retrieval["query_sha256"],
            "retriever_name": retrieval["retriever_name"],
            "retriever_version": retrieval["retriever_version"],
            "candidate_count": retrieval["candidate_count"],
            "retrieved_count": len(retrieval["memories"]),
        },
    }


def _docker_command(output_dir: Path, environment_names: list[str]) -> list[str]:
    input_dir = output_dir / "input"
    result_dir = output_dir / "output"
    input_dir.mkdir(parents=True)
    result_dir.mkdir()
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--cpus",
        "2",
        "--memory",
        "4096m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{input_dir}:/workspace/input:ro",
        "-v",
        f"{result_dir}:/workspace/output:rw",
    ]
    for name in environment_names:
        command.extend(["-e", name])
    command.extend(
        [
            "-e",
            "MAX_BRANCHES_PER_RUN=1",
            "-e",
            "MAX_COMMITS_PER_RUN=3",
            IMAGE_REF,
            "python",
            "run.py",
        ]
    )
    return command


def _run_one(
    *,
    state: dict[str, Any],
    condition: str,
    pair_dir: Path,
    child_env: dict[str, str],
    environment_names: list[str],
) -> dict[str, Any]:
    run_dir = pair_dir / condition
    if run_dir.exists():
        raise SmokeLaunchError(f"refusing to overwrite output: {run_dir}")
    run_dir.mkdir(parents=True)
    command = _docker_command(run_dir, environment_names)
    stdout_path = run_dir / "container.stdout.log"
    stderr_path = run_dir / "container.stderr.log"
    started = datetime.now(timezone.utc)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        completed = subprocess.run(
            command,
            input=json.dumps(state["requests"][condition], ensure_ascii=False),
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=child_env,
            timeout=2100,
        )
    ended = datetime.now(timezone.utc)
    result_path = run_dir / "output" / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else None
    summary = {
        "schema_version": "exp2-terra-nonformal-smoke-run-v1",
        "formal_observation": False,
        "task_id": state["task_id"],
        "condition": condition,
        "rag_enabled": condition == "C1",
        "run_id": state["requests"][condition]["run_id"],
        "target_ref": _target_ref(state["pair_id"], condition),
        "container_exit_code": completed.returncode,
        "result_present": result is not None,
        "result_status": (
            (result.get("execution_summary") or {}).get("status") if result else None
        ),
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "latency_seconds": round((ended - started).total_seconds(), 6),
        "output_directory": str(run_dir),
    }
    (run_dir / "launch_result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def execute_pair(state: dict[str, Any], output_root: Path) -> dict[str, Any]:
    if os.getenv("EXP2_TERRA_SMOKE_AUTHORIZATION") != PAIR_AUTHORIZATION:
        raise SmokeLaunchError(
            "set EXP2_TERRA_SMOKE_AUTHORIZATION=" + PAIR_AUTHORIZATION
        )
    openai_key, github_token, github_username = _credentials()
    child_env = os.environ.copy()
    for name in (
        "API_KEY",
        "MODEL",
        "LITELLM_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "LITELLM_PROXY_API_KEY",
        "RAE_OFFLINE",
        "RAE_ENABLE_MANAGER_STAR",
    ):
        child_env.pop(name, None)
    child_env.update(
        {
            "OPENAI_API_KEY": openai_key,
            "GITHUB_TOKEN": github_token,
            "LLM_PROVIDER": PROVIDER_MODE,
            "OPENAI_BASE_URL": PROVIDER_BASE_URL,
            "OPENAI_MODEL": MODEL_ALIAS,
            "USE_MCP_GITHUB": "true",
            "ALLOWED_REPOS": "bankingscience/BSLAgenticQuantDevLoop",
        }
    )
    if github_username:
        child_env["GITHUB_USERNAME"] = github_username
    environment_names = [
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "LLM_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "USE_MCP_GITHUB",
        "ALLOWED_REPOS",
    ]
    if github_username:
        environment_names.append("GITHUB_USERNAME")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pair_dir = (output_root / f"{state['pair_id']}-{stamp}").resolve()
    if pair_dir.exists():
        raise SmokeLaunchError(f"output directory already exists: {pair_dir}")
    pair_dir.mkdir(parents=True)
    (pair_dir / "pair_start_record.json").write_text(
        json.dumps(
            {
                "schema_version": "exp2-terra-nonformal-smoke-pair-v1",
                "formal_observation": False,
                "task_id": state["task_id"],
                "pair_id": state["pair_id"],
                "pair_common_sha256": state["pair_common_sha256"],
                "source_ref": SOURCE_REF,
                "source_commit": SOURCE_COMMIT,
                "image_ref": IMAGE_REF,
                "provider_identity": _provider_identity(),
                "conditions": ["C0", "C1"],
                "retrieval_audit": state["retrieval_audit"],
                "shared_call_cap_per_run": 15,
                "shared_token_cap_per_run": 200000,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    runs = [
        _run_one(
            state=state,
            condition=condition,
            pair_dir=pair_dir,
            child_env=child_env,
            environment_names=environment_names,
        )
        for condition in ("C0", "C1")
    ]
    pair_result = {
        "schema_version": "exp2-terra-nonformal-smoke-pair-result-v1",
        "formal_observation": False,
        "task_id": state["task_id"],
        "pair_id": state["pair_id"],
        "output_directory": str(pair_dir),
        "runs": runs,
        "status": "completed" if all(run["result_present"] for run in runs) else "failed",
    }
    (pair_dir / "pair_result.json").write_text(
        json.dumps(pair_result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return pair_result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", choices=("T1", "T2", "T3"), default="T1")
    parser.add_argument("--pair-id")
    parser.add_argument("--output-root", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--execute-pair", action="store_true")
    args = parser.parse_args()
    try:
        pair_id = args.pair_id or datetime.now(timezone.utc).strftime(
            f"{args.task_id}-%Y%m%dT%H%M%SZ"
        )
        state = preflight(_safe_pair_id(pair_id), task_id=args.task_id)
        safe = {
            "schema_version": "exp2-terra-nonformal-smoke-preflight-v1",
            "status": "ready",
            "formal_observation": False,
            "provider_call_count": 0,
            "pair_id": state["pair_id"],
            "task_id": state["task_id"],
            "conditions": ["C0", "C1"],
            "rag_enabled": {"C0": False, "C1": True},
            "source_ref": SOURCE_REF,
            "source_commit": SOURCE_COMMIT,
            "image_ref": IMAGE_REF,
            "provider_identity": _provider_identity(),
            "pair_common_sha256": state["pair_common_sha256"],
            "retrieval_audit": state["retrieval_audit"],
            "shared_call_cap_per_run": 15,
            "shared_token_cap_per_run": 200000,
            "external_surfaces": {
                "jira": False,
                "airflow": False,
                "openai": "configuration_only",
                "github": "read_only_preflight",
            },
        }
        if not args.execute_pair:
            print(json.dumps(safe, sort_keys=True))
            return
        print(json.dumps({**safe, "status": "starting"}, sort_keys=True), flush=True)
        print(json.dumps(execute_pair(state, args.output_root), sort_keys=True))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "formal_observation": False,
                    "failure_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
