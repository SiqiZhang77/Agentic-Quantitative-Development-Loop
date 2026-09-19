"""Draft-local identity layer for Experiment 3 request construction.

This is not a formal run-package builder and performs no external operation. It
binds the public/runtime identity needed to prove that M0 and M1 differ in
architecture only. Hidden evaluator identities remain outside this agent-visible
local layer and must be bound later by the separately approved private freeze.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schemas" / "run_identity_v2.schema.json"
ARCHITECTURES = {"M0": "single_agent", "M1": "manager_star"}
SHARED_BUDGET = {"max_calls": 15, "max_tokens": 200_000}
ROLE_BUDGETS = {
    "manager": {"max_calls": 3, "max_tokens": 45_000},
    "architect": {"max_calls": 3, "max_tokens": 35_000},
    "developer": {"max_calls": 9, "max_tokens": 120_000},
    "unused_capacity_transferable": False,
}
RUNTIME_LIMITS = {
    "timeout_seconds": 1_800,
    "cpu_vcpus": 2,
    "memory_mb": 4_096,
    "pids_limit": 256,
}


class IdentityError(ValueError):
    """The proposed E3 run/pair would not preserve the frozen comparison."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_identity(value: Any) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _validate(value: Any) -> None:
    errors = sorted(
        _validator().iter_errors(value),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        reasons = [
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"violates {error.validator or 'schema'}"
            for error in errors
        ]
        raise IdentityError("run identity: " + "; ".join(reasons))


@dataclass(frozen=True)
class RunIdentity:
    sha256: str
    _canonical: str = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, value: Any) -> "RunIdentity":
        _validate(value)
        parsed = json.loads(canonical_json(value))
        arm = "M0" if parsed["architecture_mode"] == "single_agent" else "M1"
        expected_key = f"E3-{parsed['task_id']}-R{parsed['replicate']}-{arm}"
        if parsed["run_key"] != expected_key:
            raise IdentityError("run_key does not match task/replicate/architecture")
        if parsed["source_ref"] == "main":
            raise IdentityError("E3 source_ref must not be main")
        if len({parsed["source_ref"], parsed["target_ref"], parsed["paired_target_ref"]}) != 3:
            raise IdentityError("source, target and paired target refs must be distinct")
        canonical = canonical_json(parsed)
        return cls(sha256=sha256_identity(parsed), _canonical=canonical)

    @property
    def value(self) -> dict[str, Any]:
        return json.loads(self._canonical)


def build_run_identity(
    *,
    task_id: str,
    replicate: int,
    arm: str,
    provider_mode: str,
    provider_base_url: str,
    provider_identity_sha256: str,
    model_alias: str,
    source_ref: str,
    source_commit: str,
    target_ref: str,
    paired_target_ref: str,
    task_text_sha256: str,
    public_inputs_sha256: dict[str, str],
    rubric_sha256: str,
    tool_implementation_sha256: str,
    sandbox_profile_sha256: str,
    negative_ref_manifest_sha256: str,
) -> RunIdentity:
    if arm not in ARCHITECTURES:
        raise IdentityError("arm must be M0 or M1")
    value = {
        "schema_version": "exp3-run-identity-v2",
        "experiment_id": "E3-manager-star-v1",
        "protocol_version": "1.0.0-rc2",
        "status": "draft_local_only",
        "run_key": f"E3-{task_id}-R{replicate}-{arm}",
        "task_id": task_id,
        "replicate": replicate,
        "architecture_mode": ARCHITECTURES[arm],
        "rag_enabled": False,
        "retrieval_context_included": False,
        "provider_mode": provider_mode,
        "provider_base_url": provider_base_url,
        "provider_identity_sha256": provider_identity_sha256,
        "model_alias": model_alias,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "target_ref": target_ref,
        "paired_target_ref": paired_target_ref,
        "task_text_sha256": task_text_sha256,
        "public_inputs_sha256": copy.deepcopy(public_inputs_sha256),
        "rubric_sha256": rubric_sha256,
        "tool_implementation_sha256": tool_implementation_sha256,
        "sandbox_profile_sha256": sandbox_profile_sha256,
        "negative_ref_manifest_sha256": negative_ref_manifest_sha256,
        "shared_budget": copy.deepcopy(SHARED_BUDGET),
        "role_budgets": copy.deepcopy(ROLE_BUDGETS) if arm == "M1" else None,
        "runtime_limits": copy.deepcopy(RUNTIME_LIMITS),
    }
    return RunIdentity.from_dict(value)


def apply_identity_to_runtime_request(
    request: dict[str, Any],
    identity: RunIdentity,
) -> dict[str, Any]:
    """Return a detached E3 request, rejecting rather than masking conflicts."""

    if not isinstance(request, dict) or not isinstance(identity, RunIdentity):
        raise TypeError("request and RunIdentity are required")
    output = copy.deepcopy(request)
    value = identity.value
    if "retrieval_context" in output:
        raise IdentityError("E3 runtime requests must omit retrieval_context")
    objectives = output.get("execution_objectives")
    if not isinstance(objectives, dict):
        raise IdentityError("runtime request requires execution_objectives")
    parameters = objectives.setdefault("parsed_task_parameters", {})
    if not isinstance(parameters, dict):
        raise IdentityError("parsed_task_parameters must be an object")
    if parameters.get("rag_enabled") not in {None, False}:
        raise IdentityError("E3 cannot enable RAG")
    if "rag_top_k" in parameters:
        raise IdentityError("E3 cannot carry retrieval parameters")
    existing_model = parameters.get("model")
    if existing_model not in {None, value["model_alias"]}:
        raise IdentityError("runtime model alias conflicts with run identity")
    for name in ("provider_mode", "provider_base_url", "provider_identity_sha256"):
        existing = parameters.get(name)
        if existing not in {None, value[name]}:
            raise IdentityError(f"runtime {name} conflicts with run identity")

    repositories = output.get("repository_details")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise IdentityError("local E3 identity requires exactly one repository")
    repository = repositories[0]
    if repository.get("source_branch") != value["source_ref"]:
        raise IdentityError("runtime source ref conflicts with run identity")
    if repository.get("target_branch") != value["target_ref"]:
        raise IdentityError("runtime target ref conflicts with run identity")

    controls = output.get("iteration_controls")
    if not isinstance(controls, dict):
        raise IdentityError("runtime request requires iteration_controls")
    frozen_controls = {
        "allow_iteration": False,
        "max_iterations": 1,
        "max_failed_iterations": 1,
        "max_agent_turns": 15,
        "max_token_budget_per_run": 200_000,
        "timeout_seconds": 1_800,
    }
    for key, expected in frozen_controls.items():
        if key in controls and controls[key] != expected:
            raise IdentityError(f"runtime {key} conflicts with E3 identity")
        controls[key] = expected

    resources = output.setdefault("resource_requirements", {})
    if not isinstance(resources, dict):
        raise IdentityError("resource_requirements must be an object")
    frozen_resources = {
        "cpu_vcpus": 2,
        "memory_mb": 4_096,
        "pids_limit": 256,
    }
    for key, expected in frozen_resources.items():
        if key in resources and resources[key] != expected:
            raise IdentityError(f"runtime {key} conflicts with E3 identity")
        resources[key] = expected

    output["run_id"] = value["run_key"]
    output["architecture_mode"] = value["architecture_mode"]
    parameters["rag_enabled"] = False
    parameters["provider_mode"] = value["provider_mode"]
    parameters["provider_base_url"] = value["provider_base_url"]
    parameters["provider_identity_sha256"] = value["provider_identity_sha256"]
    parameters["model"] = value["model_alias"]
    return output


def validate_pair(m0: RunIdentity, m1: RunIdentity) -> None:
    """Require the same frozen public/runtime identity aside from architecture."""

    if not isinstance(m0, RunIdentity) or not isinstance(m1, RunIdentity):
        raise TypeError("m0 and m1 RunIdentity values are required")
    left = m0.value
    right = m1.value
    if left["architecture_mode"] != "single_agent" or right["architecture_mode"] != "manager_star":
        raise IdentityError("pair must be ordered M0 then M1")
    for field in (
        "experiment_id",
        "protocol_version",
        "task_id",
        "replicate",
        "rag_enabled",
        "retrieval_context_included",
        "provider_mode",
        "provider_base_url",
        "provider_identity_sha256",
        "model_alias",
        "source_ref",
        "source_commit",
        "task_text_sha256",
        "public_inputs_sha256",
        "rubric_sha256",
        "tool_implementation_sha256",
        "sandbox_profile_sha256",
        "shared_budget",
        "runtime_limits",
    ):
        if left[field] != right[field]:
            raise IdentityError(f"paired identities differ in frozen field {field}")
    if left["paired_target_ref"] != right["target_ref"]:
        raise IdentityError("M0 paired target does not bind the M1 target")
    if right["paired_target_ref"] != left["target_ref"]:
        raise IdentityError("M1 paired target does not bind the M0 target")
