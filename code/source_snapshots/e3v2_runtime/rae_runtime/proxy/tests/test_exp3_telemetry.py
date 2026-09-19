from __future__ import annotations

import json

import pytest
from provider_config import ProviderConfig

from exp3.telemetry import (
    TelemetryError,
    build_manager_star_telemetry,
    build_single_agent_telemetry,
)


def _provider_identity() -> dict[str, str]:
    return ProviderConfig(
        provider_mode="company_litellm",
        base_url="http://weles.cs.ucl.ac.uk:4000",
        model_alias="qwen3-coder",
        transport_model="litellm_proxy/qwen3-coder",
        adapter="litellm_chat_completions",
        api_key="test-secret",
        api_key_source="test",
        explicit_provider=True,
    ).identity


def _budget() -> dict:
    roles = {}
    for role, max_calls, max_tokens in (
        ("manager", 3, 45_000),
        ("architect", 3, 35_000),
        ("developer", 9, 120_000),
    ):
        roles[role] = {
            "calls": 1 if role == "manager" else 0,
            "prompt_tokens": 7 if role == "manager" else 0,
            "completion_tokens": 3 if role == "manager" else 0,
            "total_tokens": 10 if role == "manager" else 0,
            "failures": 0,
            "latency_seconds": 0.25 if role == "manager" else 0,
            "pending_calls": 0,
            "records": [],
            "max_calls": max_calls,
            "max_tokens": max_tokens,
            "over_budget": False,
        }
    return {
        "shared": {
            "calls": 1,
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
            "failures": 0,
            "latency_seconds": 0.25,
            "pending_calls": 0,
            "max_calls": 15,
            "max_tokens": 200_000,
            "over_budget": False,
        },
        "roles": roles,
    }


def _outcome(secret: str = "") -> dict:
    return {
        "provider_identity": _provider_identity(),
        "status": "succeeded",
        "failure_phase": None,
        "budget": _budget(),
        "provider_calls": [
            {
                "call_id": "exp3-000001",
                "role": "manager",
                "phase": "manager_to_architect",
                "status": "succeeded",
                "failure_type": None,
                "input_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "latency_seconds": 0.25,
            }
        ],
        "audit": [
            {
                "phase": "architect_plan_validation",
                "status": "validated",
                "handoff": {
                    "version": "architect_plan_v1",
                    "producer": "architect",
                    "hash": "c" * 64,
                    "char_count": 100,
                },
            },
            {"private": secret},
        ],
        "isolation_preflight": [
            {
                "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                "phase": "pre_orchestration",
                "passed": True,
                "manifest_sha256": "d" * 64,
                "deny_ref_set_sha256": "e" * 64,
                "repository_count": 1,
                "denied_ref_count": 7,
                "source_presence_checked": True,
                "current_target_absence_checked": True,
            },
            {
                "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                "phase": "pre_model_call",
                "passed": True,
                "manifest_sha256": "d" * 64,
                "deny_ref_set_sha256": "e" * 64,
                "repository_count": 1,
                "denied_ref_count": 7,
                "source_presence_checked": True,
                "current_target_absence_checked": False,
            },
        ],
        "manager_acceptance_map": {
            "approved_count": 1,
            "evaluated_count": 1,
            "passed_count": 1,
            "complete": True,
            "sha256": "f" * 64,
        },
        "topology": {"cursor": 5, "sequence_complete": True, "finalized": True},
    }


def _single_agent_snapshot() -> dict:
    zero = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "failures": 0,
        "latency_seconds": 0.0,
        "pending_calls": 0,
        "records": [],
        "max_calls": 0,
        "max_tokens": 0,
        "over_budget": False,
    }
    developer = {
        **zero,
        "calls": 1,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
        "latency_seconds": 0.25,
        "max_calls": 15,
        "max_tokens": 200_000,
    }
    return {
        "architecture_mode": "single_agent",
        "rag_enabled": False,
        "provider_identity": _provider_identity(),
        "budget": {
            "shared": {
                **developer,
                "records": [],
            },
            "roles": {
                "manager": dict(zero),
                "architect": dict(zero),
                "developer": developer,
            },
        },
        "provider_calls": [
            {
                "call_id": "exp3-000001",
                "role": "developer",
                "phase": "single_agent_developer",
                "status": "succeeded",
                "failure_type": None,
                "input_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "latency_seconds": 0.25,
            }
        ],
        "isolation_preflight": _outcome()["isolation_preflight"],
    }


def test_builds_versioned_content_free_manager_star_telemetry() -> None:
    secret = "PRIVATE prompt and handoff body"
    telemetry = build_manager_star_telemetry(
        _outcome(secret),
        model_alias="qwen3-coder",
        identity_sha256="1" * 64,
    )

    assert telemetry["schema_version"] == "exp3-runtime-telemetry-v2"
    assert telemetry["provider_identity"]["provider_mode"] == "company_litellm"
    assert telemetry["architecture_mode"] == "manager_star"
    assert telemetry["rag_enabled"] is False
    assert telemetry["shared_budget"]["calls"] == 1
    assert telemetry["role_usage"]["manager"]["total_tokens"] == 10
    assert telemetry["provider_calls"][0]["input_sha256"] == "a" * 64
    assert telemetry["isolation_preflight"]["pre_model_call_checks"] == 1
    assert telemetry["manager_acceptance_map"]["complete"] is True
    assert secret not in json.dumps(telemetry)


def test_builds_versioned_single_agent_telemetry_without_m1_role_artifacts() -> None:
    telemetry = build_single_agent_telemetry(
        _single_agent_snapshot(),
        status="succeeded",
        model_alias="qwen3-coder",
        commit_count=1,
    )

    assert telemetry["architecture_mode"] == "single_agent"
    assert telemetry["rag_enabled"] is False
    assert telemetry["shared_budget"]["max_calls"] == 15
    assert telemetry["role_usage"]["developer"]["max_tokens"] == 200_000
    assert telemetry["role_usage"]["manager"]["call_exhausted"] is False
    assert telemetry["provider_calls"][0]["role"] == "developer"
    assert telemetry["commit_count"] == 1
    assert telemetry["handoffs"] == []
    assert telemetry["manager_acceptance_map"] is None
    assert telemetry["topology"] is None


def test_exhaustion_is_derived_from_fixed_capacity_without_reallocation() -> None:
    outcome = _outcome()
    manager = outcome["budget"]["roles"]["manager"]
    manager.update(calls=3)
    outcome["budget"]["shared"].update(calls=3)
    outcome["provider_calls"] = [
        {
            **outcome["provider_calls"][0],
            "call_id": f"exp3-{index:06d}",
            "prompt_tokens": 7 if index == 1 else 0,
            "completion_tokens": 3 if index == 1 else 0,
            "total_tokens": 10 if index == 1 else 0,
        }
        for index in range(1, 4)
    ]

    telemetry = build_manager_star_telemetry(outcome)

    assert telemetry["role_usage"]["manager"]["call_exhausted"] is True
    assert telemetry["shared_budget"]["call_exhausted"] is False
    assert telemetry["role_usage"]["architect"]["call_exhausted"] is False


def test_rejects_call_or_token_records_that_do_not_reconcile() -> None:
    outcome = _outcome()
    outcome["budget"]["shared"]["total_tokens"] = 11
    with pytest.raises(TelemetryError, match="total_tokens"):
        build_manager_star_telemetry(outcome)


def test_rejects_non_hash_or_free_text_failure_metadata() -> None:
    outcome = _outcome()
    outcome["provider_calls"][0]["failure_type"] = "PRIVATE provider response"
    outcome["provider_calls"][0]["status"] = "failed"
    with pytest.raises(TelemetryError, match="content-free"):
        build_manager_star_telemetry(outcome)


@pytest.mark.parametrize("latency", [float("nan"), float("inf"), -0.01])
def test_rejects_non_finite_or_negative_latency(latency: float) -> None:
    outcome = _outcome()
    outcome["provider_calls"][0]["latency_seconds"] = latency
    with pytest.raises(TelemetryError, match="finite and non-negative"):
        build_manager_star_telemetry(outcome)


def test_failure_object_uses_provider_records_from_safe_audit() -> None:
    class Failure(RuntimeError):
        failure_phase = "developer_to_manager"
        budget = _budget()
        topology = {"cursor": 4, "sequence_complete": False, "finalized": False}
        audit = [
            {
                "record_type": "provider_call",
                **_outcome()["provider_calls"][0],
            }
        ]
        provider_identity = _provider_identity()

    telemetry = build_manager_star_telemetry(Failure("PRIVATE failure body"))

    assert telemetry["status"] == "failed"
    assert telemetry["failure_phase"] == "developer_to_manager"
    assert len(telemetry["provider_calls"]) == 1
    assert "PRIVATE failure body" not in json.dumps(telemetry)
