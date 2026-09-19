from __future__ import annotations

import json

import pytest
from provider_config import ProviderConfig
from quant_calculator import CALCULATION_ERROR_DETAILS

from exp3.telemetry import (
    TelemetryError,
    aggregate_attempt_accountings,
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


def _role_retrieval_delivery(
    *, attempt: int = 1, phase: str = "manager_to_architect", role: str = "manager"
) -> dict:
    return {
        "record_type": "role_retrieval_delivery",
        "attempt": attempt,
        "phase": phase,
        "role": role,
        "status": "delivered",
        "evidence_text_sha256": "1" * 64,
        "prompt_template_sha256": "2" * 64,
        "injected_memory_count": 1,
        "injected_memory_ids_sha256": "3" * 64,
    }


def _audit_provider_call(value: dict) -> dict:
    return {"record_type": "provider_call", **value}


def _t3_attempt_record(
    *,
    attempt: int = 1,
    status: str = "succeeded",
    completion: str = "complete",
    provider_call_count: int = 1,
    saved_item_count: int = 25,
) -> dict:
    return {
        "attempt": attempt,
        "status": status,
        "completion": completion,
        "provider_call_count": provider_call_count,
        "commit_count": 0,
        "committed_paths": [],
        "answer_capture_status": (
            "complete"
            if saved_item_count == 25
            else "partial"
            if saved_item_count > 0
            else "none"
        ),
        "saved_item_count": saved_item_count,
        "required_item_count": 25,
        "artifact_delivery_status": "not_committed",
        "runtime_exit_status": "clean" if status == "succeeded" else "error",
    }


def test_builds_versioned_content_free_manager_star_telemetry() -> None:
    secret = "PRIVATE prompt and handoff body"
    telemetry = build_manager_star_telemetry(
        _outcome(secret),
        model_alias="qwen3-coder",
        identity_sha256="1" * 64,
    )

    assert telemetry["schema_version"] == "exp3-runtime-telemetry-v3"
    assert telemetry["provider_identity"]["provider_mode"] == "company_litellm"
    assert telemetry["architecture_mode"] == "manager_star"
    assert telemetry["rag_enabled"] is False
    assert telemetry["shared_budget"]["calls"] == 1
    assert telemetry["role_usage"]["manager"]["total_tokens"] == 10
    assert telemetry["provider_calls"][0]["input_sha256"] == "a" * 64
    assert telemetry["isolation_preflight"]["pre_model_call_checks"] == 1
    assert telemetry["manager_acceptance_map"]["complete"] is True
    assert telemetry["role_retrieval_deliveries"] == []
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
    assert telemetry["role_retrieval_deliveries"] == []
    assert telemetry["topology"] is None


def test_single_agent_telemetry_preserves_exp2_rag_state() -> None:
    snapshot = _single_agent_snapshot()
    snapshot["rag_enabled"] = True

    telemetry = build_single_agent_telemetry(snapshot, status="succeeded")

    assert telemetry["rag_enabled"] is True


def test_two_attempt_accounting_requires_one_stable_rag_state() -> None:
    first = _single_agent_snapshot()
    second = _single_agent_snapshot()
    first["rag_enabled"] = second["rag_enabled"] = True

    aggregate = aggregate_attempt_accountings(
        [first, second],
        architecture_mode="single_agent",
    )

    assert aggregate["rag_enabled"] is True
    second["rag_enabled"] = False
    with pytest.raises(TelemetryError, match="changed between attempts"):
        aggregate_attempt_accountings(
            [first, second],
            architecture_mode="single_agent",
        )


def test_manager_star_telemetry_and_attempt_aggregate_preserve_rag_true() -> None:
    outcome = _outcome()
    outcome["rag_enabled"] = True
    outcome["audit"].extend(
        [
            _role_retrieval_delivery(),
            _audit_provider_call(outcome["provider_calls"][0]),
        ]
    )
    first = {
        "architecture_mode": "manager_star",
        "rag_enabled": True,
        "provider_identity": outcome["provider_identity"],
        "identity_sha256": "1" * 64,
        "budget": outcome["budget"],
        "provider_calls": outcome["provider_calls"],
        "isolation_preflight": outcome["isolation_preflight"],
        "calculation_tool_calls": [],
        "commit_count": 0,
    }

    aggregate = aggregate_attempt_accountings(
        [first], architecture_mode="manager_star", planned_attempts=1
    )
    telemetry = build_manager_star_telemetry(outcome)

    assert aggregate["rag_enabled"] is True
    assert telemetry["rag_enabled"] is True
    assert telemetry["role_retrieval_deliveries"] == [
        {
            "attempt": 1,
            "phase": "manager_to_architect",
            "role": "manager",
            "status": "delivered",
            "evidence_text_sha256": "1" * 64,
            "prompt_template_sha256": "2" * 64,
            "injected_memory_count": 1,
            "injected_memory_ids_sha256": "3" * 64,
        }
    ]


def test_rag_manager_telemetry_rejects_missing_stage_delivery_evidence() -> None:
    outcome = _outcome()
    outcome["rag_enabled"] = True
    outcome["audit"].append(_audit_provider_call(outcome["provider_calls"][0]))

    with pytest.raises(TelemetryError, match="no preceding retrieval delivery"):
        build_manager_star_telemetry(outcome)


def test_two_attempt_rag_manager_keeps_ten_attempt_scoped_deliveries() -> None:
    phase_roles = [
        ("manager_to_architect", "manager"),
        ("architect_to_manager", "architect"),
        ("manager_to_developer", "manager"),
        ("developer_to_manager", "developer"),
        ("manager_final", "manager"),
    ]
    outcome = _outcome()
    outcome["rag_enabled"] = True
    outcome["provider_calls"] = []
    for number, (attempt, (phase, role)) in enumerate(
        (
            (attempt, phase_role)
            for attempt in (1, 2)
            for phase_role in phase_roles
        ),
        start=1,
    ):
        provider_call = {
            "call_id": f"exp3-{number:06d}",
            "role": role,
            "phase": phase,
            "status": "succeeded",
            "failure_type": None,
            "input_sha256": "a" * 64,
            "output_sha256": "b" * 64,
            "prompt_tokens": 1,
            "completion_tokens": 0,
            "total_tokens": 1,
            "latency_seconds": 0.01,
        }
        outcome["provider_calls"].append(provider_call)
        outcome["audit"].extend(
            [
                _role_retrieval_delivery(
                    attempt=attempt, phase=phase, role=role
                ),
                _audit_provider_call(provider_call),
            ]
        )
    budget = outcome["budget"]
    budget["shared"].update(
        calls=10,
        prompt_tokens=10,
        completion_tokens=0,
        total_tokens=10,
        latency_seconds=0.1,
        max_calls=40,
        max_tokens=700_000,
    )
    for role, calls in {"manager": 6, "architect": 2, "developer": 2}.items():
        budget["roles"][role].update(
            calls=calls,
            prompt_tokens=calls,
            completion_tokens=0,
            total_tokens=calls,
            latency_seconds=round(calls * 0.01, 6),
            max_calls={"manager": 6, "architect": 6, "developer": 28}[role],
            max_tokens=700_000,
        )
    outcome["attempts"] = [
        _t3_attempt_record(
            attempt=1,
            status="failed",
            completion="runtime_failure",
            provider_call_count=5,
            saved_item_count=12,
        ),
        _t3_attempt_record(attempt=2, provider_call_count=5),
    ]

    telemetry = build_manager_star_telemetry(outcome)

    assert len(telemetry["role_retrieval_deliveries"]) == 10
    assert [item["attempt"] for item in telemetry["role_retrieval_deliveries"]] == [
        1,
        1,
        1,
        1,
        1,
        2,
        2,
        2,
        2,
        2,
    ]


def test_rag_manager_allows_multiple_provider_calls_after_one_stage_delivery() -> None:
    outcome = _outcome()
    outcome["rag_enabled"] = True
    provider_calls = []
    for number in (1, 2):
        provider_calls.append(
            {
                "call_id": f"exp3-{number:06d}",
                "role": "developer",
                "phase": "developer_to_manager",
                "status": "succeeded",
                "failure_type": None,
                "input_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "latency_seconds": 0.25,
            }
        )
    outcome["provider_calls"] = provider_calls
    outcome["audit"].extend(
        [
            _role_retrieval_delivery(
                phase="developer_to_manager", role="developer"
            ),
            *(_audit_provider_call(call) for call in provider_calls),
        ]
    )
    outcome["budget"]["shared"].update(
        calls=2,
        prompt_tokens=14,
        completion_tokens=6,
        total_tokens=20,
        latency_seconds=0.5,
    )
    outcome["budget"]["roles"]["manager"].update(
        calls=0,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        latency_seconds=0.0,
    )
    outcome["budget"]["roles"]["developer"].update(
        calls=2,
        prompt_tokens=14,
        completion_tokens=6,
        total_tokens=20,
        latency_seconds=0.5,
    )

    telemetry = build_manager_star_telemetry(outcome)

    assert len(telemetry["role_retrieval_deliveries"]) == 1
    assert len(telemetry["provider_calls"]) == 2


def test_rag_manager_rejects_provider_call_under_wrong_stage_delivery() -> None:
    outcome = _outcome()
    outcome["rag_enabled"] = True
    outcome["provider_calls"][0]["phase"] = "manager_final"
    outcome["audit"].extend(
        [
            _role_retrieval_delivery(phase="manager_to_architect", role="manager"),
            _audit_provider_call(outcome["provider_calls"][0]),
        ]
    )

    with pytest.raises(TelemetryError, match="phase does not match"):
        build_manager_star_telemetry(outcome)


def test_rag_manager_rejects_duplicate_phase_within_one_attempt() -> None:
    outcome = _outcome()
    outcome["rag_enabled"] = True
    delivery = _role_retrieval_delivery()
    outcome["audit"].extend([delivery, dict(delivery)])

    with pytest.raises(TelemetryError, match="duplicated within one attempt"):
        build_manager_star_telemetry(outcome)


def test_t3_attempt_telemetry_accepts_required_items_missing() -> None:
    outcome = _outcome()
    outcome["attempts"] = [
        {
            "attempt": 1,
            "status": "succeeded",
            "completion": "required_items_missing",
            "provider_call_count": 1,
            "commit_count": 1,
            "committed_paths": ["answer.json"],
            "answer_capture_status": "partial",
            "saved_item_count": 12,
            "required_item_count": 25,
            "artifact_delivery_status": "committed",
            "runtime_exit_status": "clean",
        }
    ]

    telemetry = build_manager_star_telemetry(outcome)

    assert telemetry["attempts"][0]["completion"] == "required_items_missing"


def test_formal_two_attempt_budget_requires_attempt_records_for_both_arms() -> None:
    outcome = _outcome()
    outcome["budget"]["shared"]["max_calls"] = 40
    with pytest.raises(TelemetryError, match="requires one or two attempt records"):
        build_manager_star_telemetry(outcome)
    outcome["attempts"] = [_t3_attempt_record()]
    assert build_manager_star_telemetry(outcome)["attempts"][0]["attempt"] == 1

    snapshot = _single_agent_snapshot()
    snapshot["budget"]["shared"]["max_calls"] = 40
    snapshot["budget"]["roles"]["developer"]["max_calls"] = 40
    with pytest.raises(TelemetryError, match="requires one or two attempt records"):
        build_single_agent_telemetry(snapshot, status="succeeded")
    snapshot["attempts"] = [_t3_attempt_record()]
    assert build_single_agent_telemetry(
        snapshot, status="succeeded"
    )["attempts"][0]["attempt"] == 1


def test_attempt_provider_call_count_is_capped_at_twenty() -> None:
    outcome = _outcome()
    outcome["attempts"] = [_t3_attempt_record(provider_call_count=20)]
    assert build_manager_star_telemetry(outcome)["attempts"][0][
        "provider_call_count"
    ] == 20

    outcome["attempts"][0]["provider_call_count"] = 21
    with pytest.raises(TelemetryError, match="provider_call_count exceeds 20"):
        build_manager_star_telemetry(outcome)


@pytest.mark.parametrize(
    "committed_paths",
    [
        None,
        ["answer.json", 7],
        ["../answer.json"],
        ["answer.json", "answer.json"],
    ],
)
def test_attempt_telemetry_rejects_invalid_committed_paths(
    committed_paths: object,
) -> None:
    outcome = _outcome()
    attempt = _t3_attempt_record()
    attempt["committed_paths"] = committed_paths
    outcome["attempts"] = [attempt]

    with pytest.raises(TelemetryError, match="committed_paths are invalid"):
        build_manager_star_telemetry(outcome)


def test_complete_t3_attempt_requires_all_twenty_five_saved_items() -> None:
    outcome = _outcome()
    outcome["attempts"] = [
        _t3_attempt_record(completion="complete", saved_item_count=24)
    ]
    with pytest.raises(TelemetryError, match="retain all 25 items"):
        build_manager_star_telemetry(outcome)

    outcome["attempts"] = [
        _t3_attempt_record(
            completion="required_items_missing", saved_item_count=25
        )
    ]
    with pytest.raises(TelemetryError, match="requires complete status"):
        build_manager_star_telemetry(outcome)


def test_both_arms_emit_the_same_content_free_calculation_tool_contract() -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "succeeded",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "stored_results_sha256": "4" * 64,
        "latency_seconds": 0.125,
        "operation_count": 7,
    }
    outcome = _outcome()
    outcome["calculation_tool_calls"] = [record]
    snapshot = _single_agent_snapshot()
    snapshot["calculation_tool_calls"] = [record]

    m1 = build_manager_star_telemetry(outcome)
    m0 = build_single_agent_telemetry(snapshot, status="succeeded")

    assert m0["calculation_tool_calls"] == m1["calculation_tool_calls"] == [record]
    assert m0["shared_budget"]["calls"] == 1
    assert m1["shared_budget"]["calls"] == 1


def test_calculation_failure_telemetry_keeps_safe_location_and_rule() -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "failed",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "latency_seconds": 0.125,
        "operation_count": 25,
        "error": "invalid_operand_object",
        "error_detail": CALCULATION_ERROR_DETAILS["invalid_operand_object"],
        "failed_operation_index": 17,
        "failed_operation": "subtract",
        "failed_argument": "left",
    }
    snapshot = _single_agent_snapshot()
    snapshot["calculation_tool_calls"] = [record]

    telemetry = build_single_agent_telemetry(snapshot, status="failed")

    assert telemetry["calculation_tool_calls"] == [record]
    assert "request" not in telemetry["calculation_tool_calls"][0]


def test_partial_calculation_telemetry_preserves_only_shape_and_failure_metadata() -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "partial",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "stored_results_sha256": "4" * 64,
        "latency_seconds": 0.125,
        "operation_count": 4,
        "completed_operation_count": 3,
        "stored_result_shapes": [
            {"kind": "array", "shape": [120]},
            {"kind": "scalar", "shape": []},
        ],
        "error": "invalid_operand_object",
        "error_detail": CALCULATION_ERROR_DETAILS["invalid_operand_object"],
        "failed_operation_index": 3,
        "failed_operation": "subtract",
        "failed_argument": "left",
    }
    outcome = _outcome()
    outcome["calculation_tool_calls"] = [record]

    telemetry = build_manager_star_telemetry(outcome)

    assert telemetry["calculation_tool_calls"] == [record]
    saved_record = telemetry["calculation_tool_calls"][0]
    assert "stored_result_ids" not in saved_record
    assert "results" not in saved_record
    assert "values" not in saved_record


def test_partial_calculation_telemetry_requires_retained_prefix_evidence() -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "partial",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "latency_seconds": 0.125,
        "operation_count": 4,
        "completed_operation_count": 3,
        "error": "invalid_operand_object",
        "failed_operation_index": 3,
    }
    outcome = _outcome()
    outcome["calculation_tool_calls"] = [record]

    with pytest.raises(TelemetryError, match="partial"):
        build_manager_star_telemetry(outcome)


@pytest.mark.parametrize(
    "change,match",
    [
        ({"role": "manager"}, "identity"),
        ({"tool": "python"}, "identity"),
        ({"input_sha256": "secret"}, "hash"),
        ({"stored_results_sha256": "secret"}, "hash"),
        ({"latency_seconds": float("nan")}, "finite"),
        ({"operation_count": 65}, "cap"),
    ],
)
def test_rejects_unsafe_calculation_tool_telemetry(change, match) -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "succeeded",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "latency_seconds": 0.125,
        "operation_count": 1,
        **change,
    }
    outcome = _outcome()
    outcome["calculation_tool_calls"] = [record]

    with pytest.raises(TelemetryError, match=match):
        build_manager_star_telemetry(outcome)


def test_preserves_rejected_oversized_calculation_request() -> None:
    record = {
        "tool": "quant_calculate",
        "role": "developer",
        "status": "failed",
        "input_sha256": "2" * 64,
        "output_sha256": "3" * 64,
        "latency_seconds": 0.125,
        "operation_count": 69,
    }
    outcome = _outcome()
    outcome["calculation_tool_calls"] = [record]

    telemetry = build_manager_star_telemetry(outcome)

    assert telemetry["calculation_tool_calls"] == [record]


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
