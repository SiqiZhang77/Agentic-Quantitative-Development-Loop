#!/usr/bin/env python3
"""Preflight or serially execute the private E2 Terra T1/T2 formal package."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
EXP2_ROOT = HERE.parent
REPO_ROOT = EXP2_ROOT.parents[1]
DEFAULT_PROJECT_ROOT = REPO_ROOT.parents[1]
PROJECT_ROOT = Path(os.getenv("BSL_PROJECT_ROOT", str(DEFAULT_PROJECT_ROOT))).resolve()
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
    / "exp2-terra-t12-v2-observations"
)
AUTHORIZATION_ENV = "E2_T12_V2_PAIR_AUTHORIZATION"


class FormalLaunchError(RuntimeError):
    pass


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FormalLaunchError(f"could not load control helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_module("exp2_terra_t12_builder", HERE / "build_control_package.py")
SMOKE = _load_module("exp2_terra_smoke_credentials", EXP2_ROOT / "run_terra_smoke_pair.py")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalLaunchError(f"control JSON is unavailable or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise FormalLaunchError(f"control JSON must be an object: {path}")
    return value


def _verify_checksums(control_dir: Path) -> None:
    checksum_path = control_dir / "checksums.sha256"
    try:
        rows = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FormalLaunchError("private control checksums are unavailable") from exc
    seen: set[str] = set()
    for row in rows:
        digest, separator, relative = row.partition("  ")
        if not separator or relative in seen or len(digest) != 64:
            raise FormalLaunchError("private control checksum inventory is malformed")
        seen.add(relative)
        path = control_dir / relative
        if not path.is_file() or sha256_file(path) != digest:
            raise FormalLaunchError(f"private control checksum mismatch: {relative}")
    expected = {
        str(path.relative_to(control_dir))
        for path in control_dir.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if seen != expected:
        raise FormalLaunchError("private control checksum inventory is incomplete")


def _image_available(image_digest: str) -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", image_digest],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout).split())[:500]
        raise FormalLaunchError(f"agreed local image is unavailable: {image_digest}: {detail}")


def load_control(
    control_dir: Path,
    *,
    heads: dict[str, str],
    image_check: Callable[[str], None] = _image_available,
) -> dict[str, Any]:
    control_dir = control_dir.resolve()
    _verify_checksums(control_dir)
    agreement_path = control_dir / "agreement.json"
    agreement = _load_json(agreement_path)
    digest_record = _load_json(control_dir / "agreement.sha256.json")
    agreement_sha = sha256_file(agreement_path)
    if digest_record.get("schema_version") != BUILDER.AGREEMENT_DIGEST_SCHEMA:
        raise FormalLaunchError("agreement digest schema is not approved")
    if digest_record.get("agreement_sha256") != agreement_sha:
        raise FormalLaunchError("agreement hash mismatch")
    if agreement.get("schema_version") != BUILDER.AGREEMENT_SCHEMA:
        raise FormalLaunchError("agreement schema is not approved")
    if agreement.get("status") != "user_agreed_private_control_ready":
        raise FormalLaunchError("agreement status is not runnable")
    if (
        agreement.get("formal_run_count") != BUILDER.FORMAL_RUN_COUNT
        or agreement.get("pair_count") != BUILDER.PAIR_COUNT
    ):
        raise FormalLaunchError("agreement run inventory is invalid")
    if agreement.get("source_ref") != BUILDER.SOURCE_REF:
        raise FormalLaunchError("agreement source ref drifted")
    if agreement.get("source_commit") != BUILDER.SOURCE_COMMIT:
        raise FormalLaunchError("agreement source commit drifted")
    if agreement.get("image_digest") != BUILDER.IMAGE_DIGEST:
        raise FormalLaunchError("agreement image identity drifted")
    provider = agreement.get("provider_identity") or {}
    if provider != SMOKE._provider_identity():
        raise FormalLaunchError("agreement provider identity drifted")
    if agreement.get("index_sha256") != BUILDER.INDEX_SHA256:
        raise FormalLaunchError("agreement index identity drifted")

    evaluator_path = control_dir / "private_evaluator_hashes.json"
    evaluator_value = _load_json(evaluator_path)
    try:
        evaluator_hashes = BUILDER.validate_evaluator_hashes(evaluator_value)
    except ValueError as exc:
        raise FormalLaunchError(str(exc)) from exc
    evaluator_hashes_sha256 = BUILDER.sha256_bytes(
        BUILDER.canonical_bytes(evaluator_hashes)
    )
    if agreement.get("evaluator_hashes_schema") != BUILDER.EVALUATOR_SCHEMA:
        raise FormalLaunchError("agreement evaluator hash schema drifted")
    if agreement.get("evaluator_hash_count") != len(BUILDER.EVALUATOR_HASH_FIELDS):
        raise FormalLaunchError("agreement evaluator hash inventory is incomplete")
    if agreement.get("evaluator_hashes_sha256") != evaluator_hashes_sha256:
        raise FormalLaunchError("agreement evaluator hash binding mismatch")

    if heads.get(BUILDER.SOURCE_REF) != BUILDER.SOURCE_COMMIT:
        raise FormalLaunchError("remote formal source ref is absent or drifted")
    image_check(BUILDER.IMAGE_DIGEST)
    if not SMOKE.INDEX_PATH.is_file() or sha256_file(SMOKE.INDEX_PATH) != BUILDER.INDEX_SHA256:
        raise FormalLaunchError("frozen private RAG index is absent or drifted")

    execution_order = agreement.get("execution_order")
    runs = agreement.get("runs")
    if not isinstance(execution_order, list) or not isinstance(runs, list):
        raise FormalLaunchError("agreement execution inventory is malformed")
    by_id = {item.get("submission_id"): item for item in runs if isinstance(item, dict)}
    if (
        len(by_id) != BUILDER.FORMAL_RUN_COUNT
        or execution_order != [item.get("submission_id") for item in runs]
    ):
        raise FormalLaunchError("agreement execution order does not match run records")

    private_map = _load_json(control_dir / "blinding" / "private_condition_map.json")
    submissions = private_map.get("submissions")
    if not isinstance(submissions, dict) or set(submissions) != set(execution_order):
        raise FormalLaunchError("private condition map does not match agreement")

    prepared: list[dict[str, Any]] = []
    pair_projections: dict[str, dict[str, dict[str, Any]]] = {}
    for submission_id in execution_order:
        record = by_id[submission_id]
        files = record.get("files") or {}
        hashes = record.get("file_sha256") or {}
        loaded: dict[str, dict[str, Any]] = {}
        for name in ("request", "identity", "negative_ref_manifest"):
            path = control_dir / str(files.get(name, ""))
            if not path.is_file() or sha256_file(path) != hashes.get(name):
                raise FormalLaunchError(f"run control binding mismatch: {submission_id}/{name}")
            loaded[name] = _load_json(path)
        identity = loaded["identity"]
        request = loaded["request"]
        negative = loaded["negative_ref_manifest"]
        private = submissions[submission_id]
        condition = private.get("condition")
        if condition not in {"C0", "C1"} or identity.get("condition") != condition:
            raise FormalLaunchError("private condition identity mismatch")
        if request.get("run_id") != submission_id:
            raise FormalLaunchError("request run ID mismatch")
        repository = (request.get("repository_details") or [{}])[0]
        if repository.get("source_branch") != BUILDER.SOURCE_REF:
            raise FormalLaunchError("request source ref mismatch")
        if repository.get("target_branch") != record.get("target_ref"):
            raise FormalLaunchError("request target ref mismatch")
        if heads.get(record["target_ref"]) is not None:
            raise FormalLaunchError(f"formal target already exists: {record['target_ref']}")
        parameters = request["execution_objectives"]["parsed_task_parameters"]
        if parameters.get("rag_enabled") is not (condition == "C1"):
            raise FormalLaunchError("request treatment flag mismatch")
        retrieval = request.get("retrieval_context")
        if condition == "C0" and retrieval is not None:
            raise FormalLaunchError("C0 contains retrieval context")
        if condition == "C1" and (
            not isinstance(retrieval, dict)
            or retrieval.get("index_sha256") != BUILDER.INDEX_SHA256
            or len(retrieval.get("memories") or []) > 5
        ):
            raise FormalLaunchError("C1 retrieval context is absent or drifted")
        deny_refs = negative.get("deny_refs")
        if not isinstance(deny_refs, list) or "main" not in deny_refs:
            raise FormalLaunchError("negative-ref manifest is incomplete")
        if BUILDER.SOURCE_REF in deny_refs or record["target_ref"] in deny_refs:
            raise FormalLaunchError("negative-ref manifest denies an allowed ref")
        pair_id = record.get("pair_id")
        pair_projections.setdefault(pair_id, {})[condition] = BUILDER._pair_projection(request)
        prepared.append(
            {
                "submission_id": submission_id,
                "task_id": record["task_id"],
                "replicate": record["replicate"],
                "pair_id": pair_id,
                "target_ref": record["target_ref"],
                "condition": condition,
                "request": request,
                "identity_sha256": hashes["identity"],
                "negative_ref_manifest_sha256": hashes["negative_ref_manifest"],
            }
        )
    if len(pair_projections) != BUILDER.PAIR_COUNT:
        raise FormalLaunchError("pair inventory is invalid")
    for pair_id, projections in pair_projections.items():
        if set(projections) != {"C0", "C1"} or projections["C0"] != projections["C1"]:
            raise FormalLaunchError(f"pair differs beyond treatment: {pair_id}")
    return {
        "control_dir": control_dir,
        "agreement": agreement,
        "agreement_sha256": agreement_sha,
        "evaluator_hashes_schema": BUILDER.EVALUATOR_SCHEMA,
        "evaluator_hashes_sha256": evaluator_hashes_sha256,
        "runs": prepared,
    }


def _docker_command(run_dir: Path, environment_names: list[str]) -> list[str]:
    input_dir = run_dir / "input"
    result_dir = run_dir / "output"
    input_dir.mkdir(parents=True)
    result_dir.mkdir()
    command = [
        "docker", "run", "--rm", "-i", "--pull", "never", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--cpus", "2",
        "--memory", "4096m", "--pids-limit", "256", "--security-opt",
        "no-new-privileges:true", "--cap-drop", "ALL", "-v",
        f"{input_dir}:/workspace/input:ro", "-v", f"{result_dir}:/workspace/output:rw",
    ]
    for name in environment_names:
        command.extend(["-e", name])
    command.extend(
        [
            "-e", "MAX_BRANCHES_PER_RUN=1", "-e", "MAX_COMMITS_PER_RUN=3",
            BUILDER.IMAGE_DIGEST, "python", "run.py",
        ]
    )
    return command


def _credentials() -> tuple[str, str, str | None]:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        key = getpass.getpass("Paste OPENAI_API_KEY once for E2 formal batch (hidden): ").strip()
    if not key.startswith("sk-") or any(ch.isspace() for ch in key):
        raise FormalLaunchError("OPENAI_API_KEY shape is invalid")
    github_username = os.getenv("GITHUB_USERNAME")
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
        )
        values = dict(
            line.split("=", 1)
            for line in completed.stdout.splitlines()
            if "=" in line
        ) if completed.returncode == 0 else {}
        github_token = values.get("password")
    if not github_token:
        raise FormalLaunchError("GitHub credential helper returned no token")
    return key, github_token, github_username


def _model_call_evidence(result: dict[str, Any]) -> bool:
    usage = (result.get("telemetry") or {}).get("model_usage") or []
    return any(
        isinstance(item, dict) and int(item.get("total_tokens") or 0) > 0
        for item in usage
    )


def _treatment_evidence(result: dict[str, Any], condition: str) -> bool:
    retrieval = (result.get("telemetry") or {}).get("retrieval") or {}
    if condition == "C0":
        return (
            retrieval.get("enabled") is False
            and retrieval.get("delivery_mode") == "disabled"
            and retrieval.get("prompt_injected") is False
        )
    if condition == "C1":
        return (
            retrieval.get("enabled") is True
            and retrieval.get("delivery_mode") == "generator_prompt"
            and retrieval.get("prompt_injected") is True
            and retrieval.get("status") == "ok"
            and retrieval.get("index_id") == BUILDER.INDEX_ID
            and retrieval.get("index_sha256") == BUILDER.INDEX_SHA256
        )
    return False


def _run_one(
    run: dict[str, Any],
    *,
    batch_dir: Path,
    child_env: dict[str, str],
    environment_names: list[str],
    agreement_sha256: str,
) -> dict[str, Any]:
    run_dir = batch_dir / run["submission_id"]
    if run_dir.exists():
        raise FormalLaunchError(f"refusing to overwrite run output: {run_dir}")
    run_dir.mkdir(parents=True)
    (run_dir / "formal_start_record.json").write_text(
        json.dumps(
            {
                "schema_version": "exp2-terra-t12-formal-start-v1",
                "formal_observation": True,
                "submission_id": run["submission_id"],
                "task_id": run["task_id"],
                "replicate": run["replicate"],
                "target_ref": run["target_ref"],
                "agreement_sha256": agreement_sha256,
                "identity_sha256": run["identity_sha256"],
                "negative_ref_manifest_sha256": run["negative_ref_manifest_sha256"],
                "source_ref": BUILDER.SOURCE_REF,
                "source_commit": BUILDER.SOURCE_COMMIT,
                "image_digest": BUILDER.IMAGE_DIGEST,
                "provider_identity_sha256": SMOKE._provider_identity()["sha256"],
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    stdout_path = run_dir / "container.stdout.log"
    stderr_path = run_dir / "container.stderr.log"
    started = datetime.now(timezone.utc)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        completed = subprocess.run(
            _docker_command(run_dir, environment_names),
            input=json.dumps(run["request"], ensure_ascii=False),
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=child_env,
            timeout=2100,
        )
    ended = datetime.now(timezone.utc)
    result_path = run_dir / "output" / "result.json"
    result = _load_json(result_path) if result_path.is_file() else None
    result_status = ((result or {}).get("execution_summary") or {}).get("status")
    model_call_evidence = bool(result and _model_call_evidence(result))
    treatment_evidence = bool(
        result and _treatment_evidence(result, run["condition"])
    )
    complete_observation = bool(
        result
        and result.get("run_id") == run["submission_id"]
        and result_status in {"succeeded", "failed"}
        and model_call_evidence
        and treatment_evidence
    )
    summary = {
        "schema_version": "exp2-terra-t12-formal-launch-result-v1",
        "formal_observation": True,
        "submission_id": run["submission_id"],
        "task_id": run["task_id"],
        "replicate": run["replicate"],
        "target_ref": run["target_ref"],
        "container_exit_code": completed.returncode,
        "result_present": result is not None,
        "result_status": result_status,
        "model_call_evidence": model_call_evidence,
        "treatment_evidence": treatment_evidence,
        "complete_observation": complete_observation,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "latency_seconds": round((ended - started).total_seconds(), 6),
        "output_directory": str(run_dir),
    }
    (run_dir / "launch_result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if not complete_observation:
        raise FormalLaunchError(
            f"incomplete or infrastructure-invalid observation: {run['submission_id']}"
        )
    return summary


def _select_pair(state: dict[str, Any], task_id: str, replicate: int) -> dict[str, Any]:
    selected = [
        run for run in state["runs"]
        if run["task_id"] == task_id and run["replicate"] == replicate
    ]
    if len(selected) != 2 or {run["condition"] for run in selected} != {"C0", "C1"}:
        raise FormalLaunchError(f"selected pair is incomplete: {task_id}-R{replicate}")
    return {**state, "runs": selected, "selected_task": task_id, "selected_replicate": replicate}


def _pair_authorization(task_id: str, replicate: int) -> str:
    return f"AUTHORIZE_E2T12V2_{task_id}_R{replicate}_TWO_OBSERVATIONS"


def execute_batch(state: dict[str, Any], output_root: Path) -> dict[str, Any]:
    expected_authorization = _pair_authorization(
        state["selected_task"], state["selected_replicate"]
    )
    if os.getenv(AUTHORIZATION_ENV) != expected_authorization:
        raise FormalLaunchError(f"set {AUTHORIZATION_ENV}={expected_authorization}")
    openai_key, github_token, github_username = _credentials()
    child_env = os.environ.copy()
    for name in (
        "API_KEY", "MODEL", "LITELLM_API_KEY", "LITELLM_BASE_URL",
        "LITELLM_MODEL", "LITELLM_PROXY_API_KEY", "RAE_OFFLINE",
        "RAE_ENABLE_MANAGER_STAR",
    ):
        child_env.pop(name, None)
    child_env.update(
        {
            "OPENAI_API_KEY": openai_key,
            "GITHUB_TOKEN": github_token,
            "LLM_PROVIDER": BUILDER.PROVIDER_MODE,
            "OPENAI_BASE_URL": BUILDER.PROVIDER_BASE_URL,
            "OPENAI_MODEL": BUILDER.MODEL_ALIAS,
            "USE_MCP_GITHUB": "true",
            "ALLOWED_REPOS": "bankingscience/BSLAgenticQuantDevLoop",
        }
    )
    if github_username:
        child_env["GITHUB_USERNAME"] = github_username
    environment_names = [
        "OPENAI_API_KEY", "GITHUB_TOKEN", "LLM_PROVIDER", "OPENAI_BASE_URL",
        "OPENAI_MODEL", "USE_MCP_GITHUB", "ALLOWED_REPOS",
    ]
    if github_username:
        environment_names.append("GITHUB_USERNAME")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pair_label = f"{state['selected_task']}-R{state['selected_replicate']}"
    batch_dir = (output_root / f"{BUILDER.EXECUTION_EPOCH}-{pair_label}-{stamp}").resolve()
    if batch_dir.exists():
        raise FormalLaunchError(f"batch output already exists: {batch_dir}")
    batch_dir.mkdir(parents=True)
    (batch_dir / "batch_start_record.json").write_text(
        json.dumps(
            {
                "schema_version": "exp2-terra-t12-formal-batch-start-v1",
                "formal_observation": True,
                "agreement_sha256": state["agreement_sha256"],
                "evaluator_hashes_schema": state["evaluator_hashes_schema"],
                "evaluator_hashes_sha256": state["evaluator_hashes_sha256"],
                "execution_epoch": BUILDER.EXECUTION_EPOCH,
                "run_count": len(state["runs"]),
                "task_id": state["selected_task"],
                "replicate": state["selected_replicate"],
                "source_ref": BUILDER.SOURCE_REF,
                "source_commit": BUILDER.SOURCE_COMMIT,
                "image_digest": BUILDER.IMAGE_DIGEST,
                "provider_identity": SMOKE._provider_identity(),
                "index_id": BUILDER.INDEX_ID,
                "index_sha256": BUILDER.INDEX_SHA256,
            },
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    results: list[dict[str, Any]] = []
    for index, run in enumerate(state["runs"], start=1):
        if BUILDER.remote_heads().get(run["target_ref"]) is not None:
            raise FormalLaunchError(f"target appeared before execution: {run['target_ref']}")
        summary = _run_one(
            run,
            batch_dir=batch_dir,
            child_env=child_env,
            environment_names=environment_names,
            agreement_sha256=state["agreement_sha256"],
        )
        results.append(summary)
        print(
            json.dumps(
                {
                    "schema_version": "exp2-terra-t12-formal-batch-progress-v1",
                    "status": "observation_recorded",
                    "completed_count": index,
                    "remaining_count": len(state["runs"]) - index,
                    "submission_id": summary["submission_id"],
                    "task_id": summary["task_id"],
                    "replicate": summary["replicate"],
                    "result_status": summary["result_status"],
                    "output_directory": summary["output_directory"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    final = {
        "schema_version": "exp2-terra-t12-formal-batch-result-v1",
        "status": "completed",
        "formal_observation": True,
        "agreement_sha256": state["agreement_sha256"],
        "observation_count": len(results),
        "succeeded_count": sum(item["result_status"] == "succeeded" for item in results),
        "failed_count": sum(item["result_status"] == "failed" for item in results),
        "output_directory": str(batch_dir),
    }
    (batch_dir / "batch_result.json").write_text(
        json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--task", choices=BUILDER.TASKS, required=True)
    parser.add_argument("--replicate", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        state = load_control(
            args.control_dir,
            heads=BUILDER.remote_heads(),
        )
        state = _select_pair(state, args.task, args.replicate)
        safe = {
            "schema_version": "exp2-terra-t12-formal-preflight-v1",
            "status": "ready",
            "formal_observation": True,
            "provider_call_count": 0,
            "execution_epoch": BUILDER.EXECUTION_EPOCH,
            "agreement_sha256": state["agreement_sha256"],
            "evaluator_hashes_schema": state["evaluator_hashes_schema"],
            "evaluator_hashes_sha256": state["evaluator_hashes_sha256"],
            "evaluator_hash_count": len(BUILDER.EVALUATOR_HASH_FIELDS),
            "run_count": len(state["runs"]),
            "pair_count": 1,
            "task_id": args.task,
            "replicate": args.replicate,
            "source_ref": BUILDER.SOURCE_REF,
            "source_commit": BUILDER.SOURCE_COMMIT,
            "image_digest": BUILDER.IMAGE_DIGEST,
            "provider_identity": SMOKE._provider_identity(),
            "index_id": BUILDER.INDEX_ID,
            "index_sha256": BUILDER.INDEX_SHA256,
            "external_surfaces": {
                "jira": False,
                "airflow": False,
                "litellm": False,
                "openai": "configuration_only",
                "github": "read_only_preflight",
            },
        }
        if not args.execute:
            print(json.dumps(safe, sort_keys=True))
            return 0
        print(json.dumps({**safe, "status": "starting"}, sort_keys=True), flush=True)
        print(json.dumps(execute_batch(state, args.output_root), sort_keys=True))
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "formal_observation": True,
                    "failure_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
