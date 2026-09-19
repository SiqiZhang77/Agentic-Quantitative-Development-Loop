"""Build the public, condition-stable Terra agreement for Experiment 3.

The builder reads only the public task registry, task text, input CSV and public
rubric.  It records remote branch names and hashes, never branch contents.  No
model, Jira, Airflow or evaluator is contacted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PROJECT_ROOT = REPO_ROOT.parents[1]
CONTROL_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL" / "protocols"

import sys

sys.path.insert(0, str(REPO_ROOT / "rae_runtime" / "proxy"))
sys.path.insert(0, str(HERE))

import harness_identity as identity_harness  # noqa: E402
from exp3.isolation import (  # noqa: E402
    NegativeRefManifest,
    PRE_ORCHESTRATION,
    run_negative_ref_preflight,
)


SCHEMA_VERSION = "exp3-terra-agreement-v1.5"
EXPERIMENT_ID = "E3-manager-star-v1"
SOURCE_REF = "exp3/terra-source-v6"
SOURCE_COMMIT = "d22af8748c152be865bd854a222875fdf8bdd10d"
IMAGE_DIGEST = "sha256:d703a54b23645c57627a466d88235efabb39e1f7e9017a06b38d57b00260bfa1"
REQUIREMENTS_SHA256 = "e503ec722a99f0d7b92fdbb14813751722248f3ed8b245e3a76f87ececc59d8b"
PROVIDER_IDENTITY_SHA256 = "f5712a84b16c220abf9b470e0a5df839b9cdf3f2be4386989c2e4b18f9086cdc"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
MODEL_ALIAS = "gpt-5.6-terra"
REPOSITORY = "bankingscience/BSLAgenticQuantDevLoop"
REMOTE_NAME = "github"
FIXED_TIMESTAMP = "2026-08-26T00:00:00+00:00"
REQUEST_SCHEMA_PATH = REPO_ROOT / "rae_runtime" / "sandbox" / "schemas" / "runtime_request.schema.json"

PAIR_ARM_ORDER = {
    ("T1", 1): ("M0", "M1"),
    ("T1", 2): ("M1", "M0"),
    ("T1", 3): ("M0", "M1"),
    ("T2", 1): ("M1", "M0"),
    ("T2", 2): ("M0", "M1"),
    ("T2", 3): ("M1", "M0"),
    ("T3", 1): ("M0", "M1"),
    ("T3", 2): ("M1", "M0"),
    ("T3", 3): ("M0", "M1"),
}
BLOCK_ORDER = (
    ("T1", 2),
    ("T2", 2),
    ("T2", 1),
    ("T3", 1),
    ("T1", 1),
    ("T2", 3),
    ("T3", 3),
    ("T1", 3),
    ("T3", 2),
)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _remote_heads() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-remote", "--heads", REMOTE_NAME],
        check=True,
        capture_output=True,
        text=True,
    )
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        commit, full_ref = line.split("\t", 1)
        prefix = "refs/heads/"
        if not full_ref.startswith(prefix):
            continue
        result[full_ref[len(prefix) :]] = commit
    return result


def _known_negative_refs(remote_heads: dict[str, str]) -> tuple[list[str], list[str]]:
    v5_refs = sorted(ref for ref in remote_heads if "v5" in ref.casefold())
    e2_refs = sorted(
        ref
        for ref in remote_heads
        if ref not in v5_refs
        and (
            ref.startswith("quant/")
            or ref.startswith("exp2/")
            or ref.startswith("feature/exp2")
            or ref == "experiment-2-data"
        )
    )
    if not v5_refs or not e2_refs:
        raise RuntimeError("remote ref inventory cannot populate required E3 deny categories")
    return v5_refs, e2_refs


def _task_configuration() -> dict[str, dict[str, Any]]:
    registry_path = CONTROL_ROOT / "task_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    result: dict[str, dict[str, Any]] = {}
    for item in registry["tasks"]:
        task_id = item["task_id"]
        task_path = CONTROL_ROOT / item["task_file"]
        public_inputs = {
            name: _sha256_file(CONTROL_ROOT / name)
            for name in item.get("input_files") or []
        }
        result[task_id] = {
            **item,
            "task_text": task_path.read_text(encoding="utf-8").strip(),
            "task_text_sha256": _sha256_file(task_path),
            "public_inputs_sha256": public_inputs,
        }
    if set(result) != {"T1", "T2", "T3"}:
        raise RuntimeError("public task registry must contain exactly T1/T2/T3")
    return result


def _target_ref(task_id: str, replicate: int, arm: str) -> str:
    return f"quant/E3-{task_id}-R{replicate}-{arm}"


def _run_id(task_id: str, replicate: int, arm: str) -> str:
    return f"E3-{task_id}-R{replicate}-{arm}"


def _base_request(
    *,
    task: dict[str, Any],
    task_id: str,
    replicate: int,
    arm: str,
    target_ref: str,
    sequence: int,
) -> dict[str, Any]:
    run_id = _run_id(task_id, replicate, arm)
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "repository_details": [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": REPOSITORY,
                "clone_url": f"https://github.com/{REPOSITORY}.git",
                "source_branch": SOURCE_REF,
                "target_branch": target_ref,
                "runtime_role": "default",
                "allowed_directories": task["allowed_directories"],
            }
        ],
        "jira_metadata": {
            "ticket_id": f"E3-{sequence:03d}",
            "current_status": "Approved local formal execution",
            "summary": f"Experiment 3 {task_id} replicate {replicate}",
            "description": task["task_text"],
            "triggering_comment": {
                "comment_id": run_id,
                "timestamp": FIXED_TIMESTAMP,
                "author": "experiment-operator",
                "text": task["task_text"],
            },
            "events_history": [
                {
                    "event_type": "comment",
                    "timestamp": FIXED_TIMESTAMP,
                    "author": "experiment-operator",
                    "text": task["task_text"],
                }
            ],
        },
        "execution_objectives": {
            "strategy_type": task["strategy_type"],
            # The runtime contract requires the object even when a non-backtest
            # task intentionally inherits no date window.
            "target_date_range": {},
            "parsed_task_parameters": {},
            "system_instruction_override": None,
            "resource_path": task["resource_path"],
            "target_path": task["target_path"],
        },
        "iteration_controls": {},
        "resource_requirements": {
            "gpu_count": 0,
            "execution_timeout_seconds": 2_100,
        },
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }


def build(output: Path, *, replace: bool = False) -> dict[str, Any]:
    if output.exists():
        if not replace:
            raise FileExistsError(f"agreement output already exists: {output}")
        shutil.rmtree(output)

    remote_heads = _remote_heads()
    if remote_heads.get(SOURCE_REF) != SOURCE_COMMIT:
        raise RuntimeError("published Terra source ref does not match the agreed commit")
    v5_refs, e2_refs = _known_negative_refs(remote_heads)
    tasks = _task_configuration()
    rubric_sha256 = _sha256_file(CONTROL_ROOT / "RUBRIC.md")
    task_registry_sha256 = _sha256_file(CONTROL_ROOT / "task_registry.json")
    request_validator = Draft202012Validator(
        json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    tool_implementation_sha256 = _sha256_bytes(
        _canonical_bytes(
            {
                "implementation_commit": SOURCE_COMMIT,
                "image_digest": IMAGE_DIGEST,
                "requirements_sha256": REQUIREMENTS_SHA256,
            }
        )
    )
    sandbox_profile = {
        "image_digest": IMAGE_DIGEST,
        "cpu_vcpus": 2,
        "memory_mb": 4_096,
        "pids_limit": 256,
        "timeout_seconds": 1_800,
        "execution_timeout_seconds": 2_100,
        "read_only_root": True,
        "cap_drop": "ALL",
        "no_new_privileges": True,
    }
    sandbox_profile_sha256 = _sha256_bytes(_canonical_bytes(sandbox_profile))
    remote_inventory = [
        {"ref": ref, "commit": remote_heads[ref]}
        for ref in sorted(remote_heads)
    ]
    remote_inventory_sha256 = _sha256_bytes(_canonical_bytes(remote_inventory))

    temporary = Path(tempfile.mkdtemp(prefix=".terra-agreement-", dir=output.parent))
    prior_targets: list[str] = []
    run_entries: list[dict[str, Any]] = []
    pair_identities: dict[tuple[str, int], dict[str, identity_harness.RunIdentity]] = {}

    try:
        sequence = 0
        for task_id, replicate in BLOCK_ORDER:
            for arm in PAIR_ARM_ORDER[(task_id, replicate)]:
                sequence += 1
                run_id = _run_id(task_id, replicate, arm)
                target_ref = _target_ref(task_id, replicate, arm)
                paired_arm = "M1" if arm == "M0" else "M0"
                paired_target = _target_ref(task_id, replicate, paired_arm)
                if target_ref in remote_heads:
                    raise RuntimeError(f"formal target already exists: {target_ref}")
                earlier_targets = sorted(
                    ref for ref in prior_targets if ref != paired_target
                )
                manifest_value = {
                    "schema_version": "exp3-negative-ref-manifest-v1",
                    "experiment_id": EXPERIMENT_ID,
                    "run_id": run_id,
                    "repositories": [
                        {
                            "repo_full_name": REPOSITORY,
                            "source_ref": SOURCE_REF,
                            "current_target_ref": target_ref,
                            "paired_target_ref": paired_target,
                            "deny_refs": {
                                "v5_refs": v5_refs,
                                "e2_output_refs": e2_refs,
                                "earlier_e3_targets": earlier_targets,
                                "protected_refs": ["main"],
                                "arbitrary_probe_refs": [
                                    f"exp3/probe/{run_id}/a",
                                    f"exp3/probe/{run_id}/b",
                                ],
                            },
                        }
                    ],
                }
                manifest = NegativeRefManifest.from_dict(manifest_value)
                run_negative_ref_preflight(
                    manifest,
                    phase=PRE_ORCHESTRATION,
                    ref_exists=lambda repo, ref: (
                        repo == REPOSITORY and ref in remote_heads
                    ),
                )

                task = tasks[task_id]
                identity = identity_harness.build_run_identity(
                    task_id=task_id,
                    replicate=replicate,
                    arm=arm,
                    provider_mode="openai",
                    provider_base_url=PROVIDER_BASE_URL,
                    provider_identity_sha256=PROVIDER_IDENTITY_SHA256,
                    model_alias=MODEL_ALIAS,
                    source_ref=SOURCE_REF,
                    source_commit=SOURCE_COMMIT,
                    target_ref=target_ref,
                    paired_target_ref=paired_target,
                    task_text_sha256=task["task_text_sha256"],
                    public_inputs_sha256=task["public_inputs_sha256"],
                    rubric_sha256=rubric_sha256,
                    tool_implementation_sha256=tool_implementation_sha256,
                    sandbox_profile_sha256=sandbox_profile_sha256,
                    negative_ref_manifest_sha256=manifest.sha256,
                )
                request = identity_harness.apply_identity_to_runtime_request(
                    _base_request(
                        task=task,
                        task_id=task_id,
                        replicate=replicate,
                        arm=arm,
                        target_ref=target_ref,
                        sequence=sequence,
                    ),
                    identity,
                )
                request_validator.validate(request)

                run_dir = temporary / "runs" / run_id
                manifest_path = run_dir / "negative_ref_manifest.json"
                identity_path = run_dir / "identity.json"
                request_path = run_dir / "request.json"
                _write_json(manifest_path, manifest.value)
                _write_json(identity_path, identity.value)
                _write_json(request_path, request)
                pair_identities.setdefault((task_id, replicate), {})[arm] = identity
                run_entries.append(
                    {
                        "sequence": sequence,
                        "run_id": run_id,
                        "task_id": task_id,
                        "replicate": replicate,
                        "arm": arm,
                        "architecture_mode": identity.value["architecture_mode"],
                        "target_ref": target_ref,
                        "paired_target_ref": paired_target,
                        "manifest_sha256": manifest.sha256,
                        "deny_ref_set_sha256": manifest.deny_ref_set_sha256,
                        "identity_sha256": identity.sha256,
                        "files": {
                            "manifest": str(manifest_path.relative_to(temporary)),
                            "identity": str(identity_path.relative_to(temporary)),
                            "request": str(request_path.relative_to(temporary)),
                        },
                        "file_sha256": {
                            "manifest": _sha256_file(manifest_path),
                            "identity": _sha256_file(identity_path),
                            "request": _sha256_file(request_path),
                        },
                    }
                )
                prior_targets.append(target_ref)

        for pair in pair_identities.values():
            identity_harness.validate_pair(pair["M0"], pair["M1"])

        agreement = {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": EXPERIMENT_ID,
            "status": "user_agreed_preflight_ready",
            "approval_authority": "user",
            "task_policy": "retain_public_T1_T2_T3_and_current_rubric_metrics",
            "formal_run_count": 18,
            "execution_order": [item["run_id"] for item in run_entries],
            "repository": REPOSITORY,
            "source_ref": SOURCE_REF,
            "source_commit": SOURCE_COMMIT,
            "implementation_commit": SOURCE_COMMIT,
            "image_digest": IMAGE_DIGEST,
            "requirements_sha256": REQUIREMENTS_SHA256,
            "provider": {
                "mode": "openai",
                "base_url": PROVIDER_BASE_URL,
                "model_alias": MODEL_ALIAS,
                "identity_sha256": PROVIDER_IDENTITY_SHA256,
                "reasoning_effort": "none",
            },
            "rag_enabled": False,
            "shared_budget": {"max_calls": 15, "max_tokens": 200_000},
            "role_budgets": identity_harness.ROLE_BUDGETS,
            "sandbox_profile": sandbox_profile,
            "sandbox_profile_sha256": sandbox_profile_sha256,
            "tool_implementation_sha256": tool_implementation_sha256,
            "rubric_sha256": rubric_sha256,
            "task_registry_sha256": task_registry_sha256,
            "task_text_sha256": {
                task_id: tasks[task_id]["task_text_sha256"]
                for task_id in sorted(tasks)
            },
            "public_inputs_sha256": {
                task_id: tasks[task_id]["public_inputs_sha256"]
                for task_id in sorted(tasks)
            },
            "remote_ref_inventory_sha256": remote_inventory_sha256,
            "known_negative_ref_counts": {
                "v5_refs": len(v5_refs),
                "e2_output_refs": len(e2_refs),
            },
            "runs": run_entries,
        }
        _write_json(temporary / "agreement.json", agreement)
        agreement_sha256 = _sha256_file(temporary / "agreement.json")
        _write_json(
            temporary / "agreement.sha256.json",
            {
                "schema_version": "exp3-terra-agreement-digest-v1.5",
                "agreement_sha256": agreement_sha256,
            },
        )
        os.replace(temporary, output)
        return {
            "status": "built",
            "output": str(output),
            "agreement_sha256": agreement_sha256,
            "run_count": len(run_entries),
            "first_run": run_entries[0]["run_id"],
        }
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE / "agreement-terra-v1.5",
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(
        json.dumps(
            build(args.output.resolve(), replace=args.replace),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
