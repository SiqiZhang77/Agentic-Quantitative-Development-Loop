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
from typing import Any, Callable, Mapping

from exp3.architecture import (
    MANAGER_STAR,
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
        self.isolation_preflight = None
        self.manager_acceptance_map = None
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


def _task_context(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Select task-only fields; omit output paths, retrieval text, and controls."""

    return _json_copy(
        {
            "issue_key": payload.get("issue_key"),
            "command": payload.get("command"),
            "execution_objectives": payload.get("execution_objectives") or {},
            "jira_context": payload.get("jira_context") or {},
        }
    )


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
    provider_record_count = len(adapters.provider_records())
    try:
        output = adapters.execute(
            role=role,
            phase=phase,
            context=_json_copy(context),
        )
    except Exception as exc:
        new_records = adapters.provider_records()[provider_record_count:]
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
    task = _task_context(payload)
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
        "status": (
            "succeeded" if final_value["decision"] == "accepted" else "failed"
        ),
        "failure_phase": final_value.get("failure_phase"),
        "manager_final": final_value,
        "developer_result": developer_result.value,
        "audit": copy.deepcopy(audit),
        "provider_calls": adapters.provider_records(),
        "commit_count": commit_count,
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
        return run_manager_star(
            payload,
            executor,
            ledger=ledger,
            preflight=preflight,
            role_runner=role_runner,
        )

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
        error.isolation_preflight = boundary.preflight.evidence()
        if production_adapters is not None:
            try:
                error.commit_count = production_adapters.commit_count()
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
