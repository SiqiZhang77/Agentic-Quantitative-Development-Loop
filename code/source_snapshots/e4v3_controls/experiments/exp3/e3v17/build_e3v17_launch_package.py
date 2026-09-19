#!/usr/bin/env python3
"""Build the separately bound E3V17 formal-launch package.

The frozen agreement intentionally contains no runnable request. This builder
may run only after that agreement passes all five zero-model pair preflights.
It deterministically adds request and negative-ref files, binds the launch
control commit, and still makes no provider call or formal observation.
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
    from . import controls, preflight_e3v17
except ImportError:  # pragma: no cover - direct script execution
    import controls  # type: ignore[no-redef]
    import preflight_e3v17  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[2]
FIXED_TIMESTAMP = "2026-09-02T16:00:00+00:00"
TARGET_PATH = (
    "experiments/shared/t3-quant-suite-v2/submissions/"
    "quant_portfolio_analytics.json"
)
LAUNCH_CONTROL_FILES = (
    "experiments/exp3/e3v17/build_e3v17_launch_package.py",
    "experiments/exp3/e3v17/run_e3v17_observation.py",
    "experiments/exp3/e3v17/run_e3v17_pair.py",
    "experiments/exp3/e3v17/item_store_projection.py",
)
PRIOR_STUDY_REFS = (
    "exp/shared-t3-runtime-v1",
    "exp3/e3v8-item-results-source",
    "quant/E3V8-T3-R1-M0",
    "quant/E3V8-T3-R1-M1",
    "quant/E3V8-T3-R2-M0",
    "quant/E3V8-T3-R2-M1",
    "quant/E3V8-T3-R3-M0",
    "quant/E3V8-T3-R3-M1",
    "quant/E3V8-T3-R4-M0",
    "quant/E3V8-T3-R4-M1",
    "quant/E3V8-T3-R5-M0",
    "quant/E3V8-T3-R5-M1",
    *(
        f"quant/E3V9-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V10-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V11-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V12-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V13-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V14-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V15-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
    *(
        f"quant/E3V16-T3-R{replicate}-{arm}"
        for replicate in range(1, 6)
        for arm in ("M0", "M1")
    ),
)


class LaunchPackageError(RuntimeError):
    """The frozen agreement cannot safely become a runnable package."""


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


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchPackageError(f"cannot read launch evidence: {path}") from exc


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
        raise LaunchPackageError(f"launch control Git check failed: {detail}")
    return completed.stdout.strip()


def _launch_control(control_ref: str, control_commit: str) -> dict[str, Any]:
    if _git(["rev-parse", "HEAD"]) != control_commit:
        raise LaunchPackageError("launch checkout HEAD does not match --control-commit")
    if _git(["status", "--porcelain"]):
        raise LaunchPackageError("launch checkout is dirty")
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
    result: list[dict[str, Any]] = []
    for pair in schedule:
        observations = pair.get("observations")
        if not isinstance(observations, list) or len(observations) != 2:
            raise LaunchPackageError("agreement schedule contains an invalid pair")
        result.extend(copy.deepcopy(observations))
    if len(result) != 10 or len({item.get("run_id") for item in result}) != 10:
        raise LaunchPackageError("agreement schedule does not contain ten unique runs")
    return result


def _request(
    *,
    identity: dict[str, Any],
    sequence: int,
    task_text: str,
    config: dict[str, Any],
    experiment_id: str = "E3V17",
) -> dict[str, Any]:
    run_id = identity["run_id"]
    suite = config["task_suite"]
    runtime = config["runtime"]
    launch_text = (
        "Execute the complete frozen T3 task supplied as the canonical parsed "
        "objective using only the declared source, current target and identical "
        f"{experiment_id} v2 tools."
    )
    budget = config["shared_budget"]
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "architecture_mode": identity["architecture_mode"],
        "repository_details": [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                "source_branch": config["source"]["source_ref"],
                "target_branch": identity["target_ref"],
                "runtime_role": "default",
                "allowed_directories": [suite["root"]],
            }
        ],
        "jira_metadata": {
            "ticket_id": f"{experiment_id}-{sequence:03d}",
            "current_status": "Approved local formal execution",
            "summary": f"{experiment_id} T3 replicate {identity['replicate']}",
            # runtime_request.schema.json caps the Jira description at 12,000
            # characters, while the hash-bound T3 v2 task is slightly longer.
            # The runtime's prompt builder treats parsed `objective` as the
            # canonical task request, so keep the transport description short
            # and put the complete byte-identical task in that field.
            "description": (
                f"Complete the hash-bound {experiment_id} T3 v2 task supplied as the "
                "canonical parsed objective."
            ),
            "triggering_comment": {
                "comment_id": run_id,
                "timestamp": FIXED_TIMESTAMP,
                "author": "experiment-operator",
                "text": launch_text,
            },
            "events_history": [
                {
                    "event_type": "comment",
                    "timestamp": FIXED_TIMESTAMP,
                    "author": "experiment-operator",
                    "text": launch_text,
                }
            ],
        },
        "execution_objectives": {
            "strategy_type": "other",
            "target_date_range": {},
            "parsed_task_parameters": {
                "rag_enabled": False,
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": suite["output_schema_path"],
                "provider_mode": "openai",
                "provider_base_url": runtime["provider_base_url"],
                "provider_identity_sha256": runtime["provider_identity_sha256"],
                "model": runtime["model_alias"],
                "answer_capture_profile": suite["answer_capture_profile"],
                "t3_item_submission_enabled": True,
                "t3_item_submission_schema_path": suite[
                    "item_submission_schema_path"
                ],
                "t3_primary_outcome": "item_correctness_25",
                "objective": task_text,
            },
            "system_instruction_override": None,
            "resource_path": suite["task_path"],
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
            "cpu_vcpus": budget["cpu_vcpus"],
            "memory_mb": budget["memory_mb"],
            "gpu_count": 0,
            "execution_timeout_seconds": budget["observation_timeout_seconds"],
            "pids_limit": budget["pids_limit"],
        },
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }


def _negative_manifest(
    identity: dict[str, Any],
    all_targets: set[str],
    *,
    experiment_id: str = "E3V17",
    prior_study_refs: tuple[str, ...] = PRIOR_STUDY_REFS,
) -> tuple[dict[str, Any], str, str]:
    current = identity["target_ref"]
    paired = identity["paired_target_ref"]
    other_results = sorted(all_targets - {current, paired})
    value = {
        "schema_version": "rae-negative-ref-manifest-v2",
        "experiment_id": experiment_id,
        "run_id": identity["run_id"],
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_ref": identity["source"]["source_ref"],
                "current_target_ref": current,
                "paired_target_ref": paired,
                "deny_refs": {
                    "prior_study_refs": list(prior_study_refs),
                    "other_result_refs": other_results,
                    "protected_refs": ["main"],
                    "arbitrary_probe_refs": [
                        f"exp3/probe/{experiment_id}/{identity['run_id']}/a",
                        f"exp3/probe/{experiment_id}/{identity['run_id']}/b",
                    ],
                },
            }
        ],
    }
    manifest_sha = _sha256_bytes(_canonical(value))
    denied = sorted(
        {
            paired,
            *prior_study_refs,
            *other_results,
            "main",
            f"exp3/probe/{experiment_id}/{identity['run_id']}/a",
            f"exp3/probe/{experiment_id}/{identity['run_id']}/b",
        }
    )
    deny_sha = _sha256_bytes(
        _canonical(
            [
                {
                    "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                    "deny_refs": denied,
                }
            ]
        )
    )
    return value, manifest_sha, deny_sha


def build(
    *,
    config_path: Path,
    agreement_dir: Path,
    public_bindings: Path,
    private_evaluator_manifest: Path,
    control_ref: str,
    control_commit: str,
    output: Path,
) -> dict[str, Any]:
    config = controls.load_config(config_path)
    for replicate in range(1, 6):
        result = preflight_e3v17.preflight(
            config,
            replicate=replicate,
            agreement_dir=agreement_dir,
            public_bindings=public_bindings,
            private_evaluator_manifest=private_evaluator_manifest,
        )
        if result.get("status") != "ready" or result.get("provider_call_count") != 0:
            raise LaunchPackageError(f"base zero-model preflight failed for replicate {replicate}")
    agreement = _load_json(agreement_dir / "agreement.json")
    schedule = _load_json(agreement_dir / "schedule.json")
    digest = _load_json(agreement_dir / "agreement.sha256.json")
    if not isinstance(agreement, dict) or not isinstance(schedule, list) or not isinstance(digest, dict):
        raise LaunchPackageError("frozen agreement files have invalid root types")
    agreement_sha = digest.get("agreement_sha256")
    if agreement_sha != agreement.get("agreement_sha256"):
        raise LaunchPackageError("frozen agreement digest records disagree")
    launch_control = _launch_control(control_ref, control_commit)
    identities = _flatten_schedule(schedule)
    all_targets = {item["target_ref"] for item in identities}
    task_path = CONTROL_ROOT / config["task_suite"]["task_path"]
    task_text = task_path.read_text(encoding="utf-8")
    if _sha256_file(task_path) != config["task_suite"]["task_sha256"]:
        raise LaunchPackageError("model-visible T3 task text changed before launch packaging")

    output = output.resolve()
    if output.exists():
        raise LaunchPackageError(f"refusing to overwrite launch package: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        shutil.copytree(agreement_dir, staging / "agreement")
        evidence = staging / "evidence"
        evidence.mkdir()
        shutil.copy2(config_path, evidence / "freeze_config.json")
        shutil.copy2(public_bindings, evidence / "public_bindings.json")
        shutil.copy2(
            private_evaluator_manifest,
            evidence / "private_evaluator_hashes.json",
        )
        run_records: list[dict[str, Any]] = []
        for sequence, identity in enumerate(identities, start=1):
            run_dir = staging / "runs" / identity["run_id"]
            run_dir.mkdir(parents=True)
            request = _request(
                identity=identity,
                sequence=sequence,
                task_text=task_text,
                config=config,
            )
            negative, manifest_sha, deny_sha = _negative_manifest(
                identity, all_targets
            )
            _write_json(run_dir / "identity.json", identity)
            _write_json(run_dir / "request.json", request)
            _write_json(run_dir / "negative_ref_manifest.json", negative)
            run_records.append(
                {
                    "run_id": identity["run_id"],
                    "pair": f"T3_R{identity['replicate']}",
                    "replicate": identity["replicate"],
                    "sequence_in_pair": identity["sequence_in_pair"],
                    "architecture_mode": identity["architecture_mode"],
                    "target_ref": identity["target_ref"],
                    "paired_target_ref": identity["paired_target_ref"],
                    "identity_sha256": identity["identity_sha256"],
                    "negative_ref_manifest_sha256": manifest_sha,
                    "negative_ref_set_sha256": deny_sha,
                    "file_sha256": {
                        "identity": _sha256_file(run_dir / "identity.json"),
                        "request": _sha256_file(run_dir / "request.json"),
                        "negative_ref_manifest": _sha256_file(
                            run_dir / "negative_ref_manifest.json"
                        ),
                    },
                }
            )
        manifest: dict[str, Any] = {
            "schema_version": "exp3-e3v17-launch-package-v1",
            "status": "execution_authorized_not_started",
            "experiment_id": "E3V17",
            "agreement_sha256": agreement_sha,
            "formal_model_execution_enabled": True,
            "provider_call_count_at_build": 0,
            "formal_observation_count_at_build": 0,
            "serial": True,
            "observations_per_authorization": 2,
            "advance_only_after_pair_audit": True,
            "launch_control": launch_control,
            "evidence_sha256": {
                "freeze_config": _sha256_file(evidence / "freeze_config.json"),
                "public_bindings": _sha256_file(evidence / "public_bindings.json"),
                "private_evaluator_hashes": _sha256_file(
                    evidence / "private_evaluator_hashes.json"
                ),
            },
            "runs": run_records,
        }
        manifest["launch_package_sha256"] = _sha256_bytes(_canonical(manifest))
        _write_json(staging / "launch_manifest.json", manifest)
        _write_json(
            staging / "launch_manifest.sha256.json",
            {
                "schema_version": "exp3-e3v17-launch-package-sha256-v1",
                "launch_package_sha256": manifest["launch_package_sha256"],
            },
        )
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--agreement", type=Path, required=True)
    parser.add_argument("--public-bindings", type=Path, required=True)
    parser.add_argument("--private-evaluator-manifest", type=Path, required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = build(
            config_path=args.config.resolve(),
            agreement_dir=args.agreement.resolve(),
            public_bindings=args.public_bindings.resolve(),
            private_evaluator_manifest=args.private_evaluator_manifest.resolve(),
            control_ref=args.control_ref,
            control_commit=args.control_commit,
            output=args.output,
        )
    except LaunchPackageError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "provider_call_count": 0,
                    "formal_observation_count": 0,
                    "issue": str(exc),
                },
                sort_keys=True,
            )
        )
        return 2
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "agreement_sha256": manifest["agreement_sha256"],
                "launch_package_sha256": manifest["launch_package_sha256"],
                "run_count": len(manifest["runs"]),
                "provider_call_count": 0,
                "formal_observation_count": 0,
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
