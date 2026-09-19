#!/usr/bin/env python3
"""Preflight or serially execute five E4V1 observations for one condition."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from . import identity, run_e4v1_observation as observation
    from experiments.exp3.e3v17 import run_e3v17_pair as shared_scoring
except ImportError:  # pragma: no cover
    import identity  # type: ignore[no-redef]
    import run_e4v1_observation as observation  # type: ignore[no-redef]
    from experiments.exp3.e3v17 import run_e3v17_pair as shared_scoring


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[1]
PROJECT_ROOT = CONTROL_ROOT.parents[1]
RESULT_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL/status-and-logs/e4v1-batch-results"


class BatchLaunchError(RuntimeError):
    """The five-run condition batch cannot safely advance."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _scorer_preflight(config: dict[str, Any]) -> dict[str, Any]:
    bindings = config["evaluator_bindings"]
    checks = (
        (shared_scoring.PRIVATE_ITEM_SCORER, bindings["T3"], "private item scorer"),
        (shared_scoring.PRIVATE_BASE_SCORER, bindings["private_base_scorer_sha256"], "private base scorer"),
        (shared_scoring.RESULT_ADAPTER, bindings["coordinator_sha256"], "result adapter"),
        (shared_scoring.TRACE_SCHEMA, bindings["visible_response_trace_schema_sha256"], "visible trace schema"),
        (CONTROL_ROOT / "experiments/exp3/e3v17/item_store_projection.py", bindings["item_store_projection_sha256"], "item-store projection"),
    )
    for path, expected, label in checks:
        if not path.is_file() or _sha256_file(path) != expected:
            raise BatchLaunchError(f"frozen {label} hash mismatch")
    return {"status": "ready", "provider_call_count": 0, "total_items": 25}


def preflight(package: Path, condition_code: str) -> dict[str, Any]:
    manifest = json.loads((package / "launch_manifest.json").read_text(encoding="utf-8"))
    selected = sorted(
        [row for row in manifest.get("runs", []) if isinstance(row, dict) and row.get("condition_code") == condition_code],
        key=lambda row: row["replicate"],
    )
    if len(selected) != 5 or [row["replicate"] for row in selected] != list(range(1, 6)):
        raise BatchLaunchError("condition batch does not contain replicates 1 through 5")
    states = [observation.preflight(package, row["run_id"]) for row in selected]
    if len({state["agreement_sha256"] for state in states}) != 1 or len({state["launch_package_sha256"] for state in states}) != 1:
        raise BatchLaunchError("five observations do not share one frozen identity")
    if {state["condition_code"] for state in states} != {condition_code}:
        raise BatchLaunchError("condition batch contains the wrong treatment")
    scorer = _scorer_preflight(states[0]["config"])
    return {
        "schema_version": "exp4-e4v1-batch-preflight-v1", "status": "ready",
        "formal_observation": False, "provider_call_count": 0, "external_model_calls_made": False,
        "experiment_id": "E4V1", "task_id": "T3", "condition_code": condition_code,
        "agreement_sha256": states[0]["agreement_sha256"], "launch_package_sha256": states[0]["launch_package_sha256"],
        "observation_count": 5, "run_ids": [state["run_id"] for state in states],
        "observations": [{key: state[key] for key in ("run_id", "target_ref", "condition_code", "architecture_mode", "rag_enabled", "image_digest", "status", "provider_call_count")} for state in states],
        "scorer_preflight": scorer, "states": states,
    }


def _execution_environment(condition_code: str) -> dict[str, str]:
    expected = f"AUTHORIZE_E4V1_T3_{condition_code}_FIVE_OBSERVATIONS"
    if os.getenv("E4V1_BATCH_AUTHORIZATION") != expected:
        raise BatchLaunchError(f"set E4V1_BATCH_AUTHORIZATION={expected}")
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        key = getpass.getpass(f"Paste OPENAI_API_KEY once for E4V1 T3 {condition_code} five-run batch (hidden): ").strip()
    if not key.startswith("sk-") or any(char.isspace() for char in key):
        raise BatchLaunchError("OPENAI_API_KEY shape is invalid")
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = key
    return environment


def run_batch(package: Path, condition_code: str, execute: bool) -> dict[str, Any]:
    ready = preflight(package, condition_code)
    print(json.dumps({key: value for key, value in ready.items() if key != "states"}, sort_keys=True), flush=True)
    if not execute:
        return ready
    environment = _execution_environment(condition_code)
    launches: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    launch_errors: list[dict[str, str]] = []
    scoring_errors: list[dict[str, str]] = []
    for state in ready["states"]:
        os.environ["OPENAI_API_KEY"] = environment["OPENAI_API_KEY"]
        os.environ["E4V1_RUN_AUTHORIZATION"] = f"AUTHORIZE_{state['run_id']}"
        try:
            launch = observation.execute(state)
        except Exception as exc:
            launch_errors.append({"run_id": state["run_id"], "failure_type": type(exc).__name__, "message": str(exc)[:500]})
            print(json.dumps({
                "schema_version": "exp4-e4v1-batch-progress-v1", "status": "observation_launch_error_recorded",
                "run_id": state["run_id"], "condition_code": condition_code,
                "completed_count": len(launches) + len(launch_errors),
                "remaining_count": 5 - len(launches) - len(launch_errors),
                "failure_type": type(exc).__name__,
            }, sort_keys=True), flush=True)
            continue
        launches.append(launch)
        try:
            score = shared_scoring._score(state, launch)
        except Exception as exc:
            scoring_errors.append({"run_id": state["run_id"], "failure_type": type(exc).__name__, "message": str(exc)[:500]})
            score_summary: dict[str, Any] = {"status": "scoring_incomplete", "failure_type": type(exc).__name__}
        else:
            score = {**score, "condition_code": condition_code, "rag_enabled": state["rag_enabled"]}
            scores.append(score)
            score_summary = {"status": "scored", **score}
        print(json.dumps({
            "schema_version": "exp4-e4v1-batch-progress-v1", "status": "observation_recorded",
            "run_id": state["run_id"], "condition_code": condition_code,
            "workflow_status": launch["result_status"], "output_directory": launch["output_directory"],
            "score": score_summary, "completed_count": len(launches) + len(launch_errors),
            "remaining_count": 5 - len(launches) - len(launch_errors),
        }, sort_keys=True), flush=True)
    summary = {
        "schema_version": "exp4-e4v1-batch-result-v1",
        "status": "completed" if not launch_errors and not scoring_errors else "completed_with_recorded_errors",
        "formal_observation": True, "condition_code": condition_code,
        "agreement_sha256": ready["agreement_sha256"], "launch_package_sha256": ready["launch_package_sha256"],
        "planned_observation_count": 5, "launched_observation_count": len(launches),
        "scored_observation_count": len(scores),
        "workflow_succeeded_count": sum(row["result_status"] == "succeeded" for row in launches),
        "workflow_failed_count": sum(row["result_status"] != "succeeded" for row in launches),
        "run_ids": ready["run_ids"], "scores": scores,
        "launch_errors": launch_errors, "scoring_errors": scoring_errors,
    }
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary_path = RESULT_ROOT / f"T3_{condition_code}-{stamp}.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["summary_path"] = str(summary_path)
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--condition", choices=identity.CONDITION_CODES, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = run_batch(args.launch_package.resolve(), args.condition, args.execute)
        return 2 if args.execute and result.get("status") == "completed_with_recorded_errors" else 0
    except Exception as exc:
        print(json.dumps({"schema_version": "exp4-e4v1-batch-result-v1", "status": "failed", "provider_call_count": 0 if not args.execute else None, "formal_observation": bool(args.execute), "failure_type": type(exc).__name__, "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
