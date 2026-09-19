#!/usr/bin/env python3
"""Build a new E3V10 agreement from the frozen E3V9 design.

E3V9 stopped before any model call because its local launcher supplied an
incompatible GitHub username.  This builder proves that the two supplied E3V9
results contain zero model calls, preserves their hashes, and creates new run
identities without changing the task, source, image, budgets or comparison.
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

from experiments.exp3.e3v9 import controls


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[2]
CONTROL_FILES = (
    "experiments/exp3/e3v9/build_e3v9_launch_package.py",
    "experiments/exp3/e3v9/run_e3v9_observation.py",
    "experiments/exp3/e3v9/run_e3v9_pair.py",
    "experiments/exp3/e3v10/build_e3v10_replacement.py",
    "experiments/exp3/e3v10/build_e3v10_launch_package.py",
    "experiments/exp3/e3v10/run_e3v10_pair.py",
)
FAILED_RUN_IDS = {
    "E3V9-T3-R1-M0",
    "E3V9-T3-R1-M1",
}


class ReplacementError(RuntimeError):
    """The historical evidence cannot authorize an E3V10 replacement."""


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
        raise ReplacementError(f"cannot read replacement evidence: {path}") from exc


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
        raise ReplacementError(f"replacement control Git check failed: {detail}")
    return completed.stdout.strip()


def _control_binding(control_ref: str, control_commit: str) -> dict[str, Any]:
    if _git(["rev-parse", "HEAD"]) != control_commit:
        raise ReplacementError("control checkout HEAD does not match --control-commit")
    if _git(["status", "--porcelain"]):
        raise ReplacementError("control checkout is dirty")
    files: dict[str, str] = {}
    for relative in CONTROL_FILES:
        path = CONTROL_ROOT / relative
        if not path.is_file():
            raise ReplacementError(f"replacement control file is missing: {relative}")
        files[relative] = _sha256_file(path)
    return {
        "repository": "bankingscience/BSLAgenticQuantDevLoop",
        "control_ref": control_ref,
        "control_commit": control_commit,
        "files_sha256": files,
        "files_fingerprint_sha256": _sha256_bytes(_canonical(files)),
    }


def _validate_parent_agreement(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    agreement = _load_json(root / "agreement.json")
    schedule = _load_json(root / "schedule.json")
    digest = _load_json(root / "agreement.sha256.json")
    if not isinstance(agreement, dict) or not isinstance(schedule, list) or not isinstance(digest, dict):
        raise ReplacementError("parent E3V9 agreement files have invalid root types")
    claimed = agreement.get("agreement_sha256")
    unsigned = copy.deepcopy(agreement)
    unsigned.pop("agreement_sha256", None)
    actual = controls.sha256_value({"manifest": unsigned, "schedule": schedule})
    if (
        agreement.get("experiment_id") != "E3V9"
        or actual != claimed
        or digest.get("agreement_sha256") != claimed
    ):
        raise ReplacementError("parent E3V9 agreement identity is invalid")
    return agreement, schedule


def _validate_parent_launch(root: Path, agreement_sha: str) -> dict[str, Any]:
    manifest = _load_json(root / "launch_manifest.json")
    digest = _load_json(root / "launch_manifest.sha256.json")
    if not isinstance(manifest, dict) or not isinstance(digest, dict):
        raise ReplacementError("parent E3V9 launch package is invalid")
    claimed = manifest.get("launch_package_sha256")
    unsigned = copy.deepcopy(manifest)
    unsigned.pop("launch_package_sha256", None)
    if (
        manifest.get("experiment_id") != "E3V9"
        or manifest.get("agreement_sha256") != agreement_sha
        or _sha256_bytes(_canonical(unsigned)) != claimed
        or digest.get("launch_package_sha256") != claimed
    ):
        raise ReplacementError("parent E3V9 launch package identity is invalid")
    return manifest


def _validate_zero_call_failure(path: Path) -> dict[str, str]:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ReplacementError(f"failed result root is invalid: {path}")
    run_id = value.get("run_id")
    telemetry = value.get("telemetry")
    diagnostics = value.get("diagnostics")
    generated = value.get("generated_artifacts")
    if (
        run_id not in FAILED_RUN_IDS
        or (value.get("execution_summary") or {}).get("status") != "failed"
        or not isinstance(telemetry, dict)
        or telemetry.get("model_usage") != []
        or not isinstance(diagnostics, dict)
        or not isinstance(generated, dict)
        or generated.get("modified_files") != []
        or generated.get("new_files") != []
    ):
        raise ReplacementError(f"result is not an approved zero-call E3V9 failure: {path}")
    exp3 = telemetry.get("experiment3")
    if isinstance(exp3, dict):
        shared = exp3.get("shared_budget") or {}
        if (
            shared.get("calls") != 0
            or exp3.get("provider_calls") != []
            or exp3.get("calculation_tool_calls") != []
            or exp3.get("commit_count") != 0
        ):
            raise ReplacementError(f"result contains non-zero execution evidence: {path}")
    return {
        "run_id": run_id,
        "result_sha256": _sha256_file(path),
        "failure_type": str(diagnostics.get("error_message", "")).split(":", 1)[0],
        "disposition": "invalid_zero_model_infrastructure_observation",
    }


def _replace_version(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _replace_version(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_replace_version(child) for child in value]
    if isinstance(value, str):
        return value.replace("E3V9", "E3V10").replace("e3v9", "e3v10")
    return value


def _replacement_schedule(
    parent: list[dict[str, Any]], control: dict[str, Any]
) -> list[dict[str, Any]]:
    schedule = _replace_version(copy.deepcopy(parent))
    run_ids: set[str] = set()
    for pair in schedule:
        observations = pair.get("observations")
        if not isinstance(observations, list) or len(observations) != 2:
            raise ReplacementError("parent schedule contains an invalid pair")
        for identity in observations:
            identity["control"] = copy.deepcopy(control)
            identity.pop("identity_sha256", None)
            identity["identity_sha256"] = controls.sha256_value(identity)
            run_ids.add(identity["run_id"])
    if len(run_ids) != 10 or any(not run_id.startswith("E3V10-") for run_id in run_ids):
        raise ReplacementError("replacement schedule does not contain ten E3V10 runs")
    return schedule


def build(
    *,
    parent_agreement: Path,
    parent_launch: Path,
    failed_results: list[Path],
    control_ref: str,
    control_commit: str,
    output: Path,
) -> dict[str, Any]:
    if len(failed_results) != 2:
        raise ReplacementError("exactly two E3V9 failed results are required")
    parent, parent_schedule = _validate_parent_agreement(parent_agreement)
    parent_launch_manifest = _validate_parent_launch(
        parent_launch, parent["agreement_sha256"]
    )
    for filename in ("agreement.json", "schedule.json", "agreement.sha256.json"):
        source_file = parent_agreement / filename
        copied_file = parent_launch / "agreement" / filename
        if (
            not source_file.is_file()
            or not copied_file.is_file()
            or _sha256_file(source_file) != _sha256_file(copied_file)
        ):
            raise ReplacementError(
                f"parent launch copied a different agreement file: {filename}"
            )
    failures = [_validate_zero_call_failure(path) for path in failed_results]
    if {item["run_id"] for item in failures} != FAILED_RUN_IDS:
        raise ReplacementError("the E3V9 M0/M1 zero-call pair is incomplete")
    control = _control_binding(control_ref, control_commit)
    schedule = _replacement_schedule(parent_schedule, control)
    agreement = _replace_version(copy.deepcopy(parent))
    agreement.pop("agreement_sha256", None)
    agreement.update(
        {
            "schema_version": "exp3-e3v10-agreement-v1",
            "experiment_id": "E3V10",
            "study_id": "E3V10-T3-immediate-item-save-architecture-comparison-v1",
            "status": "frozen_not_executed",
            "formal_observation": False,
            "provider_call_count": 0,
            "external_calls_made": False,
            "control": control,
            "replacement_of": {
                "experiment_id": "E3V9",
                "agreement_sha256": parent["agreement_sha256"],
                "launch_package_sha256": parent_launch_manifest[
                    "launch_package_sha256"
                ],
                "reason": "zero_model_launcher_credential_identity_mismatch",
                "failed_observations": sorted(
                    failures, key=lambda item: item["run_id"]
                ),
            },
        }
    )
    agreement["agreement_sha256"] = controls.sha256_value(
        {"manifest": agreement, "schedule": schedule}
    )
    output = output.resolve()
    if output.exists():
        raise ReplacementError(f"refusing to overwrite replacement agreement: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        _write_json(staging / "agreement.json", agreement)
        _write_json(staging / "schedule.json", schedule)
        _write_json(
            staging / "agreement.sha256.json",
            {
                "schema_version": "exp3-e3v10-agreement-sha256-v1",
                "agreement_sha256": agreement["agreement_sha256"],
            },
        )
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return agreement


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-agreement", type=Path, required=True)
    parser.add_argument("--parent-launch", type=Path, required=True)
    parser.add_argument("--failed-result", type=Path, action="append", required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(
            parent_agreement=args.parent_agreement.resolve(),
            parent_launch=args.parent_launch.resolve(),
            failed_results=[path.resolve() for path in args.failed_result],
            control_ref=args.control_ref,
            control_commit=args.control_commit,
            output=args.output,
        )
    except ReplacementError as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "frozen_not_executed",
                "agreement_sha256": result["agreement_sha256"],
                "run_count": result["observation_count"],
                "provider_call_count": 0,
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
