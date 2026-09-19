#!/usr/bin/env python3
"""Preflight or serially execute five E4V2 observations for one condition."""

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
    from . import identity_e4v2 as identity, run_e4v2_observation as observation
    from experiments.exp3.e3v17 import run_e3v17_pair as shared_scoring
except ImportError:  # pragma: no cover
    import identity_e4v2 as identity  # type: ignore[no-redef]
    import run_e4v2_observation as observation  # type: ignore[no-redef]
    from experiments.exp3.e3v17 import run_e3v17_pair as shared_scoring


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[1]
PROJECT_ROOT = CONTROL_ROOT.parents[1]
RESULT_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL/status-and-logs/e4v2-batch-results"
E4V3_RESULT_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL/status-and-logs/e4v3-batch-results"
E4V3_SELECTION_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL/status-and-logs/e4v3-batch-selections"


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
        "schema_version": "exp4-e4v2-batch-preflight-v1", "status": "ready",
        "formal_observation": False, "provider_call_count": 0, "external_model_calls_made": False,
        "experiment_id": "E4V2", "task_id": "T3", "condition_code": condition_code,
        "agreement_sha256": states[0]["agreement_sha256"], "launch_package_sha256": states[0]["launch_package_sha256"],
        "observation_count": 5, "run_ids": [state["run_id"] for state in states],
        "observations": [{key: state[key] for key in ("run_id", "target_ref", "condition_code", "architecture_mode", "rag_enabled", "image_digest", "status", "provider_call_count")} for state in states],
        "scorer_preflight": scorer, "states": states,
    }


def _execution_environment(condition_code: str) -> dict[str, str]:
    expected = f"AUTHORIZE_E4V2_T3_{condition_code}_FIVE_OBSERVATIONS"
    if os.getenv("E4V2_BATCH_AUTHORIZATION") != expected:
        raise BatchLaunchError(f"set E4V2_BATCH_AUTHORIZATION={expected}")
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        key = getpass.getpass(f"Paste OPENAI_API_KEY once for E4V2 T3 {condition_code} five-run batch (hidden): ").strip()
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
        os.environ["E4V2_RUN_AUTHORIZATION"] = f"AUTHORIZE_{state['run_id']}"
        try:
            launch = observation.execute(state)
        except Exception as exc:
            launch_errors.append({"run_id": state["run_id"], "failure_type": type(exc).__name__, "message": str(exc)[:500]})
            print(json.dumps({
                "schema_version": "exp4-e4v2-batch-progress-v1", "status": "observation_launch_error_recorded",
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
            "schema_version": "exp4-e4v2-batch-progress-v1", "status": "observation_recorded",
            "run_id": state["run_id"], "condition_code": condition_code,
            "workflow_status": launch["result_status"], "output_directory": launch["output_directory"],
            "score": score_summary, "completed_count": len(launches) + len(launch_errors),
            "remaining_count": 5 - len(launches) - len(launch_errors),
        }, sort_keys=True), flush=True)
    summary = {
        "schema_version": "exp4-e4v2-batch-result-v1",
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


def _selected_conditions(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    try:
        return identity.normalize_selected_conditions(values)
    except ValueError as exc:
        raise BatchLaunchError(str(exc)) from exc


def _selected_replicates(start: int, count: int, registered_count: int) -> tuple[int, ...]:
    if type(start) is not int or start < 1:
        raise BatchLaunchError("--replicate-start must be at least 1")
    if type(count) is not int or count < 1:
        raise BatchLaunchError("--replicates must be at least 1")
    end = start + count - 1
    if end > registered_count:
        raise BatchLaunchError(
            f"selected replicate range K{start}..K{end} exceeds the registered K1..K{registered_count} pool"
        )
    return tuple(range(start, end + 1))


def preflight_global_batch(
    package: Path,
    replicate_start: int,
    replicate_count: int,
    conditions: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    """Verify the requested unused E4V3 subset before asking for a model key.

    The launch package registers four conditions by fifteen replicate IDs.  A
    command may select any condition subset and one continuous replicate range.
    Filtering preserves the interleaved order stored in that frozen package.
    """

    manifest = json.loads((package / "launch_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("experiment_id") != "E4V3":
        raise BatchLaunchError("global batch mode requires an E4V3 launch package")
    registered_replicates = manifest.get("replicate_count")
    if registered_replicates != 15:
        raise BatchLaunchError("E4V3 launch package must register 15 replicates per condition")
    if (
        manifest.get("registered_conditions") != list(identity.CONDITION_CODES)
        or manifest.get("registered_observation_count") != 60
        or manifest.get("selection_mode") != "runtime_explicit_subset"
        or manifest.get("observations_per_authorization") is not None
    ):
        raise BatchLaunchError("E4V3 launch package does not contain the approved runtime-selectable pool")
    selected_conditions = _selected_conditions(conditions)
    selected_replicates = _selected_replicates(
        replicate_start, replicate_count, registered_replicates
    )
    expected_count = len(selected_conditions) * replicate_count
    registered_runs = manifest.get("runs")
    if not isinstance(registered_runs, list) or len(registered_runs) != 60:
        raise BatchLaunchError("launch package does not contain all 60 registered observations")
    requested_pairs = {
        (replicate, condition)
        for replicate in selected_replicates
        for condition in selected_conditions
    }
    runs = [
        row for row in registered_runs
        if isinstance(row, dict)
        and (row.get("replicate"), row.get("condition_code")) in requested_pairs
    ]
    observed_pairs = {
        (row.get("replicate"), row.get("condition_code")) for row in runs
    }
    if len(runs) != expected_count or observed_pairs != requested_pairs:
        raise BatchLaunchError("launch package is missing one or more requested run identities")
    expected_run_ids = [row.get("run_id") for row in runs]
    if len(expected_run_ids) != expected_count or len(set(expected_run_ids)) != expected_count:
        raise BatchLaunchError("selected execution table has missing or duplicate run identities")
    states = [observation.preflight(package, run_id) for run_id in expected_run_ids]
    if {state["config"]["experiment_id"] for state in states} != {"E4V3"}:
        raise BatchLaunchError("one or more observations do not bind E4V3")
    if len({state["agreement_sha256"] for state in states}) != 1 or len({state["launch_package_sha256"] for state in states}) != 1:
        raise BatchLaunchError("global observations do not share one frozen identity")
    if {state["condition_code"] for state in states} != set(selected_conditions):
        raise BatchLaunchError("global batch does not contain exactly the selected conditions")
    per_condition = {
        condition: sum(state["condition_code"] == condition for state in states)
        for condition in selected_conditions
    }
    if set(per_condition.values()) != {replicate_count}:
        raise BatchLaunchError("each condition must appear the frozen number of times")
    scorer = _scorer_preflight(states[0]["config"])
    return {
        "schema_version": "exp4-e4v3-global-batch-preflight-v1",
        "status": "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "experiment_id": "E4V3",
        "task_id": "T3",
        "registered_replicate_count": registered_replicates,
        "replicate_start": replicate_start,
        "replicate_count": replicate_count,
        "replicate_end": selected_replicates[-1],
        "selected_conditions": list(selected_conditions),
        "observation_count": expected_count,
        "condition_counts": per_condition,
        "agreement_sha256": states[0]["agreement_sha256"],
        "launch_package_sha256": states[0]["launch_package_sha256"],
        "run_ids": expected_run_ids,
        "observations": [
            {key: state[key] for key in ("run_id", "target_ref", "condition_code", "architecture_mode", "rag_enabled", "image_digest", "status", "provider_call_count")}
            for state in states
        ],
        "scorer_preflight": scorer,
        "states": states,
    }


def _global_authorization_value(
    replicate_start: int,
    replicate_count: int,
    selected_conditions: tuple[str, ...],
) -> str:
    observation_count = len(selected_conditions) * replicate_count
    condition_label = "_".join(selected_conditions)
    replicate_end = replicate_start + replicate_count - 1
    return (
        f"AUTHORIZE_E4V3_T3_{condition_label}_K{replicate_start}_TO_K{replicate_end}_"
        f"{observation_count}_OBSERVATIONS"
    )


def _validate_global_authorization(
    replicate_start: int,
    replicate_count: int,
    selected_conditions: tuple[str, ...],
) -> None:
    expected = _global_authorization_value(
        replicate_start, replicate_count, selected_conditions
    )
    if os.getenv("E4V3_BATCH_AUTHORIZATION") != expected:
        raise BatchLaunchError(f"set E4V3_BATCH_AUTHORIZATION={expected}")


def _global_execution_environment(
    replicate_start: int,
    replicate_count: int,
    selected_conditions: tuple[str, ...],
) -> dict[str, str]:
    _validate_global_authorization(
        replicate_start, replicate_count, selected_conditions
    )
    observation_count = len(selected_conditions) * replicate_count
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        key = getpass.getpass(f"Paste OPENAI_API_KEY once for E4V3 T3 {observation_count}-observation batch (hidden): ").strip()
    if not key.startswith("sk-") or any(char.isspace() for char in key):
        raise BatchLaunchError("OPENAI_API_KEY shape is invalid")
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = key
    return environment


def _record_runtime_selection(ready: dict[str, Any]) -> Path:
    """Persist the exact batch selection before any model credential is read."""

    selected_at = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {
        "schema_version": "exp4-e4v3-runtime-selection-v1",
        "status": "selection_recorded_before_model_key",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "selected_at": selected_at,
        "agreement_sha256": ready["agreement_sha256"],
        "launch_package_sha256": ready["launch_package_sha256"],
        "registered_replicate_count": ready["registered_replicate_count"],
        "replicate_start": ready["replicate_start"],
        "replicate_count": ready["replicate_count"],
        "replicate_end": ready["replicate_end"],
        "selected_conditions": ready["selected_conditions"],
        "condition_counts": ready["condition_counts"],
        "observation_count": ready["observation_count"],
        "run_ids": ready["run_ids"],
    }
    record["selection_sha256"] = identity.sha256_value(record)
    E4V3_SELECTION_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    condition_label = "-".join(ready["selected_conditions"])
    path = E4V3_SELECTION_ROOT / (
        f"T3_{condition_label}_K{ready['replicate_start']}-K{ready['replicate_end']}-{stamp}.json"
    )
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return path


def run_global_batch(
    package: Path,
    replicate_start: int,
    replicate_count: int,
    conditions: list[str] | tuple[str, ...],
    execute: bool,
) -> dict[str, Any]:
    selected_conditions = _selected_conditions(conditions)
    ready = preflight_global_batch(
        package, replicate_start, replicate_count, selected_conditions
    )
    print(json.dumps({key: value for key, value in ready.items() if key != "states"}, sort_keys=True), flush=True)
    if not execute:
        return ready
    _validate_global_authorization(
        replicate_start, replicate_count, selected_conditions
    )
    selection_path = _record_runtime_selection(ready)
    print(json.dumps({
        "schema_version": "exp4-e4v3-runtime-selection-event-v1",
        "status": "selection_recorded_before_model_key",
        "provider_call_count": 0,
        "selection_path": str(selection_path),
        "run_ids": ready["run_ids"],
    }, sort_keys=True), flush=True)
    environment = _global_execution_environment(
        replicate_start, replicate_count, selected_conditions
    )
    launches: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    launch_errors: list[dict[str, str]] = []
    scoring_errors: list[dict[str, str]] = []
    total = ready["observation_count"]
    for state in ready["states"]:
        os.environ["OPENAI_API_KEY"] = environment["OPENAI_API_KEY"]
        os.environ["E4V3_RUN_AUTHORIZATION"] = f"AUTHORIZE_{state['run_id']}"
        try:
            launch = observation.execute(state)
        except Exception as exc:
            launch_errors.append({"run_id": state["run_id"], "failure_type": type(exc).__name__, "message": str(exc)[:500]})
            print(json.dumps({
                "schema_version": "exp4-e4v3-global-batch-progress-v1",
                "status": "observation_launch_error_recorded",
                "run_id": state["run_id"],
                "condition_code": state["condition_code"],
                "completed_count": len(launches) + len(launch_errors),
                "remaining_count": total - len(launches) - len(launch_errors),
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
            score = {**score, "condition_code": state["condition_code"], "rag_enabled": state["rag_enabled"]}
            scores.append(score)
            score_summary = {"status": "scored", **score}
        print(json.dumps({
            "schema_version": "exp4-e4v3-global-batch-progress-v1",
            "status": "observation_recorded",
            "run_id": state["run_id"],
            "condition_code": state["condition_code"],
            "workflow_status": launch["result_status"],
            "output_directory": launch["output_directory"],
            "score": score_summary,
            "completed_count": len(launches) + len(launch_errors),
            "remaining_count": total - len(launches) - len(launch_errors),
        }, sort_keys=True), flush=True)
    summary = {
        "schema_version": "exp4-e4v3-global-batch-result-v1",
        "status": "completed" if not launch_errors and not scoring_errors else "completed_with_recorded_errors",
        "formal_observation": True,
        "registered_replicate_count": ready["registered_replicate_count"],
        "replicate_start": replicate_start,
        "replicate_count": replicate_count,
        "replicate_end": ready["replicate_end"],
        "selected_conditions": list(selected_conditions),
        "condition_counts": ready["condition_counts"],
        "selection_path": str(selection_path),
        "planned_observation_count": total,
        "launched_observation_count": len(launches),
        "scored_observation_count": len(scores),
        "workflow_succeeded_count": sum(row["result_status"] == "succeeded" for row in launches),
        "workflow_failed_count": sum(row["result_status"] != "succeeded" for row in launches),
        "agreement_sha256": ready["agreement_sha256"],
        "launch_package_sha256": ready["launch_package_sha256"],
        "run_ids": ready["run_ids"],
        "scores": scores,
        "launch_errors": launch_errors,
        "scoring_errors": scoring_errors,
    }
    E4V3_RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    condition_label = "-".join(selected_conditions)
    summary_path = E4V3_RESULT_ROOT / (
        f"T3_{condition_label}_K{replicate_start}-K{ready['replicate_end']}-{stamp}.json"
    )
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
        print(json.dumps({"schema_version": "exp4-e4v2-batch-result-v1", "status": "failed", "provider_call_count": 0 if not args.execute else None, "formal_observation": bool(args.execute), "failure_type": type(exc).__name__, "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
