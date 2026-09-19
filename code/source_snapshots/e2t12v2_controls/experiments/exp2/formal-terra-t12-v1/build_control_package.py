#!/usr/bin/env python3
"""Build the private direct-OpenAI Experiment 2 T1/T2 control package.

The CLI reads the private salt and hash-only evaluator manifest, performs the
frozen deterministic C1 retrieval, and writes only to an operator-selected
directory outside Git. It never calls a model, Jira, Airflow or LiteLLM.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable


HERE = Path(__file__).resolve().parent
EXP2_ROOT = HERE.parent
REPO_ROOT = EXP2_ROOT.parents[1]
SMOKE_PATH = EXP2_ROOT / "run_terra_smoke_pair.py"
FORMAL_V2_ROOT = EXP2_ROOT / "formal-v2"
PROTOCOL_PATH = HERE / "PROTOCOL.md"
RUBRIC_PATH = FORMAL_V2_ROOT / "RUBRIC.md"
REGISTRY_PATH = FORMAL_V2_ROOT / "task_registry.json"

SOURCE_REF = "exp2/terra-formal-source-v1"
SOURCE_COMMIT = "6e7d29b3f220dd4cc639219aa7e765aee88e3988"
IMAGE_DIGEST = "sha256:8f135afdd816ffeac21f67834d10af91dda6509b4c8f993e33c65413b128465a"
IMAGE_REF = f"exp2-terra-smoke-candidate@{IMAGE_DIGEST}"
INDEX_ID = "e2-jira-index-v1"
INDEX_SHA256 = "72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1"
PROVIDER_MODE = "openai"
PROVIDER_BASE_URL = "https://api.openai.com/v1"
MODEL_ALIAS = "gpt-5.6-terra"
EXPERIMENT_ID = "E2-terra-t12-v2"
EXECUTION_EPOCH = "E2T12V2"
TARGET_PREFIX = f"quant/{EXECUTION_EPOCH}-"
TASKS = ("T1", "T2")
ALLOCATIONS = {
    "T1": ("C0-C1", "C1-C0", "C0-C1", "C1-C0", "C0-C1"),
    "T2": ("C1-C0", "C0-C1", "C1-C0", "C0-C1", "C1-C0"),
}
BLOCK_ORDER = (
    "T2-R5", "T2-R4", "T2-R3", "T2-R2", "T2-R1",
    "T1-R5", "T1-R4", "T1-R3", "T1-R2", "T1-R1",
)
FORMAL_RUN_COUNT = sum(len(order.split("-")) for orders in ALLOCATIONS.values() for order in orders)
PAIR_COUNT = sum(len(orders) for orders in ALLOCATIONS.values())
EVALUATOR_SCHEMA = "exp2-terra-t12-private-evaluator-hashes-v2"
EVALUATOR_HASH_FIELDS = (
    "t1_gold_claims_sha256",
    "t2_hidden_tests_sha256",
    "t2_evaluator_runner_sha256",
    "private_evaluator_coordinator_sha256",
    "primary_scorer_commitment_sha256",
)
AGREEMENT_SCHEMA = "exp2-terra-t12-agreement-v3"
AGREEMENT_DIGEST_SCHEMA = "exp2-terra-t12-agreement-digest-v3"


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("exp2_terra_smoke_common", SMOKE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load Terra smoke request helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def git_worktree_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def require_outside_git(path: Path, label: str) -> None:
    if git_worktree_root(path) is not None:
        raise ValueError(f"{label} must remain outside every Git worktree")


def validate_evaluator_hashes(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("schema_version") != EVALUATOR_SCHEMA:
        raise ValueError("private evaluator hash manifest schema is invalid")
    result = {"schema_version": EVALUATOR_SCHEMA}
    for field in EVALUATOR_HASH_FIELDS:
        digest = value.get(field)
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError(f"private evaluator hash manifest field is invalid: {field}")
        result[field] = digest
    if set(value) != set(result):
        raise ValueError("private evaluator hash manifest contains unapproved fields")
    return result


def execution_schedule() -> list[str]:
    run_keys: list[str] = []
    for block in BLOCK_ORDER:
        task_id, replicate_label = block.split("-", 1)
        replicate = int(replicate_label.removeprefix("R"))
        for condition in ALLOCATIONS[task_id][replicate - 1].split("-"):
            run_keys.append(f"{task_id}-R{replicate}-{condition}")
    if len(run_keys) != FORMAL_RUN_COUNT or len(set(run_keys)) != FORMAL_RUN_COUNT:
        raise AssertionError(
            f"E2 Terra T1/T2 schedule must contain {FORMAL_RUN_COUNT} unique runs"
        )
    return run_keys


def neutral_submission_id(salt: bytes, run_key: str) -> str:
    digest = hmac.new(salt, run_key.encode("utf-8"), hashlib.sha256).hexdigest()
    return "E2S-" + digest[:12].upper()


def remote_heads() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-remote", "--heads", "github"],
        check=True,
        capture_output=True,
        text=True,
    )
    heads: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        commit, ref = line.split()
        heads[ref.removeprefix("refs/heads/")] = commit
    return heads


def _task_registry() -> dict[str, dict[str, Any]]:
    value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    tasks = {
        str(item["task_id"]): item
        for item in value.get("tasks", [])
        if isinstance(item, dict) and item.get("task_id") in TASKS
    }
    if tuple(tasks) != TASKS:
        raise ValueError("public task registry does not contain T1/T2 in order")
    return tasks


def _target_ref(submission_id: str) -> str:
    return TARGET_PREFIX + submission_id


def _pair_projection(request: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(request)
    value.pop("run_id", None)
    value.pop("retrieval_context", None)
    value["repository_details"][0].pop("target_branch", None)
    value["execution_objectives"]["parsed_task_parameters"].pop("rag_enabled", None)
    metadata = value["jira_metadata"]
    metadata.pop("ticket_id", None)
    metadata.pop("summary", None)
    metadata["triggering_comment"].pop("comment_id", None)
    return value


def _formal_request(
    smoke: Any,
    *,
    task_id: str,
    replicate: int,
    condition: str,
    submission_id: str,
    target_ref: str,
) -> dict[str, Any]:
    request = smoke._request(
        f"{task_id}-R{replicate}-CONTROL",
        condition,
        task_id=task_id,
    )
    request["run_id"] = submission_id
    repository = request["repository_details"][0]
    repository["source_branch"] = SOURCE_REF
    repository["target_branch"] = target_ref
    metadata = request["jira_metadata"]
    # The frozen retriever validates Jira-style ticket IDs even though this
    # direct-local study never contacts Jira.  Keep one neutral, valid query
    # ticket per pair; the salt-derived submission ID remains the run identity.
    metadata["ticket_id"] = f"E2T12-{task_id.removeprefix('T')}{replicate}"
    metadata["current_status"] = "Approved direct-local formal execution"
    metadata["summary"] = f"Experiment 2 neutral submission {submission_id}"
    metadata["triggering_comment"]["comment_id"] = submission_id
    return request


def build_package(
    *,
    salt: bytes,
    evaluator_hashes: dict[str, str],
    output: Path,
    heads: dict[str, str],
    retrieve: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    stripped_salt = salt.strip()
    if len(stripped_salt) < 32:
        raise ValueError("blinding salt must contain at least 32 non-whitespace bytes")
    evaluator_hashes = validate_evaluator_hashes(evaluator_hashes)
    output = output.resolve()
    require_outside_git(output, "private control output")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")
    if heads.get(SOURCE_REF) != SOURCE_COMMIT:
        raise RuntimeError("published formal source ref is absent or has drifted")

    output.parent.mkdir(parents=True, exist_ok=True)

    smoke = _load_smoke_module()
    registry = _task_registry()
    schedule = execution_schedule()
    remote_inventory = [
        {"ref": ref, "commit": heads[ref]} for ref in sorted(heads)
    ]
    existing_denied = sorted(ref for ref in heads if ref != SOURCE_REF)
    task_records = {
        task_id: {
            "task_sha256": sha256_file(FORMAL_V2_ROOT / registry[task_id]["task_file"]),
            "resource_path": registry[task_id]["resource_path"],
            "target_path": registry[task_id]["target_path"],
            "allowed_directories": registry[task_id]["allowed_directories"],
        }
        for task_id in TASKS
    }

    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    run_entries: list[dict[str, Any]] = []
    private_map: dict[str, Any] = {}
    planned_prior_targets: list[str] = []
    pair_requests: dict[str, dict[str, dict[str, Any]]] = {}
    try:
        for sequence, run_key in enumerate(schedule, start=1):
            task_id, replicate_label, condition = run_key.split("-")
            replicate = int(replicate_label.removeprefix("R"))
            submission_id = neutral_submission_id(stripped_salt, run_key)
            target_ref = _target_ref(submission_id)
            if target_ref in heads:
                raise RuntimeError(f"formal target already exists: {target_ref}")
            pair_key = f"{task_id}-R{replicate}"
            other_condition = "C1" if condition == "C0" else "C0"
            paired_run_key = f"{task_id}-R{replicate}-{other_condition}"
            paired_submission_id = neutral_submission_id(stripped_salt, paired_run_key)
            paired_target_ref = _target_ref(paired_submission_id)
            request = _formal_request(
                smoke,
                task_id=task_id,
                replicate=replicate,
                condition=condition,
                submission_id=submission_id,
                target_ref=target_ref,
            )
            if condition == "C1":
                retrieval = retrieve(request)
                if retrieval.get("index_id") != INDEX_ID or retrieval.get("index_sha256") != INDEX_SHA256:
                    raise RuntimeError("C1 retrieval identity drifted")
                request["retrieval_context"] = retrieval
            elif "retrieval_context" in request:
                raise RuntimeError("C0 request unexpectedly contains retrieval context")

            pair_requests.setdefault(pair_key, {})[condition] = request
            deny_refs = sorted(
                set(existing_denied)
                | set(planned_prior_targets)
                | {paired_target_ref, "main"}
                | {
                    f"exp2/probe/{EXECUTION_EPOCH}/{submission_id}/a",
                    f"exp2/probe/{EXECUTION_EPOCH}/{submission_id}/b",
                }
            )
            deny_refs = [ref for ref in deny_refs if ref not in {SOURCE_REF, target_ref}]
            negative_manifest = {
                "schema_version": "exp2-terra-t12-negative-ref-manifest-v1",
                "experiment_id": EXPERIMENT_ID,
                "submission_id": submission_id,
                "repository": "bankingscience/BSLAgenticQuantDevLoop",
                "source_ref": SOURCE_REF,
                "current_target_ref": target_ref,
                "paired_target_ref": paired_target_ref,
                "deny_refs": deny_refs,
            }
            identity = {
                "schema_version": "exp2-terra-t12-run-identity-v1",
                "experiment_id": EXPERIMENT_ID,
                "execution_epoch": EXECUTION_EPOCH,
                "submission_id": submission_id,
                "task_id": task_id,
                "replicate": replicate,
                "condition": condition,
                "rag_enabled": condition == "C1",
                "source_ref": SOURCE_REF,
                "source_commit": SOURCE_COMMIT,
                "target_ref": target_ref,
                "paired_target_ref": paired_target_ref,
                "provider_identity_sha256": smoke._provider_identity()["sha256"],
                "image_digest": IMAGE_DIGEST,
                "index_sha256": INDEX_SHA256,
                "task_sha256": task_records[task_id]["task_sha256"],
                "rubric_sha256": sha256_file(RUBRIC_PATH),
                "negative_ref_manifest_sha256": sha256_bytes(canonical_bytes(negative_manifest)),
            }
            run_dir = staging / "runs" / submission_id
            write_json(run_dir / "request.json", request)
            write_json(run_dir / "identity.json", identity)
            write_json(run_dir / "negative_ref_manifest.json", negative_manifest)
            run_entries.append(
                {
                    "sequence": sequence,
                    "submission_id": submission_id,
                    "task_id": task_id,
                    "replicate": replicate,
                    "pair_id": pair_key,
                    "target_ref": target_ref,
                    "files": {
                        name: f"runs/{submission_id}/{name}.json"
                        for name in ("request", "identity", "negative_ref_manifest")
                    },
                    "file_sha256": {
                        name: sha256_file(run_dir / f"{name}.json")
                        for name in ("request", "identity", "negative_ref_manifest")
                    },
                }
            )
            private_map[submission_id] = {
                "run_key": run_key,
                "task_id": task_id,
                "replicate": replicate,
                "condition": condition,
                "rag_enabled": condition == "C1",
            }
            planned_prior_targets.append(target_ref)

        for pair_id, requests in pair_requests.items():
            if set(requests) != {"C0", "C1"}:
                raise AssertionError(f"pair is incomplete: {pair_id}")
            if _pair_projection(requests["C0"]) != _pair_projection(requests["C1"]):
                raise AssertionError(f"pair differs beyond treatment identity: {pair_id}")

        write_json(
            staging / "blinding" / "private_condition_map.json",
            {
                "schema_version": "exp2-terra-t12-private-condition-map-v1",
                "salt_sha256": sha256_bytes(stripped_salt),
                "submissions": private_map,
            },
        )
        write_json(staging / "private_evaluator_hashes.json", evaluator_hashes)
        agreement = {
            "schema_version": AGREEMENT_SCHEMA,
            "experiment_id": EXPERIMENT_ID,
            "execution_epoch": EXECUTION_EPOCH,
            "status": "user_agreed_private_control_ready",
            "approval_authority": "user",
            "scope": "T1_T2_only",
            "formal_run_count": FORMAL_RUN_COUNT,
            "pair_count": PAIR_COUNT,
            "source_ref": SOURCE_REF,
            "source_commit": SOURCE_COMMIT,
            "image_ref": IMAGE_REF,
            "image_digest": IMAGE_DIGEST,
            "provider_identity": smoke._provider_identity(),
            "index_id": INDEX_ID,
            "index_sha256": INDEX_SHA256,
            "protocol_sha256": sha256_file(PROTOCOL_PATH),
            "formal_v2_rubric_sha256": sha256_file(RUBRIC_PATH),
            "formal_v2_registry_sha256": sha256_file(REGISTRY_PATH),
            "task_records": task_records,
            "evaluator_hashes_schema": EVALUATOR_SCHEMA,
            "evaluator_hash_count": len(EVALUATOR_HASH_FIELDS),
            "evaluator_hashes_sha256": sha256_bytes(canonical_bytes(evaluator_hashes)),
            "blinding_salt_sha256": sha256_bytes(stripped_salt),
            "block_order": list(BLOCK_ORDER),
            "execution_order": [item["submission_id"] for item in run_entries],
            "shared_call_cap_per_run": 15,
            "shared_token_cap_per_run": 200000,
            "rag_enabled": {"C0": False, "C1": True},
            "remote_ref_inventory_sha256": sha256_bytes(canonical_bytes(remote_inventory)),
            "runs": run_entries,
        }
        write_json(staging / "agreement.json", agreement)
        agreement_sha = sha256_file(staging / "agreement.json")
        write_json(
            staging / "agreement.sha256.json",
            {
                "schema_version": AGREEMENT_DIGEST_SCHEMA,
                "agreement_sha256": agreement_sha,
            },
        )
        checksum_rows = []
        for path in sorted(item for item in staging.rglob("*") if item.is_file()):
            if path.name == "checksums.sha256":
                continue
            checksum_rows.append(f"{sha256_file(path)}  {path.relative_to(staging)}")
        (staging / "checksums.sha256").write_text(
            "\n".join(checksum_rows) + "\n", encoding="utf-8"
        )
        os.replace(staging, output)
        return {
            "status": "built",
            "formal_run_count": FORMAL_RUN_COUNT,
            "pair_count": PAIR_COUNT,
            "agreement_sha256": agreement_sha,
            "output": str(output),
            "external_model_calls": 0,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--blinding-salt-file", type=Path, required=True)
    parser.add_argument("--evaluator-hashes-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require_outside_git(args.blinding_salt_file, "blinding salt")
    require_outside_git(args.evaluator_hashes_file, "evaluator hash manifest")
    result = build_package(
        salt=args.blinding_salt_file.read_bytes(),
        evaluator_hashes=json.loads(args.evaluator_hashes_file.read_text(encoding="utf-8")),
        output=args.output,
        heads=remote_heads(),
        retrieve=_load_smoke_module()._retrieve_for_c1,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
