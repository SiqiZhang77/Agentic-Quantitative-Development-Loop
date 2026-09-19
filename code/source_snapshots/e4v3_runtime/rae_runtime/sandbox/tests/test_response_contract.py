import io
import json
import os
import contextlib
import tempfile
from pathlib import Path
import pytest
import logging
import sys

from contract import response_errors
from result_builder import (
    build_response,
    minimal_failed_response,
    FALLBACK_TICKET,
)
from result_io import emit_result
from trace import TraceCollector, now_iso
from exp3.budget import RoleBudgetLedger
from exp3.telemetry import build_manager_star_telemetry, build_single_agent_telemetry
from provider_config import ProviderConfig
from quant_calculator import CALCULATION_ERROR_DETAILS

SANDBOX = Path(__file__).resolve().parents[1]
REPO = SANDBOX.parents[1]  # sandbox -> rae_runtime -> repo root
GATEWAY = REPO / "jira-chatops-gateway"
VENDORED = SANDBOX / "schemas"


def _provider_identity():
    return ProviderConfig(
        provider_mode="company_litellm",
        base_url="http://weles.cs.ucl.ac.uk:4000",
        model_alias="qwen3-coder",
        transport_model="litellm_proxy/qwen3-coder",
        adapter="litellm_chat_completions",
        api_key="test-secret",
        api_key_source="test",
        explicit_provider=True,
    ).identity


def _load(p):
    return json.loads(Path(p).read_text())


# --- drift guard: vendored schemas must equal IW's canonical ones ---
@pytest.mark.parametrize(
    "name", ["runtime_response.schema.json", "runtime_request.schema.json"]
)
def test_vendored_schema_matches_canonical(name):
    assert (VENDORED / name).read_bytes() == (
        GATEWAY / "schemas" / name
    ).read_bytes(), f"{name} drifted from jira-chatops-gateway; re-run sync_schemas.sh"


# --- IW's own examples must validate against the vendored schema (shared fixtures) ---
@pytest.mark.parametrize(
    "ex",
    [
        "runtime_response_success_example.json",
        "runtime_response_failure_example.json",
        "runtime_response_success_non_backtest_example.json",
    ],
)
def test_iw_examples_validate(ex):
    assert response_errors(_load(GATEWAY / "examples" / ex)) == []


# --- RAE builds valid objects on every path ---
def _bt():
    return {
        "metrics": {
            "sharpe_ratio": 1.42,
            "max_drawdown": "-8.7%",
            "total_return": "18.4%",
        },
        "equity_curve": "/workspace/output/artifacts/equity_curve.csv",
    }


def test_build_success_valid():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
        pipeline_out={"artifacts": {"changed_file": "rae_runtime/proxy/strategy.py"}},
        log_ref="/workspace/output/artifacts/run.log",
    )
    assert response_errors(r) == []
    assert r["performance_metrics"]["total_return"] == pytest.approx(0.184)
    assert r["generated_artifacts"]["modified_files"] == [
        "rae_runtime/proxy/strategy.py"
    ]


def test_build_response_records_request_type_from_command():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {"changed_file": "src/data/loader.py"}},
        log_ref="/workspace/output/artifacts/run.log",
        command="refactor",
    )
    assert response_errors(r) == []
    assert r["performance_metrics"] is None
    assert r["execution_summary"]["request_type"] == "refactor"


def test_explicit_pipeline_provider_mode_labels_direct_openai_without_exp3():
    response = build_response(
        run_id="e2-smoke",
        ticket_id="E2SMOKE-3",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={
            "artifacts": {},
            "usage": {
                "model": "gpt-5.6-terra",
                "provider_mode": "openai",
                "calls": 5,
                "prompt_tokens": 17_199,
                "completion_tokens": 1_201,
                "total_tokens": 18_400,
            },
        },
    )

    assert response_errors(response) == []
    assert response["telemetry"]["model_usage"][0]["provider"] == "openai"


def test_build_response_preserves_verified_input_dataset_lineage():
    lineage = [
        {
            "dataset_id": "experiment_2_input",
            "source_kind": "hdfs",
            "original_filename": "experiment_2_input.csv",
            "source_uri": (
                "hdfs://namenode:8020/user/masteruser/quant-experiment-data/"
                "SCRUM-195/experiment_2_input.csv"
            ),
            "run_staged_hdfs_uri": (
                "hdfs://namenode:8020/user/masteruser/quant-runs/"
                "SCRUM-195/run/input/datasets/experiment_2_input.csv"
            ),
            "container_path": (
                "/workspace/input/datasets/experiment_2_input.csv"
            ),
            "format": "csv",
            "read_only": True,
            "size_bytes": 9620,
            "sha256": "a" * 64,
        }
    ]
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        input_datasets=lineage,
    )

    assert response_errors(r) == []
    assert r["input_datasets"] == lineage


def test_build_response_records_text_free_retrieval_audit():
    retrieval_context = {
        "enabled": True,
        "status": "ok",
        "query": "sensitive canonical query",
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
        "candidate_count": 10,
        "memories": [
            {
                "memory_id": "MEM-01",
                "rank": 1,
                "score": 2.5,
                "text": "sensitive historical Jira evidence",
            }
        ],
    }
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        retrieval_context=retrieval_context,
    )

    assert response_errors(r) == []
    assert r["telemetry"]["retrieval"]["enabled"] is True
    assert r["telemetry"]["retrieval"]["delivery_mode"] == "shadow"
    assert r["telemetry"]["retrieval"]["prompt_injected"] is False
    assert r["telemetry"]["retrieval"]["memories"] == [
        {"memory_id": "MEM-01", "rank": 1, "score": 2.5}
    ]
    serialized = json.dumps(r)
    assert "sensitive canonical query" not in serialized
    assert "sensitive historical Jira evidence" not in serialized


def test_build_response_records_generator_only_delivery_without_raw_text():
    retrieval_context = {
        "enabled": True,
        "status": "ok",
        "query": "sensitive canonical query",
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
        "candidate_count": 10,
        "memories": [
            {
                "memory_id": "MEM-01",
                "rank": 1,
                "score": 2.5,
                "text": "sensitive historical Jira evidence",
            }
        ],
    }
    retrieval_delivery = {
        "delivery_mode": "generator_prompt",
        "prompt_injected": True,
        "evidence_text_sha256": "5" * 64,
        "prompt_template_sha256": "6" * 64,
        "injected_memory_ids": ["MEM-01"],
    }
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        retrieval_context=retrieval_context,
        retrieval_delivery=retrieval_delivery,
    )

    assert response_errors(r) == []
    audit = r["telemetry"]["retrieval"]
    assert audit["delivery_mode"] == "generator_prompt"
    assert audit["prompt_injected"] is True
    assert audit["evidence_text_sha256"] == "5" * 64
    assert audit["prompt_template_sha256"] == "6" * 64
    assert audit["injected_memory_ids"] == ["MEM-01"]
    serialized = json.dumps(r)
    assert "sensitive canonical query" not in serialized
    assert "sensitive historical Jira evidence" not in serialized


def test_build_response_marks_c0_retrieval_disabled():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
    )

    assert response_errors(r) == []
    assert r["telemetry"]["retrieval"] == {
        "enabled": False,
        "delivery_mode": "disabled",
        "prompt_injected": False,
    }


def _exp3_telemetry_fixture():
    ledger = RoleBudgetLedger()
    call_id = ledger.begin("manager", input_hash="a" * 64)
    ledger.finish(
        call_id,
        prompt_tokens=2,
        completion_tokens=1,
        latency_seconds=0.25,
        output_hash="b" * 64,
    )
    return build_manager_star_telemetry(
        {
            "status": "failed",
            "failure_phase": "manager_to_architect",
            "budget": ledger.snapshot(),
            "provider_calls": [
                {
                    "call_id": call_id,
                    "role": "manager",
                    "phase": "manager_to_architect",
                    "status": "succeeded",
                    "failure_type": None,
                    "input_sha256": "a" * 64,
                    "output_sha256": "b" * 64,
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                    "latency_seconds": 0.25,
                }
            ],
            "audit": [],
            "isolation_preflight": [
                {
                    "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                    "phase": "pre_orchestration",
                    "passed": True,
                    "manifest_sha256": "c" * 64,
                    "deny_ref_set_sha256": "d" * 64,
                    "repository_count": 1,
                    "denied_ref_count": 6,
                }
            ],
            "manager_acceptance_map": None,
            "topology": {
                "cursor": 1,
                "sequence_complete": False,
                "finalized": False,
            },
        },
        model_alias="qwen3-coder",
        identity_sha256="e" * 64,
        provider_identity=_provider_identity(),
    )


def _exp3_m0_telemetry_fixture():
    ledger = RoleBudgetLedger.for_single_agent()
    return build_single_agent_telemetry(
        {
            "architecture_mode": "single_agent",
            "rag_enabled": False,
            "budget": ledger.snapshot(),
            "provider_calls": [],
            "isolation_preflight": [
                {
                    "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                    "phase": "pre_orchestration",
                    "passed": True,
                    "manifest_sha256": "c" * 64,
                    "deny_ref_set_sha256": "d" * 64,
                    "repository_count": 1,
                    "denied_ref_count": 6,
                }
            ],
        },
        status="succeeded",
        model_alias="qwen3-coder",
        identity_sha256="e" * 64,
        provider_identity=_provider_identity(),
    )


def test_build_response_attaches_versioned_exp3_telemetry_only_when_supplied():
    ordinary = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
    )
    assert "experiment3" not in ordinary["telemetry"]

    telemetry = _exp3_telemetry_fixture()
    e3 = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=telemetry,
    )

    assert response_errors(e3) == []
    assert e3["telemetry"]["experiment3"] == telemetry
    telemetry.clear()
    assert e3["telemetry"]["experiment3"]["rag_enabled"] is False


def test_formal_two_attempt_response_requires_and_validates_attempt_evidence():
    telemetry = _exp3_telemetry_fixture()
    telemetry["shared_budget"]["max_calls"] = 40

    response = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=telemetry,
    )
    assert any("attempts" in error for error in response_errors(response))

    telemetry["attempts"] = [
        {
            "attempt": 1,
            "status": "failed",
            "completion": "runtime_failure",
            "provider_call_count": 1,
            "commit_count": 0,
            "committed_paths": [],
            "answer_capture_status": "partial",
            "saved_item_count": 12,
            "required_item_count": 25,
            "artifact_delivery_status": "not_committed",
            "runtime_exit_status": "error",
            "failure_type": "RuntimeError",
        }
    ]
    response = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=telemetry,
    )
    assert response_errors(response) == []

    telemetry["attempts"][0].update(
        completion="complete",
        saved_item_count=24,
    )
    response = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=telemetry,
    )
    assert response_errors(response)


def test_response_contract_accepts_explicit_m0_provider_telemetry():
    telemetry = _exp3_m0_telemetry_fixture()
    response = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        experiment_telemetry=telemetry,
    )

    assert response_errors(response) == []
    assert response["telemetry"]["experiment3"]["architecture_mode"] == "single_agent"
    assert response["telemetry"]["experiment3"]["role_usage"]["manager"][
        "max_calls"
    ] == 0


def test_emit_preserves_complete_exp2_c1_response_with_rag_enabled():
    telemetry = _exp3_m0_telemetry_fixture()
    telemetry["rag_enabled"] = True
    telemetry["calculation_tool_calls"] = [
        {
            "tool": "quant_calculate",
            "role": "developer",
            "status": "failed",
            "input_sha256": "7" * 64,
            "output_sha256": "8" * 64,
            "latency_seconds": 0.01,
            "operation_count": 25,
            "error": "invalid_operand_object",
            "error_detail": CALCULATION_ERROR_DETAILS["invalid_operand_object"],
            "failed_operation_index": 17,
            "failed_operation": "subtract",
            "failed_argument": "left",
        }
    ]
    retrieval_context = {
        "enabled": True,
        "status": "empty",
        "query": "content excluded from response",
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
        "candidate_count": 0,
        "memories": [],
    }
    retrieval_delivery = {
        "delivery_mode": "generator_prompt",
        "prompt_injected": True,
        "evidence_text_sha256": "5" * 64,
        "prompt_template_sha256": "6" * 64,
        "injected_memory_ids": [],
    }
    response = build_response(
        run_id="E2T3S-C1",
        ticket_id="E2T3-1",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        retrieval_context=retrieval_context,
        retrieval_delivery=retrieval_delivery,
        experiment_telemetry=telemetry,
    )
    assert response_errors(response) == []

    directory = tempfile.mkdtemp()
    path = os.path.join(directory, "result.json")
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        emit_result(response, path)
    persisted = json.loads(Path(path).read_text(encoding="utf-8"))

    assert persisted["telemetry"]["experiment3"]["rag_enabled"] is True
    assert persisted["telemetry"]["retrieval"]["enabled"] is True
    assert persisted["telemetry"]["retrieval"]["prompt_injected"] is True
    assert persisted["telemetry"]["experiment3"]["calculation_tool_calls"][0][
        "failed_argument"
    ] == "left"
    assert "response failed schema validation" not in str(
        persisted["diagnostics"].get("error_message")
    )


def test_response_contract_allows_preregistered_manager_star_rag_cell():
    telemetry = _exp3_telemetry_fixture()
    telemetry["rag_enabled"] = True
    telemetry["role_retrieval_deliveries"] = [
        {
            "attempt": 1,
            "phase": "manager_to_architect",
            "role": "manager",
            "status": "delivered",
            "evidence_text_sha256": "1" * 64,
            "prompt_template_sha256": "2" * 64,
            "injected_memory_count": 1,
            "injected_memory_ids_sha256": "3" * 64,
        }
    ]
    response = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=telemetry,
    )

    assert response_errors(response) == []
    serialized = json.dumps(response["telemetry"]["experiment3"])
    assert "PRIVATE memory text" not in serialized
    assert "MEM-01" not in serialized


def test_response_contract_rejects_malformed_exp3_hash_or_dynamic_role():
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-390",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="bounded failure",
        experiment_telemetry=_exp3_telemetry_fixture(),
    )
    r["telemetry"]["experiment3"]["provider_calls"][0]["role"] = "reviewer"
    r["telemetry"]["experiment3"]["provider_calls"][0]["input_sha256"] = "bad"

    assert response_errors(r)


def test_response_contract_rejects_condition_ambiguous_retrieval_telemetry():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
    )
    r["telemetry"]["retrieval"]["status"] = "ok"

    assert response_errors(r)


def test_response_contract_requires_hashes_for_generator_prompt_delivery():
    retrieval_context = {
        "enabled": True,
        "status": "empty",
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
        "candidate_count": 0,
        "memories": [],
    }
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"artifacts": {}},
        retrieval_context=retrieval_context,
    )
    r["telemetry"]["retrieval"].update(
        {
            "delivery_mode": "generator_prompt",
            "prompt_injected": True,
            "injected_memory_ids": [],
        }
    )

    assert response_errors(r)


def test_build_response_preserves_measured_analysis_results():
    analysis_results = {
        "planner": "llm_structured_plan",
        "executor": "deterministic_standard_library",
        "dataset_summaries": [{"dataset_id": "input", "row_count": 2}],
        "plan": [{"type": "row_count", "dataset_id": "input"}],
        "operations": [
            {
                "operation": "row_count",
                "dataset_id": "input",
                "parameters": {},
                "result": {"row_count": 2},
            }
        ],
    }
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-195",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        pipeline_out={"status": "no_changes", "artifacts": {"no_code_changes": True}},
        backtest={
            "analysis_results": analysis_results,
            "evaluation": {
                "met_criteria": None,
                "confidence": 0.0,
                "criteria_results": [],
                "unrecognised_criteria": [],
                "summary": "Measured analysis complete.",
            },
            "recommended_action": "review",
        },
        command="analysis",
    )

    assert response_errors(r) == []
    assert r["analysis_results"] == analysis_results
    assert r["generated_artifacts"]["modified_files"] == []
    assert r["generated_artifacts"]["new_files"] == []


def test_build_response_omits_request_type_when_command_missing():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
        log_ref="/workspace/output/artifacts/run.log",
    )
    assert response_errors(r) == []
    assert "request_type" not in r["execution_summary"]


def test_build_no_changes_success_has_empty_code_artifacts():
    r = build_response(
        run_id="r1",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
        pipeline_out={
            "status": "no_changes",
            "artifacts": {
                "changed_file": "rae_runtime/proxy/strategy.py",
                "modified_files": ["rae_runtime/proxy/strategy.py"],
                "new_files": ["should_not_survive.py"],
                "no_code_changes": True,
            },
        },
        log_ref="/workspace/output/artifacts/run.log",
    )
    assert response_errors(r) == []
    assert r["execution_summary"]["status"] == "succeeded"
    assert r["execution_summary"]["zero_code_modifications"] is True
    assert r["generated_artifacts"]["modified_files"] == []
    assert r["generated_artifacts"]["new_files"] == []


@pytest.mark.parametrize(
    "outcome,status,code",
    [
        ("timeout", "timeout", "TIMEOUT_REACHED"),
        ("compile", "failed", "STRATEGY_COMPILE_ERROR"),
        ("rate_limit", "failed", "RATE_LIMIT_EXCEEDED"),
        ("quality", "failed", "QUALITY_VALIDATION_FAILED"),
        ("runtime", "failed", "RUNTIME_ERROR"),
    ],
)
def test_build_failure_paths_valid(outcome, status, code):
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome=outcome,
        traces=[],
        error_message="boom",
        log_ref="/workspace/output/artifacts/run.log",
    )
    assert response_errors(r) == []
    assert r["execution_summary"]["status"] == status
    assert r["diagnostics"]["error_code"] == code


def test_minimal_failed_uses_explicit_unknown_ticket():
    r = minimal_failed_response("unknown", FALLBACK_TICKET, "x", outcome="bad_payload")
    assert response_errors(r) == []
    assert r["execution_summary"]["ticket_id"] == "unknown"


def test_nonexistent_raw_log_is_omitted():
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="quality",
        traces=[],
        log_ref="/workspace/output/artifacts/does-not-exist.log",
    )

    assert r["diagnostics"]["raw_log_reference"] is None


def test_quality_failure_preserves_iterate_verdict_and_branch_artifacts():
    review = {
        "evaluation": {
            "met_criteria": False,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "README references a missing LICENSE file.",
        },
        "recommended_action": "iterate",
    }
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-115",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="quality",
        traces=[],
        backtest=review,
        pipeline_out={
            "artifacts": {
                "feature_branch": "quant/SCRUM-115",
                "new_files": ["README.md"],
                "repository_branches": [
                    {
                        "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                        "source_branch": "develop",
                        "target_branch": "quant/SCRUM-115",
                        "branch_action": "reused",
                        "commit_sha": "9a669f936b1a2468",
                        "modified_files": [],
                        "new_files": ["README.md"],
                    }
                ],
            }
        },
        error_message=review["evaluation"]["summary"],
        command="other",
    )

    assert response_errors(r) == []
    assert r["execution_summary"]["status"] == "failed"
    assert r["recommended_action"] == "iterate"
    assert r["generated_artifacts"]["new_files"] == ["README.md"]


def test_non_quality_failure_still_requires_escalation():
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-115",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="runtime",
        traces=[],
        error_message="boom",
    )
    r["recommended_action"] = "iterate"

    assert any("recommended_action" in error for error in response_errors(r))


# --- fail-closed: the gate rejects malformed objects ---
def test_gate_rejects_uppercase_status_and_extra_key():
    good = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
    )
    bad = json.loads(json.dumps(good))
    bad["execution_summary"]["status"] = "SUCCESS"  # uppercase
    bad["surprise"] = 1  # additionalProperties:false
    assert len(response_errors(bad)) >= 2


# --- emit: atomic, byte-identical, under budget, no tmp left behind ---
def test_emit_atomic_and_byte_identical():
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
    )
    d = tempfile.mkdtemp()
    path = os.path.join(d, "result.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_result(r, path)
    last = buf.getvalue().splitlines()[-1]
    assert Path(path).read_text() == last  # byte-identical
    assert len(last) < 4096
    assert not list(Path(d).glob("*.tmp.json"))  # rename cleaned up
    assert json.loads(last)["execution_summary"]["status"] == "succeeded"


# --- emit self-heals an invalid object into a valid failed result rather than emitting junk ---
def test_emit_repairs_invalid_object():
    broken = {"schema_version": "1.0", "run_id": "r"}  # missing required blocks
    d = tempfile.mkdtemp()
    path = os.path.join(d, "result.json")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        emit_result(broken, path)
    out = json.loads(Path(path).read_text())
    assert response_errors(out) == []
    assert out["execution_summary"]["status"] == "failed"


# --- DoD: schema_version mismatch is surfaced, not silently mis-parsed ---
def test_schema_version_mismatch_rejected():
    good = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
    )
    bumped = json.loads(json.dumps(good))
    bumped["schema_version"] = "0.9"
    assert response_errors(bumped), "a wrong schema_version must fail validation"


# --- DoD: strict stream separation. Verbose logging on stderr; stdout = result line only ---
def test_strict_stream_separation(capsys):
    logging.basicConfig(stream=sys.stderr, level=logging.DEBUG, force=True)
    log = logging.getLogger("rae.test")

    log.info("chatty pipeline log that must NOT pollute stdout")
    log.debug("fastmcp/litellm-style noise")
    r = build_response(
        run_id="r",
        ticket_id="SCRUM-46",
        start_time=now_iso(),
        end_time=now_iso(),
        outcome="success",
        traces=[],
        backtest=_bt(),
    )
    emit_result(r, os.path.join(tempfile.mkdtemp(), "result.json"))
    log.info("a log line AFTER emit, still on stderr")

    captured = capsys.readouterr()
    stdout_lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(stdout_lines) == 1, (
        f"stdout must hold only the result line, got {len(stdout_lines)}"
    )
    assert json.loads(stdout_lines[-1])["execution_summary"]["status"] == "succeeded"
    assert "chatty pipeline log" in captured.err
