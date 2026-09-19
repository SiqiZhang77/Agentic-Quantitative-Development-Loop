#!/usr/bin/env python3
"""Build the runnable, zero-model E4V1 launch package."""

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

from jsonschema import Draft202012Validator, FormatChecker

try:
    from . import identity, preflight_e4v1
except ImportError:  # pragma: no cover
    import identity  # type: ignore[no-redef]
    import preflight_e4v1  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[1]
FIXED_TIMESTAMP = "2026-09-03T18:00:00+00:00"
TARGET_PATH = (
    "experiments/shared/t3-quant-suite-v2/submissions/"
    "quant_portfolio_analytics.json"
)
LAUNCH_CONTROL_FILES = (
    *identity.EXPECTED_CONTROL_FILES,
    "experiments/exp3/e3v17/run_e3v17_pair.py",
    "experiments/exp3/e3v17/item_store_projection.py",
    "experiments/prospective_freezes/e3v17/build_scorer_result_v2.py",
    "experiments/prospective_freezes/e3v17/model_visible_trace_schema_v1.json",
)
PRIOR_STUDY_REFS = (
    "exp/shared-t3-runtime-v1",
    "exp/shared-t3-runtime-v2",
    "exp/shared-t3-runtime-v3",
    "exp/shared-t3-runtime-v6",
    *(f"quant/E3V{version}-T3-R{replicate}-{arm}" for version in range(8, 18) for replicate in range(1, 6) for arm in ("M0", "M1")),
)


class LaunchPackageError(RuntimeError):
    """The frozen E4 agreement cannot safely become executable."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchPackageError(f"cannot read launch evidence: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_canonical(value) + b"\n")


def _git(args: list[str]) -> str:
    completed = subprocess.run(["git", *args], cwd=CONTROL_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise LaunchPackageError(f"launch control Git check failed: {detail}")
    return completed.stdout.strip()


def _launch_control(control_ref: str, control_commit: str) -> dict[str, Any]:
    if _git(["rev-parse", "HEAD"]) != control_commit:
        raise LaunchPackageError("launch checkout HEAD differs from --control-commit")
    if _git(["status", "--porcelain"]):
        raise LaunchPackageError("launch checkout must be clean")
    files: dict[str, str] = {}
    for relative in LAUNCH_CONTROL_FILES:
        path = CONTROL_ROOT / relative
        if not path.is_file():
            raise LaunchPackageError(f"launch control file is missing: {relative}")
        files[relative] = _sha256_file(path)
    return {
        "repository": "bankingscience/BSLAgenticQuantDevLoop",
        "control_ref": control_ref,
        "control_commit": control_commit,
        "files_sha256": files,
        "files_fingerprint_sha256": _sha256_bytes(_canonical(files)),
    }


def _flatten_schedule(schedule: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = [copy.deepcopy(item) for block in schedule for item in block.get("observations", [])]
    if len(values) != 20 or len({row.get("run_id") for row in values}) != 20:
        raise LaunchPackageError("agreement schedule does not contain 20 unique observations")
    return values


def _request(
    *,
    run_identity: dict[str, Any],
    sequence: int,
    task_text: str,
    config: dict[str, Any],
    retrieval_context: dict[str, Any],
) -> dict[str, Any]:
    run_id = run_identity["run_id"]
    task = config["task_bindings"]["T3"]
    runtime = config["runtime"]
    budget = config["shared_budget"]
    rag_enabled = run_identity["rag_enabled"]
    launch_text = "Execute the complete frozen E4V1 T3 task using only the declared source, target and shared tools."
    value: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "architecture_mode": run_identity["architecture_mode"],
        "repository_details": [{
            "alias": "BSLAgenticQuantDevLoop",
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
            "source_branch": config["source"]["source_ref"],
            "target_branch": run_identity["target_ref"],
            "runtime_role": "default",
            "allowed_directories": [task["suite_root"]],
        }],
        "jira_metadata": {
            "ticket_id": f"E4V1-{sequence:03d}",
            "current_status": "Approved local formal execution",
            "summary": f"E4V1 T3 replicate {run_identity['replicate']}",
            "description": "Complete the hash-bound E4V1 T3 v2 task supplied as the canonical parsed objective.",
            "triggering_comment": {"comment_id": run_id, "timestamp": FIXED_TIMESTAMP, "author": "experiment-operator", "text": launch_text},
            "events_history": [{"event_type": "comment", "timestamp": FIXED_TIMESTAMP, "author": "experiment-operator", "text": launch_text}],
        },
        "execution_objectives": {
            "strategy_type": "other",
            "target_date_range": {},
            "parsed_task_parameters": {
                "rag_enabled": rag_enabled,
                "rag_top_k": config["retrieval_binding"]["requested_top_k"],
                "formal_execution_contract": runtime["formal_execution_contract"],
                "rag_delivery_policy": runtime["rag_delivery_policy"],
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": task["output_schema_path"],
                "provider_mode": runtime["provider_mode"],
                "provider_base_url": runtime["provider_base_url"],
                "provider_identity_sha256": runtime["provider_identity_sha256"],
                "model": runtime["model_alias"],
                "answer_capture_profile": task["answer_capture_profile"],
                "t3_item_submission_enabled": True,
                "t3_item_submission_schema_path": task["item_submission_schema_path"],
                "t3_primary_outcome": "item_correctness_25",
                "objective": task_text,
            },
            "system_instruction_override": None,
            "resource_path": task["task_text_path"],
            "target_path": TARGET_PATH,
        },
        "iteration_controls": {
            "allow_iteration": True,
            "max_iterations": budget["max_attempts"],
            "max_failed_iterations": budget["max_attempts"],
            "max_agent_turns": budget["max_model_calls_per_attempt"],
            "max_commits_per_run": 3,
            "timeout_seconds": budget["attempt_timeout_seconds"],
            "max_token_budget_per_run": budget["max_tokens_per_observation"],
        },
        "resource_requirements": {
            "cpu_vcpus": budget["cpu_vcpus"], "memory_mb": budget["memory_mb"],
            "gpu_count": 0, "execution_timeout_seconds": budget["observation_timeout_seconds"],
            "pids_limit": budget["pids_limit"],
        },
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    if rag_enabled:
        value["retrieval_context"] = copy.deepcopy(retrieval_context)
    schema = _load_json(CONTROL_ROOT / "rae_runtime/sandbox/schemas/runtime_request.schema.json")
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)
    return value


def _negative_manifest(run_identity: dict[str, Any], all_runs: list[dict[str, Any]]) -> tuple[dict[str, Any], str, str]:
    current = run_identity["target_ref"]
    source_ref = run_identity["source"]["source_ref"]
    prior_refs = [ref for ref in PRIOR_STUDY_REFS if ref != source_ref]
    same_replicate = sorted(row["target_ref"] for row in all_runs if row["replicate"] == run_identity["replicate"] and row["target_ref"] != current)
    paired = same_replicate[0]
    other_results = sorted(row["target_ref"] for row in all_runs if row["target_ref"] not in {current, paired})
    probe_a = f"exp4/probe/E4V1/{run_identity['run_id']}/a"
    probe_b = f"exp4/probe/E4V1/{run_identity['run_id']}/b"
    value = {
        "schema_version": "rae-negative-ref-manifest-v2",
        "experiment_id": "E4V1",
        "run_id": run_identity["run_id"],
        "repositories": [{
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "source_ref": source_ref,
            "current_target_ref": current,
            "paired_target_ref": paired,
            "deny_refs": {
                "prior_study_refs": prior_refs,
                "other_result_refs": other_results,
                "protected_refs": ["main"],
                "arbitrary_probe_refs": [probe_a, probe_b],
            },
        }],
    }
    denied = sorted({paired, *prior_refs, *other_results, "main", probe_a, probe_b})
    deny_value = [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop", "deny_refs": denied}]
    return value, _sha256_bytes(_canonical(value)), _sha256_bytes(_canonical(deny_value))


def build(
    *,
    freeze_package: Path,
    agreement_dir: Path,
    control_ref: str,
    control_commit: str,
    output: Path,
) -> dict[str, Any]:
    config = identity.load_config(freeze_package / "freeze_config.json")
    identity.require_ready(config)
    for condition in identity.CONDITION_CODES:
        result = preflight_e4v1.preflight(config, condition_code=condition)
        if result["status"] != "ready" or result["provider_call_count"] != 0 or result["observation_count"] != 5:
            raise LaunchPackageError(f"zero-model identity preflight failed for {condition}")
    agreement = _load_json(agreement_dir / "agreement.json")
    schedule = _load_json(agreement_dir / "schedule.json")
    digest = _load_json(agreement_dir / "agreement.sha256.json")
    if not isinstance(agreement, dict) or not isinstance(schedule, list) or not isinstance(digest, dict):
        raise LaunchPackageError("agreement files have invalid root types")
    unsigned = copy.deepcopy(agreement)
    unsigned.pop("agreement_sha256", None)
    calculated = identity.sha256_value({"manifest": unsigned, "schedule": schedule})
    if agreement.get("agreement_sha256") != calculated or digest.get("agreement_sha256") != calculated:
        raise LaunchPackageError("agreement logical SHA-256 differs")
    if agreement.get("source") != config["source"] or agreement.get("runtime") != config["runtime"] or agreement.get("execution") != config["execution"]:
        raise LaunchPackageError("agreement differs from resolved E4 config")
    launch_control = _launch_control(control_ref, control_commit)
    if config["control"]["control_ref"] != control_ref or config["control"]["control_commit"] != control_commit:
        raise LaunchPackageError("resolved control ref or commit differs")
    identities = _flatten_schedule(schedule)
    task_path = CONTROL_ROOT / config["task_bindings"]["T3"]["task_text_path"]
    if _sha256_file(task_path) != config["task_bindings"]["T3"]["task_text_sha256"]:
        raise LaunchPackageError("model-visible T3 task bytes changed")
    context = _load_json(freeze_package / "retrieval_context.json")
    expected_context_sha = config["retrieval_binding"]["task_query_context"]["T3"]["retrieval_context_sha256"]
    if _sha256_bytes(_canonical(context)) != expected_context_sha:
        raise LaunchPackageError("frozen retrieval context changed")
    output = output.resolve()
    if output.exists():
        raise LaunchPackageError(f"refusing to overwrite launch package: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        shutil.copytree(agreement_dir, staging / "agreement")
        shutil.copytree(freeze_package, staging / "evidence")
        run_records: list[dict[str, Any]] = []
        for sequence, run_identity in enumerate(identities, start=1):
            run_dir = staging / "runs" / run_identity["run_id"]
            run_dir.mkdir(parents=True)
            request = _request(run_identity=run_identity, sequence=sequence, task_text=task_path.read_text(encoding="utf-8"), config=config, retrieval_context=context)
            negative, negative_sha, deny_sha = _negative_manifest(run_identity, identities)
            _write_json(run_dir / "identity.json", run_identity)
            _write_json(run_dir / "request.json", request)
            _write_json(run_dir / "negative_ref_manifest.json", negative)
            run_records.append({
                "run_id": run_identity["run_id"], "task_id": "T3",
                "replicate": run_identity["replicate"], "block_position": run_identity["block_position"],
                "condition_code": run_identity["condition_code"], "architecture_mode": run_identity["architecture_mode"],
                "rag_enabled": run_identity["rag_enabled"], "target_ref": run_identity["target_ref"],
                "identity_sha256": run_identity["identity_sha256"],
                "negative_ref_manifest_sha256": negative_sha, "negative_ref_set_sha256": deny_sha,
                "file_sha256": {
                    "identity": _sha256_file(run_dir / "identity.json"),
                    "request": _sha256_file(run_dir / "request.json"),
                    "negative_ref_manifest": _sha256_file(run_dir / "negative_ref_manifest.json"),
                },
            })
        evidence_files = {
            str(path.relative_to(staging / "evidence")): _sha256_file(path)
            for path in sorted((staging / "evidence").rglob("*")) if path.is_file()
        }
        manifest: dict[str, Any] = {
            "schema_version": "exp4-e4v1-launch-package-v1",
            "status": "execution_authorized_not_started", "experiment_id": "E4V1",
            "agreement_sha256": calculated, "formal_model_execution_enabled": True,
            "provider_call_count_at_build": 0, "formal_observation_count_at_build": 0,
            "serial": True, "observations_per_authorization": 5,
            "continue_after_observation_failure": True,
            "first_authorized_condition": "M1R1",
            "launch_control": launch_control, "evidence_sha256": evidence_files,
            "runs": run_records,
        }
        manifest["launch_package_sha256"] = _sha256_bytes(_canonical(manifest))
        _write_json(staging / "launch_manifest.json", manifest)
        _write_json(staging / "launch_manifest.sha256.json", {"schema_version": "exp4-e4v1-launch-package-sha256-v1", "launch_package_sha256": manifest["launch_package_sha256"]})
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--freeze-package", type=Path, required=True)
    parser.add_argument("--agreement", type=Path, required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build(freeze_package=args.freeze_package.resolve(), agreement_dir=args.agreement.resolve(), control_ref=args.control_ref, control_commit=args.control_commit, output=args.output)
    except Exception as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "formal_observation_count": 0, "issue": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": manifest["status"], "agreement_sha256": manifest["agreement_sha256"], "launch_package_sha256": manifest["launch_package_sha256"], "run_count": len(manifest["runs"]), "provider_call_count": 0, "formal_observation_count": 0, "output": str(args.output.resolve())}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
