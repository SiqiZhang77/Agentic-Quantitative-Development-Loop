"""Build the prospective six-run Experiment 3 quantitative-T3 amendment.

The builder is intentionally separate from ``agreement-terra-v2``. It reads
only public/local control material plus a remote branch-name inventory, binds
an already approved source commit and immutable local image digest, and never
contacts a model, evaluator, Jira or Airflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PROJECT_ROOT = REPO_ROOT.parents[1]
CONTROL_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL" / "protocols"
AMENDMENT_ROOT = CONTROL_ROOT / "amendments"
T3_CONTROL_ROOT = AMENDMENT_ROOT / "t3-quant-suite-v1"
T3_SOURCE_ROOT = REPO_ROOT / "experiments" / "shared" / "t3-quant-suite-v1"

import sys

sys.path.insert(0, str(REPO_ROOT / "rae_runtime" / "proxy"))
sys.path.insert(0, str(HERE))

import build_terra_agreement as common  # noqa: E402
import harness_identity as identity_harness  # noqa: E402
from exp3.isolation import (  # noqa: E402
    NegativeRefManifest,
    PRE_ORCHESTRATION,
    run_negative_ref_preflight,
)


SCHEMA_VERSION = "exp3-t3-quant-agreement-v1"
EXPERIMENT_ID = "E3-manager-star-v1"
EXECUTION_EPOCH = "E3V2T3Q1"
TARGET_REF_PREFIX = f"quant/{EXECUTION_EPOCH}-"
DEFAULT_SOURCE_REF = "exp3/t3-quant-source-v1"
MODEL_ALIAS = "gpt-5.6-terra"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
PROVIDER_IDENTITY_SHA256 = (
    "f5712a84b16c220abf9b470e0a5df839b9cdf3f2be4386989c2e4b18f9086cdc"
)
REPOSITORY = "bankingscience/BSLAgenticQuantDevLoop"
FIXED_TIMESTAMP = "2026-08-28T00:00:00+00:00"
REQUEST_SCHEMA_PATH = (
    REPO_ROOT / "rae_runtime" / "sandbox" / "schemas" / "runtime_request.schema.json"
)
AMENDMENT_PATH = (
    AMENDMENT_ROOT / "EXP2_EXP3_T3_QUANT_SUITE_AMENDMENT_V1.md"
)
TASK_IDENTITY_PATH = T3_CONTROL_ROOT / "T3_TASK_IDENTITY_V1.json"
T3_ORDER = (
    (1, "M0"),
    (1, "M1"),
    (3, "M0"),
    (3, "M1"),
    (2, "M1"),
    (2, "M0"),
)
TOOL_IMPLEMENTATION_FILES = (
    "rae_runtime/proxy/quant_calculator.py",
    "rae_runtime/proxy/quant_calculator_audit.py",
    "rae_runtime/proxy/github_mcp_server.py",
    "rae_runtime/proxy/pipeline_mcp.py",
    "rae_runtime/proxy/exp3/role_adapters.py",
    "rae_runtime/proxy/exp3/agents_runtime.py",
    "rae_runtime/proxy/exp3/telemetry.py",
    "rae_runtime/sandbox/schemas/runtime_request.schema.json",
    "rae_runtime/sandbox/schemas/runtime_response.schema.json",
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


def _validated_task() -> dict[str, Any]:
    identity = json.loads(TASK_IDENTITY_PATH.read_text(encoding="utf-8"))
    if identity.get("schema_version") != "t3-quant-task-identity-v1":
        raise RuntimeError("T3 task identity schema is invalid")
    if identity.get("task_id") != "T3" or identity.get("task_version") != "t3-quant-suite-v1":
        raise RuntimeError("T3 task identity is not the approved candidate")
    if identity.get("runtime", {}).get("parsed_task_parameters") != {
        "quant_calculator_enabled": True
    }:
        raise RuntimeError("T3 task identity must enable exactly quant_calculate")

    copies = {
        T3_CONTROL_ROOT / "T3_PUBLIC_TASK_V1.txt": T3_SOURCE_ROOT / "TASK.md",
        T3_CONTROL_ROOT / "T3_PUBLIC_RUBRIC_V1.md": T3_SOURCE_ROOT / "RUBRIC.md",
        T3_CONTROL_ROOT / "output_schema_v1.json": T3_SOURCE_ROOT / "output_schema_v1.json",
        T3_CONTROL_ROOT / "synthetic_portfolio_returns_v1.csv": (
            T3_SOURCE_ROOT / "input" / "synthetic_portfolio_returns_v1.csv"
        ),
        T3_CONTROL_ROOT / "portfolio_config_v1.json": (
            T3_SOURCE_ROOT / "input" / "portfolio_config_v1.json"
        ),
    }
    for control_path, source_path in copies.items():
        if control_path.read_bytes() != source_path.read_bytes():
            raise RuntimeError(f"T3 public copy drifted: {source_path}")

    runtime = identity["runtime"]
    task_path = T3_SOURCE_ROOT / "TASK.md"
    public_files = {
        str(path.relative_to(REPO_ROOT)): _sha256_file(path)
        for path in (
            T3_SOURCE_ROOT / "input" / "synthetic_portfolio_returns_v1.csv",
            T3_SOURCE_ROOT / "input" / "portfolio_config_v1.json",
            T3_SOURCE_ROOT / "output_schema_v1.json",
        )
    }
    return {
        "identity": identity,
        "task_text": task_path.read_text(encoding="utf-8").strip(),
        "task_text_sha256": _sha256_file(task_path),
        "rubric_sha256": _sha256_file(T3_SOURCE_ROOT / "RUBRIC.md"),
        "public_inputs_sha256": public_files,
        "strategy_type": runtime["strategy_type"],
        "resource_path": runtime["resource_path"],
        "target_path": runtime["target_path"],
        "allowed_directories": runtime["allowed_directories"],
        "parsed_task_parameters": runtime["parsed_task_parameters"],
    }


def _run_id(replicate: int, arm: str) -> str:
    return f"E3-T3-R{replicate}-{arm}"


def _target_ref(replicate: int, arm: str) -> str:
    return f"{TARGET_REF_PREFIX}T3-R{replicate}-{arm}"


def _base_request(
    *,
    task: dict[str, Any],
    source_ref: str,
    replicate: int,
    arm: str,
    sequence: int,
) -> dict[str, Any]:
    run_id = _run_id(replicate, arm)
    launch_text = (
        "Execute the complete frozen quantitative T3 specification in "
        "jira_metadata.description. Use only the declared source, target and "
        "quant_calculate tool profile."
    )
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "repository_details": [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": REPOSITORY,
                "clone_url": f"https://github.com/{REPOSITORY}.git",
                "source_branch": source_ref,
                "target_branch": _target_ref(replicate, arm),
                "runtime_role": "default",
                "allowed_directories": task["allowed_directories"],
            }
        ],
        "jira_metadata": {
            "ticket_id": f"E3T3Q-{sequence:02d}",
            "current_status": "Approved local formal execution",
            "summary": f"Experiment 3 quantitative T3 replicate {replicate}",
            "description": task["task_text"],
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
            "strategy_type": task["strategy_type"],
            "target_date_range": {},
            "parsed_task_parameters": dict(task["parsed_task_parameters"]),
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


def build(
    output: Path,
    *,
    source_ref: str,
    source_commit: str,
    image_digest: str,
    replace: bool = False,
    remote_heads: dict[str, str] | None = None,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        raise ValueError("source_commit must be a full lowercase Git SHA")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest) is None:
        raise ValueError("image_digest must be a full sha256 image ID")
    if source_ref == "main" or not source_ref.startswith("exp3/"):
        raise ValueError("source_ref must be a dedicated exp3/ branch")
    if output.exists():
        if not replace:
            raise FileExistsError(f"agreement output already exists: {output}")
        shutil.rmtree(output)

    heads = dict(remote_heads) if remote_heads is not None else common._remote_heads()
    if heads.get(source_ref) != source_commit:
        raise RuntimeError("published T3 source ref does not match the approved commit")
    v5_refs, e2_refs, preexisting_e3_targets = common._known_negative_refs(heads)
    for replicate, arm in T3_ORDER:
        if _target_ref(replicate, arm) in heads:
            raise RuntimeError(f"formal target already exists: {_target_ref(replicate, arm)}")

    task = _validated_task()
    request_validator = Draft202012Validator(
        json.loads(REQUEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    )
    requirements_sha256 = _sha256_file(
        REPO_ROOT / "rae_runtime" / "sandbox" / "requirements.txt"
    )
    tool_files_sha256 = {
        name: _sha256_file(REPO_ROOT / name) for name in TOOL_IMPLEMENTATION_FILES
    }
    tool_profile = task["identity"]["tool_profile"]
    tool_implementation_sha256 = _sha256_bytes(
        _canonical_bytes(
            {
                "source_commit": source_commit,
                "image_digest": image_digest,
                "requirements_sha256": requirements_sha256,
                "tool_profile": tool_profile,
                "files": tool_files_sha256,
            }
        )
    )
    sandbox_profile = {
        "image_digest": image_digest,
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
        {"ref": ref, "commit": heads[ref]} for ref in sorted(heads)
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".t3-quant-agreement-", dir=output.parent))
    prior_targets = list(preexisting_e3_targets)
    run_entries: list[dict[str, Any]] = []
    pair_identities: dict[int, dict[str, identity_harness.RunIdentity]] = {}
    try:
        for sequence, (replicate, arm) in enumerate(T3_ORDER, start=1):
            run_id = _run_id(replicate, arm)
            target_ref = _target_ref(replicate, arm)
            paired_arm = "M1" if arm == "M0" else "M0"
            paired_target = _target_ref(replicate, paired_arm)
            manifest_value = {
                "schema_version": "exp3-negative-ref-manifest-v1",
                "experiment_id": EXPERIMENT_ID,
                "run_id": run_id,
                "repositories": [
                    {
                        "repo_full_name": REPOSITORY,
                        "source_ref": source_ref,
                        "current_target_ref": target_ref,
                        "paired_target_ref": paired_target,
                        "deny_refs": {
                            "v5_refs": v5_refs,
                            "e2_output_refs": e2_refs,
                            "earlier_e3_targets": sorted(
                                ref for ref in set(prior_targets) if ref != paired_target
                            ),
                            "protected_refs": ["main"],
                            "arbitrary_probe_refs": [
                                f"exp3/probe/{EXECUTION_EPOCH}/{run_id}/a",
                                f"exp3/probe/{EXECUTION_EPOCH}/{run_id}/b",
                            ],
                        },
                    }
                ],
            }
            manifest = NegativeRefManifest.from_dict(manifest_value)
            run_negative_ref_preflight(
                manifest,
                phase=PRE_ORCHESTRATION,
                ref_exists=lambda repo, ref: repo == REPOSITORY and ref in heads,
            )
            identity = identity_harness.build_run_identity(
                task_id="T3",
                replicate=replicate,
                arm=arm,
                provider_mode="openai",
                provider_base_url=PROVIDER_BASE_URL,
                provider_identity_sha256=PROVIDER_IDENTITY_SHA256,
                model_alias=MODEL_ALIAS,
                source_ref=source_ref,
                source_commit=source_commit,
                target_ref=target_ref,
                paired_target_ref=paired_target,
                task_text_sha256=task["task_text_sha256"],
                public_inputs_sha256=task["public_inputs_sha256"],
                rubric_sha256=task["rubric_sha256"],
                tool_implementation_sha256=tool_implementation_sha256,
                sandbox_profile_sha256=sandbox_profile_sha256,
                negative_ref_manifest_sha256=manifest.sha256,
            )
            request = identity_harness.apply_identity_to_runtime_request(
                _base_request(
                    task=task,
                    source_ref=source_ref,
                    replicate=replicate,
                    arm=arm,
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
            pair_identities.setdefault(replicate, {})[arm] = identity
            run_entries.append(
                {
                    "sequence": sequence,
                    "run_id": run_id,
                    "task_id": "T3",
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
            "execution_epoch": EXECUTION_EPOCH,
            "target_ref_prefix": TARGET_REF_PREFIX,
            "status": "user_agreed_preflight_ready",
            "approval_authority": "user",
            "amendment_version": "t3-quant-suite-v1",
            "scope": "prospective_T3_only",
            "pre_amendment_formal_observations": 7,
            "pre_amendment_tasks": ["T1", "T2"],
            "formal_run_count": 6,
            "planned_total_e3_observations": 18,
            "execution_order": [item["run_id"] for item in run_entries],
            "repository": REPOSITORY,
            "source_ref": source_ref,
            "source_commit": source_commit,
            "implementation_commit": source_commit,
            "image_digest": image_digest,
            "requirements_sha256": requirements_sha256,
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
            "tool_profile": tool_profile,
            "tool_implementation_files_sha256": tool_files_sha256,
            "tool_implementation_sha256": tool_implementation_sha256,
            "task_identity_sha256": _sha256_file(TASK_IDENTITY_PATH),
            "amendment_control_sha256": _sha256_file(AMENDMENT_PATH),
            "task_text_sha256": {"T3": task["task_text_sha256"]},
            "rubric_sha256": task["rubric_sha256"],
            "public_inputs_sha256": {"T3": task["public_inputs_sha256"]},
            "remote_ref_inventory_sha256": _sha256_bytes(
                _canonical_bytes(remote_inventory)
            ),
            "known_negative_ref_counts": {
                "v5_refs": len(v5_refs),
                "e2_output_refs": len(e2_refs),
                "preexisting_e3_targets": len(preexisting_e3_targets),
            },
            "runs": run_entries,
        }
        _write_json(temporary / "agreement.json", agreement)
        agreement_sha256 = _sha256_file(temporary / "agreement.json")
        _write_json(
            temporary / "agreement.sha256.json",
            {
                "schema_version": "exp3-t3-quant-agreement-digest-v1",
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
    parser.add_argument("--source-ref", default=DEFAULT_SOURCE_REF)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=HERE / "agreement-terra-t3-quant-v1",
    )
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            build(
                args.output.resolve(),
                source_ref=args.source_ref,
                source_commit=args.source_commit,
                image_digest=args.image_digest,
                replace=args.replace,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
