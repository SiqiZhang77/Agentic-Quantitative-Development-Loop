"""Sanitized, versioned Experiment 3 runtime telemetry construction."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Final

from exp3.policy import ARCHITECT, DEVELOPER, MANAGER


TELEMETRY_VERSION: Final = "exp3-runtime-telemetry-v2"
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")


class TelemetryError(ValueError):
    """E3 telemetry inputs are incomplete, inconsistent or unsafe."""


def _provider_identity(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TelemetryError("E3 telemetry requires provider identity")
    expected_keys = {
        "schema_version",
        "provider_mode",
        "base_url",
        "model_alias",
        "transport_model",
        "adapter",
        "sha256",
    }
    if set(value) != expected_keys:
        raise TelemetryError("provider identity fields are incomplete")
    if value.get("schema_version") != "llm-provider-identity-v1":
        raise TelemetryError("provider identity schema version is invalid")
    if value.get("provider_mode") not in {"company_litellm", "openai"}:
        raise TelemetryError("provider identity mode is invalid")
    for name in ("base_url", "model_alias", "transport_model", "adapter"):
        if type(value.get(name)) is not str or not value[name]:
            raise TelemetryError(f"provider identity {name} is invalid")
    safe = {name: value[name] for name in expected_keys if name != "sha256"}
    canonical = json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if _safe_hash(value.get("sha256")) != expected_hash:
        raise TelemetryError("provider identity hash is invalid")
    if value["provider_mode"] == "openai" and value["transport_model"].startswith(
        "litellm_proxy/"
    ):
        raise TelemetryError("OpenAI telemetry contains a LiteLLM proxy prefix")
    return copy.deepcopy(dict(value))


def _safe_hash(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise TelemetryError("telemetry hash must be a lowercase SHA-256")
    return value


def _safe_phase(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _PHASE_RE.fullmatch(value) is None:
        raise TelemetryError("failure phase must be a bounded content-free code")
    return value


def _safe_nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise TelemetryError(f"{name} must be a non-negative integer")
    return value


def _safe_latency(value: object) -> float:
    if type(value) not in (int, float):
        raise TelemetryError("latency_seconds must be finite and non-negative")
    latency = float(value)
    if not math.isfinite(latency) or latency < 0:
        raise TelemetryError("latency_seconds must be finite and non-negative")
    return round(latency, 6)


def _budget_record(value: Mapping[str, Any]) -> dict[str, Any]:
    calls = _safe_nonnegative_int("calls", value.get("calls"))
    prompt = _safe_nonnegative_int("prompt_tokens", value.get("prompt_tokens"))
    completion = _safe_nonnegative_int(
        "completion_tokens", value.get("completion_tokens")
    )
    total = _safe_nonnegative_int("total_tokens", value.get("total_tokens"))
    failures = _safe_nonnegative_int("failures", value.get("failures"))
    max_calls = _safe_nonnegative_int("max_calls", value.get("max_calls"))
    max_tokens = _safe_nonnegative_int("max_tokens", value.get("max_tokens"))
    if total != prompt + completion:
        raise TelemetryError("budget total_tokens must equal prompt plus completion")
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "failures": failures,
        "latency_seconds": _safe_latency(value.get("latency_seconds")),
        "max_calls": max_calls,
        "max_tokens": max_tokens,
        "call_exhausted": max_calls > 0 and calls >= max_calls,
        "token_exhausted": max_tokens > 0 and total >= max_tokens,
        "over_budget": bool(value.get("over_budget")),
    }


def _provider_record(value: Mapping[str, Any]) -> dict[str, Any]:
    call_id = value.get("call_id")
    role = value.get("role")
    status = value.get("status")
    failure_type = value.get("failure_type")
    if type(call_id) is not str or not re.fullmatch(r"exp3-[0-9]{6}", call_id):
        raise TelemetryError("provider call_id is invalid")
    if role not in {MANAGER, ARCHITECT, DEVELOPER}:
        raise TelemetryError("provider role is invalid")
    if status not in {"succeeded", "failed"}:
        raise TelemetryError("provider status is invalid")
    if failure_type is not None and (
        type(failure_type) is not str
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", failure_type) is None
    ):
        raise TelemetryError("provider failure_type is not content-free")
    if status == "succeeded" and failure_type is not None:
        raise TelemetryError("successful provider call cannot carry failure_type")
    prompt = _safe_nonnegative_int("prompt_tokens", value.get("prompt_tokens"))
    completion = _safe_nonnegative_int(
        "completion_tokens", value.get("completion_tokens")
    )
    total = _safe_nonnegative_int("total_tokens", value.get("total_tokens"))
    if total != prompt + completion:
        raise TelemetryError("provider total_tokens must equal prompt plus completion")
    return {
        "call_id": call_id,
        "role": role,
        "phase": _safe_phase(value.get("phase")),
        "status": status,
        "failure_type": failure_type,
        "input_sha256": _safe_hash(value.get("input_sha256")),
        "output_sha256": _safe_hash(value.get("output_sha256"), nullable=True),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "latency_seconds": _safe_latency(value.get("latency_seconds")),
    }


def _handoff_records(audit: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for item in audit:
        handoff = item.get("handoff")
        if not isinstance(handoff, Mapping):
            continue
        version = handoff.get("version")
        producer = handoff.get("producer")
        char_count = handoff.get("char_count")
        if version not in {
            "architect_plan_v1",
            "developer_result_v1",
            "manager_final_v1",
        }:
            raise TelemetryError("handoff version is invalid")
        if producer not in {MANAGER, ARCHITECT, DEVELOPER}:
            raise TelemetryError("handoff producer is invalid")
        records.append(
            {
                "version": version,
                "producer": producer,
                "sha256": _safe_hash(handoff.get("hash")),
                "char_count": _safe_nonnegative_int("char_count", char_count),
            }
        )
    return records


def _isolation_summary(records: object) -> dict[str, Any] | None:
    if not isinstance(records, list) or not records:
        return None
    safe_records = [item for item in records if isinstance(item, Mapping)]
    if len(safe_records) != len(records):
        raise TelemetryError("isolation evidence contains an invalid record")
    first = safe_records[0]
    manifest_hash = _safe_hash(first.get("manifest_sha256"))
    deny_hash = _safe_hash(first.get("deny_ref_set_sha256"))
    for item in safe_records:
        if item.get("passed") is not True:
            raise TelemetryError("only passing isolation evidence may be emitted")
        if _safe_hash(item.get("manifest_sha256")) != manifest_hash:
            raise TelemetryError("isolation manifest hash changed during a run")
        if _safe_hash(item.get("deny_ref_set_sha256")) != deny_hash:
            raise TelemetryError("isolation deny-set hash changed during a run")
    return {
        "schema_version": "exp3-negative-ref-preflight-evidence-v1",
        "passed": True,
        "manifest_sha256": manifest_hash,
        "deny_ref_set_sha256": deny_hash,
        "pre_orchestration_checks": sum(
            item.get("phase") == "pre_orchestration" for item in safe_records
        ),
        "pre_model_call_checks": sum(
            item.get("phase") == "pre_model_call" for item in safe_records
        ),
        "repository_count": max(
            _safe_nonnegative_int("repository_count", item.get("repository_count"))
            for item in safe_records
        ),
        "denied_ref_count": max(
            _safe_nonnegative_int("denied_ref_count", item.get("denied_ref_count"))
            for item in safe_records
        ),
    }


def _provider_records_from_failure_audit(
    audit: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in audit
        if item.get("record_type") == "provider_call"
    ]


def build_manager_star_telemetry(
    outcome: Any,
    *,
    model_alias: str | None = None,
    identity_sha256: str | None = None,
    provider_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a schema-ready telemetry block from a success result or run error."""

    if isinstance(outcome, Mapping):
        status = outcome.get("status")
        failure_phase = outcome.get("failure_phase")
        budget = outcome.get("budget")
        audit = outcome.get("audit") or []
        provider_values = outcome.get("provider_calls") or []
        commit_count = outcome.get("commit_count", 0)
        isolation = outcome.get("isolation_preflight")
        manager_acceptance = outcome.get("manager_acceptance_map")
        topology = outcome.get("topology")
        outcome_provider_identity = outcome.get("provider_identity")
    else:
        status = "failed"
        failure_phase = getattr(outcome, "failure_phase", None)
        budget = getattr(outcome, "budget", None)
        audit = getattr(outcome, "audit", []) or []
        provider_values = _provider_records_from_failure_audit(audit)
        commit_count = getattr(outcome, "commit_count", 0)
        isolation = getattr(outcome, "isolation_preflight", None)
        manager_acceptance = getattr(outcome, "manager_acceptance_map", None)
        topology = getattr(outcome, "topology", None)
        outcome_provider_identity = getattr(outcome, "provider_identity", None)

    if status not in {"succeeded", "failed"}:
        raise TelemetryError("manager-star status must be succeeded or failed")
    if not isinstance(budget, Mapping) or not isinstance(audit, list):
        raise TelemetryError("manager-star telemetry requires budget and audit")
    shared = _budget_record(budget.get("shared") or {})
    roles_value = budget.get("roles") or {}
    if set(roles_value) != {MANAGER, ARCHITECT, DEVELOPER}:
        raise TelemetryError("manager-star telemetry requires all fixed role budgets")
    roles = {role: _budget_record(roles_value[role]) for role in sorted(roles_value)}
    provider_calls = [_provider_record(item) for item in provider_values]
    if shared["calls"] != len(provider_calls):
        raise TelemetryError("shared call count does not equal provider-call records")
    if shared["total_tokens"] != sum(item["total_tokens"] for item in provider_calls):
        raise TelemetryError("shared token count does not equal provider-call records")
    safe_provider = _provider_identity(
        provider_identity or outcome_provider_identity
    )
    resolved_model_alias = model_alias or safe_provider["model_alias"]
    if resolved_model_alias != safe_provider["model_alias"]:
        raise TelemetryError("telemetry model alias does not match provider identity")

    safe_acceptance = None
    if manager_acceptance is not None:
        if not isinstance(manager_acceptance, Mapping):
            raise TelemetryError("manager acceptance metadata must be an object")
        safe_acceptance = {
            "approved_count": _safe_nonnegative_int(
                "approved_count", manager_acceptance.get("approved_count")
            ),
            "evaluated_count": _safe_nonnegative_int(
                "evaluated_count", manager_acceptance.get("evaluated_count")
            ),
            "passed_count": _safe_nonnegative_int(
                "passed_count", manager_acceptance.get("passed_count")
            ),
            "complete": manager_acceptance.get("complete") is True,
            "sha256": _safe_hash(manager_acceptance.get("sha256")),
        }

    safe_topology = None
    if isinstance(topology, Mapping):
        safe_topology = {
            "cursor": _safe_nonnegative_int("topology cursor", topology.get("cursor")),
            "sequence_complete": topology.get("sequence_complete") is True,
            "finalized": topology.get("finalized") is True,
        }

    result = {
        "schema_version": TELEMETRY_VERSION,
        "architecture_mode": "manager_star",
        "rag_enabled": False,
        "status": status,
        "failure_phase": _safe_phase(failure_phase),
        "model_alias": resolved_model_alias,
        "provider_identity": safe_provider,
        "identity_sha256": (
            _safe_hash(identity_sha256, nullable=True)
            if identity_sha256 is not None
            else None
        ),
        "shared_budget": shared,
        "role_usage": roles,
        "provider_calls": provider_calls,
        "commit_count": _safe_nonnegative_int("commit_count", commit_count),
        "handoff_schema_failures": sum(
            item.get("failure_type") == "HandoffValidationError"
            for item in audit
            if isinstance(item, Mapping)
        ),
        "handoffs": _handoff_records(
            [item for item in audit if isinstance(item, Mapping)]
        ),
        "manager_acceptance_map": safe_acceptance,
        "isolation_preflight": _isolation_summary(isolation),
        "topology": safe_topology,
    }
    return copy.deepcopy(result)


def build_single_agent_telemetry(
    snapshot: Mapping[str, Any],
    *,
    status: str,
    failure_phase: str | None = None,
    model_alias: str | None = None,
    identity_sha256: str | None = None,
    commit_count: int = 0,
    provider_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the same content-free E3 envelope for the explicit M0 arm."""

    if not isinstance(snapshot, Mapping):
        raise TelemetryError("single-agent telemetry requires a boundary snapshot")
    if snapshot.get("architecture_mode") != "single_agent":
        raise TelemetryError("single-agent telemetry requires single_agent mode")
    if snapshot.get("rag_enabled") is not False:
        raise TelemetryError("single-agent E3 telemetry requires RAG disabled")
    if status not in {"succeeded", "failed"}:
        raise TelemetryError("single-agent status must be succeeded or failed")
    budget = snapshot.get("budget")
    if not isinstance(budget, Mapping):
        raise TelemetryError("single-agent telemetry requires budget accounting")
    shared = _budget_record(budget.get("shared") or {})
    roles_value = budget.get("roles") or {}
    if set(roles_value) != {MANAGER, ARCHITECT, DEVELOPER}:
        raise TelemetryError("single-agent telemetry requires fixed role identities")
    roles = {role: _budget_record(roles_value[role]) for role in sorted(roles_value)}
    if roles[MANAGER]["max_calls"] != 0 or roles[ARCHITECT]["max_calls"] != 0:
        raise TelemetryError("single-agent manager/architect capacity must be zero")
    if (
        roles[DEVELOPER]["max_calls"] != shared["max_calls"]
        or roles[DEVELOPER]["max_tokens"] != shared["max_tokens"]
    ):
        raise TelemetryError("single-agent developer must own the shared capacity")
    provider_calls = [
        _provider_record(item) for item in snapshot.get("provider_calls") or []
    ]
    if shared["calls"] != len(provider_calls):
        raise TelemetryError("shared call count does not equal provider-call records")
    if shared["total_tokens"] != sum(item["total_tokens"] for item in provider_calls):
        raise TelemetryError("shared token count does not equal provider-call records")
    safe_provider = _provider_identity(
        provider_identity or snapshot.get("provider_identity")
    )
    resolved_model_alias = model_alias or safe_provider["model_alias"]
    if resolved_model_alias != safe_provider["model_alias"]:
        raise TelemetryError("telemetry model alias does not match provider identity")

    return {
        "schema_version": TELEMETRY_VERSION,
        "architecture_mode": "single_agent",
        "rag_enabled": False,
        "status": status,
        "failure_phase": _safe_phase(failure_phase),
        "model_alias": resolved_model_alias,
        "provider_identity": safe_provider,
        "identity_sha256": (
            _safe_hash(identity_sha256, nullable=True)
            if identity_sha256 is not None
            else None
        ),
        "shared_budget": shared,
        "role_usage": roles,
        "provider_calls": provider_calls,
        "commit_count": _safe_nonnegative_int("commit_count", commit_count),
        "handoff_schema_failures": 0,
        "handoffs": [],
        "manager_acceptance_map": None,
        "isolation_preflight": _isolation_summary(
            snapshot.get("isolation_preflight")
        ),
        "topology": None,
    }
