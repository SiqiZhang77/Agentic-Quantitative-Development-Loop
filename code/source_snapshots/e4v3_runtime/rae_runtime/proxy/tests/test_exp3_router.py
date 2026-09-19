from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest
import exp3.router as router_module

from exp3.architecture import ArchitectureModeError
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.policy import ROLE_SEQUENCE
from exp3.production_provider import ProductionProviderBoundaryUnavailable
from exp3.router import (
    PHASE_ARCHITECT_TO_MANAGER,
    PHASE_DEVELOPER_TO_MANAGER,
    PHASE_MANAGER_FINAL,
    PHASE_MANAGER_TO_ARCHITECT,
    PHASE_MANAGER_TO_DEVELOPER,
    ManagerStarAdapterUnavailable,
    ManagerStarRunError,
    ProviderBoundaryBypassError,
    RoleCallResult,
    run_manager_star,
    run_manager_star_runtime,
)


ROUTER_SOURCE = Path(__file__).resolve().parents[1] / "exp3" / "router.py"


def _payload() -> dict:
    return {
        "architecture_mode": "manager_star",
        "issue_key": "SCRUM-390",
        "command": "refactor",
        "execution_objectives": {
            "strategy_type": "refactor",
            "resource_path": "rae_runtime/proxy/budget_guard.py",
            "parsed_task_parameters": {
                "rag_enabled": False,
                "objective": "Implement the frozen E3 fixture",
            },
        },
        "jira_context": {
            "ticket_id": "SCRUM-390",
            "summary": "E3 fixture",
            "description": "Implement once and verify once.",
        },
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "frozen-source",
                "target_branch": "quant/SCRUM-390",
                "allowed_directories": ["rae_runtime/proxy"],
            }
        ],
        "strategy": {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "ref": "frozen-source",
            "target_branch": "quant/SCRUM-390",
            "source_path": "rae_runtime/proxy/budget_guard.py",
            "target_path": "rae_runtime/proxy/budget_guard.py",
        },
        "iteration_controls": {
            "allow_iteration": False,
            "max_iterations": 1,
            "max_failed_iterations": 1,
        },
        "retrieval_context": None,
        "output_paths": {"result_path": "/workspace/output/result.json"},
    }


def _factorial_rag_payload() -> dict:
    payload = _payload()
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters.update(
        {
            "formal_execution_contract": "factorial_rag_architecture_v1",
            "rag_enabled": True,
            "rag_top_k": 5,
            "rag_delivery_policy": "all_model_stages_v1",
        }
    )
    payload["retrieval_context"] = {
        "enabled": True,
        "status": "ok",
        "memories": [
            {
                "memory_id": "MEM-RAG-01",
                "source_ticket_id": "SCRUM-20",
                "source_type": "comment",
                "source_id": "comment:20",
                "source_timestamp": "2026-06-01T00:00:00+00:00",
                "rank": 1,
                "score": 1.0,
                "text": "PRIVATE FROZEN RAG EVIDENCE FOR FIVE STAGES",
            }
        ],
    }
    return payload


def _negative_preflight(payload: dict) -> NegativeRefPreflight:
    repositories = []
    for item in payload["repositories"]:
        repositories.append(
            {
                "repo_full_name": item["repo_full_name"],
                "source_ref": item["source_branch"],
                "current_target_ref": item["target_branch"],
                "paired_target_ref": item["target_branch"] + "-paired",
                "deny_refs": {
                    "v5_refs": ["exp2/v5"],
                    "e2_output_refs": ["quant/E2-output"],
                    "earlier_e3_targets": [],
                    "protected_refs": ["main"],
                    "arbitrary_probe_refs": ["arbitrary/a", "arbitrary/b"],
                },
            }
        )
    preflight = NegativeRefPreflight(
        NegativeRefManifest.from_dict(
            {
                "schema_version": "exp3-negative-ref-manifest-v1",
                "experiment_id": "E3-manager-star-v1",
                "run_id": "E3-router-test",
                "repositories": repositories,
            }
        ),
        ref_exists=lambda _repo, ref: ref
        in {item["source_branch"] for item in payload["repositories"]},
    )
    return preflight


def _run_manager_star(payload: dict, executor, **kwargs):
    return run_manager_star(
        payload,
        executor,
        preflight=_negative_preflight(payload),
        **kwargs,
    )


def _architect_plan(secret: str = "") -> dict:
    return {
        "version": "architect_plan_v1",
        "producer": "architect",
        "risks": [f"Preserve M0 compatibility{secret}"],
        "files": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "purpose": "Keep the legacy budget path unchanged.",
            }
        ],
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "One implementation pass",
                "planned_evidence": "Focused offline tests",
                "files": ["rae_runtime/proxy/budget_guard.py"],
            }
        ],
    }


def _developer_result() -> dict:
    return {
        "version": "developer_result_v1",
        "producer": "developer",
        "implementation": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "summary": "No M0 behavior was changed.",
            }
        ],
        "verification_evidence": [
            {
                "check": "focused tests",
                "status": "passed",
                "evidence": "All tests passed offline.",
            }
        ],
    }


def _manager_final(*, accepted: bool = True) -> dict:
    return {
        "version": "manager_final_v1",
        "producer": "manager",
        "decision": "accepted" if accepted else "failed",
        "failure_phase": None if accepted else "developer_verification",
        "summary": "Complete." if accepted else "Verification was incomplete.",
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "One implementation pass",
                "status": "passed" if accepted else "not_verified",
                "evidence": (
                    "The developer result contains passing evidence."
                    if accepted
                    else "The expected evidence was not present."
                ),
            }
        ],
    }


class RecordingExecutor:
    def __init__(self, *, final_accepted: bool = True) -> None:
        self.calls: list[dict] = []
        self.final_accepted = final_accepted

    def __call__(self, *, role: str, phase: str, context: dict) -> RoleCallResult:
        self.calls.append(
            {"role": role, "phase": phase, "context": copy.deepcopy(context)}
        )
        outputs = {
            PHASE_MANAGER_TO_ARCHITECT: {
                "instruction": "Produce one bounded plan."
            },
            PHASE_ARCHITECT_TO_MANAGER: _architect_plan(),
            PHASE_MANAGER_TO_DEVELOPER: {
                "instruction": "Implement the approved plan exactly once."
            },
            PHASE_DEVELOPER_TO_MANAGER: _developer_result(),
            PHASE_MANAGER_FINAL: _manager_final(accepted=self.final_accepted),
        }
        return RoleCallResult(
            output=outputs[phase],
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            latency_seconds=0.01,
        )


def test_success_uses_exact_topology_and_only_manager_mediated_context() -> None:
    executor = RecordingExecutor()

    result = _run_manager_star(_payload(), executor)

    assert [call["role"] for call in executor.calls] == list(ROLE_SEQUENCE)
    assert [call["phase"] for call in executor.calls] == [
        PHASE_MANAGER_TO_ARCHITECT,
        PHASE_ARCHITECT_TO_MANAGER,
        PHASE_MANAGER_TO_DEVELOPER,
        PHASE_DEVELOPER_TO_MANAGER,
        PHASE_MANAGER_FINAL,
    ]
    architect_context = executor.calls[1]["context"]
    assert set(architect_context) == {
        "task",
        "repository_context",
        "manager_instruction",
    }
    assert "developer_result" not in architect_context
    developer_context = executor.calls[3]["context"]
    assert set(developer_context) == {
        "task",
        "repository_context",
        "manager_handoff",
    }
    assert set(developer_context["manager_handoff"]) == {
        "approved_plan",
        "instruction",
    }
    assert result["status"] == "succeeded"
    assert result["manager_final"] == _manager_final()
    assert result["topology"]["finalized"] is True
    assert result["budget"]["shared"]["calls"] == 5
    assert result["budget"]["shared"]["total_tokens"] == 75


def test_r1_delivers_one_frozen_evidence_block_to_all_five_role_stages(
    monkeypatch,
) -> None:
    payload = _factorial_rag_payload()
    executor = RecordingExecutor()
    freezes = 0
    original_freeze = router_module.freeze_retrieval_prompt_delivery

    def counted_freeze(value):
        nonlocal freezes
        freezes += 1
        return original_freeze(value)

    monkeypatch.setattr(router_module, "freeze_retrieval_prompt_delivery", counted_freeze)

    result = _run_manager_star(payload, executor)

    assert freezes == 1
    assert result["rag_enabled"] is True
    assert len(executor.calls) == 5
    rendered_blocks = [
        call["context"]["task"]["retrieved_memory_context"]
        for call in executor.calls
    ]
    assert len(set(rendered_blocks)) == 1
    assert "PRIVATE FROZEN RAG EVIDENCE FOR FIVE STAGES" in rendered_blocks[0]
    assert all(
        "retrieval_delivery" not in call["context"]["task"]
        for call in executor.calls
    )

    deliveries = [
        item
        for item in result["audit"]
        if item.get("record_type") == "role_retrieval_delivery"
    ]
    assert [item["phase"] for item in deliveries] == [
        PHASE_MANAGER_TO_ARCHITECT,
        PHASE_ARCHITECT_TO_MANAGER,
        PHASE_MANAGER_TO_DEVELOPER,
        PHASE_DEVELOPER_TO_MANAGER,
        PHASE_MANAGER_FINAL,
    ]
    assert all(
        set(item)
        == {
            "record_type",
            "attempt",
            "phase",
            "role",
            "status",
            "evidence_text_sha256",
            "prompt_template_sha256",
            "injected_memory_count",
            "injected_memory_ids_sha256",
        }
        for item in deliveries
    )
    assert len({item["evidence_text_sha256"] for item in deliveries}) == 1
    assert len({item["injected_memory_ids_sha256"] for item in deliveries}) == 1
    assert all(item["injected_memory_count"] == 1 for item in deliveries)
    serialized = json.dumps(deliveries, sort_keys=True)
    assert "PRIVATE FROZEN RAG EVIDENCE" not in serialized
    assert "MEM-RAG-01" not in serialized


def test_r0_never_builds_or_delivers_retrieval_context(monkeypatch) -> None:
    def forbidden(_payload):
        raise AssertionError("R0 must not prepare a retrieval delivery")

    monkeypatch.setattr("exp3.router.freeze_retrieval_prompt_delivery", forbidden)
    executor = RecordingExecutor()

    result = _run_manager_star(_payload(), executor)

    assert result["rag_enabled"] is False
    assert all(
        "retrieved_memory_context" not in call["context"]["task"]
        and "retrieval_delivery" not in call["context"]["task"]
        for call in executor.calls
    )
    assert not any(
        item.get("record_type") == "role_retrieval_delivery"
        for item in result["audit"]
    )


def test_r1_second_attempt_labels_all_five_delivery_records() -> None:
    payload = _factorial_rag_payload()
    payload["iteration_controls"] = {
        "allow_iteration": True,
        "max_iterations": 2,
        "max_failed_iterations": 2,
    }
    payload["_experiment_attempt"] = {"number": 2, "maximum": 2}

    result = _run_manager_star(payload, RecordingExecutor())

    deliveries = [
        item
        for item in result["audit"]
        if item.get("record_type") == "role_retrieval_delivery"
    ]
    assert len(deliveries) == 5
    assert {item["attempt"] for item in deliveries} == {2}


def test_r1_runtime_failure_preserves_true_rag_state(monkeypatch) -> None:
    payload = _factorial_rag_payload()
    base = RecordingExecutor()

    def invalid_architect(*, role: str, phase: str, context: dict) -> RoleCallResult:
        result = base(role=role, phase=phase, context=context)
        if phase == PHASE_ARCHITECT_TO_MANAGER:
            return RoleCallResult(
                {"invalid": "handoff"},
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                latency_seconds=0.01,
            )
        return result

    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    with pytest.raises(ManagerStarRunError) as raised:
        run_manager_star_runtime(
            payload,
            executor=invalid_architect,
            preflight=_negative_preflight(payload),
        )

    assert raised.value.failure_phase == "architect_plan_validation"
    assert raised.value.rag_enabled is True
    deliveries = [
        item
        for item in raised.value.audit
        if item.get("record_type") == "role_retrieval_delivery"
    ]
    assert [item["phase"] for item in deliveries] == [
        PHASE_MANAGER_TO_ARCHITECT,
        PHASE_ARCHITECT_TO_MANAGER,
    ]


def test_router_accounts_multiple_provider_turns_inside_one_architect_stage() -> None:
    executor = RecordingExecutor()

    def runner(*, role, phase, context, provider_call, mcp_launch_spec):
        assert mcp_launch_spec.role == role
        result = provider_call(request=context)
        if role == "architect":
            result = provider_call(request={**context, "turn": 2})
        return result

    result = _run_manager_star(
        _payload(),
        executor,
        role_runner=runner,
    )

    assert result["topology"]["finalized"] is True
    assert result["budget"]["shared"]["calls"] == 6
    assert result["budget"]["roles"]["architect"]["calls"] == 2
    assert len(result["provider_calls"]) == 6
    assert [record["phase"] for record in result["provider_calls"]].count(
        PHASE_ARCHITECT_TO_MANAGER
    ) == 2


def test_role_runner_cannot_return_without_using_provider_boundary() -> None:
    executor = RecordingExecutor()

    def bypassing_runner(**kwargs):
        del kwargs
        return {"instruction": "unaccounted output"}

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(
            _payload(),
            executor,
            role_runner=bypassing_runner,
        )

    assert raised.value.failure_phase == PHASE_MANAGER_TO_ARCHITECT
    assert raised.value.failure_type == ProviderBoundaryBypassError.__name__
    assert raised.value.budget["shared"]["calls"] == 0
    assert executor.calls == []


def test_router_skeleton_has_no_model_network_mcp_or_github_import() -> None:
    tree = ast.parse(
        ROUTER_SOURCE.read_text(encoding="utf-8"),
        filename=str(ROUTER_SOURCE),
    )
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(
        {
            "agents",
            "boto3",
            "fastmcp",
            "github",
            "httpx",
            "litellm",
            "requests",
        }
    )


def test_router_does_not_mutate_payload_or_share_executor_context() -> None:
    payload = _payload()
    before = copy.deepcopy(payload)

    class MutatingExecutor(RecordingExecutor):
        def __call__(self, *, role: str, phase: str, context: dict) -> RoleCallResult:
            result = super().__call__(role=role, phase=phase, context=context)
            context.clear()
            return result

    _run_manager_star(payload, MutatingExecutor())

    assert payload == before


def test_manager_failed_final_is_terminal_without_retry() -> None:
    executor = RecordingExecutor(final_accepted=False)

    result = _run_manager_star(_payload(), executor)

    assert result["status"] == "failed"
    assert result["failure_phase"] == "developer_verification"
    assert len(executor.calls) == 5
    assert sum(call["role"] == "developer" for call in executor.calls) == 1
    assert result["topology"]["finalized"] is True


def test_invalid_architect_handoff_stops_before_manager_or_developer_retry() -> None:
    calls: list[tuple[str, str]] = []

    def executor(*, role: str, phase: str, context: dict) -> RoleCallResult:
        del context
        calls.append((role, phase))
        output = (
            {"instruction": "one plan"}
            if phase == PHASE_MANAGER_TO_ARCHITECT
            else {**_architect_plan(), "unexpected": "PRIVATE VALUE"}
        )
        return RoleCallResult(output, 2, 1, 0.01)

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(_payload(), executor)

    assert raised.value.failure_phase == "architect_plan_validation"
    assert raised.value.failure_type == "HandoffValidationError"
    assert calls == [
        ("manager", PHASE_MANAGER_TO_ARCHITECT),
        ("architect", PHASE_ARCHITECT_TO_MANAGER),
    ]
    assert raised.value.budget["shared"]["calls"] == 2
    assert "PRIVATE VALUE" not in json.dumps(raised.value.audit, sort_keys=True)
    assert "PRIVATE VALUE" not in str(raised.value)


def test_duplicate_architect_acceptance_ids_stop_before_developer() -> None:
    base = RecordingExecutor()

    def executor(*, role: str, phase: str, context: dict) -> RoleCallResult:
        result = base(role=role, phase=phase, context=context)
        if phase == PHASE_ARCHITECT_TO_MANAGER:
            plan = copy.deepcopy(result.output)
            plan["acceptance_mapping"].append(
                {
                    **plan["acceptance_mapping"][0],
                    "requirement": "A second requirement with a reused ID",
                }
            )
            return RoleCallResult(plan, 10, 5, 0.01, 15)
        return result

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(_payload(), executor)

    assert raised.value.failure_phase == "architect_acceptance_mapping_validation"
    assert raised.value.failure_type == "AcceptanceMappingError"
    assert [call["role"] for call in base.calls] == ["manager", "architect"]


def test_final_manager_must_cover_exact_approved_acceptance_id_set() -> None:
    base = RecordingExecutor()
    private_requirement = "PRIVATE SECOND REQUIREMENT"

    def executor(*, role: str, phase: str, context: dict) -> RoleCallResult:
        result = base(role=role, phase=phase, context=context)
        if phase == PHASE_ARCHITECT_TO_MANAGER:
            plan = copy.deepcopy(result.output)
            plan["acceptance_mapping"].append(
                {
                    "requirement_id": "REQ-2",
                    "requirement": private_requirement,
                    "planned_evidence": "A second focused check",
                    "files": ["rae_runtime/proxy/exp3/router.py"],
                }
            )
            return RoleCallResult(plan, 10, 5, 0.01, 15)
        return result

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(_payload(), executor)

    assert raised.value.failure_phase == "manager_acceptance_mapping_validation"
    assert raised.value.failure_type == "AcceptanceMappingError"
    assert len(base.calls) == 5
    assert private_requirement not in json.dumps(raised.value.audit, sort_keys=True)


def test_executor_failure_is_counted_and_never_retried() -> None:
    base = RecordingExecutor()

    def executor(*, role: str, phase: str, context: dict) -> RoleCallResult:
        if phase == PHASE_DEVELOPER_TO_MANAGER:
            base.calls.append(
                {"role": role, "phase": phase, "context": copy.deepcopy(context)}
            )
            raise RuntimeError("PRIVATE provider response")
        return base(role=role, phase=phase, context=context)

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(_payload(), executor)

    assert raised.value.failure_phase == PHASE_DEVELOPER_TO_MANAGER
    assert raised.value.failure_type == "RuntimeError"
    assert [call["role"] for call in base.calls] == [
        "manager",
        "architect",
        "manager",
        "developer",
    ]
    assert sum(call["role"] == "developer" for call in base.calls) == 1
    assert raised.value.budget["shared"]["calls"] == 4
    assert raised.value.budget["roles"]["developer"]["failures"] == 1
    assert "PRIVATE provider response" not in str(raised.value)
    assert "PRIVATE provider response" not in json.dumps(
        raised.value.audit,
        sort_keys=True,
    )


def test_role_token_overrun_is_recorded_then_stops_without_second_call() -> None:
    calls = 0

    def executor(*, role: str, phase: str, context: dict) -> RoleCallResult:
        nonlocal calls
        del role, phase, context
        calls += 1
        return RoleCallResult(
            {"instruction": "too expensive"},
            prompt_tokens=350_000,
            completion_tokens=1,
            total_tokens=350_001,
            latency_seconds=0.2,
        )

    with pytest.raises(ManagerStarRunError) as raised:
        _run_manager_star(_payload(), executor)

    assert calls == 1
    assert raised.value.failure_phase == PHASE_MANAGER_TO_ARCHITECT
    assert raised.value.failure_type == "BudgetExceeded"
    manager = raised.value.budget["roles"]["manager"]
    assert manager["calls"] == 1
    assert manager["total_tokens"] == 350_001
    assert manager["over_budget"] is True


def test_success_audit_and_budget_never_contain_private_bodies() -> None:
    secret = "PRIVATE-HANDOFF-BODY-9f8d"
    payload = _payload()
    payload["jira_context"]["description"] = secret

    class SecretExecutor(RecordingExecutor):
        def __call__(self, *, role: str, phase: str, context: dict) -> RoleCallResult:
            result = super().__call__(role=role, phase=phase, context=context)
            if phase == PHASE_ARCHITECT_TO_MANAGER:
                return RoleCallResult(_architect_plan(secret), 10, 5, 0.01, 15)
            return result

    result = _run_manager_star(payload, SecretExecutor())

    assert secret not in json.dumps(result["audit"], sort_keys=True)
    assert secret not in json.dumps(result["budget"], sort_keys=True)
    handoff_audits = [item["handoff"] for item in result["audit"] if "handoff" in item]
    assert len(handoff_audits) == 3
    assert all(set(item) == {"version", "hash", "char_count", "producer"} for item in handoff_audits)
    assert all(len(item["hash"]) == 64 for item in handoff_audits)


def test_runtime_wrapper_is_explicit_and_fails_closed_without_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _payload()
    monkeypatch.delenv("RAE_ENABLE_MANAGER_STAR", raising=False)

    with pytest.raises(ArchitectureModeError, match="disabled"):
        run_manager_star_runtime(payload)

    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    with pytest.raises(
        ProductionProviderBoundaryUnavailable,
        match="E3_NEGATIVE_REF_MANIFEST_PATH",
    ):
        run_manager_star_runtime(payload)


def test_runtime_wrapper_rejects_m0_before_adapter_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")

    m0 = _payload()
    m0["architecture_mode"] = "single_agent"
    with pytest.raises(ArchitectureModeError, match="requires manager_star"):
        run_manager_star_runtime(m0)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload["execution_objectives"][
            "parsed_task_parameters"
        ].update(rag_enabled=True),
        lambda payload: payload["iteration_controls"].update(allow_iteration=True),
        lambda payload: payload["iteration_controls"].update(max_iterations=2),
    ],
)
def test_runtime_wrapper_rejects_contamination_before_executor(
    monkeypatch: pytest.MonkeyPatch,
    mutation,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    payload = _payload()
    mutation(payload)

    with pytest.raises(ArchitectureModeError):
        run_manager_star_runtime(payload)
