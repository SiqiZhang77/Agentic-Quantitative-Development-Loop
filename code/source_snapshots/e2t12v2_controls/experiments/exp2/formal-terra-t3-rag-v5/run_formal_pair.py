#!/usr/bin/env python3
"""Preflight all six E2 T3 v5 observations or execute one C0/C1 pair."""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
EXP2_ROOT = HERE.parent
REPO_ROOT = EXP2_ROOT.parents[1]
PROJECT_ROOT = REPO_ROOT.parents[1]
DEFAULT_CONTROL_DIR = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "exp2-private"
    / "exp2"
    / "formal-t3-rag-v5"
    / "control-v5"
)
OUTPUT_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
    / "exp2-terra-t3-rag-v5-observations"
)
AUTHORIZATION_ENV = "E2_T3_RAG_V5_PAIR_AUTHORIZATION"


class FormalLaunchError(RuntimeError):
    """The frozen E2 T3 observation cannot safely advance."""


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FormalLaunchError(f"could not load control helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_module("exp2_terra_t3_rag_v5_builder", HERE / "build_control_package.py")


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
        raise FormalLaunchError(
            f"agreed local image is unavailable or Docker is stopped: {detail}"
        )


def _validate_evaluator_manifest(value: Any) -> dict[str, str]:
    expected = {
        "schema_version": BUILDER.EVALUATOR_SCHEMA,
        "private_t3_scorer_sha256": BUILDER.PRIVATE_SCORER_SHA256,
    }
    if value != expected:
        raise FormalLaunchError("private T3 evaluator hash manifest drifted")
    return expected


def _validate_negative_ref_manifest(
    manifest: Mapping[str, Any], record: Mapping[str, Any]
) -> str:
    validator = Draft202012Validator(
        json.loads(BUILDER.NEGATIVE_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    validator.validate(manifest)
    repositories = manifest.get("repositories")
    if (
        manifest.get("schema_version") != "rae-negative-ref-manifest-v2"
        or manifest.get("experiment_id") != BUILDER.EXPERIMENT_ID
        or manifest.get("run_id") != record.get("submission_id")
        or not isinstance(repositories, list)
        or len(repositories) != 1
    ):
        raise FormalLaunchError("negative-ref manifest identity is invalid")
    repository = repositories[0]
    deny_refs = repository.get("deny_refs") or {}
    if (
        repository.get("repo_full_name") != BUILDER.REPO_FULL_NAME
        or repository.get("source_ref") != BUILDER.SOURCE_REF
        or repository.get("current_target_ref") != record.get("target_ref")
        or repository.get("paired_target_ref") != record.get("paired_target_ref")
        or set(deny_refs)
        != {
            "prior_study_refs",
            "other_result_refs",
            "protected_refs",
            "arbitrary_probe_refs",
        }
    ):
        raise FormalLaunchError("negative-ref manifest scope is invalid")
    flattened = [
        ref
        for name in (
            "prior_study_refs",
            "other_result_refs",
            "protected_refs",
            "arbitrary_probe_refs",
        )
        for ref in deny_refs[name]
    ]
    complete_deny_set = sorted(
        set(flattened) | {str(record.get("paired_target_ref"))}
    )
    if (
        len(flattened) != len(set(flattened))
        or BUILDER.SOURCE_REF in complete_deny_set
        or record.get("target_ref") in complete_deny_set
        or "main" not in deny_refs["protected_refs"]
    ):
        raise FormalLaunchError("negative-ref manifest deny set is invalid")
    return BUILDER.sha256_bytes(
        BUILDER.canonical_bytes(
            [
                {
                    "repo_full_name": BUILDER.REPO_FULL_NAME,
                    "deny_refs": complete_deny_set,
                }
            ]
        )
    )


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
    agreement_sha256 = sha256_file(agreement_path)
    if digest_record != {
        "schema_version": BUILDER.AGREEMENT_DIGEST_SCHEMA,
        "agreement_sha256": agreement_sha256,
    }:
        raise FormalLaunchError("agreement digest record is invalid")
    if (
        agreement.get("schema_version") != BUILDER.AGREEMENT_SCHEMA
        or agreement.get("status") != "user_agreed_private_control_ready"
        or agreement.get("scope") != "T3_only_RAG_vs_no_RAG"
        or agreement.get("formal_run_count") != 6
        or agreement.get("pair_count") != 3
    ):
        raise FormalLaunchError("agreement study identity or run inventory is invalid")
    if (
        agreement.get("source_ref") != BUILDER.SOURCE_REF
        or agreement.get("source_commit") != BUILDER.SOURCE_COMMIT
        or agreement.get("control_ref") != BUILDER.CONTROL_REF
        or agreement.get("image_digest") != BUILDER.IMAGE_DIGEST
        or agreement.get("index_sha256") != BUILDER.INDEX_SHA256
        or agreement.get("private_t3_scorer_sha256")
        != BUILDER.PRIVATE_SCORER_SHA256
        or agreement.get("architecture_mode") != "single_agent"
        or agreement.get("iteration_controls") != BUILDER.ITERATION_CONTROLS
        or agreement.get("shared_budget") != BUILDER.SHARED_BUDGET
    ):
        raise FormalLaunchError("agreement source, image, treatment or budget drifted")
    if agreement.get("provider_identity") != BUILDER.provider_identity():
        raise FormalLaunchError("agreement provider identity drifted")
    if heads.get(BUILDER.SOURCE_REF) != BUILDER.SOURCE_COMMIT:
        raise FormalLaunchError("remote E2 T3 source ref is absent or drifted")
    if heads.get(BUILDER.CONTROL_REF) != agreement.get("control_commit"):
        raise FormalLaunchError("remote E2 T3 control ref is absent or drifted")
    image_check(BUILDER.IMAGE_DIGEST)

    try:
        static = BUILDER._validate_static_inputs()
    except Exception as exc:
        raise FormalLaunchError(str(exc)) from exc
    for field in (
        "task_sha256",
        "canonical_task_sha256",
        "public_inputs_sha256",
        "tool_implementation_files_sha256",
        "tool_implementation_sha256",
        "control_files_sha256",
        "control_commit",
    ):
        agreement_field = "task_text_sha256" if field == "task_sha256" else field
        if agreement.get(agreement_field) != static[field]:
            raise FormalLaunchError(f"agreement {agreement_field} binding drifted")
    if agreement.get("prospective_study_control_sha256") != sha256_file(
        BUILDER.PROTOCOL_PATH
    ):
        raise FormalLaunchError("prospective study control hash drifted")

    evaluator = _validate_evaluator_manifest(
        _load_json(control_dir / "private_evaluator_hashes.json")
    )
    if agreement.get("evaluator_hashes_sha256") != BUILDER.sha256_bytes(
        BUILDER.canonical_bytes(evaluator)
    ):
        raise FormalLaunchError("private evaluator hash binding drifted")
    private_map_value = _load_json(
        control_dir / "blinding" / "private_condition_map.json"
    )
    if agreement.get("private_condition_map_sha256") != BUILDER.sha256_bytes(
        BUILDER.canonical_bytes(private_map_value)
    ):
        raise FormalLaunchError("private condition map hash drifted")
    submissions = private_map_value.get("submissions")
    if not isinstance(submissions, dict):
        raise FormalLaunchError("private condition map is malformed")

    runs = agreement.get("runs")
    execution_order = agreement.get("execution_order")
    if (
        not isinstance(runs, list)
        or len(runs) != 6
        or not isinstance(execution_order, list)
        or execution_order != [item.get("submission_id") for item in runs]
        or set(execution_order) != set(submissions)
    ):
        raise FormalLaunchError("agreement execution order does not match run records")

    validator = Draft202012Validator(
        json.loads(BUILDER.REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    prepared: list[dict[str, Any]] = []
    pair_requests: dict[int, dict[str, dict[str, Any]]] = {}
    retrieval_hashes: set[str] = set()
    for record in runs:
        submission_id = record.get("submission_id")
        private = submissions.get(submission_id) or {}
        condition = private.get("condition")
        replicate = private.get("replicate")
        if condition not in {"C0", "C1"} or replicate not in {1, 2, 3}:
            raise FormalLaunchError("private run condition or replicate is invalid")
        files = record.get("files") or {}
        hashes = record.get("file_sha256") or {}
        loaded: dict[str, dict[str, Any]] = {}
        for name in ("request", "identity", "negative_ref_manifest"):
            path = control_dir / str(files.get(name, ""))
            if not path.is_file() or sha256_file(path) != hashes.get(name):
                raise FormalLaunchError(f"run control binding mismatch: {submission_id}/{name}")
            loaded[name] = _load_json(path)
        request = loaded["request"]
        identity = loaded["identity"]
        manifest = loaded["negative_ref_manifest"]
        validator.validate(request)
        if BUILDER.sha256_bytes(BUILDER.canonical_bytes(identity)) != record.get(
            "identity_sha256"
        ):
            raise FormalLaunchError("run identity hash mismatch")
        if BUILDER.sha256_bytes(BUILDER.canonical_bytes(manifest)) != record.get(
            "negative_ref_manifest_sha256"
        ):
            raise FormalLaunchError("negative-ref manifest hash mismatch")
        if _validate_negative_ref_manifest(manifest, record) != record.get(
            "deny_ref_set_sha256"
        ):
            raise FormalLaunchError("negative-ref set hash mismatch")
        if (
            identity.get("condition") != condition
            or identity.get("replicate") != replicate
            or identity.get("source_ref") != BUILDER.SOURCE_REF
            or identity.get("source_commit") != BUILDER.SOURCE_COMMIT
            or identity.get("control_ref") != BUILDER.CONTROL_REF
            or identity.get("control_commit") != agreement.get("control_commit")
            or identity.get("image_digest") != BUILDER.IMAGE_DIGEST
        ):
            raise FormalLaunchError("private condition or frozen run identity differs")
        if request.get("run_id") != submission_id:
            raise FormalLaunchError("request run ID mismatch")
        repository = (request.get("repository_details") or [{}])[0]
        if (
            request.get("architecture_mode") != "single_agent"
            or repository.get("source_branch") != BUILDER.SOURCE_REF
            or repository.get("target_branch") != record.get("target_ref")
            or request.get("iteration_controls") != BUILDER.ITERATION_CONTROLS
        ):
            raise FormalLaunchError("request architecture, source, target or attempts drifted")
        parameters = request["execution_objectives"]["parsed_task_parameters"]
        if (
            parameters.get("rag_enabled") is not (condition == "C1")
            or parameters.get("formal_execution_contract")
            != "exp2_single_agent_rag_v1"
            or parameters.get("rag_top_k") != 5
            or parameters.get("quant_calculator_enabled") is not True
            or parameters.get("quant_calculator_schema_path")
            != BUILDER.T3_SCHEMA_PATH
            or parameters.get("model") != BUILDER.MODEL_ALIAS
        ):
            raise FormalLaunchError("request treatment, model or calculation tool drifted")
        retrieval = request.get("retrieval_context")
        if condition == "C0" and retrieval is not None:
            raise FormalLaunchError("C0 request contains retrieval context")
        if condition == "C1":
            if (
                not isinstance(retrieval, dict)
                or retrieval.get("enabled") is not True
                or retrieval.get("index_id") != BUILDER.INDEX_ID
                or retrieval.get("index_sha256") != BUILDER.INDEX_SHA256
                or len(retrieval.get("memories") or []) > 5
            ):
                raise FormalLaunchError("C1 retrieval context is absent or drifted")
            retrieval_hashes.add(
                BUILDER.sha256_bytes(BUILDER.canonical_bytes(retrieval))
            )
        pair_requests.setdefault(replicate, {})[condition] = request
        prepared.append(
            {
                **copy.deepcopy(record),
                "condition": condition,
                "request": request,
                "identity": identity,
                "manifest": manifest,
                "run_dir": control_dir / "runs" / submission_id,
            }
        )
    if retrieval_hashes != {agreement.get("retrieval_context_sha256")}:
        raise FormalLaunchError("the three C1 requests do not share one frozen retrieval")
    for replicate, requests in pair_requests.items():
        if set(requests) != {"C0", "C1"} or BUILDER.pair_projection(
            requests["C0"]
        ) != BUILDER.pair_projection(requests["C1"]):
            raise FormalLaunchError(f"T3-R{replicate} differs beyond RAG treatment")
    return {
        "control_dir": control_dir,
        "agreement": agreement,
        "agreement_sha256": agreement_sha256,
        "runs": prepared,
        "heads": dict(heads),
    }


def _json_lines(value: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in value.splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            records.append(candidate)
    return records


def _preflight_scorer(agreement: Mapping[str, Any]) -> dict[str, Any]:
    if sha256_file(BUILDER.PRIVATE_SCORER_PATH) != agreement.get(
        "private_t3_scorer_sha256"
    ):
        raise FormalLaunchError("private T3 scorer hash does not match agreement")
    inputs = (
        BUILDER.T3_ROOT / "input" / "synthetic_portfolio_returns_v1.csv",
        BUILDER.T3_ROOT / "input" / "portfolio_config_v1.json",
        BUILDER.T3_ROOT / "output_schema_v1.json",
    )
    with tempfile.TemporaryDirectory(prefix="e2-t3-score-preflight-") as raw:
        output_path = Path(raw) / "score.json"
        completed = subprocess.run(
            [
                sys.executable,
                str(BUILDER.PRIVATE_SCORER_PATH),
                "--csv",
                str(inputs[0]),
                "--config",
                str(inputs[1]),
                "--schema",
                str(inputs[2]),
                "--scope-passed",
                "false",
                "--run-id",
                "E2T3RAGV5-SCORER-PREFLIGHT",
                "--architecture-mode",
                "single_agent",
                "--output",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        records = _json_lines(completed.stdout)
        score = records[-1] if records else None
        if (
            completed.returncode != 0
            or not output_path.is_file()
            or not isinstance(score, dict)
            or score.get("total_items") != 25
        ):
            raise FormalLaunchError("private T3 scorer zero-model preflight failed")
    return {"status": "ready", "provider_call_count": 0, "total_items": 25}


def _existing_local_evidence(submission_id: str) -> list[Path]:
    if not OUTPUT_ROOT.is_dir():
        return []
    return sorted(
        path
        for path in OUTPUT_ROOT.glob(f"{submission_id}-*")
        if (path / "formal_start_record.json").is_file()
    )


def _validate_prior_evidence(
    run: Mapping[str, Any], agreement_sha256: str
) -> None:
    evidence = _existing_local_evidence(str(run["submission_id"]))
    if len(evidence) != 1:
        raise FormalLaunchError(
            f"preceding formal evidence is missing or duplicated: {run['submission_id']}"
        )
    start = _load_json(evidence[0] / "formal_start_record.json")
    result = _load_json(evidence[0] / "launch_result.json")
    if (
        start.get("agreement_sha256") != agreement_sha256
        or start.get("submission_id") != run["submission_id"]
        or result.get("submission_id") != run["submission_id"]
        or result.get("complete_observation") is not True
    ):
        raise FormalLaunchError(
            f"preceding formal evidence does not match agreement: {run['submission_id']}"
        )


def preflight(
    control_dir: Path,
    *,
    replicate: int | None = None,
) -> dict[str, Any]:
    state = load_control(control_dir, heads=BUILDER.remote_heads())
    selected = [
        run for run in state["runs"] if replicate is None or run["replicate"] == replicate
    ]
    expected_count = 6 if replicate is None else 2
    if len(selected) != expected_count:
        raise FormalLaunchError("selected preflight run count is invalid")
    selected_ids = {run["submission_id"] for run in selected}
    for run in state["runs"]:
        existing_target = state["heads"].get(run["target_ref"])
        if run["submission_id"] in selected_ids:
            if existing_target is not None:
                raise FormalLaunchError(
                    f"selected formal target already exists: {run['target_ref']}"
                )
            if _existing_local_evidence(run["submission_id"]):
                raise FormalLaunchError(
                    f"formal evidence already exists for {run['submission_id']}"
                )
        elif replicate is not None and run["replicate"] < replicate:
            _validate_prior_evidence(run, state["agreement_sha256"])
        else:
            if existing_target is not None:
                raise FormalLaunchError(
                    f"future formal target already exists: {run['target_ref']}"
                )
            if _existing_local_evidence(run["submission_id"]):
                raise FormalLaunchError(
                    f"future formal evidence already exists: {run['submission_id']}"
                )
    records = []
    for run in selected:
        record = {
            "schema_version": "exp2-terra-t3-rag-observation-preflight-v1",
            "status": "ready",
            "formal_observation": True,
            "provider_call_count": 0,
            "execution_epoch": BUILDER.EXECUTION_EPOCH,
            "submission_id": run["submission_id"],
            "pair_id": run["pair_id"],
            "target_ref": run["target_ref"],
            "architecture_mode": "single_agent",
            "max_attempts": 2,
            "agreement_sha256": state["agreement_sha256"],
            "image_digest": BUILDER.IMAGE_DIGEST,
        }
        records.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)
    scorer = _preflight_scorer(state["agreement"])
    summary = {
        "schema_version": "exp2-terra-t3-rag-preflight-v1",
        "status": "ready",
        "formal_observation": True,
        "provider_call_count": 0,
        "execution_epoch": BUILDER.EXECUTION_EPOCH,
        "agreement_sha256": state["agreement_sha256"],
        "preflight_count": len(records),
        "pair_count": 3 if replicate is None else 1,
        "conditions": ["C0", "C1"],
        "rag_enabled": {"C0": False, "C1": True},
        "max_attempts": 2,
        "scorer_preflight": scorer,
    }
    print(json.dumps(summary, sort_keys=True), flush=True)
    return {**state, "selected": selected, "preflight_summary": summary}


def _credentials(replicate: int) -> tuple[str, str, str | None]:
    expected = f"AUTHORIZE_{BUILDER.EXECUTION_EPOCH}_T3_R{replicate}_TWO_OBSERVATIONS"
    if os.getenv(AUTHORIZATION_ENV) != expected:
        raise FormalLaunchError(f"set {AUTHORIZATION_ENV}={expected}")
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        openai_key = getpass.getpass(
            f"Paste OPENAI_API_KEY once for E2 T3 replicate {replicate} pair (hidden): "
        ).strip()
    if not openai_key.startswith("sk-") or any(ch.isspace() for ch in openai_key):
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
        values = (
            dict(
                line.split("=", 1)
                for line in completed.stdout.splitlines()
                if "=" in line
            )
            if completed.returncode == 0
            else {}
        )
        github_token = values.get("password")
    if not github_token:
        raise FormalLaunchError("GitHub credential helper returned no token")
    return openai_key, github_token, github_username


def _docker_command(
    *,
    run: Mapping[str, Any],
    input_dir: Path,
    result_dir: Path,
    environment_names: list[str],
) -> list[str]:
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
            "E3_NEGATIVE_REF_MANIFEST_PATH=/workspace/input/negative_ref_manifest.json",
            "-e",
            f"E3_NEGATIVE_REF_MANIFEST_SHA256={run['negative_ref_manifest_sha256']}",
            "-e",
            f"E3_NEGATIVE_REF_SET_SHA256={run['deny_ref_set_sha256']}",
            "-e",
            f"E3_RUN_IDENTITY_SHA256={run['identity_sha256']}",
            "-e",
            "MAX_BRANCHES_PER_RUN=1",
            "-e",
            "MAX_COMMITS_PER_RUN=3",
            BUILDER.IMAGE_DIGEST,
            "python",
            "run.py",
        ]
    )
    return command


def _positive_model_tokens(result: Mapping[str, Any]) -> bool:
    telemetry = result.get("telemetry") or {}
    if any(
        isinstance(item, dict) and int(item.get("total_tokens") or 0) > 0
        for item in telemetry.get("model_usage") or []
    ):
        return True
    calls = ((telemetry.get("experiment3") or {}).get("provider_calls") or [])
    return any(
        isinstance(item, dict)
        and item.get("status") == "succeeded"
        and int(item.get("total_tokens") or 0) > 0
        for item in calls
    )


def _treatment_evidence(result: Mapping[str, Any], condition: str) -> bool:
    retrieval = (result.get("telemetry") or {}).get("retrieval") or {}
    if condition == "C0":
        return (
            retrieval.get("enabled") is False
            and retrieval.get("delivery_mode") == "disabled"
            and retrieval.get("prompt_injected") is False
        )
    return (
        retrieval.get("enabled") is True
        and retrieval.get("delivery_mode") == "generator_prompt"
        and retrieval.get("prompt_injected") is True
        and retrieval.get("status") == "ok"
        and retrieval.get("index_id") == BUILDER.INDEX_ID
        and retrieval.get("index_sha256") == BUILDER.INDEX_SHA256
    )


def _observation_admission_failures(
    result: Mapping[str, Any] | None,
    *,
    submission_id: str,
    model_evidence: bool,
    treatment_evidence: bool,
) -> list[str]:
    """Name each missing proof without exposing prompts, retrieval text or answers."""

    if result is None:
        return ["result_json_missing"]
    failures: list[str] = []
    if result.get("run_id") != submission_id:
        failures.append("run_id_mismatch")
    result_status = (result.get("execution_summary") or {}).get("status")
    if result_status not in {"succeeded", "failed"}:
        failures.append("terminal_result_status_missing")
    if not model_evidence:
        failures.append("positive_model_token_evidence_missing")
    if not treatment_evidence:
        failures.append("condition_delivery_evidence_missing")
    return failures


def _execute_one(
    run: Mapping[str, Any],
    *,
    agreement_sha256: str,
    child_env: dict[str, str],
    environment_names: list[str],
) -> dict[str, Any]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (OUTPUT_ROOT / f"{run['submission_id']}-{stamp}").resolve()
    if output_dir.exists():
        raise FormalLaunchError(f"output directory already exists: {output_dir}")
    input_dir = output_dir / "input"
    result_dir = output_dir / "output"
    input_dir.mkdir(parents=True)
    result_dir.mkdir()
    shutil.copy2(
        run["run_dir"] / "negative_ref_manifest.json",
        input_dir / "negative_ref_manifest.json",
    )
    start_record = {
        "schema_version": "exp2-terra-t3-rag-formal-start-v1",
        "formal_observation": True,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "submission_id": run["submission_id"],
        "pair_id": run["pair_id"],
        "replicate": run["replicate"],
        "condition": run["condition"],
        "rag_enabled": run["condition"] == "C1",
        "agreement_sha256": agreement_sha256,
        "identity_sha256": run["identity_sha256"],
        "negative_ref_manifest_sha256": run["negative_ref_manifest_sha256"],
        "source_ref": BUILDER.SOURCE_REF,
        "source_commit": BUILDER.SOURCE_COMMIT,
        "control_ref": BUILDER.CONTROL_REF,
        "control_commit": run["identity"]["control_commit"],
        "target_ref": run["target_ref"],
        "image_digest": BUILDER.IMAGE_DIGEST,
        "provider_identity_sha256": BUILDER.provider_identity()["sha256"],
    }
    (output_dir / "formal_start_record.json").write_text(
        json.dumps(start_record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    command = _docker_command(
        run=run,
        input_dir=input_dir,
        result_dir=result_dir,
        environment_names=environment_names,
    )
    stdout_path = output_dir / "container.stdout.log"
    stderr_path = output_dir / "container.stderr.log"
    started = datetime.now(timezone.utc)
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        completed = subprocess.run(
            command,
            input=json.dumps(run["request"], ensure_ascii=False),
            text=True,
            stdout=stdout_handle,
            stderr=stderr_handle,
            env=child_env,
            timeout=3_900,
        )
    ended = datetime.now(timezone.utc)
    result_path = result_dir / "result.json"
    result = _load_json(result_path) if result_path.is_file() else None
    result_status = ((result or {}).get("execution_summary") or {}).get("status")
    model_evidence = bool(result and _positive_model_tokens(result))
    treatment_evidence = bool(
        result and _treatment_evidence(result, str(run["condition"]))
    )
    admission_failures = _observation_admission_failures(
        result,
        submission_id=str(run["submission_id"]),
        model_evidence=model_evidence,
        treatment_evidence=treatment_evidence,
    )
    complete_observation = not admission_failures
    summary = {
        "schema_version": "exp2-terra-t3-rag-formal-launch-result-v1",
        "formal_observation": True,
        "submission_id": run["submission_id"],
        "pair_id": run["pair_id"],
        "replicate": run["replicate"],
        "condition": run["condition"],
        "rag_enabled": run["condition"] == "C1",
        "target_ref": run["target_ref"],
        "container_exit_code": completed.returncode,
        "result_present": result is not None,
        "result_status": result_status,
        "model_call_evidence": model_evidence,
        "treatment_evidence": treatment_evidence,
        "complete_observation": complete_observation,
        "admission_failures": admission_failures,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "latency_seconds": round((ended - started).total_seconds(), 6),
        "output_directory": str(output_dir),
    }
    (output_dir / "launch_result.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not complete_observation:
        raise FormalLaunchError(
            "observation admission failed: "
            f"{run['submission_id']}; reasons={','.join(admission_failures)}"
        )
    return summary


def _fetch_submission(
    target_ref: str, destination: Path
) -> tuple[Path | None, bool, list[str]]:
    with tempfile.TemporaryDirectory(prefix="e2-t3-score-git-") as raw:
        bare = Path(raw) / "repo.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "fetch",
                "--no-tags",
                "--depth=200",
                "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                f"refs/heads/{target_ref}:refs/heads/score-target",
            ],
            capture_output=True,
            text=True,
        )
        if fetched.returncode != 0:
            if "couldn't find remote ref" in fetched.stderr.lower():
                return None, False, []
            raise FormalLaunchError("T3 target branch could not be read for scoring")
        changed = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "diff",
                "--name-only",
                BUILDER.SOURCE_COMMIT,
                "refs/heads/score-target",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        scope_passed = changed == [BUILDER.T3_TARGET_PATH]
        shown = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "show",
                f"refs/heads/score-target:{BUILDER.T3_TARGET_PATH}",
            ],
            capture_output=True,
        )
        if shown.returncode != 0:
            return None, scope_passed, changed
        destination.write_bytes(shown.stdout)
        return destination, scope_passed, changed


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _validated_jsonl_lines(path: Path) -> list[str]:
    if path.is_symlink() or not path.is_file():
        raise FormalLaunchError(f"calculation audit is not a regular file: {path.name}")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise FormalLaunchError(f"calculation audit is unreadable: {path.name}") from exc
    if not raw_lines:
        raise FormalLaunchError(f"calculation audit is empty: {path.name}")
    for line_number, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            raise FormalLaunchError(
                f"calculation audit contains a blank record: {path.name}:{line_number}"
            )
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FormalLaunchError(
                f"calculation audit contains invalid JSON: {path.name}:{line_number}"
            ) from exc
        if not isinstance(record, dict):
            raise FormalLaunchError(
                f"calculation audit record is not an object: {path.name}:{line_number}"
            )
    return raw_lines


def _merge_numbered_mcp_audits(output_directory: Path) -> Path | None:
    artifact_dir = output_directory / "output" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    numbered = [
        artifact_dir / "mcp_tool_audit_attempt_1.jsonl",
        artifact_dir / "mcp_tool_audit_attempt_2.jsonl",
    ]
    destination = artifact_dir / "mcp_tool_audit.jsonl"
    manager_destination = artifact_dir / "exp3_mcp_tool_audit.jsonl"

    if _path_present(manager_destination) or list(
        artifact_dir.glob("exp3_mcp_tool_audit_attempt_*.jsonl")
    ):
        raise FormalLaunchError(
            "single-agent and manager-star calculation audits are mixed"
        )

    recognised = {path.name for path in numbered}
    unexpected = [
        path
        for path in artifact_dir.glob("mcp_tool_audit_attempt_*.jsonl")
        if path.name not in recognised
    ]
    if unexpected:
        raise FormalLaunchError("calculation audit contains an unsupported attempt number")

    present = [_path_present(path) for path in numbered]
    if present == [False, False]:
        if _path_present(destination):
            raise FormalLaunchError(
                "legacy calculation audit exists without numbered v2 attempt evidence"
            )
        return None
    if present == [False, True]:
        raise FormalLaunchError("attempt 2 calculation audit exists without attempt 1")

    merged_lines: list[str] = []
    for path, exists in zip(numbered, present, strict=True):
        if exists:
            merged_lines.extend(_validated_jsonl_lines(path))
    merged = "\n".join(merged_lines) + "\n"

    if _path_present(destination):
        if destination.is_symlink() or not destination.is_file():
            raise FormalLaunchError(
                "legacy calculation audit destination is not a regular file"
            )
        try:
            existing = destination.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise FormalLaunchError(
                "legacy calculation audit destination is unreadable"
            ) from exc
        if existing != merged:
            raise FormalLaunchError(
                "legacy calculation audit conflicts with numbered v2 evidence"
            )
        return destination

    try:
        destination.write_text(merged, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise FormalLaunchError(
            "numbered calculation audits could not be merged"
        ) from exc
    return destination


def _score(
    *,
    run: Mapping[str, Any],
    output_directory: Path,
    agreement: Mapping[str, Any],
) -> dict[str, Any]:
    if sha256_file(BUILDER.PRIVATE_SCORER_PATH) != agreement.get(
        "private_t3_scorer_sha256"
    ):
        raise FormalLaunchError("private T3 scorer hash does not match agreement")
    artifact_dir = output_directory / "output" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = artifact_dir / "t3_submission_candidate.json"
    submission, scope_passed, changed_paths = _fetch_submission(
        run["target_ref"], candidate_path
    )
    checkpoint = artifact_dir / "t3_quant_checkpoints.json"
    audit = _merge_numbered_mcp_audits(output_directory)
    score_path = artifact_dir / "t3_score_25.json"
    command = [
        sys.executable,
        str(BUILDER.PRIVATE_SCORER_PATH),
        "--csv",
        str(BUILDER.T3_ROOT / "input" / "synthetic_portfolio_returns_v1.csv"),
        "--config",
        str(BUILDER.T3_ROOT / "input" / "portfolio_config_v1.json"),
        "--schema",
        str(BUILDER.T3_ROOT / "output_schema_v1.json"),
        "--scope-passed",
        "true" if scope_passed else "false",
        "--run-id",
        run["submission_id"],
        "--architecture-mode",
        "single_agent",
        "--output",
        str(score_path),
    ]
    if submission is not None:
        command.extend(["--submission", str(submission)])
    if checkpoint.is_file():
        command.extend(["--checkpoints", str(checkpoint)])
    if audit is not None:
        command.extend(["--audit", str(audit)])
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    score = json.loads(completed.stdout.splitlines()[-1])
    score["condition"] = run["condition"]
    score["rag_enabled"] = run["condition"] == "C1"
    score["changed_paths"] = changed_paths
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return score


def execute_pair(state: dict[str, Any], replicate: int) -> dict[str, Any]:
    selected = [run for run in state["selected"] if run["replicate"] == replicate]
    expected_order = list(BUILDER.PAIR_ORDER[replicate])
    if len(selected) != 2 or [run["condition"] for run in selected] != expected_order:
        raise FormalLaunchError("selected pair order does not match agreement")
    openai_key, github_token, github_username = _credentials(replicate)
    child_env = os.environ.copy()
    for name in (
        "API_KEY",
        "MODEL",
        "LITELLM_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "LITELLM_PROXY_API_KEY",
        "RAE_OFFLINE",
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
            "RAE_ENABLE_MANAGER_STAR": "false",
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
        "RAE_ENABLE_MANAGER_STAR",
        "ALLOWED_REPOS",
    ]
    if github_username:
        environment_names.append("GITHUB_USERNAME")

    results: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    scoring_errors: list[dict[str, str]] = []
    for run in selected:
        if BUILDER.remote_heads().get(run["target_ref"]) is not None:
            raise FormalLaunchError(f"target appeared before execution: {run['target_ref']}")
        result = _execute_one(
            run,
            agreement_sha256=state["agreement_sha256"],
            child_env=child_env,
            environment_names=environment_names,
        )
        results.append(result)
        try:
            score = _score(
                run=run,
                output_directory=Path(result["output_directory"]),
                agreement=state["agreement"],
            )
        except Exception as exc:
            scoring_errors.append(
                {
                    "submission_id": run["submission_id"],
                    "failure_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )
            score_summary = {
                "status": "scoring_incomplete",
                "failure_type": type(exc).__name__,
            }
        else:
            scores.append(score)
            score_summary = {
                "status": "scored",
                "correct_items": score["correct_items"],
                "total_items": score["total_items"],
                "correct_numeric_cells": score["correct_numeric_cells"],
                "total_numeric_cells": score["total_numeric_cells"],
                "complete_task": score["complete_task"],
            }
        print(
            json.dumps(
                {
                    "schema_version": "exp2-terra-t3-rag-pair-progress-v2",
                    "status": "observation_recorded",
                    "submission_id": run["submission_id"],
                    "replicate": replicate,
                    "condition": run["condition"],
                    "result_status": result["result_status"],
                    "output_directory": result["output_directory"],
                    "score": score_summary,
                    "completed_count": len(results),
                    "remaining_count": 2 - len(results),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    summary = {
        "schema_version": "exp2-terra-t3-rag-pair-result-v2",
        "status": (
            "completed"
            if not scoring_errors
            else "pair_observations_completed_scoring_incomplete"
        ),
        "formal_observation": True,
        "execution_epoch": BUILDER.EXECUTION_EPOCH,
        "replicate": replicate,
        "agreement_sha256": state["agreement_sha256"],
        "observation_count": 2,
        "succeeded_count": sum(item["result_status"] == "succeeded" for item in results),
        "failed_count": sum(item["result_status"] == "failed" for item in results),
        "scoring_error_count": len(scoring_errors),
        "scoring_errors": scoring_errors,
        "scores": [
            {
                "submission_id": score["run_id"],
                "condition": score["condition"],
                "rag_enabled": score["rag_enabled"],
                "correct_items": score["correct_items"],
                "total_items": score["total_items"],
                "correct_numeric_cells": score["correct_numeric_cells"],
                "total_numeric_cells": score["total_numeric_cells"],
                "complete_task": score["complete_task"],
            }
            for score in scores
        ],
    }
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    parser.add_argument("--replicate", type=int, choices=(1, 2, 3))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if args.execute and args.replicate is None:
            raise FormalLaunchError("--execute requires one explicit --replicate")
        state = preflight(args.control_dir, replicate=args.replicate)
        if not args.execute:
            return 0
        execute_pair(state, args.replicate)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "formal_observation": True,
                    "failure_type": type(exc).__name__,
                    "message": str(exc)[:1000],
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
