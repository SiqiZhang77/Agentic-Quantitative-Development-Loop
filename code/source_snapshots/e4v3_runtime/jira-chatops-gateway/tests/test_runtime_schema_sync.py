from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from adf_utils.builder import (
    LIVE_ONLY_PROGRESS_STATUSES,
    PROGRESS_EVENT_STATUSES,
    PROGRESS_EVENT_STATUS_TO_ITERATION_TRACE_STATUS,
)


REPO_ROOT = Path(__file__).parents[2]
GATEWAY_ROOT = REPO_ROOT / "jira-chatops-gateway"


def test_runtime_request_schema_matches_rae_sandbox_copy() -> None:
    canonical = GATEWAY_ROOT / "schemas/runtime_request.schema.json"
    sandbox_copy = REPO_ROOT / "rae_runtime/sandbox/schemas/runtime_request.schema.json"

    assert sandbox_copy.read_text(encoding="utf-8") == canonical.read_text(
        encoding="utf-8"
    )


def test_runtime_request_architecture_mode_is_optional_with_single_agent_default(
) -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert "architecture_mode" not in schema["required"]
    assert schema["properties"]["architecture_mode"]["default"] == "single_agent"
    assert "architecture_mode" not in payload
    assert list(load_runtime_request_validator().iter_errors(payload)) == []
    assert "architecture_mode" not in payload


@pytest.mark.parametrize("architecture_mode", ["single_agent", "manager_star"])
def test_runtime_request_accepts_supported_architecture_modes(
    architecture_mode: str,
) -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["architecture_mode"] = architecture_mode

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def test_runtime_request_rejects_unknown_architecture_mode() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["architecture_mode"] = "peer_mesh"

    errors = list(load_runtime_request_validator().iter_errors(payload))

    assert errors
    assert any(error.absolute_path[-1] == "architecture_mode" for error in errors)


def load_runtime_request_validator() -> Draft202012Validator:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def load_runtime_response_validator() -> Draft202012Validator:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


@pytest.mark.parametrize(
    "example_path",
    [
        GATEWAY_ROOT / "examples/runtime_response_success_example.json",
        GATEWAY_ROOT / "examples/runtime_response_failure_example.json",
        GATEWAY_ROOT / "examples/runtime_response_timeout_example.json",
        GATEWAY_ROOT / "examples/runtime_response_success_non_backtest_example.json",
    ],
)
def test_runtime_response_examples_validate(example_path: Path) -> None:
    validator = load_runtime_response_validator()
    payload = json.loads(example_path.read_text(encoding="utf-8"))

    assert list(validator.iter_errors(payload)) == []


def test_runtime_response_telemetry_is_optional() -> None:
    validator = load_runtime_response_validator()
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_response_success_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload.pop("telemetry")

    assert list(validator.iter_errors(payload)) == []


def test_runtime_response_failure_example_records_handled_container_failure() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_response_failure_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload["execution_summary"]["status"] == "failed"
    assert payload["generated_artifacts"]["branch_name"] is None
    assert payload["telemetry"]["container_exit_code"] == 1
    assert payload["telemetry"]["model_usage"] == []
    assert payload["telemetry"]["stage_timings"] == []
    assert payload["telemetry"]["audit_trace"] is None


def test_runtime_response_success_example_records_git_branch() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_response_success_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert payload["generated_artifacts"]["branch_name"] == "quant/SCRUM-46"


def test_runtime_response_schema_matches_rae_sandbox_copy() -> None:
    canonical = REPO_ROOT / "jira-chatops-gateway/schemas/runtime_response.schema.json"
    sandbox_copy = REPO_ROOT / "rae_runtime/sandbox/schemas/runtime_response.schema.json"

    assert sandbox_copy.read_text(encoding="utf-8") == canonical.read_text(
        encoding="utf-8"
    )


def test_runtime_response_has_strict_versioned_exp3_telemetry_contract() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    exp3 = schema["properties"]["telemetry"]["properties"]["experiment3"]

    assert exp3["additionalProperties"] is False
    assert exp3["properties"]["schema_version"]["const"] == (
        "exp3-runtime-telemetry-v4"
    )
    assert exp3["properties"]["architecture_mode"]["enum"] == [
        "single_agent",
        "manager_star",
    ]
    assert exp3["properties"]["rag_enabled"] == {"type": "boolean"}
    # The shared response format supports all four future factorial cells.
    # Its only object-level conditional requires attempt evidence for the
    # frozen 40-call, two-attempt budget; it does not constrain RAG by arm.
    formal_attempt_guard = exp3["allOf"][0]
    assert formal_attempt_guard["if"]["properties"]["shared_budget"][
        "properties"
    ]["max_calls"] == {"const": 40}
    assert formal_attempt_guard["then"] == {"required": ["attempts"]}
    assert "rag_enabled" not in json.dumps(exp3["allOf"])
    assert exp3["properties"]["provider_identity"]["additionalProperties"] is False
    assert exp3["properties"]["provider_calls"]["maxItems"] == 40
    assert exp3["properties"]["calculation_tool_calls"]["maxItems"] == 8
    isolation = exp3["properties"]["isolation_preflight"]["oneOf"][1]
    assert isolation["properties"]["pre_orchestration_checks"]["maximum"] == 2
    assert exp3["properties"]["calculation_tool_calls"]["items"]["properties"][
        "tool"
    ]["const"] == "quant_calculate"
    stored_results_sha256 = exp3["properties"]["calculation_tool_calls"]["items"][
        "properties"
    ]["stored_results_sha256"]
    assert stored_results_sha256["type"] == ["string", "null"]
    assert stored_results_sha256["pattern"] == "^[a-f0-9]{64}$"
    calculation_properties = exp3["properties"]["calculation_tool_calls"][
        "items"
    ]["properties"]
    assert "partial" in calculation_properties["status"]["enum"]
    assert calculation_properties["completed_operation_count"]["maximum"] == 64
    assert calculation_properties["stored_result_shapes"]["maxItems"] == 64
    assert calculation_properties["stored_result_shapes"]["items"]["properties"][
        "shape"
    ]["items"]["maximum"] == 32768
    assert calculation_properties["operation_count"] == {
        "type": "integer",
        "minimum": 0,
    }
    role_deliveries = exp3["properties"]["role_retrieval_deliveries"]
    assert role_deliveries["maxItems"] == 10
    assert role_deliveries["items"]["additionalProperties"] is False
    assert role_deliveries["items"]["properties"]["attempt"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 2,
    }
    assert "required_items_missing" in exp3["properties"]["attempts"]["items"][
        "properties"
    ]["completion"]["enum"]
    assert "text" not in role_deliveries["items"]["properties"]
    assert role_deliveries["items"]["properties"][
        "evidence_text_sha256"
    ]["pattern"] == "^[a-f0-9]{64}$"
    assert role_deliveries["items"]["properties"][
        "injected_memory_count"
    ]["maximum"] == 10
    assert set(exp3["properties"]["role_usage"]["required"]) == {
        "manager",
        "architect",
        "developer",
    }
    budget_usage = schema["$defs"]["exp3BudgetUsage"]
    assert "configured_max_calls" in budget_usage["required"]
    assert "transferred_calls" in budget_usage["required"]
    assert budget_usage["properties"]["configured_max_calls"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert budget_usage["properties"]["transferred_calls"] == {
        "type": "integer"
    }


def test_attempt_telemetry_schema_enforces_caps_paths_and_t3_completion() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    attempt_schema = schema["properties"]["telemetry"]["properties"][
        "experiment3"
    ]["properties"]["attempts"]["items"]
    validator = Draft202012Validator(attempt_schema)
    valid = {
        "attempt": 1,
        "status": "succeeded",
        "completion": "complete",
        "provider_call_count": 20,
        "commit_count": 0,
        "committed_paths": [],
        "answer_capture_status": "complete",
        "saved_item_count": 25,
        "required_item_count": 25,
        "artifact_delivery_status": "not_committed",
        "runtime_exit_status": "clean",
    }
    assert list(validator.iter_errors(valid)) == []

    for invalid in (
        {**valid, "provider_call_count": 21},
        {**valid, "committed_paths": ["../answer.json"]},
        {**valid, "saved_item_count": 24, "answer_capture_status": "partial"},
        {
            **valid,
            "completion": "required_items_missing",
            "saved_item_count": 25,
        },
    ):
        assert list(validator.iter_errors(invalid))


def test_calculation_telemetry_allows_only_rejected_oversized_batches() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    calculation = schema["properties"]["telemetry"]["properties"]["experiment3"][
        "properties"
    ]["calculation_tool_calls"]["items"]
    validator = Draft202012Validator(calculation)
    base = {
        "tool": "quant_calculate",
        "role": "developer",
        "input_sha256": "a" * 64,
        "output_sha256": None,
        "latency_seconds": 0.0,
    }

    rejected = {
        **base,
        "status": "failed",
        "operation_count": 65,
        "error": "invalid_operation_count",
    }
    assert list(validator.iter_errors(rejected)) == []

    for invalid in (
        {**rejected, "status": "succeeded"},
        {**rejected, "status": "partial"},
        {**rejected, "error": "calculation_failed"},
        {key: value for key, value in rejected.items() if key != "error"},
    ):
        assert list(validator.iter_errors(invalid))

    for status in ("succeeded", "partial"):
        accepted = {**base, "status": status, "operation_count": 64}
        assert list(validator.iter_errors(accepted)) == []


def test_runtime_request_non_backtest_example_validates() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def test_runtime_request_calculator_flag_is_boolean_only() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    parameter = schema["properties"]["execution_objectives"]["properties"][
        "parsed_task_parameters"
    ]["properties"]["quant_calculator_enabled"]

    assert parameter["type"] == "boolean"


def test_runtime_request_calculator_schema_path_is_repository_relative() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    parameter = schema["properties"]["execution_objectives"]["properties"][
        "parsed_task_parameters"
    ]["properties"]["quant_calculator_schema_path"]

    assert parameter["type"] == "string"
    assert parameter["minLength"] == 1
    assert parameter["maxLength"] == 500
    assert "(?!/)" in parameter["pattern"]


def test_runtime_request_requires_schema_path_when_calculator_is_enabled() -> None:
    validator = load_runtime_request_validator()
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    parameters = payload["execution_objectives"].setdefault(
        "parsed_task_parameters", {}
    )
    parameters["quant_calculator_enabled"] = True

    errors = list(validator.iter_errors(payload))

    assert any(
        "quant_calculator_schema_path" in error.message for error in errors
    )
    parameters["quant_calculator_schema_path"] = (
        "experiments/shared/t3-quant-suite-v1/output_schema_v1.json"
    )
    assert list(validator.iter_errors(payload)) == []


def test_runtime_request_hdfs_input_example_validates() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_hdfs_input_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def test_runtime_request_jira_attachment_example_validates() -> None:
    payload = json.loads(
        (
            GATEWAY_ROOT
            / "examples/runtime_request_jira_attachment_example.json"
        ).read_text(encoding="utf-8")
    )

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def _valid_retrieval_context() -> dict:
    return {
        "enabled": True,
        "status": "ok",
        "query": "Summary: Refactor Jira history",
        "query_sha256": "1" * 64,
        "retriever_name": "jira_lexical_bm25",
        "retriever_version": "1.0.0",
        "corpus_id": "e2-corpus-v1",
        "corpus_sha256": "2" * 64,
        "index_id": "e2-index-v1",
        "index_sha256": "3" * 64,
        "exclusion_list_id": "e2-exclusions-v1",
        "exclusion_list_sha256": "4" * 64,
        "cutoff_at": "2026-06-01T00:00:00+00:00",
        "requested_top_k": 5,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 20,
        "memories": [
            {
                "memory_id": "MEM-01",
                "source_ticket_id": "SCRUM-4",
                "source_type": "comment",
                "source_id": "comment:10001",
                "source_timestamp": "2026-05-01T00:00:00+00:00",
                "rank": 1,
                "score": 1.25,
                "text": "History remains chronological and capped.",
            }
        ],
    }


def test_runtime_request_accepts_auditable_retrieval_context() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["execution_objectives"].setdefault("parsed_task_parameters", {}).update(
        {"rag_enabled": True, "rag_top_k": 5}
    )
    payload["retrieval_context"] = _valid_retrieval_context()

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


@pytest.mark.parametrize(
    "status, memories",
    [
        ("ok", []),
        ("empty", _valid_retrieval_context()["memories"]),
    ],
)
def test_runtime_request_rejects_inconsistent_retrieval_status(
    status: str,
    memories: list[dict],
) -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["execution_objectives"].setdefault("parsed_task_parameters", {}).update(
        {"rag_enabled": True, "rag_top_k": 5}
    )
    payload["retrieval_context"] = {
        **_valid_retrieval_context(),
        "status": status,
        "memories": memories,
    }

    assert list(load_runtime_request_validator().iter_errors(payload))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda context: context.update({"surprise": True}),
        lambda context: context.update({"requested_top_k": 11}),
        lambda context: context.update({"max_memories_per_source_ticket": 3}),
        lambda context: context["memories"][0].update({"text": "x" * 4_001}),
        lambda context: context["memories"][0].update({"score": -1}),
    ],
)
def test_runtime_request_rejects_invalid_retrieval_context(mutate) -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )
    context = _valid_retrieval_context()
    mutate(context)
    payload["retrieval_context"] = context

    assert list(load_runtime_request_validator().iter_errors(payload))


def test_runtime_request_progress_events_path_is_optional() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert "progress_events_path" not in schema["properties"]["output_paths"]["required"]
    payload["output_paths"].pop("progress_events_path")

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def test_runtime_request_resource_requirements_are_optional_for_legacy_payloads(
) -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload.pop("resource_requirements")

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


def test_runtime_request_rejects_unsupported_gpu_request() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_example.json").read_text(
            encoding="utf-8"
        )
    )
    payload["resource_requirements"]["gpu_count"] = 1

    errors = list(load_runtime_request_validator().iter_errors(payload))

    assert any(error.absolute_path[-1] == "gpu_count" for error in errors)


def test_progress_event_statuses_do_not_drift_from_iteration_trace_vocabulary() -> None:
    schema = json.loads(
        (GATEWAY_ROOT / "schemas/runtime_response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    trace_statuses = set(
        schema["properties"]["execution_summary"]["properties"]["iteration_traces"]
        ["items"]["properties"]["status"]["enum"]
    )

    assert set(PROGRESS_EVENT_STATUS_TO_ITERATION_TRACE_STATUS.values()) == trace_statuses
    assert PROGRESS_EVENT_STATUSES == (
        LIVE_ONLY_PROGRESS_STATUSES
        | set(PROGRESS_EVENT_STATUS_TO_ITERATION_TRACE_STATUS)
    )
