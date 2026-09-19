#!/usr/bin/env python3
"""Build the runnable E3V10 package without contacting a model."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from experiments.exp3.e3v9 import build_e3v9_launch_package as common
from experiments.exp3.e3v9 import controls
from experiments.exp3.e3v10.build_e3v10_replacement import (
    CONTROL_FILES,
    _control_binding,
)


E3V9_TARGETS = tuple(
    f"quant/E3V9-T3-R{replicate}-{arm}"
    for replicate in range(1, 6)
    for arm in ("M0", "M1")
)


class LaunchPackageError(RuntimeError):
    """The E3V10 agreement cannot safely become runnable."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LaunchPackageError(f"cannot read launch evidence: {path}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(common._canonical(value) + b"\n")


def _validate_agreement(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    agreement = _load_json(root / "agreement.json")
    schedule = _load_json(root / "schedule.json")
    digest = _load_json(root / "agreement.sha256.json")
    if not isinstance(agreement, dict) or not isinstance(schedule, list) or not isinstance(digest, dict):
        raise LaunchPackageError("E3V10 agreement files have invalid root types")
    claimed = agreement.get("agreement_sha256")
    unsigned = copy.deepcopy(agreement)
    unsigned.pop("agreement_sha256", None)
    if (
        agreement.get("experiment_id") != "E3V10"
        or agreement.get("status") != "frozen_not_executed"
        or controls.sha256_value({"manifest": unsigned, "schedule": schedule}) != claimed
        or digest.get("agreement_sha256") != claimed
    ):
        raise LaunchPackageError("E3V10 agreement identity is invalid")
    identities = common._flatten_schedule(schedule)
    for identity in identities:
        claimed_identity = identity.get("identity_sha256")
        unsigned_identity = copy.deepcopy(identity)
        unsigned_identity.pop("identity_sha256", None)
        if (
            not str(identity.get("run_id", "")).startswith("E3V10-")
            or controls.sha256_value(unsigned_identity) != claimed_identity
        ):
            raise LaunchPackageError("E3V10 schedule contains an invalid identity")
    return agreement, schedule


def _validate_parent_launch(root: Path, agreement: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_json(root / "launch_manifest.json")
    digest = _load_json(root / "launch_manifest.sha256.json")
    if not isinstance(manifest, dict) or not isinstance(digest, dict):
        raise LaunchPackageError("parent E3V9 launch package is invalid")
    claimed = manifest.get("launch_package_sha256")
    unsigned = copy.deepcopy(manifest)
    unsigned.pop("launch_package_sha256", None)
    replacement = agreement.get("replacement_of") or {}
    if (
        manifest.get("experiment_id") != "E3V9"
        or claimed != replacement.get("launch_package_sha256")
        or manifest.get("agreement_sha256") != replacement.get("agreement_sha256")
        or common._sha256_bytes(common._canonical(unsigned)) != claimed
        or digest.get("launch_package_sha256") != claimed
    ):
        raise LaunchPackageError("parent E3V9 launch identity does not match E3V10")
    parent_agreement = _load_json(root / "agreement" / "agreement.json")
    parent_schedule = _load_json(root / "agreement" / "schedule.json")
    parent_digest = _load_json(root / "agreement" / "agreement.sha256.json")
    if (
        not isinstance(parent_agreement, dict)
        or not isinstance(parent_schedule, list)
        or not isinstance(parent_digest, dict)
    ):
        raise LaunchPackageError("parent launch copied agreement is invalid")
    unsigned_parent = copy.deepcopy(parent_agreement)
    unsigned_parent.pop("agreement_sha256", None)
    if (
        parent_agreement.get("agreement_sha256") != replacement.get("agreement_sha256")
        or parent_digest.get("agreement_sha256") != replacement.get("agreement_sha256")
        or controls.sha256_value(
            {"manifest": unsigned_parent, "schedule": parent_schedule}
        )
        != replacement.get("agreement_sha256")
    ):
        raise LaunchPackageError("parent launch copied agreement identity is invalid")
    expected = manifest.get("evidence_sha256")
    if not isinstance(expected, dict):
        raise LaunchPackageError("parent launch evidence inventory is missing")
    for name, filename in {
        "freeze_config": "freeze_config.json",
        "public_bindings": "public_bindings.json",
        "private_evaluator_hashes": "private_evaluator_hashes.json",
    }.items():
        path = root / "evidence" / filename
        if not path.is_file() or common._sha256_file(path) != expected.get(name):
            raise LaunchPackageError(f"parent launch evidence changed: {name}")
    return manifest


def build(
    *,
    agreement_dir: Path,
    parent_launch: Path,
    control_ref: str,
    control_commit: str,
    output: Path,
) -> dict[str, Any]:
    agreement, schedule = _validate_agreement(agreement_dir)
    parent_manifest = _validate_parent_launch(parent_launch, agreement)
    try:
        launch_control = _control_binding(control_ref, control_commit)
    except Exception as exc:
        raise LaunchPackageError(str(exc)) from exc
    if set(launch_control["files_sha256"]) != set(CONTROL_FILES):
        raise LaunchPackageError("E3V10 launch-control inventory is incomplete")
    identities = common._flatten_schedule(schedule)
    all_targets = {identity["target_ref"] for identity in identities}
    config_path = parent_launch / "evidence" / "freeze_config.json"
    config = controls.load_config(config_path)
    for field in (
        "source",
        "runtime",
        "task_suite",
        "tool_contract",
        "shared_budget",
        "evaluation",
    ):
        if agreement.get(field) != config.get(field):
            raise LaunchPackageError(f"E3V10 changed the frozen scientific binding: {field}")
    task_path = controls.REPOSITORY_ROOT / config["task_suite"]["task_path"]
    if (
        not task_path.is_file()
        or common._sha256_file(task_path) != config["task_suite"]["task_sha256"]
    ):
        raise LaunchPackageError("model-visible T3 task text changed")
    task_text = task_path.read_text(encoding="utf-8")
    prior_refs = tuple(common.PRIOR_STUDY_REFS) + E3V9_TARGETS

    output = output.resolve()
    if output.exists():
        raise LaunchPackageError(f"refusing to overwrite launch package: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        shutil.copytree(agreement_dir, staging / "agreement")
        shutil.copytree(parent_launch / "evidence", staging / "evidence")
        run_records: list[dict[str, Any]] = []
        for sequence, identity in enumerate(identities, start=1):
            run_dir = staging / "runs" / identity["run_id"]
            run_dir.mkdir(parents=True)
            request = common._request(
                identity=identity,
                sequence=sequence,
                task_text=task_text,
                config=config,
                experiment_id="E3V10",
            )
            negative, manifest_sha, deny_sha = common._negative_manifest(
                identity,
                all_targets,
                experiment_id="E3V10",
                prior_study_refs=prior_refs,
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
                        "identity": common._sha256_file(run_dir / "identity.json"),
                        "request": common._sha256_file(run_dir / "request.json"),
                        "negative_ref_manifest": common._sha256_file(
                            run_dir / "negative_ref_manifest.json"
                        ),
                    },
                }
            )
        manifest: dict[str, Any] = {
            "schema_version": "exp3-e3v10-launch-package-v1",
            "status": "execution_authorized_not_started",
            "experiment_id": "E3V10",
            "agreement_sha256": agreement["agreement_sha256"],
            "formal_model_execution_enabled": True,
            "provider_call_count_at_build": 0,
            "formal_observation_count_at_build": 0,
            "serial": True,
            "observations_per_authorization": 2,
            "advance_only_after_pair_audit": True,
            "launch_control": launch_control,
            "replacement_of": {
                "experiment_id": "E3V9",
                "agreement_sha256": parent_manifest["agreement_sha256"],
                "launch_package_sha256": parent_manifest[
                    "launch_package_sha256"
                ],
            },
            "evidence_sha256": {
                name: common._sha256_file(staging / "evidence" / filename)
                for name, filename in {
                    "freeze_config": "freeze_config.json",
                    "public_bindings": "public_bindings.json",
                    "private_evaluator_hashes": "private_evaluator_hashes.json",
                }.items()
            },
            "runs": run_records,
        }
        manifest["launch_package_sha256"] = common._sha256_bytes(
            common._canonical(manifest)
        )
        _write_json(staging / "launch_manifest.json", manifest)
        _write_json(
            staging / "launch_manifest.sha256.json",
            {
                "schema_version": "exp3-e3v10-launch-package-sha256-v1",
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
    parser.add_argument("--agreement", type=Path, required=True)
    parser.add_argument("--parent-launch", type=Path, required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(
            agreement_dir=args.agreement.resolve(),
            parent_launch=args.parent_launch.resolve(),
            control_ref=args.control_ref,
            control_commit=args.control_commit,
            output=args.output,
        )
    except LaunchPackageError as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": result["status"],
                "agreement_sha256": result["agreement_sha256"],
                "launch_package_sha256": result["launch_package_sha256"],
                "run_count": len(result["runs"]),
                "provider_call_count": 0,
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
