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
        "exp3-runtime-telemetry-v2"
    )
    assert exp3["properties"]["architecture_mode"]["enum"] == [
        "single_agent",
        "manager_star",
    ]
    assert exp3["properties"]["rag_enabled"]["const"] is False
    assert exp3["properties"]["provider_identity"]["additionalProperties"] is False
    assert exp3["properties"]["provider_calls"]["maxItems"] == 15
    assert set(exp3["properties"]["role_usage"]["required"]) == {
        "manager",
        "architect",
        "developer",
    }


def test_runtime_request_non_backtest_example_validates() -> None:
    payload = json.loads(
        (GATEWAY_ROOT / "examples/runtime_request_non_backtest_example.json").read_text(
            encoding="utf-8"
        )
    )

    assert list(load_runtime_request_validator().iter_errors(payload)) == []


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
