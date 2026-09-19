"""Frozen architecture-mode contract for Experiment 3.

Missing metadata is the compatibility path: it resolves to the current
single-agent implementation without mutating the request.  Manager-star is an
explicit, fail-closed opt-in and has a second local kill switch while its real
role adapters remain intentionally unwired.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any


SINGLE_AGENT = "single_agent"
MANAGER_STAR = "manager_star"
SUPPORTED_ARCHITECTURE_MODES = frozenset({SINGLE_AGENT, MANAGER_STAR})
MANAGER_STAR_ENABLE_ENV = "RAE_ENABLE_MANAGER_STAR"
FORMAL_EXECUTION_CONTRACT_KEY = "formal_execution_contract"
EXP2_SINGLE_AGENT_RAG_CONTRACT = "exp2_single_agent_rag_v1"
EXP2_SINGLE_AGENT_RAG_CONTRACT_V2 = "exp2_single_agent_rag_v2"
EXP2_SINGLE_AGENT_RAG_CONTRACTS = frozenset(
    {
        EXP2_SINGLE_AGENT_RAG_CONTRACT,
        EXP2_SINGLE_AGENT_RAG_CONTRACT_V2,
    }
)
FACTORIAL_RAG_ARCHITECTURE_CONTRACT = "factorial_rag_architecture_v1"
FACTORIAL_RAG_DELIVERY_POLICY = "all_model_stages_v1"


class ArchitectureModeError(ValueError):
    """The request cannot be routed without contaminating the architecture."""


def resolve_architecture_mode(payload: Mapping[str, Any]) -> str:
    """Return the requested mode, defaulting to M0 without changing ``payload``."""

    raw = payload.get("architecture_mode", SINGLE_AGENT)
    if not isinstance(raw, str) or raw not in SUPPORTED_ARCHITECTURE_MODES:
        raise ArchitectureModeError(
            "architecture_mode must be one of: "
            + ", ".join(sorted(SUPPORTED_ARCHITECTURE_MODES))
        )
    return raw


def manager_star_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Whether the local manager-star execution adapter is deliberately enabled."""

    source = os.environ if environment is None else environment
    return str(source.get(MANAGER_STAR_ENABLE_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def require_manager_star_enabled(
    environment: Mapping[str, str] | None = None,
) -> None:
    """Fail closed instead of silently falling back to the M0 treatment."""

    if not manager_star_enabled(environment):
        raise ArchitectureModeError(
            "manager_star is disabled; set RAE_ENABLE_MANAGER_STAR=true only for "
            "the isolated E3 runtime after its role adapter is configured"
        )


def validate_explicit_e3_request(payload: Mapping[str, Any]) -> str:
    """Validate an explicit formal architecture request.

    Omitted architecture metadata remains the untouched legacy compatibility
    path. E3 names either arm explicitly and freezes RAG off. The separately
    named E2 contract may use RAG only with the single-agent architecture.
    """

    if "architecture_mode" not in payload:
        raise ArchitectureModeError(
            "an E3 request must explicitly declare architecture_mode"
        )
    mode = resolve_architecture_mode(payload)
    objectives = payload.get("execution_objectives") or {}
    if not isinstance(objectives, Mapping):
        raise ArchitectureModeError(
            "explicit E3 requests require structured execution parameters"
        )
    parameters = objectives.get("parsed_task_parameters") or {}
    if not isinstance(parameters, Mapping):
        raise ArchitectureModeError(
            "explicit E3 requests require structured execution parameters"
        )
    contract = parameters.get(FORMAL_EXECUTION_CONTRACT_KEY)
    if contract in EXP2_SINGLE_AGENT_RAG_CONTRACTS:
        if mode != SINGLE_AGENT:
            raise ArchitectureModeError(
                "the E2 RAG contract requires single_agent architecture"
            )
        rag_enabled = parameters.get("rag_enabled")
        if type(rag_enabled) is not bool:
            raise ArchitectureModeError(
                "the E2 RAG contract requires an explicit boolean rag_enabled"
            )
        rag_top_k = parameters.get("rag_top_k")
        if (
            isinstance(rag_top_k, bool)
            or not isinstance(rag_top_k, int)
            or not 1 <= rag_top_k <= 10
        ):
            raise ArchitectureModeError(
                "the E2 RAG contract requires rag_top_k between 1 and 10"
            )
        retrieval_context = payload.get("retrieval_context")
        if rag_enabled and not isinstance(retrieval_context, Mapping):
            raise ArchitectureModeError(
                "the E2 C1 contract requires a structured retrieval_context"
            )
        if not rag_enabled and retrieval_context is not None:
            raise ArchitectureModeError(
                "the E2 C0 contract forbids retrieval_context"
            )
        return mode
    if contract == FACTORIAL_RAG_ARCHITECTURE_CONTRACT:
        rag_enabled = parameters.get("rag_enabled")
        if type(rag_enabled) is not bool:
            raise ArchitectureModeError(
                "the factorial contract requires an explicit boolean rag_enabled"
            )
        rag_top_k = parameters.get("rag_top_k")
        if (
            isinstance(rag_top_k, bool)
            or not isinstance(rag_top_k, int)
            or not 1 <= rag_top_k <= 10
        ):
            raise ArchitectureModeError(
                "the factorial contract requires rag_top_k between 1 and 10"
            )
        if parameters.get("rag_delivery_policy") != FACTORIAL_RAG_DELIVERY_POLICY:
            raise ArchitectureModeError(
                "the factorial contract requires all_model_stages_v1 RAG delivery"
            )
        retrieval_context = payload.get("retrieval_context")
        if rag_enabled and not isinstance(retrieval_context, Mapping):
            raise ArchitectureModeError(
                "the factorial R1 contract requires a structured retrieval_context"
            )
        if not rag_enabled and retrieval_context is not None:
            raise ArchitectureModeError(
                "the factorial R0 contract forbids retrieval_context"
            )
        if "quant_calculator_enabled" in parameters and type(
            parameters["quant_calculator_enabled"]
        ) is not bool:
            raise ArchitectureModeError(
                "quant_calculator_enabled must be an explicit boolean"
            )
        return mode
    if contract is not None:
        raise ArchitectureModeError("unsupported formal execution contract")
    if parameters.get("rag_enabled") is not False:
        raise ArchitectureModeError(
            "explicit E3 requests must set RAG rag_enabled=false"
        )
    if "rag_top_k" in parameters or payload.get("retrieval_context") is not None:
        raise ArchitectureModeError(
            "explicit E3 requests require RAG and retrieval_context to be disabled"
        )
    if "quant_calculator_enabled" in parameters and type(
        parameters["quant_calculator_enabled"]
    ) is not bool:
        raise ArchitectureModeError(
            "quant_calculator_enabled must be an explicit boolean"
        )
    return mode


def explicit_rag_enabled(payload: Mapping[str, Any]) -> bool:
    """Return the validated RAG state for an explicit formal request."""

    validate_explicit_e3_request(payload)
    parameters = (
        (payload.get("execution_objectives") or {}).get("parsed_task_parameters")
        or {}
    )
    return parameters.get("rag_enabled") is True


def validate_manager_star_request(payload: Mapping[str, Any]) -> None:
    """Enforce treatment invariants before any role/model/tool invocation.

    The router executes one fixed topology *attempt*.  A caller may use either the
    historical single-attempt contract or the prospective two-attempt observation
    controller; contradictory mixtures are rejected.  Legacy single-agent RAG
    behaviour is deliberately untouched.
    """

    if resolve_architecture_mode(payload) != MANAGER_STAR:
        raise ArchitectureModeError("manager-star validation requires manager_star mode")
    validate_explicit_e3_request(payload)

    controls = payload.get("iteration_controls") or {}
    contract = (
        controls.get("allow_iteration"),
        controls.get("max_iterations"),
        controls.get("max_failed_iterations"),
    )
    if contract not in {(False, 1, 1), (True, 2, 2)}:
        raise ArchitectureModeError(
            "manager_star iteration controls require coherent allow_iteration, "
            "max_iterations, and max_failed_iterations values"
        )
