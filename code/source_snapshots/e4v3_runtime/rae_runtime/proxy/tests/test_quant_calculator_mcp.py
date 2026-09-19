from __future__ import annotations

import json
from pathlib import Path

import pytest

import github_mcp_server as server
from quant_calculator import CALCULATION_ERROR_DETAILS


class RecordingFastMCP:
    def __init__(self, _name: str) -> None:
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn

        return register


@pytest.fixture
def explicit_m0(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(server, "FastMCP", RecordingFastMCP)
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("E3_REF_SCOPE_PREFLIGHT_PASSED", "true")
    monkeypatch.setenv("E3_NEGATIVE_REF_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("E3_NEGATIVE_REF_SET_SHA256", "b" * 64)
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", str(audit))
    monkeypatch.setenv("MAX_QUANT_CALCULATE_CALLS", "4")
    return audit


def _request(marker: str = "answer") -> dict:
    return {
        "schema_version": "quant-calculate-request-v2",
        "operations": [
            {
                "id": marker,
                "op": "sum",
                "args": {"values": [123456.789, 1.0]},
            }
        ],
        "return_ids": [marker],
    }


def test_calculator_is_absent_without_explicit_task_profile(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.delenv("RAE_ENABLE_QUANT_CALCULATOR", raising=False)
    mcp = server.create_mcp_server()

    assert "quant_calculate" not in mcp.tools
    assert "submit_t3_items" not in mcp.tools
    assert "submit_calculation_checkpoint" not in mcp.tools
    assert "commit_calculation_artifact" not in mcp.tools
    assert {"read_file", "commit_and_push"}.issubset(mcp.tools)


def test_calculator_is_developer_only_in_manager_star(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")

    monkeypatch.setenv("RAE_AGENT_ROLE", "manager")
    assert "quant_calculate" not in server.create_mcp_server().tools
    assert "submit_t3_items" not in server.create_mcp_server().tools
    assert "submit_calculation_checkpoint" not in server.create_mcp_server().tools
    assert "commit_calculation_artifact" not in server.create_mcp_server().tools

    monkeypatch.setenv("RAE_AGENT_ROLE", "architect")
    assert "quant_calculate" not in server.create_mcp_server().tools
    assert "submit_t3_items" not in server.create_mcp_server().tools
    assert "submit_calculation_checkpoint" not in server.create_mcp_server().tools
    assert "commit_calculation_artifact" not in server.create_mcp_server().tools

    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    tools = server.create_mcp_server().tools
    assert "quant_calculate" in tools
    assert "submit_t3_items" in tools
    assert "submit_calculation_checkpoint" in tools
    assert "commit_calculation_artifact" in tools


def test_four_calls_are_allowed_and_fifth_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    for _ in range(4):
        assert tool(_request())["status"] == "succeeded"
    fifth = tool(_request())

    assert fifth == {
        "status": "cap_exceeded",
        "error": "quant_calculate_call_cap_exceeded",
        "max_calls": 4,
    }


def test_calculator_audit_contains_hashes_not_values_or_operation_ids(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    result = tool(_request("private_marker"))

    assert result["status"] == "succeeded"
    raw = explicit_m0.read_text(encoding="utf-8")
    assert "123456.789" not in raw
    assert "private_marker" not in raw
    record = json.loads(raw)
    assert record["tool"] == "quant_calculate"
    assert record["role"] == "developer"
    assert record["status"] == "succeeded"
    assert record["operation_count"] == 1
    assert len(record["input_sha256"]) == 64
    assert len(record["output_sha256"]) == 64
    assert record["latency_seconds"] >= 0
    assert "arguments" not in record
    assert "content" not in record


def test_malformed_calculation_returns_only_a_safe_error_code(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    result = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [],
            "return_ids": ["secret_should_not_echo"],
        }
    )

    assert result == {"status": "failed", "error": "invalid_operation_count"}
    assert "secret_should_not_echo" not in explicit_m0.read_text(encoding="utf-8")


def test_call_cap_cannot_be_widened_by_environment(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setenv("MAX_QUANT_CALCULATE_CALLS", "5")

    with pytest.raises(RuntimeError, match="fixed tool profile"):
        server.create_mcp_server()


def test_model_visible_tools_explain_calculate_assemble_validate_commit_workflow(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tools = server.create_mcp_server().tools
    descriptions = {
        name: " ".join((tool.__doc__ or "").split())
        for name, tool in tools.items()
    }

    assert "complete arrays remain available inside this run" in descriptions["quant_calculate"]
    assert "stored_ref" in descriptions["quant_calculate"]
    assert "partial" in descriptions["quant_calculate"]
    assert "never mapped to task items" in descriptions["quant_calculate"]
    assert "does not end the current agent attempt" in descriptions["quant_calculate"]
    assert "saved independently" in descriptions["submit_t3_items"]
    assert "never scores mathematics" in descriptions["submit_t3_items"]
    assert "does not write to GitHub" in descriptions["submit_calculation_checkpoint"]
    assert "$checkpoint" in descriptions["submit_calculation_checkpoint"]
    assert "performs no arithmetic" in descriptions["commit_calculation_artifact"]
    assert "$zip_rows" in descriptions["commit_calculation_artifact"]
    assert "new path that does not yet exist" in (
        descriptions["validate_content"]
    )
    assert "this tool creates" in descriptions["commit_and_push"]


def test_later_calculation_call_can_use_an_explicit_stored_result(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    first = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "series",
                    "op": "add",
                    "args": {"left": [1.0, 2.0, 3.0], "right": 1.0},
                }
            ],
            "return_ids": ["series"],
        }
    )
    second = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "total",
                    "op": "sum",
                    "args": {
                        "values": {
                            "stored_ref": {
                                "calculation_id": first["calculation_id"],
                                "result_id": "series",
                            }
                        }
                    },
                }
            ],
            "return_ids": ["total"],
        }
    )

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert second["results"]["total"] == {
        "kind": "scalar",
        "value": pytest.approx(9.0),
    }


def test_checkpoint_survives_a_new_server_process_and_can_be_committed(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    source = "exp3/stable-source"
    target = "quant/E3V3-T3-R1-M0"
    schema_path = "experiments/shared/t3-quant-suite-v1/output_schema_v1.json"
    output_path = "experiments/shared/t3-quant-suite-v1/submissions/result.json"
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["level_a"],
        "properties": {
            "level_a": {
                "type": "object",
                "additionalProperties": False,
                "required": ["metric"],
                "properties": {"metric": {"type": "number"}},
            }
        },
    }
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: source})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: target})
    monkeypatch.setattr(server, "_WRITABLE_PATHS_MAP", {repo: (output_path,)})
    monkeypatch.setattr(
        server,
        "_CALCULATION_SCHEMA_PATHS_MAP",
        {repo: (schema_path,)},
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **_kwargs: json.dumps(schema))
    monkeypatch.setattr(server, "_validate_proposed_content", lambda **_kwargs: [])
    pushed: list[dict] = []
    monkeypatch.setattr(server, "_push_strategy_code", lambda **kwargs: pushed.append(kwargs) or "ok")

    first_process = server.create_mcp_server().tools
    calculation = first_process["quant_calculate"](_request())
    checkpoint = first_process["submit_calculation_checkpoint"](
        section="level_a",
        schema_path=schema_path,
        document={
            "metric": {
                "$calc": {
                    "calculation_id": calculation["calculation_id"],
                    "result_id": "answer",
                }
            }
        },
        repo_name=repo,
    )

    assert checkpoint["status"] == "success"
    assert checkpoint["saved_sections"] == ["level_a"]
    persisted = explicit_m0.parent / "t3_quant_checkpoints.json"
    assert json.loads(persisted.read_text(encoding="utf-8"))["sections"]["level_a"]

    second_process = server.create_mcp_server().tools
    committed = second_process["commit_calculation_artifact"](
        branch=target,
        path=output_path,
        schema_path=schema_path,
        document={"level_a": {"$checkpoint": "level_a"}},
        commit_message="commit saved section",
        repo_name=repo,
    )

    assert committed["status"] == "success"
    assert committed["calculation_reference_count"] == 1
    assert committed["checkpoint_reference_count"] == 1
    assert json.loads(pushed[0]["code"])["level_a"]["metric"] == pytest.approx(
        123457.789
    )
    audit_records = [
        json.loads(line) for line in explicit_m0.read_text(encoding="utf-8").splitlines()
    ]
    checkpoint_record = next(
        record
        for record in audit_records
        if record["tool"] == "submit_calculation_checkpoint"
    )
    assert checkpoint_record["checkpoint_section"] == "level_a"
    assert "document" not in checkpoint_record


def test_failed_batched_calculation_identifies_the_operation_without_values(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    result = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {"id": "matrix", "op": "add", "args": {"left": [[1.0]], "right": 1.0}},
                {"id": "broken_sum", "op": "sum", "args": {"values": {"ref": "matrix"}}},
            ],
            "return_ids": ["broken_sum"],
        }
    )

    assert result == {
        "status": "failed",
        "error": "flat_numeric_vector_required",
        "error_detail": CALCULATION_ERROR_DETAILS[
            "flat_numeric_vector_required"
        ],
        "failed_operation_index": 1,
        "failed_operation_id": "broken_sum",
        "failed_operation": "sum",
        "failed_argument": "values",
    }
    audit = json.loads(explicit_m0.read_text(encoding="utf-8"))
    assert audit["error"] == "flat_numeric_vector_required"
    assert audit["failed_argument"] == "values"
    assert "broken_sum" not in json.dumps(audit)


def test_partial_batch_retains_selected_prefix_for_a_later_stored_ref(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    partial = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "series",
                    "op": "add",
                    "args": {"left": [1.0, 2.0, 3.0], "right": 1.0},
                },
                {
                    "id": "broken_sum",
                    "op": "sum",
                    "args": {"values": []},
                },
            ],
            "return_ids": ["series", "broken_sum"],
        }
    )

    assert partial["schema_version"] == "quant-calculate-mcp-response-v3"
    assert partial["status"] == "partial"
    assert partial["calculation_id"] == "calculation-1"
    assert partial["saved_result_ids"] == ["series"]
    assert partial["results"]["series"]["kind"] == "array"
    assert partial["results"]["series"]["shape"] == [3]
    assert len(partial["results"]["series"]["sha256"]) == 64
    assert partial["completed_operation_count"] == 1
    assert partial["error"] == "nonempty_vector_required"
    assert partial["error_detail"] == CALCULATION_ERROR_DETAILS[
        "nonempty_vector_required"
    ]
    assert partial["failed_operation_index"] == 1
    assert partial["failed_operation_id"] == "broken_sum"
    assert partial["failed_operation"] == "sum"
    assert partial["failed_argument"] == "values"

    continued = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "total",
                    "op": "sum",
                    "args": {
                        "values": {
                            "stored_ref": {
                                "calculation_id": "calculation-1",
                                "result_id": "series",
                            }
                        }
                    },
                }
            ],
            "return_ids": ["total"],
        }
    )

    assert continued["status"] == "succeeded"
    assert continued["results"]["total"]["value"] == pytest.approx(9.0)
    records = [
        json.loads(line)
        for line in explicit_m0.read_text(encoding="utf-8").splitlines()
    ]
    partial_audit = records[0]
    assert partial_audit["status"] == "partial"
    assert partial_audit["completed_operation_count"] == 1
    assert partial_audit["stored_result_shapes"] == [
        {"kind": "array", "shape": [3]}
    ]
    assert len(partial_audit["stored_results_sha256"]) == 64
    serialized_audit = json.dumps(partial_audit)
    assert "series" not in serialized_audit
    assert "broken_sum" not in serialized_audit
    assert "[2.0, 3.0, 4.0]" not in serialized_audit


def test_failed_calculation_returns_and_audits_a_safe_argument_rule(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    tool = server.create_mcp_server().tools["quant_calculate"]

    result = tool(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "drawdown",
                    "op": "subtract",
                    "args": {
                        "left": {"op": "running_max", "values": [1.0, 1.1]},
                        "right": 1.0,
                    },
                }
            ],
            "return_ids": ["drawdown"],
        }
    )

    assert result == {
        "status": "failed",
        "error": "invalid_operand_object",
        "error_detail": CALCULATION_ERROR_DETAILS["invalid_operand_object"],
        "failed_operation_index": 0,
        "failed_operation_id": "drawdown",
        "failed_operation": "subtract",
        "failed_argument": "left",
    }
    audit = json.loads(explicit_m0.read_text(encoding="utf-8"))
    assert audit["error_detail"] == CALCULATION_ERROR_DETAILS[
        "invalid_operand_object"
    ]
    assert audit["failed_operation_index"] == 0
    assert audit["failed_operation"] == "subtract"
    assert audit["failed_argument"] == "left"
    assert "drawdown" not in json.dumps(audit)
    assert "running_max" not in json.dumps(audit)


def test_calculation_artifact_uses_source_schema_and_commits_120_rows_once(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    source = "exp3/source-q5"
    target = "quant/E3V2T3Q5-T3-R1-M0"
    output_path = (
        "experiments/shared/t3-quant-suite-v1/submissions/"
        "quant_portfolio_analytics.json"
    )
    schema_path = "experiments/shared/t3-quant-suite-v1/output_schema_v1.json"
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: source})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: target})
    monkeypatch.setattr(server, "_WRITABLE_PATHS_MAP", {repo: (output_path,)})
    monkeypatch.setattr(
        server,
        "_CALCULATION_SCHEMA_PATHS_MAP",
        {repo: (schema_path,)},
    )
    monkeypatch.setattr(server, "_validate_proposed_content", lambda **_kwargs: [])
    reads: list[tuple[str, str, str]] = []
    pushed: list[dict] = []
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["rows"],
        "properties": {
            "rows": {
                "type": "array",
                "minItems": 120,
                "maxItems": 120,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["date", "value"],
                    "properties": {
                        "date": {"type": "string"},
                        "value": {"type": "number"},
                    },
                },
            }
        },
    }

    def read_schema(*, branch: str, path: str, repo_name: str) -> str:
        reads.append((branch, path, repo_name))
        return json.dumps(schema)

    def push(**kwargs):
        pushed.append(kwargs)
        return "committed"

    monkeypatch.setattr(server, "_get_strategy_code", read_schema)
    monkeypatch.setattr(server, "_push_strategy_code", push)
    tools = server.create_mcp_server().tools
    calculation = tools["quant_calculate"](
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "series",
                    "op": "cumulative_sum",
                    "args": {"values": [0.001] * 120},
                }
            ],
            "return_ids": ["series"],
        }
    )
    assert calculation["status"] == "succeeded"
    assert calculation["calculation_id"] == "calculation-1"
    assert calculation["results"]["series"]["shape"] == [120]
    assert len(calculation["results"]["series"]["preview"]["first"]) == 3

    wrong_schema = tools["commit_calculation_artifact"](
        branch=target,
        path=output_path,
        schema_path="experiments/shared/t3-quant-suite-v1/input/portfolio_config_v1.json",
        document={
            "value": {
                "$calc": {
                    "calculation_id": "calculation-1",
                    "result_id": "series",
                    "index": 0,
                }
            }
        },
        commit_message="E3T3Q5-02 wrong schema probe",
        repo_name=repo,
    )
    assert wrong_schema == {
        "status": "blocked",
        "error": "schema_path_not_bound_to_calculation_profile",
    }
    assert reads == []
    assert pushed == []

    result = tools["commit_calculation_artifact"](
        branch=target,
        path=output_path,
        schema_path=schema_path,
        document={
            "rows": {
                "$zip_rows": {
                    "date": [f"row-{index:03d}" for index in range(120)],
                    "value": {
                        "$calc": {
                            "calculation_id": "calculation-1",
                            "result_id": "series",
                        }
                    },
                }
            }
        },
        commit_message="E3T3Q5-02 add calculated artifact",
        repo_name=repo,
    )

    assert result["status"] == "success"
    assert result["path"] == output_path
    assert result["artifact_bytes"] > 0
    assert result["calculation_reference_count"] == 1
    assert reads == [(source, schema_path, repo)]
    assert len(pushed) == 1
    committed = json.loads(pushed[0]["code"])
    assert len(committed["rows"]) == 120
    assert committed["rows"][119]["value"] == pytest.approx(0.12)
    raw_audit = explicit_m0.read_text(encoding="utf-8")
    assert "$zip_rows" not in raw_audit
    records = [json.loads(line) for line in raw_audit.splitlines()]
    assert [record["tool"] for record in records] == [
        "quant_calculate",
        "commit_calculation_artifact",
        "commit_calculation_artifact",
    ]
    assert records[-2]["status"] == "blocked"
    assert records[-1]["status"] == "success"
    assert records[-1]["path"] == output_path
    assert records[-1]["artifact_sha256"] == result["artifact_sha256"]
    assert records[-1]["artifact_bytes"] == result["artifact_bytes"]
    assert records[-1]["calculation_reference_count"] == 1
    assert "arguments" not in records[-1]
    assert "content" not in records[-1]


def test_schema_failure_blocks_calculation_artifact_push(
    monkeypatch: pytest.MonkeyPatch,
    explicit_m0: Path,
) -> None:
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "true")
    monkeypatch.setattr(
        server,
        "_CALCULATION_SCHEMA_PATHS_MAP",
        {"bankingscience/BSLAgenticQuantDevLoop": ("schema.json",)},
    )
    monkeypatch.setattr(
        server,
        "_get_strategy_code",
        lambda **_kwargs: json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["required_field"],
            }
        ),
    )
    pushes: list[dict] = []
    monkeypatch.setattr(server, "_push_strategy_code", lambda **kwargs: pushes.append(kwargs))
    tools = server.create_mcp_server().tools
    tools["quant_calculate"](_request())

    result = tools["commit_calculation_artifact"](
        branch="quant/test",
        path="artifact.json",
        schema_path="schema.json",
        document={
            "value": {
                "$calc": {
                    "calculation_id": "calculation-1",
                    "result_id": "answer",
                }
            }
        },
        commit_message="test schema gate",
    )

    assert result["status"] == "validation_failed"
    assert result["error"] == "assembled_document_failed_schema_validation"
    assert pushes == []
