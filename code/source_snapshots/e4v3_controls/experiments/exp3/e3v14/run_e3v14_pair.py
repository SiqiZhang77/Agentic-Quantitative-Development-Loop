#!/usr/bin/env python3
"""Preflight or serially execute one E3V14 T3 M0/M1 pair."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

try:
    from . import run_e3v14_observation as observation
    from .item_store_projection import project_item_store
except ImportError:  # pragma: no cover - direct script execution
    import run_e3v14_observation as observation  # type: ignore[no-redef]
    from item_store_projection import project_item_store  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[2]
PROJECT_ROOT = CONTROL_ROOT.parents[1]
PRIVATE_ITEM_SCORER = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "e3v8-private"
    / "tools"
    / "score_t3_quant_items_25.py"
)
PRIVATE_BASE_SCORER = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "restricted"
    / "e3v3-private"
    / "tools"
    / "score_t3_quant_25.py"
)
RESULT_ADAPTER = (
    CONTROL_ROOT
    / "experiments"
    / "prospective_freezes"
    / "e3v14"
    / "build_scorer_result_v2.py"
)
TRACE_SCHEMA = (
    CONTROL_ROOT
    / "experiments"
    / "prospective_freezes"
    / "e3v14"
    / "model_visible_trace_schema_v1.json"
)
RESULT_VALIDATOR = (
    CONTROL_ROOT
    / "experiments"
    / "shared"
    / "t3-quant-suite-v2"
    / "scorer_result_validator.py"
)
PAIR_RESULT_BASE = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
)
PAIR_AUDIT_BASE = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
)


class PairLaunchError(RuntimeError):
    """The pair cannot be safely preflighted, executed or scored."""


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PairLaunchError(f"cannot read pair evidence: {path}") from exc


def _public_preflight(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: state[key]
        for key in (
            "status",
            "provider_call_count",
            "agreement_sha256",
            "launch_package_sha256",
            "run_id",
            "target_ref",
            "architecture_mode",
            "image_digest",
        )
    }


def _pair_runs(manifest: dict[str, Any], replicate: int) -> list[str]:
    pair = f"T3_R{replicate}"
    runs = [
        item
        for item in manifest.get("runs", [])
        if isinstance(item, dict) and item.get("pair") == pair
    ]
    runs.sort(key=lambda item: item["sequence_in_pair"])
    if len(runs) != 2 or {item["architecture_mode"] for item in runs} != {
        "single_agent",
        "manager_star",
    }:
        raise PairLaunchError("launch package pair inventory is invalid")
    return [item["run_id"] for item in runs]


def _scorer_preflight(agreement: dict[str, Any]) -> dict[str, Any]:
    evaluation = agreement.get("evaluation") or {}
    checks = (
        (PRIVATE_ITEM_SCORER, evaluation.get("private_item_scorer_sha256"), "item scorer"),
        (PRIVATE_BASE_SCORER, evaluation.get("private_base_scorer_sha256"), "base scorer"),
        (RESULT_ADAPTER, evaluation.get("evaluator_coordinator_sha256"), "result adapter"),
        (TRACE_SCHEMA, evaluation.get("visible_response_trace_schema_sha256"), "trace schema"),
        (
            HERE / "item_store_projection.py",
            evaluation.get("item_store_projection_sha256"),
            "item-store projection",
        ),
    )
    for path, expected, label in checks:
        if not path.is_file() or _sha256_file(path) != expected:
            raise PairLaunchError(f"frozen {label} hash mismatch")
    return {
        "status": "ready",
        "provider_call_count": 0,
        "total_items": 25,
    }


def preflight(package: Path, replicate: int) -> dict[str, Any]:
    manifest = _load_json(package / "launch_manifest.json")
    if not isinstance(manifest, dict):
        raise PairLaunchError("launch manifest is invalid")
    run_ids = _pair_runs(manifest, replicate)
    states = [observation.preflight(package, run_id) for run_id in run_ids]
    agreement_hashes = {state["agreement_sha256"] for state in states}
    launch_hashes = {state["launch_package_sha256"] for state in states}
    if len(agreement_hashes) != 1 or len(launch_hashes) != 1:
        raise PairLaunchError("paired preflights do not share one frozen identity")
    scorer = _scorer_preflight(states[0]["agreement"])
    return {
        "schema_version": (
            f"exp3-{manifest['experiment_id'].lower()}-pair-launch-preflight-v1"
        ),
        "status": "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "pair": f"T3_R{replicate}",
        "experiment_id": manifest["experiment_id"],
        "agreement_sha256": next(iter(agreement_hashes)),
        "launch_package_sha256": next(iter(launch_hashes)),
        "run_ids": run_ids,
        "preflight_count": 2,
        "observations": [_public_preflight(state) for state in states],
        "scorer_preflight": scorer,
        "states": states,
    }


def _merge_attempt_files(
    artifact_dir: Path, stem: str, destination_name: str
) -> Path | None:
    numbered = [
        artifact_dir / f"{stem}_attempt_1.jsonl",
        artifact_dir / f"{stem}_attempt_2.jsonl",
    ]
    present = [path.is_file() for path in numbered]
    unnumbered = artifact_dir / f"{stem}.jsonl"
    if present == [False, False]:
        return unnumbered if unnumbered.is_file() else None
    if present == [False, True] or unnumbered.exists():
        raise PairLaunchError(f"{stem} attempt files are inconsistent")
    lines: list[str] = []
    for path, exists in zip(numbered, present, strict=True):
        if not exists:
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                raise PairLaunchError(f"{stem} contains a blank JSONL record")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PairLaunchError(f"{stem} contains invalid JSONL") from exc
            if not isinstance(value, dict):
                raise PairLaunchError(f"{stem} JSONL record is not an object")
            lines.append(raw)
    destination = artifact_dir / destination_name
    if destination.exists():
        raise PairLaunchError(f"refusing to overwrite merged evidence: {destination.name}")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def _validate_visible_trace(path: Path | None) -> None:
    if path is None:
        return
    schema = _load_json(TRACE_SCHEMA)
    validator = Draft202012Validator(schema)
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        value = json.loads(raw)
        errors = list(validator.iter_errors(value))
        if errors:
            raise PairLaunchError(
                f"model-visible trace violates frozen schema at line {line_number}"
            )


def _fetch_submission(
    *, source_ref: str, source_commit: str, target_ref: str, destination: Path
) -> tuple[Path | None, bool, list[str]]:
    target_path = (
        "experiments/shared/t3-quant-suite-v2/submissions/"
        "quant_portfolio_analytics.json"
    )
    with tempfile.TemporaryDirectory(prefix="e3v14-score-git-") as raw:
        bare = Path(raw) / "repo.git"
        subprocess.run(
            ["git", "init", "--bare", str(bare)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        fetched = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "fetch",
                "--no-tags",
                "--depth=300",
                "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                f"refs/heads/{target_ref}:refs/heads/score-target",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if fetched.returncode:
            if "couldn't find remote ref" in fetched.stderr.lower():
                return None, False, []
            raise PairLaunchError("target branch could not be read for scoring")
        source_fetched = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "fetch",
                "--no-tags",
                "--depth=1",
                "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
                f"refs/heads/{source_ref}:refs/heads/score-source",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if source_fetched.returncode:
            raise PairLaunchError("frozen source commit could not be read for scoring")
        source_head = subprocess.run(
            ["git", "-C", str(bare), "rev-parse", "refs/heads/score-source"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if source_head.returncode or source_head.stdout.strip() != source_commit:
            raise PairLaunchError("remote source ref drifted before scoring")
        ancestry = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "merge-base",
                "--is-ancestor",
                "refs/heads/score-source",
                "refs/heads/score-target",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if ancestry.returncode:
            raise PairLaunchError("target branch does not contain the frozen source history")
        changed_run = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "diff",
                "--name-only",
                "refs/heads/score-source",
                "refs/heads/score-target",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if changed_run.returncode:
            raise PairLaunchError("target branch does not contain the frozen source history")
        changed = changed_run.stdout.splitlines()
        scope_passed = changed == [target_path]
        shown = subprocess.run(
            [
                "git",
                "-C",
                str(bare),
                "show",
                f"refs/heads/score-target:{target_path}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if shown.returncode:
            return None, scope_passed, changed
        destination.write_bytes(shown.stdout)
        return destination, scope_passed, changed


def _score(
    state: dict[str, Any], launch_result: dict[str, Any]
) -> dict[str, Any]:
    output_dir = Path(launch_result["output_directory"])
    artifact_dir = output_dir / "output" / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    architecture = state["architecture_mode"]
    audit_stem = (
        "exp3_mcp_tool_audit" if architecture == "manager_star" else "mcp_tool_audit"
    )
    audit = _merge_attempt_files(
        artifact_dir, audit_stem, f"{audit_stem}_merged.jsonl"
    )
    if audit is None:
        audit = artifact_dir / "t3_scoring_empty_audit.jsonl"
        audit.write_text("", encoding="utf-8")
    visible = _merge_attempt_files(
        artifact_dir,
        "model_visible_transcript",
        "model_visible_transcript_merged.jsonl",
    )
    _validate_visible_trace(visible)
    candidate = artifact_dir / "t3_submission_candidate.json"
    submission, scope_passed, changed_paths = _fetch_submission(
        source_ref=state["config"]["source"]["source_ref"],
        source_commit=state["config"]["source"]["source_commit"],
        target_ref=state["target_ref"],
        destination=candidate,
    )
    item_store = artifact_dir / "t3_quant_item_candidates.json"
    scorer_item_store = item_store
    projection_receipt: dict[str, Any] | None = None
    projection_receipt_path: Path | None = None
    if item_store.is_file():
        scorer_item_store = artifact_dir / "t3_quant_item_candidates_private_v1.json"
        projection_receipt_path = artifact_dir / "t3_item_store_projection_receipt.json"
        projection_receipt = project_item_store(
            item_store,
            scorer_item_store,
            projection_receipt_path,
        )
    legacy_score = artifact_dir / "t3_item_score_private_v1.json"
    suite_root = CONTROL_ROOT / "experiments/shared/t3-quant-suite-v2"
    command = [
        sys.executable,
        str(PRIVATE_ITEM_SCORER),
        "--csv",
        str(suite_root / "input/synthetic_portfolio_returns_v1.csv"),
        "--config",
        str(suite_root / "input/portfolio_config_v1.json"),
        "--schema",
        str(suite_root / "output_schema_v1.json"),
        "--base-scorer",
        str(PRIVATE_BASE_SCORER),
        "--audit",
        str(audit),
        "--scope-passed",
        "true" if scope_passed else "false",
        "--run-id",
        state["run_id"],
        "--architecture-mode",
        architecture,
        "--output",
        str(legacy_score),
    ]
    if submission is not None:
        command.extend(["--submission", str(submission)])
    if scorer_item_store.is_file():
        command.extend(["--item-store", str(scorer_item_store)])
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode or not legacy_score.is_file():
        raise PairLaunchError("private item scoring failed")
    public_score = artifact_dir / "t3_item_score_v2.json"
    adapter_command = [
        sys.executable,
        str(RESULT_ADAPTER),
        "--private-score",
        str(legacy_score),
        "--validator",
        str(RESULT_VALIDATOR),
        "--output",
        str(public_score),
    ]
    if visible is not None:
        adapter_command.extend(["--visible-trace", str(visible)])
    adapted = subprocess.run(
        adapter_command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if adapted.returncode or not public_score.is_file():
        raise PairLaunchError("answer-blind v2 result conversion failed")
    score = _load_json(public_score)
    if not isinstance(score, dict):
        raise PairLaunchError("public score root is invalid")
    return {
        "run_id": state["run_id"],
        "architecture_mode": architecture,
        "correct_items": score["correct_items"],
        "total_items": score["total_items"],
        "submitted_item_count": len(score["submitted_item_ids"]),
        "missing_item_count": len(score["missing_item_ids"]),
        "submitted_incorrect_item_count": len(
            score["submitted_incorrect_item_ids"]
        ),
        "submission_states": {
            state_name: sum(
                row["submission_state"] == state_name
                for row in score["item_results"]
            )
            for state_name in (
                "accepted",
                "invalid_format",
                "explicit_abstain",
                "tool_failure",
                "not_attempted",
            )
        },
        "changed_paths": changed_paths,
        "scope_passed": scope_passed,
        "score_path": str(public_score),
        "visible_trace_path": str(visible) if visible is not None else None,
        "item_store_projection": (
            {
                "status": "projected",
                "candidate_count": projection_receipt["candidate_count"],
                "candidates_sha256": projection_receipt["candidates_sha256"],
                "candidate_content_modified": False,
                "receipt_path": str(projection_receipt_path),
            }
            if projection_receipt is not None
            else {"status": "not_required_no_item_store"}
        ),
    }


def _prior_audit_required(experiment_id: str, replicate: int) -> None:
    if replicate == 1:
        return
    path = (
        PAIR_AUDIT_BASE
        / f"{experiment_id.lower()}-pair-audits"
        / f"T3_R{replicate - 1}.json"
    )
    if not path.is_file():
        raise PairLaunchError(
            f"reviewed audit for T3_R{replicate - 1} is required before this pair"
        )
    value = _load_json(path)
    if not isinstance(value, dict) or value.get("status") != "audited_advance_authorized":
        raise PairLaunchError("prior pair audit does not authorize advancement")


def _execution_environment(
    experiment_id: str, replicate: int
) -> dict[str, str]:
    authorization_name = f"{experiment_id}_PAIR_AUTHORIZATION"
    expected = f"AUTHORIZE_{experiment_id}_T3_R{replicate}_TWO_OBSERVATIONS"
    if os.getenv(authorization_name) != expected:
        raise PairLaunchError(f"set {authorization_name}={expected}")
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        api_key = getpass.getpass(
            f"Paste OPENAI_API_KEY once for {experiment_id} T3_R{replicate} pair (hidden): "
        ).strip()
    if not api_key.startswith("sk-") or any(char.isspace() for char in api_key):
        raise PairLaunchError("OPENAI_API_KEY shape is invalid")
    environment = os.environ.copy()
    environment["OPENAI_API_KEY"] = api_key
    return environment


def run_pair(package: Path, replicate: int, execute: bool) -> dict[str, Any]:
    ready = preflight(package, replicate)
    public_ready = {key: value for key, value in ready.items() if key != "states"}
    print(json.dumps(public_ready, sort_keys=True), flush=True)
    if not execute:
        return public_ready
    experiment_id = ready["experiment_id"]
    _prior_audit_required(experiment_id, replicate)
    environment = _execution_environment(experiment_id, replicate)
    results: list[dict[str, Any]] = []
    scores: list[dict[str, Any]] = []
    scoring_errors: list[dict[str, str]] = []
    for state in ready["states"]:
        os.environ["OPENAI_API_KEY"] = environment["OPENAI_API_KEY"]
        os.environ[f"{experiment_id}_RUN_AUTHORIZATION"] = (
            f"AUTHORIZE_{state['run_id']}"
        )
        launch_result = observation.execute(state, observation.DEFAULT_OUTPUT_ROOT)
        results.append(launch_result)
        try:
            score = _score(state, launch_result)
        except Exception as exc:
            scoring_errors.append(
                {
                    "run_id": state["run_id"],
                    "failure_type": type(exc).__name__,
                    "message": str(exc)[:500],
                }
            )
            score_summary: dict[str, Any] = {
                "status": "scoring_incomplete",
                "failure_type": type(exc).__name__,
            }
        else:
            scores.append(score)
            score_summary = {"status": "scored", **score}
        print(
            json.dumps(
                {
                    "schema_version": (
                        f"exp3-{experiment_id.lower()}-pair-progress-v1"
                    ),
                    "status": "observation_recorded",
                    "run_id": state["run_id"],
                    "workflow_status": launch_result["result_status"],
                    "output_directory": launch_result["output_directory"],
                    "score": score_summary,
                    "completed_count": len(results),
                    "remaining_count": 2 - len(results),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    summary = {
        "schema_version": f"exp3-{experiment_id.lower()}-pair-result-v1",
        "status": (
            "completed_awaiting_pair_audit"
            if not scoring_errors
            else "observations_completed_scoring_incomplete"
        ),
        "formal_observation": True,
        "pair": f"T3_R{replicate}",
        "agreement_sha256": ready["agreement_sha256"],
        "launch_package_sha256": ready["launch_package_sha256"],
        "observation_count": 2,
        "workflow_succeeded_count": sum(
            result["result_status"] == "succeeded" for result in results
        ),
        "workflow_failed_count": sum(
            result["result_status"] != "succeeded" for result in results
        ),
        "run_ids": ready["run_ids"],
        "scores": scores,
        "scoring_errors": scoring_errors,
    }
    pair_result_root = PAIR_RESULT_BASE / f"{experiment_id.lower()}-pair-results"
    pair_result_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    summary_path = pair_result_root / f"T3_R{replicate}-{stamp}.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary["summary_path"] = str(summary_path)
    print(json.dumps(summary, sort_keys=True), flush=True)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--replicate", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = run_pair(args.launch_package.resolve(), args.replicate, args.execute)
        if args.execute and result.get("status") == "observations_completed_scoring_incomplete":
            return 2
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "exp3-e3v14-pair-result-v1",
                    "status": "failed",
                    "provider_call_count": 0 if not args.execute else None,
                    "formal_observation": bool(args.execute),
                    "failure_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
