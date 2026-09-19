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
from quant_calculator import (
    CALCULATION_ERROR_DETAILS,
    MAX_OPERATIONS,
    MAX_RESULT_CELLS,
    SUPPORTED_OPERATIONS,
)


TELEMETRY_VERSION: Final = "exp3-runtime-telemetry-v4"
_MAX_PROVIDER_CALLS_PER_ATTEMPT: Final = 20
_FORMAL_TWO_ATTEMPT_MAX_CALLS: Final = 40
_ROLE_RETRIEVAL_PHASES: Final = {
    "manager_to_architect": MANAGER,
    "architect_to_manager": ARCHITECT,
    "manager_to_developer": MANAGER,
    "developer_to_manager": DEVELOPER,
    "manager_final": MANAGER,
}
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_COMMITTED_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+$")


class TelemetryError(ValueError):
    """E3 telemetry inputs are incomplete, inconsistent or unsafe."""


def _sum_budget_records(
    values: list[Mapping[str, Any]], *, planned_attempts: int
) -> dict[str, Any]:
    if not values:
        raise TelemetryError("attempt accounting requires at least one budget")
    first_configured_max_calls = _safe_nonnegative_int(
        "configured_max_calls", values[0].get("configured_max_calls")
    )
    first_max_tokens = _safe_nonnegative_int("max_tokens", values[0].get("max_tokens"))
    for item in values:
        configured_max_calls = _safe_nonnegative_int(
            "configured_max_calls", item.get("configured_max_calls")
        )
        transferred_calls = _safe_int("transferred_calls", item.get("transferred_calls"))
        effective_max_calls = _safe_nonnegative_int(
            "max_calls", item.get("max_calls")
        )
        if effective_max_calls != configured_max_calls + transferred_calls:
            raise TelemetryError(
                "effective max_calls does not equal configured capacity plus transfer"
            )
        if (
            configured_max_calls != first_configured_max_calls
            or _safe_nonnegative_int("max_tokens", item.get("max_tokens")) != first_max_tokens
        ):
            raise TelemetryError("per-attempt budget identity changed")
    transferred_calls = sum(
        _safe_int("transferred_calls", item.get("transferred_calls"))
        for item in values
    )
    configured_max_calls = first_configured_max_calls * planned_attempts
    return {
        "calls": sum(_safe_nonnegative_int("calls", item.get("calls")) for item in values),
        "prompt_tokens": sum(
            _safe_nonnegative_int("prompt_tokens", item.get("prompt_tokens"))
            for item in values
        ),
        "completion_tokens": sum(
            _safe_nonnegative_int("completion_tokens", item.get("completion_tokens"))
            for item in values
        ),
        "total_tokens": sum(
            _safe_nonnegative_int("total_tokens", item.get("total_tokens"))
            for item in values
        ),
        "failures": sum(
            _safe_nonnegative_int("failures", item.get("failures")) for item in values
        ),
        "latency_seconds": round(
            sum(_safe_latency(item.get("latency_seconds")) for item in values), 6
        ),
        "configured_max_calls": configured_max_calls,
        "transferred_calls": transferred_calls,
        "max_calls": configured_max_calls + transferred_calls,
        "max_tokens": first_max_tokens * planned_attempts,
        "over_budget": any(bool(item.get("over_budget")) for item in values),
    }


def aggregate_attempt_accountings(
    values: list[Mapping[str, Any]], *, architecture_mode: str, planned_attempts: int = 2
) -> dict[str, Any]:
    """Merge one or two separately capped provider boundaries for one observation."""

    if architecture_mode not in {"single_agent", "manager_star"}:
        raise TelemetryError("attempt accounting architecture is invalid")
    if planned_attempts not in {1, 2} or not 1 <= len(values) <= planned_attempts:
        raise TelemetryError("attempt accounting count is invalid")
    budgets = []
    provider_calls = []
    isolation = []
    calculations = []
    commit_count = 0
    provider_identity = None
    identity_sha256 = None
    rag_enabled = None
    for value in values:
        if value.get("architecture_mode") != architecture_mode:
            raise TelemetryError("attempt accounting architecture changed")
        current_rag = value.get("rag_enabled")
        if type(current_rag) is not bool:
            raise TelemetryError("attempt accounting requires a boolean RAG state")
        if rag_enabled is None:
            rag_enabled = current_rag
        elif current_rag is not rag_enabled:
            raise TelemetryError("RAG state changed between attempts")
        budget = value.get("budget")
        if not isinstance(budget, Mapping):
            raise TelemetryError("attempt accounting omitted a budget")
        budgets.append(budget)
        current_identity = value.get("provider_identity")
        current_hash = value.get("identity_sha256")
        if provider_identity is None:
            provider_identity = copy.deepcopy(current_identity)
            identity_sha256 = current_hash
        elif current_identity != provider_identity or current_hash != identity_sha256:
            raise TelemetryError("provider identity changed between attempts")
        provider_calls.extend(copy.deepcopy(value.get("provider_calls") or []))
        isolation.extend(copy.deepcopy(value.get("isolation_preflight") or []))
        calculations.extend(copy.deepcopy(value.get("calculation_tool_calls") or []))
        count = value.get("commit_count", 0)
        commit_count += _safe_nonnegative_int("commit_count", count)

    roles = {MANAGER: [], ARCHITECT: [], DEVELOPER: []}
    for budget in budgets:
        role_values = budget.get("roles") or {}
        if set(role_values) != set(roles):
            raise TelemetryError("attempt accounting omitted role budgets")
        for role in roles:
            roles[role].append(role_values[role])
    shared = _sum_budget_records(
        [budget.get("shared") or {} for budget in budgets],
        planned_attempts=planned_attempts,
    )
    merged_roles = {
        role: _sum_budget_records(items, planned_attempts=planned_attempts)
        for role, items in roles.items()
    }
    for number, record in enumerate(provider_calls, start=1):
        record["call_id"] = f"exp3-{number:06d}"
    return {
        "architecture_mode": architecture_mode,
        "rag_enabled": rag_enabled,
        "provider_identity": provider_identity,
        "identity_sha256": identity_sha256,
        "budget": {"shared": shared, "roles": merged_roles},
        "provider_calls": provider_calls,
        "isolation_preflight": isolation,
        "calculation_tool_calls": calculations,
        "commit_count": commit_count,
    }


def _attempt_records(
    value: object, *, required_for_formal_two_attempt_run: bool = False
) -> list[dict[str, Any]]:
    if value is None:
        if required_for_formal_two_attempt_run:
            raise TelemetryError(
                "formal two-attempt telemetry requires one or two attempt records"
            )
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        raise TelemetryError("attempt telemetry must contain one or two records")
    records = []
    for expected, item in enumerate(value, start=1):
        if not isinstance(item, Mapping) or item.get("attempt") != expected:
            raise TelemetryError("attempt telemetry order is invalid")
        status = item.get("status")
        completion = item.get("completion")
        if status not in {"succeeded", "failed"}:
            raise TelemetryError("attempt status is invalid")
        if completion not in {
            "complete",
            "required_items_missing",
            "required_commit_missing",
            "runtime_failure",
        }:
            raise TelemetryError("attempt completion code is invalid")
        saved_item_count = _safe_nonnegative_int(
            "saved_item_count", item.get("saved_item_count", 0)
        )
        required_item_count = _safe_nonnegative_int(
            "required_item_count", item.get("required_item_count", 0)
        )
        if saved_item_count > required_item_count or required_item_count not in {0, 25}:
            raise TelemetryError("attempt item coverage is invalid")
        if required_item_count == 25:
            if completion == "complete" and saved_item_count != 25:
                raise TelemetryError("complete T3 attempt must retain all 25 items")
            if saved_item_count == 25 and completion != "complete":
                raise TelemetryError("complete T3 coverage requires complete status")
            if completion == "required_items_missing" and saved_item_count >= 25:
                raise TelemetryError("missing-item status requires fewer than 25 items")
            if completion == "required_commit_missing":
                raise TelemetryError("T3 attempt cannot use required_commit_missing")
        elif completion == "required_items_missing":
            raise TelemetryError("required_items_missing is reserved for T3 attempts")
        if completion == "runtime_failure" and status != "failed":
            raise TelemetryError("runtime_failure requires a failed attempt")
        answer_capture_status = item.get(
            "answer_capture_status",
            "not_applicable" if required_item_count == 0 else "none",
        )
        expected_answer_status = (
            "not_applicable"
            if required_item_count == 0
            else "complete"
            if saved_item_count == required_item_count
            else "partial"
            if saved_item_count > 0
            else "none"
        )
        if answer_capture_status != expected_answer_status:
            raise TelemetryError("attempt answer capture status is inconsistent")
        artifact_delivery_status = item.get(
            "artifact_delivery_status",
            "committed" if int(item.get("commit_count") or 0) > 0 else "not_committed",
        )
        if artifact_delivery_status not in {"committed", "not_committed"}:
            raise TelemetryError("attempt artifact delivery status is invalid")
        runtime_exit_status = item.get(
            "runtime_exit_status", "clean" if status == "succeeded" else "error"
        )
        if runtime_exit_status != ("clean" if status == "succeeded" else "error"):
            raise TelemetryError("attempt runtime exit status is inconsistent")
        provider_call_count = _safe_nonnegative_int(
            "provider_call_count", item.get("provider_call_count", 0)
        )
        if provider_call_count > _MAX_PROVIDER_CALLS_PER_ATTEMPT:
            raise TelemetryError("attempt provider_call_count exceeds 20")
        committed_paths = item.get("committed_paths")
        if (
            not isinstance(committed_paths, list)
            or any(
                type(path) is not str
                or _COMMITTED_PATH_RE.fullmatch(path) is None
                for path in committed_paths
            )
            or len(committed_paths) != len(set(committed_paths))
        ):
            raise TelemetryError("attempt committed_paths are invalid")
        record = {
            "attempt": expected,
            "status": status,
            "completion": completion,
            "provider_call_count": provider_call_count,
            "commit_count": _safe_nonnegative_int(
                "attempt commit_count", item.get("commit_count", 0)
            ),
            "committed_paths": sorted(committed_paths),
            "answer_capture_status": answer_capture_status,
            "saved_item_count": saved_item_count,
            "required_item_count": required_item_count,
            "artifact_delivery_status": artifact_delivery_status,
            "runtime_exit_status": runtime_exit_status,
        }
        failure_type = item.get("failure_type")
        if failure_type is not None:
            if type(failure_type) is not str or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_.-]{0,127}", failure_type
            ):
                raise TelemetryError("attempt failure type is invalid")
            record["failure_type"] = failure_type
        records.append(record)
    return records


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


def _safe_int(name: str, value: object) -> int:
    if type(value) is not int:
        raise TelemetryError(f"{name} must be an integer")
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
    configured_max_calls = _safe_nonnegative_int(
        "configured_max_calls", value.get("configured_max_calls")
    )
    transferred_calls = _safe_int("transferred_calls", value.get("transferred_calls"))
    max_calls = _safe_nonnegative_int("max_calls", value.get("max_calls"))
    max_tokens = _safe_nonnegative_int("max_tokens", value.get("max_tokens"))
    if total != prompt + completion:
        raise TelemetryError("budget total_tokens must equal prompt plus completion")
    if max_calls != configured_max_calls + transferred_calls:
        raise TelemetryError(
            "effective max_calls does not equal configured capacity plus transfer"
        )
    return {
        "calls": calls,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "failures": failures,
        "latency_seconds": _safe_latency(value.get("latency_seconds")),
        "configured_max_calls": configured_max_calls,
        "transferred_calls": transferred_calls,
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


def _calculation_tool_records(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 8:
        raise TelemetryError("calculation tool records must be a list capped at eight")
    records = []
    expected_keys = {
        "tool",
        "role",
        "status",
        "input_sha256",
        "output_sha256",
        "latency_seconds",
        "operation_count",
    }
    optional_keys = {
        "stored_results_sha256",
        "completed_operation_count",
        "stored_result_shapes",
        "error",
        "error_detail",
        "failed_operation_index",
        "failed_operation",
        "failed_argument",
    }
    for item in value:
        if (
            not isinstance(item, Mapping)
            or not expected_keys.issubset(item)
            or not set(item).issubset(expected_keys | optional_keys)
        ):
            raise TelemetryError("calculation tool record fields are invalid")
        if item.get("tool") != "quant_calculate" or item.get("role") != DEVELOPER:
            raise TelemetryError("calculation tool identity is invalid")
        if item.get("status") not in {
            "succeeded",
            "partial",
            "failed",
            "cap_exceeded",
            "blocked",
        }:
            raise TelemetryError("calculation tool status is invalid")
        operation_count = _safe_nonnegative_int(
            "calculation operation_count", item.get("operation_count")
        )
        # A failed request may report more than 64 requested operations: that
        # is the evidence that the calculator enforced its limit. A successful
        # request above the limit would still be impossible and unsafe.
        if item["status"] in {"succeeded", "partial"} and operation_count > 64:
            raise TelemetryError(
                "successful calculation operation_count exceeds its cap"
            )
        record = {
            "tool": "quant_calculate",
            "role": DEVELOPER,
            "status": item["status"],
            "input_sha256": _safe_hash(item.get("input_sha256")),
            "output_sha256": _safe_hash(
                item.get("output_sha256"), nullable=True
            ),
            "latency_seconds": _safe_latency(item.get("latency_seconds")),
            "operation_count": operation_count,
        }
        if "stored_results_sha256" in item:
            record["stored_results_sha256"] = _safe_hash(
                item.get("stored_results_sha256"),
                nullable=True,
            )
        completed_operation_count = item.get("completed_operation_count")
        if completed_operation_count is not None:
            if (
                type(completed_operation_count) is not int
                or not 0 <= completed_operation_count <= min(operation_count, MAX_OPERATIONS)
            ):
                raise TelemetryError("calculation completed operation count is invalid")
            record["completed_operation_count"] = completed_operation_count
        stored_result_shapes = item.get("stored_result_shapes")
        if stored_result_shapes is not None:
            if (
                not isinstance(stored_result_shapes, list)
                or not 1 <= len(stored_result_shapes) <= MAX_OPERATIONS
            ):
                raise TelemetryError("calculation stored result shapes are invalid")
            safe_shapes = []
            for shape_record in stored_result_shapes:
                if (
                    not isinstance(shape_record, Mapping)
                    or set(shape_record) != {"kind", "shape"}
                    or shape_record.get("kind") not in {"scalar", "array"}
                    or not isinstance(shape_record.get("shape"), list)
                    or len(shape_record["shape"]) > 5
                    or any(
                        type(size) is not int or not 0 <= size <= MAX_RESULT_CELLS
                        for size in shape_record["shape"]
                    )
                    or (
                        shape_record["kind"] == "scalar"
                        and shape_record["shape"] != []
                    )
                    or (
                        shape_record["kind"] == "array"
                        and not shape_record["shape"]
                    )
                ):
                    raise TelemetryError("calculation stored result shapes are invalid")
                safe_shapes.append(
                    {"kind": shape_record["kind"], "shape": list(shape_record["shape"])}
                )
            record["stored_result_shapes"] = safe_shapes
        error_code = item.get("error")
        if error_code is not None:
            if type(error_code) is not str or _SAFE_NAME_RE.fullmatch(error_code) is None:
                raise TelemetryError("calculation error code is invalid")
            record["error"] = error_code
        error_detail = item.get("error_detail")
        if error_detail is not None:
            if error_detail != CALCULATION_ERROR_DETAILS.get(error_code):
                raise TelemetryError("calculation error detail is invalid")
            record["error_detail"] = error_detail
        failed_index = item.get("failed_operation_index")
        if failed_index is not None:
            if type(failed_index) is not int or not 0 <= failed_index < MAX_OPERATIONS:
                raise TelemetryError("calculation failed operation index is invalid")
            record["failed_operation_index"] = failed_index
        failed_operation = item.get("failed_operation")
        if failed_operation is not None:
            if failed_operation not in SUPPORTED_OPERATIONS:
                raise TelemetryError("calculation failed operation is invalid")
            record["failed_operation"] = failed_operation
        failed_argument = item.get("failed_argument")
        if failed_argument is not None:
            if (
                type(failed_argument) is not str
                or _SAFE_NAME_RE.fullmatch(failed_argument) is None
            ):
                raise TelemetryError("calculation failed argument is invalid")
            record["failed_argument"] = failed_argument
        if item["status"] == "partial" and (
            record.get("stored_results_sha256") is None
            or completed_operation_count is None
            or completed_operation_count <= 0
            or completed_operation_count >= operation_count
            or not stored_result_shapes
            or error_code is None
            or failed_index is None
        ):
            raise TelemetryError("partial calculation telemetry is incomplete")
        records.append(record)
    return records


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


def _role_retrieval_delivery_records(
    audit: list[Mapping[str, Any]],
    *,
    rag_enabled: bool,
    provider_calls: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Validate per-role RAG delivery while retaining only hashes and counts."""

    values = [
        item
        for item in audit
        if item.get("record_type") == "role_retrieval_delivery"
    ]
    if len(values) > 10:
        raise TelemetryError("role retrieval delivery evidence exceeds two attempts")
    if not rag_enabled:
        if values:
            raise TelemetryError("RAG-disabled manager-star emitted delivery evidence")
        return []

    records: list[dict[str, Any]] = []
    seen_keys: set[tuple[int, str]] = set()
    for item in values:
        attempt = item.get("attempt")
        phase = item.get("phase")
        role = item.get("role")
        if type(attempt) is not int or attempt not in {1, 2}:
            raise TelemetryError("role retrieval delivery attempt is invalid")
        if phase not in _ROLE_RETRIEVAL_PHASES or role != _ROLE_RETRIEVAL_PHASES[phase]:
            raise TelemetryError("role retrieval delivery phase or role is invalid")
        key = (attempt, phase)
        if key in seen_keys:
            raise TelemetryError(
                "role retrieval delivery phase is duplicated within one attempt"
            )
        if item.get("status") != "delivered":
            raise TelemetryError("role retrieval delivery status is invalid")
        count = _safe_nonnegative_int(
            "injected_memory_count", item.get("injected_memory_count")
        )
        if count > 10:
            raise TelemetryError("role retrieval memory count exceeds its cap")
        records.append(
            {
                "attempt": attempt,
                "phase": phase,
                "role": role,
                "status": "delivered",
                "evidence_text_sha256": _safe_hash(
                    item.get("evidence_text_sha256")
                ),
                "prompt_template_sha256": _safe_hash(
                    item.get("prompt_template_sha256")
                ),
                "injected_memory_count": count,
                "injected_memory_ids_sha256": _safe_hash(
                    item.get("injected_memory_ids_sha256")
                ),
            }
        )
        seen_keys.add(key)

    # The router writes one receipt immediately before the first provider call
    # for a role stage.  A stage may then make several provider calls while it
    # exchanges tool results with the model, so receipt and call counts are not
    # expected to be equal.  Audit order, rather than a phase histogram, proves
    # that every call was made under the matching frozen retrieval delivery.
    record_by_key = {
        (record["attempt"], record["phase"]): record for record in records
    }
    active_delivery: dict[str, Any] | None = None
    active_call_count = 0
    audited_provider_calls: list[dict[str, Any]] = []
    for item in audit:
        record_type = item.get("record_type")
        if record_type == "role_retrieval_delivery":
            if active_delivery is not None and active_call_count == 0:
                raise TelemetryError(
                    "role retrieval delivery has no following provider call"
                )
            active_delivery = record_by_key[(item["attempt"], item["phase"])]
            active_call_count = 0
            continue
        if record_type != "provider_call":
            continue
        provider_record = _provider_record(item)
        audited_provider_calls.append(provider_record)
        if active_delivery is None:
            raise TelemetryError(
                "RAG-enabled provider call has no preceding retrieval delivery"
            )
        if provider_record["phase"] != active_delivery["phase"]:
            raise TelemetryError(
                "RAG-enabled provider call phase does not match its retrieval delivery"
            )
        active_call_count += 1

    if active_delivery is not None and active_call_count == 0:
        raise TelemetryError("role retrieval delivery has no following provider call")

    # Two separately capped attempts restart their local call IDs before the
    # attempt accountings are merged and renumbered.  Compare every other safe
    # field so that audit coverage cannot omit, add or reorder a provider call.
    def comparable_provider_call(record: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "call_id"}

    if [
        comparable_provider_call(record) for record in audited_provider_calls
    ] != [comparable_provider_call(record) for record in provider_calls]:
        raise TelemetryError(
            "provider-call audit sequence does not match provider telemetry"
        )
    if records:
        for field in (
            "evidence_text_sha256",
            "prompt_template_sha256",
            "injected_memory_count",
            "injected_memory_ids_sha256",
        ):
            if len({record[field] for record in records}) != 1:
                raise TelemetryError(
                    "frozen retrieval delivery changed between manager-star stages"
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
        calculation_tool_values = outcome.get("calculation_tool_calls") or []
        commit_count = outcome.get("commit_count", 0)
        isolation = outcome.get("isolation_preflight")
        manager_acceptance = outcome.get("manager_acceptance_map")
        topology = outcome.get("topology")
        outcome_provider_identity = outcome.get("provider_identity")
        attempt_values = outcome.get("attempts")
        rag_enabled = outcome.get("rag_enabled", False)
    else:
        status = "failed"
        failure_phase = getattr(outcome, "failure_phase", None)
        budget = getattr(outcome, "budget", None)
        audit = getattr(outcome, "audit", []) or []
        provider_values = _provider_records_from_failure_audit(audit)
        calculation_tool_values = getattr(
            outcome, "calculation_tool_calls", []
        ) or []
        commit_count = getattr(outcome, "commit_count", 0)
        isolation = getattr(outcome, "isolation_preflight", None)
        manager_acceptance = getattr(outcome, "manager_acceptance_map", None)
        topology = getattr(outcome, "topology", None)
        outcome_provider_identity = getattr(outcome, "provider_identity", None)
        attempt_values = getattr(outcome, "experiment_attempts", None)
        rag_enabled = getattr(outcome, "rag_enabled", False)

    if status not in {"succeeded", "failed"}:
        raise TelemetryError("manager-star status must be succeeded or failed")
    if type(rag_enabled) is not bool:
        raise TelemetryError("manager-star telemetry requires a boolean RAG state")
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
    role_retrieval_deliveries = _role_retrieval_delivery_records(
        [item for item in audit if isinstance(item, Mapping)],
        rag_enabled=rag_enabled,
        provider_calls=provider_calls,
    )

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
        "rag_enabled": rag_enabled,
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
        "calculation_tool_calls": _calculation_tool_records(
            calculation_tool_values
        ),
        "role_retrieval_deliveries": role_retrieval_deliveries,
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
    attempts = _attempt_records(
        attempt_values,
        required_for_formal_two_attempt_run=(
            shared["max_calls"] == _FORMAL_TWO_ATTEMPT_MAX_CALLS
        ),
    )
    if attempts:
        result["attempts"] = attempts
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
    rag_enabled = snapshot.get("rag_enabled")
    if type(rag_enabled) is not bool:
        raise TelemetryError("single-agent telemetry requires a boolean RAG state")
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
    calculation_tool_calls = _calculation_tool_records(
        snapshot.get("calculation_tool_calls") or []
    )
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

    result = {
        "schema_version": TELEMETRY_VERSION,
        "architecture_mode": "single_agent",
        "rag_enabled": rag_enabled,
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
        "calculation_tool_calls": calculation_tool_calls,
        "role_retrieval_deliveries": [],
        "commit_count": _safe_nonnegative_int("commit_count", commit_count),
        "handoff_schema_failures": 0,
        "handoffs": [],
        "manager_acceptance_map": None,
        "isolation_preflight": _isolation_summary(
            snapshot.get("isolation_preflight")
        ),
        "topology": None,
    }
    attempts = _attempt_records(
        snapshot.get("attempts"),
        required_for_formal_two_attempt_run=(
            shared["max_calls"] == _FORMAL_TWO_ATTEMPT_MAX_CALLS
        ),
    )
    if attempts:
        result["attempts"] = attempts
    return result
