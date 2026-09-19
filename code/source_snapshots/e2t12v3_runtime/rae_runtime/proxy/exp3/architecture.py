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
    """Validate invariants shared by explicit E3 M0 and M1 requests.

    Omitted architecture metadata remains the untouched legacy compatibility
    path.  The E3 harness must name either arm explicitly; once it does, RAG is
    frozen off for both arms so architecture is the only treatment difference.
    """

    if "architecture_mode" not in payload:
        raise ArchitectureModeError(
            "an E3 request must explicitly declare architecture_mode"
        )
    mode = resolve_architecture_mode(payload)
    objectives = payload.get("execution_objectives") or {}
    parameters = objectives.get("parsed_task_parameters") or {}
    if parameters.get("rag_enabled") is not False:
        raise ArchitectureModeError(
            "explicit E3 requests must set RAG rag_enabled=false"
        )
    if "rag_top_k" in parameters or payload.get("retrieval_context") is not None:
        raise ArchitectureModeError(
            "explicit E3 requests require RAG and retrieval_context to be disabled"
        )
    return mode


def validate_manager_star_request(payload: Mapping[str, Any]) -> None:
    """Enforce treatment invariants before any role/model/tool invocation.

    The fixed router never loops, but contradictory loop/retry controls are also
    rejected so the recorded request cannot claim a treatment the service ignored.
    Legacy single-agent RAG behaviour is deliberately untouched.
    """

    if resolve_architecture_mode(payload) != MANAGER_STAR:
        raise ArchitectureModeError("manager-star validation requires manager_star mode")
    validate_explicit_e3_request(payload)

    controls = payload.get("iteration_controls") or {}
    if controls.get("allow_iteration") not in {None, False}:
        raise ArchitectureModeError("manager_star forbids iteration")
    if controls.get("max_iterations") not in {None, 1}:
        raise ArchitectureModeError("manager_star requires max_iterations=1")
    if controls.get("max_failed_iterations") not in {None, 1}:
        raise ArchitectureModeError("manager_star requires max_failed_iterations=1")
