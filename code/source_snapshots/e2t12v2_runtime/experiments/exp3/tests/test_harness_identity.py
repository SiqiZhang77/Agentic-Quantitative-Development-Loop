from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "harness_identity.py"
SPEC = importlib.util.spec_from_file_location("exp3_harness_identity", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _identity(arm: str, **overrides):
    targets = {
        "M0": ("quant/E3-T1-R1-M0", "quant/E3-T1-R1-M1"),
        "M1": ("quant/E3-T1-R1-M1", "quant/E3-T1-R1-M0"),
    }
    kwargs = {
        "task_id": "T1",
        "replicate": 1,
        "arm": arm,
        "provider_mode": "company_litellm",
        "provider_base_url": "http://weles.cs.ucl.ac.uk:4000",
        "provider_identity_sha256": "7" * 64,
        "model_alias": "qwen3-coder",
        "source_ref": "exp3/frozen-source",
        "source_commit": "a" * 40,
        "target_ref": targets[arm][0],
        "paired_target_ref": targets[arm][1],
        "task_text_sha256": "1" * 64,
        "public_inputs_sha256": {},
        "rubric_sha256": "2" * 64,
        "tool_implementation_sha256": "3" * 64,
        "sandbox_profile_sha256": "4" * 64,
        "negative_ref_manifest_sha256": ("5" if arm == "M0" else "6") * 64,
    }
    kwargs.update(overrides)
    return MODULE.build_run_identity(**kwargs)


def _request(target: str) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "draft",
        "repository_details": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "exp3/frozen-source",
                "target_branch": target,
                "allowed_directories": ["rae_runtime"],
            }
        ],
        "execution_objectives": {
            "strategy_type": "other",
            "parsed_task_parameters": {},
        },
        "iteration_controls": {},
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
        },
    }


def test_builds_explicit_rag_off_m0_and_fixed_role_budget_m1() -> None:
    m0 = _identity("M0")
    m1 = _identity("M1")

    assert m0.value["architecture_mode"] == "single_agent"
    assert m0.value["rag_enabled"] is False
    assert m0.value["retrieval_context_included"] is False
    assert m0.value["provider_mode"] == "company_litellm"
    assert m0.value["shared_budget"] == {"max_calls": 15, "max_tokens": 200_000}
    assert m0.value["role_budgets"] is None
    assert m1.value["architecture_mode"] == "manager_star"
    assert m1.value["role_budgets"] == MODULE.ROLE_BUDGETS
    assert m1.value["role_budgets"]["unused_capacity_transferable"] is False
    assert m0.value["runtime_limits"] == m1.value["runtime_limits"]


def test_pair_validation_holds_every_public_runtime_identity_constant() -> None:
    MODULE.validate_pair(_identity("M0"), _identity("M1"))

    changed = _identity("M1", model_alias="different-model")
    with pytest.raises(MODULE.IdentityError, match="model_alias"):
        MODULE.validate_pair(_identity("M0"), changed)

    changed_provider = _identity(
        "M1",
        provider_mode="openai",
        provider_base_url="https://api.openai.com/v1",
        provider_identity_sha256="8" * 64,
    )
    with pytest.raises(MODULE.IdentityError, match="provider_mode"):
        MODULE.validate_pair(_identity("M0"), changed_provider)


@pytest.mark.parametrize("arm", ["M0", "M1"])
def test_apply_identity_emits_explicit_architecture_rag_false_and_equal_limits(arm):
    identity = _identity(arm)
    request = _request(identity.value["target_ref"])
    before = copy.deepcopy(request)

    output = MODULE.apply_identity_to_runtime_request(request, identity)

    assert request == before
    assert output["architecture_mode"] == identity.value["architecture_mode"]
    parameters = output["execution_objectives"]["parsed_task_parameters"]
    assert parameters["rag_enabled"] is False
    assert parameters["provider_mode"] == "company_litellm"
    assert parameters["provider_base_url"] == "http://weles.cs.ucl.ac.uk:4000"
    assert parameters["provider_identity_sha256"] == "7" * 64
    assert parameters["model"] == "qwen3-coder"
    assert "retrieval_context" not in output
    assert output["iteration_controls"] == {
        "allow_iteration": False,
        "max_iterations": 1,
        "max_failed_iterations": 1,
        "max_agent_turns": 15,
        "max_token_budget_per_run": 200_000,
        "timeout_seconds": 1_800,
    }
    assert output["resource_requirements"] == {
        "cpu_vcpus": 2,
        "memory_mb": 4_096,
        "pids_limit": 256,
    }


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda request: request.update(retrieval_context=None), "omit retrieval_context"),
        (
            lambda request: request["execution_objectives"]["parsed_task_parameters"].update(rag_enabled=True),
            "cannot enable RAG",
        ),
        (
            lambda request: request["execution_objectives"]["parsed_task_parameters"].update(rag_top_k=5),
            "retrieval parameters",
        ),
        (
            lambda request: request["iteration_controls"].update(max_agent_turns=16),
            "max_agent_turns",
        ),
        (
            lambda request: request.setdefault("resource_requirements", {}).update(memory_mb=8192),
            "memory_mb",
        ),
    ],
)
def test_e3_harness_fails_closed_on_rag_combination_or_compute_drift(mutate, message):
    identity = _identity("M1")
    request = _request(identity.value["target_ref"])
    mutate(request)
    with pytest.raises(MODULE.IdentityError, match=message):
        MODULE.apply_identity_to_runtime_request(request, identity)


def test_schema_rejects_manager_star_without_role_budgets_and_main_source() -> None:
    value = _identity("M1").value
    value["role_budgets"] = None
    with pytest.raises(MODULE.IdentityError):
        MODULE.RunIdentity.from_dict(value)

    value = _identity("M1").value
    value["source_ref"] = "main"
    with pytest.raises(MODULE.IdentityError, match="must not be main"):
        MODULE.RunIdentity.from_dict(value)


def test_identity_contains_no_private_evaluator_or_condition_map_fields() -> None:
    serialized = json.dumps(_identity("M1").value, sort_keys=True)
    assert "evaluator" not in serialized
    assert "condition_map" not in serialized
    assert "blinding" not in serialized
    assert _identity("M1").value["status"] == "draft_local_only"
