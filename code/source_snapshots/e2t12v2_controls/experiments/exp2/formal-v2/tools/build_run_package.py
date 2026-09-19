#!/usr/bin/env python3
"""Build the private, condition-balanced Experiment 2 formal-v2 run skeleton.

This is an offline packaging tool. It does not call Jira, GitHub, Airflow,
LiteLLM, the RAG retriever, or any model, and it never reads hidden evaluators.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "task_registry.json"
PROTOCOL_PATH = ROOT / "PROTOCOL.md"
RUBRIC_PATH = ROOT / "RUBRIC.md"

ALLOCATIONS = {
    "T1": ["C0-C1", "C1-C0", "C0-C1"],
    "T2": ["C1-C0", "C0-C1", "C1-C0"],
    "T3": ["C0-C1", "C1-C0", "C0-C1"],
}

SCHEDULE_SEED = "E2-formal-v2-block-order-v1"
GLOBAL_BLOCK_ORDER = (
    "T3-R2",
    "T2-R3",
    "T2-R2",
    "T3-R1",
    "T2-R1",
    "T1-R3",
    "T1-R2",
    "T3-R3",
    "T1-R1",
)

COMMON_OPTIONS: list[tuple[str, str]] = [
    ("model", "qwen3-coder"),
    ("allow_iteration", "false"),
    ("max_iterations", "1"),
    ("max_failed_iterations", "1"),
    ("max_agent_turns", "15"),
    ("max_commits_per_run", "3"),
    ("timeout_seconds", "1800"),
    ("max_token_budget_per_run", "200000"),
    ("cpu_vcpus", "2"),
    ("memory_mb", "4096"),
    ("pids_limit", "256"),
    ("rag_top_k", "5"),
]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_registry() -> dict[str, Any]:
    value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tasks = value.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("task registry must contain a non-empty tasks array")
    task_ids = [task.get("task_id") for task in tasks if isinstance(task, dict)]
    if task_ids != ["T1", "T2", "T3"] or len(set(task_ids)) != len(task_ids):
        raise ValueError("formal-v2 registry must contain T1, T2 and T3 exactly once")
    return value


def validate_source(source_branch: str, source_commit: str) -> None:
    if not re.fullmatch(r"exp2/[A-Za-z0-9._/-]+", source_branch):
        raise ValueError("source branch must be an explicitly named exp2/* branch")
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("source commit must be a full lowercase 40-character Git SHA")


def git_worktree_root(path: Path) -> Path | None:
    """Find an enclosing Git worktree without trusting a caller-supplied path."""

    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        marker = candidate / ".git"
        if marker.exists():
            return candidate
    return None


def require_outside_git_worktree(path: Path, label: str) -> None:
    if git_worktree_root(path) is not None:
        raise ValueError(f"{label} must remain outside every Git worktree")


def execution_schedule() -> list[str]:
    expected_block_order = tuple(
        sorted(
            (f"T{task}-R{replicate}" for task in range(1, 4) for replicate in range(1, 4)),
            key=lambda block_id: sha256_bytes(f"{SCHEDULE_SEED}\0{block_id}".encode("utf-8")),
        )
    )
    if GLOBAL_BLOCK_ORDER != expected_block_order:
        raise AssertionError("frozen block order does not match its published hash schedule")
    run_keys: list[str] = []
    for block_id in GLOBAL_BLOCK_ORDER:
        task_id, replicate_label = block_id.split("-", 1)
        replicate = int(replicate_label.removeprefix("R"))
        for condition in ALLOCATIONS[task_id][replicate - 1].split("-"):
            run_keys.append(f"{task_id}-R{replicate}-{condition}")
    if len(run_keys) != 18 or len(set(run_keys)) != 18:
        raise AssertionError("global execution schedule must contain 18 unique runs")
    return run_keys


def validate_task_text(text: str) -> None:
    if not text.strip():
        raise ValueError("task text must not be empty")
    if "/quant" in text:
        raise ValueError("canonical task text must not contain a command marker")
    option_like = [line for line in text.splitlines() if re.fullmatch(r"[A-Za-z_]+:.*", line)]
    if option_like:
        raise ValueError(f"task prose contains parser-sensitive option-like lines: {option_like}")


def render_command(task: dict[str, Any], source_branch: str, condition: str) -> str:
    task_path = ROOT / str(task["task_file"])
    task_text = task_path.read_text(encoding="utf-8").strip()
    validate_task_text(task_text)
    options = [
        ("strategy_type", str(task["strategy_type"])),
        ("resource_path", str(task["resource_path"])),
        ("target_path", str(task["target_path"])),
        ("allowed_directories", " ".join(str(v) for v in task["allowed_directories"])),
        ("repo", "bankingscience/BSLAgenticQuantDevLoop"),
        ("branch_map", f"BSLAgenticQuantDevLoop={source_branch}"),
        *COMMON_OPTIONS[:-1],
        ("rag_enabled", "true" if condition == "C1" else "false"),
        COMMON_OPTIONS[-1],
    ]
    return "/quant-exp2\n" + task_text + "\n" + "\n".join(
        f"{key}: {value}" for key, value in options
    ) + "\n"


def only_rag_enabled_differs(c0: str, c1: str) -> bool:
    c0_lines = c0.splitlines()
    c1_lines = c1.splitlines()
    if len(c0_lines) != len(c1_lines):
        return False
    differences = [
        (left, right)
        for left, right in zip(c0_lines, c1_lines)
        if left != right
    ]
    return differences == [("rag_enabled: false", "rag_enabled: true")]


def neutral_submission_id(salt: bytes, run_key: str) -> str:
    digest = hmac.new(salt, run_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return "E2S-" + digest[:12].upper()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-branch", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--blinding-salt-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def build_package(
    *,
    source_branch: str,
    source_commit: str,
    salt: bytes,
    output: Path,
) -> dict[str, Any]:
    validate_source(source_branch, source_commit)
    stripped_salt = salt.strip()
    if len(stripped_salt) < 32:
        raise ValueError("blinding salt must contain at least 32 non-whitespace bytes")
    output = output.resolve()
    require_outside_git_worktree(output, "run-package output")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))

    try:
        registry = load_registry()
        task_records: dict[str, Any] = {}
        run_records: list[dict[str, Any]] = []
        private_map: dict[str, Any] = {}
        schedule = execution_schedule()
        execution_positions = {run_key: position for position, run_key in enumerate(schedule, start=1)}

        for task in registry["tasks"]:
            task_id = str(task["task_id"])
            task_path = ROOT / str(task["task_file"])
            input_files = task.get("input_files") or []
            if not isinstance(input_files, list) or not all(isinstance(value, str) for value in input_files):
                raise ValueError(f"{task_id} input_files must be a list of relative paths")
            task_records[task_id] = {
            "task_file": str(task["task_file"]),
            "task_sha256": sha256_file(task_path),
            "resource_path": task["resource_path"],
            "target_path": task["target_path"],
                "allocation": ALLOCATIONS[task_id],
                "input_sha256": {
                    input_file: sha256_file(ROOT / input_file)
                    for input_file in input_files
                },
            }
            for replicate, pair_order in enumerate(ALLOCATIONS[task_id], start=1):
                commands = {
                condition: render_command(task, source_branch, condition)
                for condition in ("C0", "C1")
            }
                if not only_rag_enabled_differs(commands["C0"], commands["C1"]):
                    raise AssertionError(f"{task_id} R{replicate} pair differs beyond rag_enabled")
                for position, condition in enumerate(pair_order.split("-"), start=1):
                    run_key = f"{task_id}-R{replicate}-{condition}"
                    submission_id = neutral_submission_id(stripped_salt, run_key)
                    run_dir = staging / "runs" / task_id / f"R{replicate}" / condition
                    run_dir.mkdir(parents=True)
                    command_path = run_dir / "command.txt"
                    command_path.write_text(commands[condition], encoding="utf-8")
                    run_manifest = {
                    "schema_version": "exp2-formal-v2-run-manifest-v1",
                    "run_key": run_key,
                    "task_id": task_id,
                    "replicate": replicate,
                    "condition": condition,
                    "pair_order": pair_order,
                        "pair_position": position,
                        "execution_position": execution_positions[run_key],
                    "submission_id": submission_id,
                    "source_branch": source_branch,
                    "source_commit": source_commit,
                    "command_sha256": sha256_file(command_path),
                    "jira_issue_key": None,
                    "jira_comment_id": None,
                    "airflow_dag_run_id": None,
                    "workflow_id": None,
                    "result_status": "not_run",
                    }
                    write_json(run_dir / "run_manifest.json", run_manifest)
                    run_records.append(run_manifest)
                    private_map[submission_id] = {
                    "run_key": run_key,
                    "task_id": task_id,
                    "replicate": replicate,
                    "condition": condition,
                    }

        allocation = {
        "schema_version": "exp2-formal-v2-allocation-v1",
        "pair_members_run_adjacently": True,
        "serial_execution": True,
            "allocations": ALLOCATIONS,
            "schedule_seed": SCHEDULE_SEED,
            "schedule_algorithm": "sha256_fixed_block_order_v1",
            "global_block_order": list(GLOBAL_BLOCK_ORDER),
            "execution_schedule": schedule,
        }
        write_json(staging / "allocation.json", allocation)
        write_json(
            staging / "blinding" / "private_condition_map.json",
        {
            "schema_version": "exp2-formal-v2-private-condition-map-v1",
            "salt_sha256": sha256_bytes(stripped_salt),
            "submissions": private_map,
        },
        )
        manifest = {
        "schema_version": "exp2-formal-v2-run-package-manifest-v1",
        "experiment_id": "E2-formal-v2",
        "protocol_version": "2.0.0-rc2",
        "status": "draft_not_executable",
        "source_branch": source_branch,
        "source_commit": source_commit,
        "protocol_sha256": sha256_file(PROTOCOL_PATH),
        "rubric_sha256": sha256_file(RUBRIC_PATH),
        "registry_sha256": sha256_file(REGISTRY_PATH),
            "allocation_sha256": sha256_file(staging / "allocation.json"),
        "blinding_salt_sha256": sha256_bytes(stripped_salt),
        "task_records": task_records,
        "run_count": len(run_records),
            "runs": [
            {
                "run_key": run["run_key"],
                "submission_id": run["submission_id"],
                "execution_position": run["execution_position"],
                "command_sha256": run["command_sha256"],
            }
                for run in sorted(run_records, key=lambda run: run["execution_position"])
            ],
            "external_calls_made": False,
        }
        write_json(staging / "run_package_manifest.json", manifest)

        checksum_rows = []
        for path in sorted(p for p in staging.rglob("*") if p.is_file()):
            if path.name == "checksums.sha256":
                continue
            checksum_rows.append(f"{sha256_file(path)}  {path.relative_to(staging)}")
        (staging / "checksums.sha256").write_text("\n".join(checksum_rows) + "\n", encoding="utf-8")
        os.replace(staging, output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    args = parse_args()
    require_outside_git_worktree(args.blinding_salt_file, "blinding salt")
    salt = args.blinding_salt_file.read_bytes()
    manifest = build_package(
        source_branch=args.source_branch,
        source_commit=args.source_commit,
        salt=salt,
        output=args.output,
    )
    print(canonical_json({"output": str(args.output), "run_count": manifest["run_count"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
