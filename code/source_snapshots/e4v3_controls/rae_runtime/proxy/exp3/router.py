"""Offline-testable, fixed Experiment 3 manager-star router.

This module deliberately imports no Agents SDK, LiteLLM, MCP, GitHub, or network
client.  A caller supplies one role executor; the router supplies the topology,
minimum mediated contexts, handoff validation, accounting, and content-free
audit.  The production wrapper remains fail-closed until a real role adapter is
explicitly wired in a later phase.
"""

from __future__ import annotations

import copy
import json
import sys
from typing import Any, Callable, Mapping

from exp3.architecture import (
    MANAGER_STAR,
    explicit_rag_enabled,
    require_manager_star_enabled,
    validate_manager_star_request,
)
from exp3.budget import (
    RoleBudgetLedger,
    sha256_text,
)
from exp3.contracts import (
    HandoffValidationError,
    ValidatedHandoff,
    canonical_json,
    validate_handoff,
)
from exp3.policy import ARCHITECT, DEVELOPER, MANAGER, TopologyGuard
from exp3.provider_accounting import ProviderCallResult
from exp3.role_adapters import ManagerStarRoleAdapters, build_role_adapters
from exp3.isolation import NegativeRefPreflight
from prompt_context import freeze_retrieval_prompt_delivery


PHASE_MANAGER_TO_ARCHITECT = "manager_to_architect"
PHASE_ARCHITECT_TO_MANAGER = "architect_to_manager"
PHASE_MANAGER_TO_DEVELOPER = "manager_to_developer"
PHASE_DEVELOPER_TO_MANAGER = "developer_to_manager"
PHASE_MANAGER_FINAL = "manager_final"


# Backwards-compatible test/import name. Accounting is now performed by
# ProviderCallAccountingAdapter around every invocation, not by this router.
RoleCallResult = ProviderCallResult
RoleExecutor = Callable[..., ProviderCallResult]
RoleAdapterFactory = Callable[..., ManagerStarRoleAdapters]


class ManagerStarRunError(RuntimeError):
    """Sanitized terminal M1 failure carrying content-free diagnostics."""

    def __init__(
        self,
        *,
        failure_phase: str,
        failure_type: str,
        audit: list[dict[str, Any]],
        budget: dict[str, Any],
        topology: dict[str, Any],
    ) -> None:
        self.failure_phase = failure_phase
        self.failure_type = failure_type
        self.audit = copy.deepcopy(audit)
        self.budget = copy.deepcopy(budget)
        self.topology = copy.deepcopy(topology)
        # Production runtime enrichment fills these content-free fields when a
        # terminal failure occurs after the developer may already have written.
        self.commit_count = 0
        self.committed_paths: list[str] = []
        self.calculation_tool_calls: list[dict[str, Any]] = []
        self.isolation_preflight = None
        self.manager_acceptance_map = None
        self.rag_enabled = False
        super().__init__(
            f"manager_star failed during {failure_phase} ({failure_type})"
        )


class ManagerStarAdapterUnavailable(RuntimeError):
    """The safe Phase 1 production seam has no real role/model adapter."""


class AcceptanceMappingError(ValueError):
    """Architect and final-manager acceptance maps are not one-to-one."""


class ProviderBoundaryBypassError(RuntimeError):
    """A role runner returned without using its accounted provider callback."""


def _json_copy(value: Any) -> Any:
    """Return a detached copy using the same canonical form used for hashing."""

    return json.loads(canonical_json(value))


def _task_context(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return model-visible task data plus a separate content-free RAG receipt.

    The receipt belongs to runtime audit, not to the model prompt.  Keeping it
    separate makes R0/R1 differ only by the frozen retrieval text that the
    treatment is meant to supply; M1 does not receive extra hashes or memory
    identifiers that M0 never sees.
    """

    task = {
        "issue_key": payload.get("issue_key"),
        "command": payload.get("command"),
        "execution_objectives": payload.get("execution_objectives") or {},
        "jira_context": payload.get("jira_context") or {},
        "observation_attempt": payload.get("_experiment_attempt"),
    }
    audit_delivery: dict[str, Any] | None = None
    if explicit_rag_enabled(payload):
        mutable_payload = dict(payload)
        rendered, delivery = freeze_retrieval_prompt_delivery(mutable_payload)
        if not rendered or not isinstance(delivery, Mapping):
            raise ValueError("RAG-enabled manager-star task omitted frozen evidence")
        task["retrieved_memory_context"] = rendered
        audit_delivery = _json_copy(dict(delivery))
        if isinstance(payload, dict):
            payload["_retrieval_delivery"] = copy.deepcopy(audit_delivery)
    return _json_copy(task), audit_delivery


def _role_retrieval_delivery_record(
    context: Mapping[str, Any],
    *,
    delivery: Mapping[str, Any] | None,
    role: str,
    phase: str,
) -> dict[str, Any] | None:
    """Describe one role-stage delivery using hashes and counts, never text."""

    if delivery is None:
        return None
    task = context.get("task")
    if not isinstance(task, Mapping):
        raise ValueError("manager-star retrieval audit omitted task context")
    observation_attempt = task.get("observation_attempt")
    if observation_attempt is None:
        attempt = 1
    elif (
        isinstance(observation_attempt, Mapping)
        and type(observation_attempt.get("number")) is int
        and observation_attempt["number"] in {1, 2}
    ):
        attempt = observation_attempt["number"]
    else:
        raise ValueError("manager-star retrieval delivery attempt is invalid")
    evidence_hash = delivery.get("evidence_text_sha256")
    template_hash = delivery.get("prompt_template_sha256")
    memory_ids = delivery.get("injected_memory_ids")
    if (
        delivery.get("delivery_mode") != "generator_prompt"
        or delivery.get("prompt_injected") is not True
        or type(evidence_hash) is not str
        or len(evidence_hash) != 64
        or any(character not in "0123456789abcdef" for character in evidence_hash)
        or type(template_hash) is not str
        or len(template_hash) != 64
        or any(character not in "0123456789abcdef" for character in template_hash)
        or not isinstance(memory_ids, list)
        or len(memory_ids) > 10
        or any(
            type(memory_id) is not str or not memory_id
            for memory_id in memory_ids
        )
        or len(memory_ids) != len(set(memory_ids))
    ):
        raise ValueError("manager-star retrieval delivery receipt is invalid")
    return {
        "record_type": "role_retrieval_delivery",
        "attempt": attempt,
        "phase": phase,
        "role": role,
        "status": "delivered",
        "evidence_text_sha256": evidence_hash,
        "prompt_template_sha256": template_hash,
        "injected_memory_count": len(memory_ids),
        "injected_memory_ids_sha256": sha256_text(canonical_json(memory_ids)),
    }


def _repository_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Select the minimum run-scoped repository/ref/path context for role tools."""

    return _json_copy(
        {
            "repositories": payload.get("repositories") or [],
            "strategy": payload.get("strategy") or {},
            "input_paths": payload.get("input_paths") or {},
            "input_datasets": payload.get("input_datasets") or [],
        }
    )


def _failure_audit(
    *,
    phase: str,
    role: str,
    failure_type: str,
    input_hash: str | None = None,
    call_id: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "phase": phase,
        "role": role,
        "status": "failed",
        "failure_type": failure_type,
    }
    if input_hash is not None:
        record["input_sha256"] = input_hash
    if call_id is not None:
        record["call_id"] = call_id
    return record


def _raise_terminal(
    exc: BaseException,
    *,
    phase: str,
    audit: list[dict[str, Any]],
    ledger: RoleBudgetLedger,
    guard: TopologyGuard,
) -> None:
    raise ManagerStarRunError(
        failure_phase=phase,
        failure_type=type(exc).__name__,
        audit=audit,
        budget=ledger.snapshot(),
        topology=guard.snapshot(),
    ) from exc


def _invoke_role(
    *,
    role: str,
    phase: str,
    context: Mapping[str, Any],
    adapters: ManagerStarRoleAdapters,
    ledger: RoleBudgetLedger,
    guard: TopologyGuard,
    audit: list[dict[str, Any]],
    retrieval_delivery: Mapping[str, Any] | None,
) -> Any:
    """Run one topology stage through a persistent, per-call role adapter."""

    context_canonical = canonical_json(context)
    input_hash = sha256_text(context_canonical)

    # Validate the next edge before reserving capacity, without advancing it.
    if guard.expected_role != role:
        error = RuntimeError("fixed topology expected a different role")
        audit.append(
            _failure_audit(
                phase=phase,
                role=role,
                failure_type=type(error).__name__,
                input_hash=input_hash,
            )
        )
        _raise_terminal(
            error,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )

    guard.advance(role)
    retrieval_delivery = _role_retrieval_delivery_record(
        context,
        delivery=retrieval_delivery,
        role=role,
        phase=phase,
    )
    provider_record_count = len(adapters.provider_records())
    try:
        output = adapters.execute(
            role=role,
            phase=phase,
            context=_json_copy(context),
        )
    except Exception as exc:
        new_records = adapters.provider_records()[provider_record_count:]
        if retrieval_delivery is not None and new_records:
            audit.append(retrieval_delivery)
        audit.extend(
            {"record_type": "provider_call", **record} for record in new_records
        )
        audit.append(
            _failure_audit(
                phase=phase,
                role=role,
                failure_type=type(exc).__name__,
                input_hash=input_hash,
            )
        )
        _raise_terminal(
            exc,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )

    new_records = adapters.provider_records()[provider_record_count:]
    if not new_records:
        error = ProviderBoundaryBypassError(
            "role runner returned without an accounted provider call"
        )
        audit.append(
            _failure_audit(
                phase=phase,
                role=role,
                failure_type=type(error).__name__,
                input_hash=input_hash,
            )
        )
        _raise_terminal(
            error,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )

    if retrieval_delivery is not None:
        audit.append(retrieval_delivery)

    try:
        output_canonical = canonical_json(output)
        output_hash = sha256_text(output_canonical)
    except (TypeError, ValueError) as exc:
        audit.extend(
            {"record_type": "provider_call", **record} for record in new_records
        )
        audit.append(
            _failure_audit(
                phase=phase,
                role=role,
                failure_type=type(exc).__name__,
                input_hash=input_hash,
            )
        )
        _raise_terminal(
            exc,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )

    audit.extend({"record_type": "provider_call", **record} for record in new_records)
    audit.append(
        {
            "phase": phase,
            "role": role,
            "status": "succeeded",
            "provider_call_ids": [record["call_id"] for record in new_records],
            "provider_call_count": len(new_records),
            "input_sha256": input_hash,
            "output_sha256": output_hash,
            "input_char_count": len(context_canonical),
            "output_char_count": len(output_canonical),
        }
    )
    return json.loads(output_canonical)


def _validate_router_handoff(
    value: Any,
    *,
    expected_schema: str,
    producer_role: str,
    phase: str,
    audit: list[dict[str, Any]],
    ledger: RoleBudgetLedger,
    guard: TopologyGuard,
) -> ValidatedHandoff:
    try:
        handoff = validate_handoff(value, expected_schema, producer_role)
    except HandoffValidationError as exc:
        audit.append(
            {
                "phase": phase,
                "role": producer_role,
                "status": "failed",
                "failure_type": type(exc).__name__,
                "handoff_version": expected_schema,
            }
        )
        _raise_terminal(
            exc,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )
    audit.append(
        {
            "phase": phase,
            "role": producer_role,
            "status": "validated",
            "handoff": handoff.audit_record(),
        }
    )
    return handoff


def _validate_acceptance_mapping(
    handoff_value: Mapping[str, Any],
    *,
    role: str,
    phase: str,
    audit: list[dict[str, Any]],
    ledger: RoleBudgetLedger,
    guard: TopologyGuard,
    expected: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Require unique stable IDs and, for final manager, exact plan coverage."""

    items = handoff_value.get("acceptance_mapping") or []
    requirements = {
        item["requirement_id"]: item["requirement"]
        for item in items
        if isinstance(item, Mapping)
    }
    passed_count = sum(
        item.get("status") == "passed"
        for item in items
        if isinstance(item, Mapping)
    )
    valid = len(requirements) == len(items)
    if expected is not None and requirements != dict(expected):
        valid = False
    mapping_hash = sha256_text(canonical_json(requirements))
    if not valid:
        error = AcceptanceMappingError(
            "acceptance mapping IDs must be unique and exactly cover the approved plan"
        )
        audit.append(
            {
                "phase": phase,
                "role": role,
                "status": "failed",
                "failure_type": type(error).__name__,
                "acceptance_count": len(items),
                "passed_count": passed_count,
                "acceptance_map_sha256": mapping_hash,
            }
        )
        _raise_terminal(
            error,
            phase=phase,
            audit=audit,
            ledger=ledger,
            guard=guard,
        )
    audit.append(
        {
            "phase": phase,
            "role": role,
            "status": "validated",
            "acceptance_count": len(items),
            "passed_count": passed_count,
            "acceptance_map_sha256": mapping_hash,
        }
    )
    return requirements


def run_manager_star(
    payload: Mapping[str, Any],
    executor: RoleExecutor | None,
    *,
    ledger: RoleBudgetLedger | None = None,
    preflight: NegativeRefPreflight,
    role_runner: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
    adapter_factory: RoleAdapterFactory | None = None,
    pre_orchestration: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Execute one and only one manager-star pass with manager-mediated context."""

    validate_manager_star_request(payload)
    active_ledger = ledger or RoleBudgetLedger()
    guard = TopologyGuard()
    audit: list[dict[str, Any]] = []
    task, retrieval_delivery = _task_context(payload)
    repository = _repository_context(payload)

    try:
        (pre_orchestration or preflight.pre_orchestration)()
    except Exception as exc:
        audit.append(
            _failure_audit(
                phase="isolation_pre_orchestration",
                role=MANAGER,
                failure_type=type(exc).__name__,
            )
        )
        _raise_terminal(
            exc,
            phase="isolation_pre_orchestration",
            audit=audit,
            ledger=active_ledger,
            guard=guard,
        )

    try:
        if adapter_factory is not None:
            if executor is not None or role_runner is not None or clock is not None:
                raise TypeError(
                    "production adapter_factory cannot be combined with fake executor seams"
                )
            adapters = adapter_factory(
                payload=payload,
                ledger=active_ledger,
                preflight=preflight,
            )
        else:
            if executor is None:
                raise ManagerStarAdapterUnavailable(
                    "manager_star provider executor is not configured"
                )

            def provider(*, role: str, phase: str, request: Any) -> ProviderCallResult:
                return executor(role=role, phase=phase, context=request)

            adapter_kwargs: dict[str, Any] = {
                "provider": provider,
                "ledger": active_ledger,
                "preflight": preflight,
            }
            if role_runner is not None:
                adapter_kwargs["runner"] = role_runner
            if clock is not None:
                adapter_kwargs["clock"] = clock
            adapters = build_role_adapters(payload, **adapter_kwargs)
        if adapters.ledger is not active_ledger or adapters.preflight is not preflight:
            raise TypeError(
                "role adapters must share the router ledger and preflight objects"
            )
    except Exception as exc:
        audit.append(
            _failure_audit(
                phase="role_adapter_initialization",
                role=MANAGER,
                failure_type=type(exc).__name__,
            )
        )
        _raise_terminal(
            exc,
            phase="role_adapter_initialization",
            audit=audit,
            ledger=active_ledger,
            guard=guard,
        )

    manager_architect_instruction = _invoke_role(
        role=MANAGER,
        phase=PHASE_MANAGER_TO_ARCHITECT,
        context={
            "task": task,
            "repository_context": repository,
            "objective": "request_one_architect_plan",
        },
        adapters=adapters,
        ledger=active_ledger,
        guard=guard,
        audit=audit,
        retrieval_delivery=retrieval_delivery,
    )

    architect_output = _invoke_role(
        role=ARCHITECT,
        phase=PHASE_ARCHITECT_TO_MANAGER,
        context={
            "task": task,
            "repository_context": repository,
            "manager_instruction": manager_architect_instruction,
        },
        adapters=adapters,
        ledger=active_ledger,
        guard=guard,
        audit=audit,
        retrieval_delivery=retrieval_delivery,
    )
    architect_plan = _validate_router_handoff(
        architect_output,
        expected_schema="architect_plan_v1",
        producer_role=ARCHITECT,
        phase="architect_plan_validation",
        audit=audit,
        ledger=active_ledger,
        guard=guard,
    )
    approved_requirements = _validate_acceptance_mapping(
        architect_plan.value,
        role=ARCHITECT,
        phase="architect_acceptance_mapping_validation",
        audit=audit,
        ledger=active_ledger,
        guard=guard,
    )

    manager_developer_instruction = _invoke_role(
        role=MANAGER,
        phase=PHASE_MANAGER_TO_DEVELOPER,
        context={
            "task": task,
            "repository_context": repository,
            "architect_plan": architect_plan.value,
            "objective": "approve_plan_and_delegate_once",
        },
        adapters=adapters,
        ledger=active_ledger,
        guard=guard,
        audit=audit,
        retrieval_delivery=retrieval_delivery,
    )

    developer_output = _invoke_role(
        role=DEVELOPER,
        phase=PHASE_DEVELOPER_TO_MANAGER,
        context={
            "task": task,
            "repository_context": repository,
            "manager_handoff": {
                "approved_plan": architect_plan.value,
                "instruction": manager_developer_instruction,
            },
        },
        adapters=adapters,
        ledger=active_ledger,
        guard=guard,
        audit=audit,
        retrieval_delivery=retrieval_delivery,
    )
    developer_result = _validate_router_handoff(
        developer_output,
        expected_schema="developer_result_v1",
        producer_role=DEVELOPER,
        phase="developer_result_validation",
        audit=audit,
        ledger=active_ledger,
        guard=guard,
    )

    manager_output = _invoke_role(
        role=MANAGER,
        phase=PHASE_MANAGER_FINAL,
        context={
            "task": task,
            "approved_plan": architect_plan.value,
            "developer_result": developer_result.value,
            "objective": "validate_completeness_and_finalize",
        },
        adapters=adapters,
        ledger=active_ledger,
        guard=guard,
        audit=audit,
        retrieval_delivery=retrieval_delivery,
    )
    manager_final = _validate_router_handoff(
        manager_output,
        expected_schema="manager_final_v1",
        producer_role=MANAGER,
        phase="manager_final_validation",
        audit=audit,
        ledger=active_ledger,
        guard=guard,
    )
    _validate_acceptance_mapping(
        manager_final.value,
        role=MANAGER,
        phase="manager_acceptance_mapping_validation",
        audit=audit,
        ledger=active_ledger,
        guard=guard,
        expected=approved_requirements,
    )
    guard.finalize(MANAGER)

    try:
        commit_counter = getattr(adapters, "commit_count", None)
        commit_count = commit_counter() if callable(commit_counter) else 0
        if type(commit_count) is not int or commit_count < 0:
            raise ValueError("commit_count must be a non-negative integer")
        committed_paths_reader = getattr(adapters, "committed_paths", None)
        committed_paths = (
            committed_paths_reader() if callable(committed_paths_reader) else []
        )
        if (
            not isinstance(committed_paths, list)
            or any(type(path) is not str or not path for path in committed_paths)
            or committed_paths != sorted(set(committed_paths))
        ):
            raise ValueError("committed_paths must be a sorted unique string list")
        if commit_count == 0 and committed_paths:
            raise ValueError("committed_paths must be empty when commit_count is zero")
        calculator_reader = getattr(adapters, "calculation_tool_records", None)
        calculation_tool_calls = (
            calculator_reader() if callable(calculator_reader) else []
        )
        if not isinstance(calculation_tool_calls, list):
            raise ValueError("calculation tool records must be a list")
    except Exception as exc:
        audit.append(
            _failure_audit(
                phase="mcp_audit_validation",
                role=DEVELOPER,
                failure_type=type(exc).__name__,
            )
        )
        _raise_terminal(
            exc,
            phase="mcp_audit_validation",
            audit=audit,
            ledger=active_ledger,
            guard=guard,
        )

    final_value = manager_final.value
    return {
        "architecture_mode": MANAGER_STAR,
        "rag_enabled": explicit_rag_enabled(payload),
        "status": (
            "succeeded" if final_value["decision"] == "accepted" else "failed"
        ),
        "failure_phase": final_value.get("failure_phase"),
        "manager_final": final_value,
        "developer_result": developer_result.value,
        "audit": copy.deepcopy(audit),
        "provider_calls": adapters.provider_records(),
        "calculation_tool_calls": copy.deepcopy(calculation_tool_calls),
        "commit_count": commit_count,
        "committed_paths": copy.deepcopy(committed_paths),
        "isolation_preflight": preflight.evidence(),
        "manager_acceptance_map": {
            "approved_count": len(approved_requirements),
            "evaluated_count": len(final_value.get("acceptance_mapping") or []),
            "passed_count": sum(
                item.get("status") == "passed"
                for item in final_value.get("acceptance_mapping") or []
            ),
            "complete": True,
            "sha256": sha256_text(canonical_json(approved_requirements)),
        },
        "budget": active_ledger.snapshot(),
        "topology": guard.snapshot(),
    }


def run_manager_star_runtime(
    payload: Mapping[str, Any],
    *,
    tracer: Any = None,
    executor: RoleExecutor | None = None,
    preflight: NegativeRefPreflight | None = None,
    role_runner: Callable[..., Any] | None = None,
    ledger: RoleBudgetLedger | None = None,
    production_boundary: Any = None,
    production_role_runner: Callable[..., Any] | None = None,
    production_audit_directory: Any = None,
) -> dict[str, Any]:
    """Run the fake seam or the dependency-backed production M1 adapter."""

    del tracer  # Reserved for the later sanitized runtime telemetry adapter.
    validate_manager_star_request(payload)
    controls = payload.get("iteration_controls") or {}
    if controls.get("max_iterations") == 2:
        from exp3.attempt_control import attempt_number

        if attempt_number(payload) is None:
            raise ArchitectureModeError(
                "two-attempt manager_star requires the observation attempt controller"
            )
    require_manager_star_enabled()
    if (executor is None) != (preflight is None):
        raise ManagerStarAdapterUnavailable(
            "manager_star fake executor and preflight must be supplied together"
        )
    if executor is not None and preflight is not None:
        if (
            production_boundary is not None
            or production_role_runner is not None
            or production_audit_directory is not None
        ):
            raise ManagerStarAdapterUnavailable(
                "fake and production manager_star adapters cannot be combined"
            )
        try:
            return run_manager_star(
                payload,
                executor,
                ledger=ledger,
                preflight=preflight,
                role_runner=role_runner,
            )
        except ManagerStarRunError as error:
            error.rag_enabled = explicit_rag_enabled(payload)
            raise

    from exp3.agents_runtime import build_production_role_adapters
    from exp3.production_provider import boundary_from_environment

    boundary = production_boundary or boundary_from_environment(payload)
    if ledger is not None and ledger is not boundary.ledger:
        raise ManagerStarAdapterUnavailable(
            "production manager_star must use the boundary-owned ledger"
        )

    production_adapters = None

    def production_adapter_factory(*, payload, ledger, preflight):
        nonlocal production_adapters
        if ledger is not boundary.ledger or preflight is not boundary.preflight:
            raise TypeError("production adapter identity drifted from boundary")
        production_adapters = build_production_role_adapters(
            payload,
            boundary=boundary,
            runner=production_role_runner,
            audit_directory=production_audit_directory,
        )
        return production_adapters

    try:
        return run_manager_star(
            payload,
            None,
            ledger=boundary.ledger,
            preflight=boundary.preflight,
            adapter_factory=production_adapter_factory,
            pre_orchestration=lambda: boundary.start(payload),
        )
    except ManagerStarRunError as error:
        # A final manager/schema failure can occur after a real developer
        # commit. Preserve that already-observed content-free evidence rather
        # than emitting the default zero/null failure telemetry. Never replace
        # the original terminal error if audit enrichment itself fails.
        error.rag_enabled = boundary.rag_enabled
        error.isolation_preflight = boundary.preflight.evidence()
        if production_adapters is not None:
            try:
                error.commit_count = production_adapters.commit_count()
            except Exception:
                pass
            try:
                error.committed_paths = production_adapters.committed_paths()
            except Exception:
                pass
            try:
                error.calculation_tool_calls = (
                    production_adapters.calculation_tool_records()
                )
            except Exception:
                pass

        approved = next(
            (
                item
                for item in error.audit
                if item.get("phase") == "architect_acceptance_mapping_validation"
                and item.get("status") == "validated"
            ),
            None,
        )
        evaluated = next(
            (
                item
                for item in error.audit
                if item.get("phase") == "manager_acceptance_mapping_validation"
            ),
            None,
        )
        if approved is not None and evaluated is not None:
            error.manager_acceptance_map = {
                "approved_count": approved.get("acceptance_count", 0),
                "evaluated_count": evaluated.get("acceptance_count", 0),
                "passed_count": evaluated.get("passed_count", 0),
                "complete": evaluated.get("status") == "validated",
                "sha256": approved.get("acceptance_map_sha256"),
            }
        raise
    finally:
        if production_adapters is not None:
            # When another terminal error is already being propagated, a cleanup
            # error must not hide it. With no earlier error, cleanup failure is
            # allowed to fail the run instead of falsely reporting clean closure.
            already_failing = sys.exc_info()[0] is not None
            try:
                production_adapters.close()
            except Exception:
                if not already_failing:
                    raise
