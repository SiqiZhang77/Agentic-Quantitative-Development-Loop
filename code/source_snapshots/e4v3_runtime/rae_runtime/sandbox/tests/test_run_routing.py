"""run.py routing (general use case): only ``strategy_type == backtest`` runs the
backtester. Every other type still creates a branch + commits via the pipeline,
and is scored by the review agent instead of the engine, so performance_metrics
stays null while evaluation/recommended_action are still populated.
"""
import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch
import unittest.mock

import pytest
from provider_config import ProviderConfig

_SANDBOX = Path(__file__).parent.parent
for _p in (str(_SANDBOX), str(_SANDBOX / "proxy")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from contract import response_errors

_MOCKED_MODULES = ("mcp_client", "pipeline", "github_client", "llm_client", "dotenv")
_patches = unittest.mock.patch.dict(
    "sys.modules",
    {mod: MagicMock() for mod in _MOCKED_MODULES if mod not in sys.modules},
)
_patches.start()
import run
_patches.stop()

_EXAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "runtime_request_example.json").read_text()
)

_PIPELINE_OUT = {
    "status": "succeeded",
    "summary": "committed",
    "artifacts": {"feature_branch": "quant/SCRUM-46", "modified_files": ["x.py"], "new_files": []},
}
_BACKTEST_OUT = {
    "summary": "bt",
    "metrics": {"sharpe_ratio": 1.2, "max_drawdown": "-8.0%", "total_return": "45.0%"},
    "evaluation": {
        "met_criteria": True, "confidence": 1.0, "criteria_results": [],
        "unrecognised_criteria": [], "summary": "ok",
    },
    "recommended_action": "accept",
}

_TEST_PROVIDER_CONFIG = ProviderConfig(
    provider_mode="company_litellm",
    base_url="http://weles.cs.ucl.ac.uk:4000",
    model_alias="qwen3-coder",
    transport_model="litellm_proxy/qwen3-coder",
    adapter="litellm_chat_completions",
    api_key="test-secret",
    api_key_source="test",
    explicit_provider=True,
)


def _boundary_mock():
    boundary = MagicMock()
    boundary.provider_config = _TEST_PROVIDER_CONFIG
    boundary.identity_sha256 = "e" * 64
    boundary.rag_enabled = False
    return boundary


def _payload_with(strategy_type: str) -> dict:
    p = json.loads(json.dumps(_EXAMPLE))
    p["execution_objectives"]["strategy_type"] = strategy_type
    return p


_REVIEW_OUT = {
    "evaluation": {
        "met_criteria": True, "confidence": 0.0, "criteria_results": [],
        "unrecognised_criteria": [], "summary": "Automated review (advisory): ok",
    },
    "recommended_action": "accept",
}


def _run_main(
    payload: dict,
    monkeypatch,
    *,
    pipeline_mock=None,
    backtest_fn=None,
    review_out=None,
    loop_side_effect=None,
    exp3_boundary_factory=None,
    exp3_production_role_runner=None,
    exp3_audit_directory=None,
    exp3_clock=None,
):
    """Drive run.main() with the agents + I/O sinks mocked, returning the exit
    code and the response dict handed to emit_result (captured, not written)."""
    monkeypatch.delenv("RAE_LEGACY_INPUT", raising=False)

    captured = {}

    def _capture(resp, path):
        captured["resp"] = resp

    if pipeline_mock is None:
        pipeline_mock = MagicMock(return_value=_PIPELINE_OUT)
    if backtest_fn is None:
        backtest_fn = MagicMock(return_value=_BACKTEST_OUT)
    review_fn = MagicMock(return_value=review_out or _REVIEW_OUT)
    # run_iteration_loop is imported lazily inside main(); patch it on its module.
    import iteration_loop
    loop_mock = MagicMock(
        side_effect=loop_side_effect
        or (
            lambda pl, edit, bt, tracer=None: {
                "pipeline_out": edit(pl),
                "backtest": bt({**pl, "iteration": 1}),
            }
        )
    )

    with patch.object(run, "run_pipeline_auto", pipeline_mock), \
         patch.object(run, "_select_backtest_fn", return_value=backtest_fn), \
         patch("review_agent.review_general_task", review_fn), \
         patch.object(run, "emit_result", _capture), \
         patch.object(run, "emit_progress_event", lambda **k: None), \
         patch.object(run, "get_progress_events_path", lambda p: None), \
         patch.object(iteration_loop, "run_iteration_loop", loop_mock), \
         patch("sys.stdin", StringIO(json.dumps(payload))):
        code = run.main(
            exp3_boundary_factory=exp3_boundary_factory,
            exp3_production_role_runner=exp3_production_role_runner,
            exp3_audit_directory=exp3_audit_directory,
            exp3_clock=exp3_clock,
        )
    return code, captured["resp"], pipeline_mock, backtest_fn, review_fn


def test_budget_failure_response_preserves_usage_and_committed_artifacts(monkeypatch):
    def fail_after_commit(payload, edit, evaluate, tracer=None):
        exc = run.BudgetExceeded("token budget (200000) exceeded")
        exc.pipeline_out = {
            "status": "succeeded",
            "usage": {
                "model": "qwen3-coder",
                "calls": 23,
                "prompt_tokens": 198000,
                "completion_tokens": 3001,
                "total_tokens": 201001,
                "cost_usd": None,
            },
            "artifacts": {
                "modified_files": ["src/example.py"],
                "new_files": ["tests/test_example.py"],
                "repository_branches": [
                    {
                        "alias": "BSLAgenticQuantDevLoop",
                        "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                        "source_branch": "exp2/source",
                        "target_branch": "quant/SCRUM-46",
                        "branch_action": "created",
                        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
                        "modified_files": ["src/example.py"],
                        "new_files": ["tests/test_example.py"],
                    }
                ],
            },
        }
        raise exc

    code, written, _, _, _ = _run_main(
        _payload_with("refactor"),
        monkeypatch,
        loop_side_effect=fail_after_commit,
    )

    assert code == 1
    assert written["diagnostics"]["error_code"] == "RATE_LIMIT_EXCEEDED"
    assert written["generated_artifacts"]["modified_files"] == ["src/example.py"]
    assert written["generated_artifacts"]["new_files"] == ["tests/test_example.py"]
    assert written["generated_artifacts"]["branch_name"] == "quant/SCRUM-46"
    assert written["generated_artifacts"]["repository_branches"][0][
        "commit_sha"
    ] == "0123456789abcdef0123456789abcdef01234567"
    assert written["telemetry"]["model_usage"] == [
        {
            "model_name": "qwen3-coder",
            "provider": "litellm",
            "prompt_tokens": 198000,
            "completion_tokens": 3001,
            "total_tokens": 201001,
        }
    ]
    assert written["telemetry"]["retry_count"] == 23


def test_absent_architecture_mode_keeps_legacy_router_path(monkeypatch):
    payload = _payload_with("refactor")

    with patch("exp3.router.run_manager_star_runtime") as manager_star:
        code, _, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
        )

    assert code == 0
    manager_star.assert_not_called()
    pipeline_mock.assert_called_once()
    backtest_fn.assert_not_called()
    review_fn.assert_called_once()


def test_explicit_single_agent_keeps_legacy_router_path(monkeypatch):
    payload = _payload_with("refactor")
    payload["architecture_mode"] = "single_agent"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False

    boundary = _boundary_mock()
    zero_role = {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "failures": 0,
        "latency_seconds": 0.0,
        "pending_calls": 0,
        "records": [],
        "configured_max_calls": 0,
        "transferred_calls": 0,
        "max_calls": 0,
        "max_tokens": 0,
        "over_budget": False,
    }
    developer = {
        **zero_role,
        "configured_max_calls": 15,
        "max_calls": 15,
        "max_tokens": 200_000,
    }
    boundary.snapshot.return_value = {
        "architecture_mode": "single_agent",
        "rag_enabled": False,
        "budget": {
            "shared": dict(developer),
            "roles": {
                "manager": dict(zero_role),
                "architect": dict(zero_role),
                "developer": dict(developer),
            },
        },
        "provider_calls": [],
        "isolation_preflight": [
            {
                "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                "phase": "pre_orchestration",
                "passed": True,
                "manifest_sha256": "a" * 64,
                "deny_ref_set_sha256": "b" * 64,
                "repository_count": 1,
                "denied_ref_count": 6,
                "source_presence_checked": True,
                "current_target_absence_checked": True,
            }
        ],
    }
    boundary_factory = MagicMock(return_value=boundary)
    with patch("exp3.router.run_manager_star_runtime") as manager_star:
        code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 0
    manager_star.assert_not_called()
    boundary_factory.assert_called_once_with(run._normalise_request(payload))
    boundary.start.assert_called_once()
    pipeline_mock.assert_called_once()
    assert pipeline_mock.call_args.kwargs["exp3_provider_boundary"] is boundary
    assert written["telemetry"]["experiment3"]["architecture_mode"] == "single_agent"
    backtest_fn.assert_not_called()
    review_fn.assert_called_once()


def test_explicit_single_agent_fails_before_pipeline_without_boundary_config(
    monkeypatch,
):
    payload = _payload_with("refactor")
    payload["architecture_mode"] = "single_agent"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False
    monkeypatch.delenv("E3_NEGATIVE_REF_MANIFEST_PATH", raising=False)
    pipeline_mock = MagicMock(return_value=_PIPELINE_OUT)

    code, written, _, _, _ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=pipeline_mock,
    )

    assert code == 1
    pipeline_mock.assert_not_called()
    assert "E3_NEGATIVE_REF_MANIFEST_PATH" in written["diagnostics"]["error_message"]


def test_manager_star_failure_never_falls_through_to_legacy_agents(monkeypatch):
    payload = _payload_with("refactor")
    payload["architecture_mode"] = "manager_star"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    unavailable = RuntimeError("manager-star adapter unavailable")

    boundary = _boundary_mock()
    boundary_factory = MagicMock(return_value=boundary)
    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=unavailable,
    ) as manager_star:
        code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 1
    boundary_factory.assert_called_once_with(run._normalise_request(payload))
    boundary.start.assert_not_called()
    manager_star.assert_called_once()
    assert manager_star.call_args.kwargs["production_boundary"] is boundary
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    review_fn.assert_not_called()
    assert written["execution_summary"]["status"] == "failed"
    assert written["diagnostics"]["error_message"] == (
        "RuntimeError: manager-star adapter unavailable"
    )


def test_injected_manager_star_result_emits_versioned_telemetry_without_legacy_loop(
    monkeypatch,
):
    from exp3.budget import RoleBudgetLedger

    payload = _payload_with("refactor")
    payload["architecture_mode"] = "manager_star"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False
    payload["iteration_controls"].update(
        allow_iteration=False,
        max_iterations=1,
        max_failed_iterations=1,
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    manager_star_result = {
        "status": "succeeded",
        "failure_phase": None,
        "manager_final": {
            "summary": "Complete; external blinded evaluation remains separate."
        },
        "developer_result": {
            "implementation": [{"path": "src/claimed_only.py", "summary": "done"}]
        },
        "committed_paths": ["src/e3_fixture.py"],
        "audit": [],
        "provider_calls": [],
        "isolation_preflight": [
            {
                "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                "phase": "pre_orchestration",
                "passed": True,
                "manifest_sha256": "a" * 64,
                "deny_ref_set_sha256": "b" * 64,
                "repository_count": 1,
                "denied_ref_count": 6,
            }
        ],
        "manager_acceptance_map": {
            "approved_count": 1,
            "evaluated_count": 1,
            "passed_count": 1,
            "complete": True,
            "sha256": "c" * 64,
        },
        "budget": RoleBudgetLedger().snapshot(),
        "topology": {"cursor": 5, "sequence_complete": True, "finalized": True},
    }

    boundary = _boundary_mock()
    boundary_factory = MagicMock(return_value=boundary)
    with patch(
        "exp3.router.run_manager_star_runtime",
        return_value=manager_star_result,
    ) as manager_star:
        code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 0
    manager_star.assert_called_once()
    assert manager_star.call_args.kwargs["production_boundary"] is boundary
    boundary.start.assert_not_called()
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    review_fn.assert_not_called()
    assert written["telemetry"]["experiment3"]["schema_version"] == (
        "exp3-runtime-telemetry-v4"
    )
    assert written["telemetry"]["experiment3"]["rag_enabled"] is False
    assert written["generated_artifacts"]["modified_files"] == ["src/e3_fixture.py"]
    assert written["evaluation"]["met_criteria"] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_phase1_manager_star_seam_fails_closed_without_any_legacy_call(
    monkeypatch,
    enabled,
):
    payload = _payload_with("refactor")
    payload["architecture_mode"] = "manager_star"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False
    payload["iteration_controls"].update(
        allow_iteration=False,
        max_iterations=1,
        max_failed_iterations=1,
    )
    if enabled:
        monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    else:
        monkeypatch.delenv("RAE_ENABLE_MANAGER_STAR", raising=False)

    code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
        payload,
        monkeypatch,
    )

    assert code == 1
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    review_fn.assert_not_called()
    expected_error = (
        "manager_star is disabled"
        if not enabled
        else "E3_NEGATIVE_REF_MANIFEST_PATH"
    )
    assert expected_error in written["diagnostics"]["error_message"]


def test_manager_star_uses_shared_boundary_factory_without_prestarting_it(
    monkeypatch,
):
    payload = _payload_with("refactor")
    payload["architecture_mode"] = "manager_star"
    payload["execution_objectives"].setdefault("parsed_task_parameters", {})[
        "rag_enabled"
    ] = False
    payload["iteration_controls"].update(
        allow_iteration=False,
        max_iterations=1,
        max_failed_iterations=1,
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    boundary = _boundary_mock()
    boundary_factory = MagicMock(return_value=boundary)

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=RuntimeError("stop after dispatch"),
    ) as manager_star:
        code, _, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 1
    boundary_factory.assert_called_once_with(run._normalise_request(payload))
    boundary.start.assert_not_called()
    manager_star.assert_called_once()
    assert manager_star.call_args.kwargs["production_boundary"] is boundary
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    review_fn.assert_not_called()


def test_backtest_request_runs_the_engine(monkeypatch):
    code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
        _payload_with("backtest"), monkeypatch)
    assert code == 0
    backtest_fn.assert_called()  # engine was driven
    review_fn.assert_not_called()  # metrics come from the engine, not a review
    assert written["performance_metrics"]["sharpe_ratio"] == 1.2
    assert written["recommended_action"] == "accept"
    assert written["execution_summary"]["request_type"] == "backtest"


def test_backtest_failure_before_metrics_preserves_request_type(monkeypatch):
    backtest_fn = MagicMock(
        side_effect=RuntimeError("backtest failed before producing metrics")
    )

    code, written, pipeline_mock, _, review_fn = _run_main(
        _payload_with("backtest"),
        monkeypatch,
        backtest_fn=backtest_fn,
    )

    assert code == 1
    pipeline_mock.assert_called()
    backtest_fn.assert_called_once()
    review_fn.assert_not_called()
    assert written["execution_summary"]["status"] == "failed"
    assert written["execution_summary"]["request_type"] == "backtest"
    assert written["performance_metrics"] is None
    assert written["diagnostics"]["error_code"] == "RUNTIME_ERROR"
    assert (
        written["diagnostics"]["error_message"]
        == "RuntimeError: backtest failed before producing metrics"
    )


def test_general_request_commits_but_skips_backtest(monkeypatch):
    for stype in ("refactor", "ingestion", "analysis", "other"):
        code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
            _payload_with(stype), monkeypatch)
        assert code == 0, stype
        # Code agent still ran (branch + commit), backtester did not.
        pipeline_mock.assert_called()
        backtest_fn.assert_not_called()
        assert written["performance_metrics"] is None, stype
        assert written["generated_artifacts"]["modified_files"] == ["x.py"], stype
        # request_type distinguishes "never ran a backtest" from a backtest run
        # whose performance_metrics is null because it failed before producing
        # metrics (see test_backtest_request_runs_the_engine's counterpart above).
        assert written["execution_summary"]["request_type"] == stype, stype


def test_general_request_is_scored_by_the_review_agent(monkeypatch):
    # Without an evaluator a general request could only ever be single-shot; the
    # review agent supplies the verdict the iteration loop runs on.
    for stype in ("refactor", "ingestion", "analysis", "other"):
        code, written, _, backtest_fn, review_fn = _run_main(
            _payload_with(stype), monkeypatch)
        assert code == 0, stype
        review_fn.assert_called()
        backtest_fn.assert_not_called()
        assert written["recommended_action"] == "accept", stype
        assert "advisory" in written["evaluation"]["summary"], stype
        # A review verdict carries no metrics and must not fake any.
        assert written["performance_metrics"] is None, stype


def test_general_request_requiring_revision_fails_with_branch_evidence(monkeypatch):
    review_out = {
        "evaluation": {
            "met_criteria": False,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "README.md contains placeholder-only content.",
        },
        "recommended_action": "iterate",
    }

    code, written, _, backtest_fn, review_fn = _run_main(
        _payload_with("other"), monkeypatch, review_out=review_out
    )

    assert code == 1
    review_fn.assert_called_once()
    backtest_fn.assert_not_called()
    assert written["execution_summary"]["status"] == "failed"
    assert written["diagnostics"]["error_code"] == "QUALITY_VALIDATION_FAILED"
    assert "placeholder-only" in written["diagnostics"]["error_message"]
    assert written["generated_artifacts"]["branch_name"] == "quant/SCRUM-46"
    assert written["generated_artifacts"]["modified_files"] == ["x.py"]


def test_general_review_or_escalation_is_not_reported_as_success(monkeypatch):
    for action in ("review", "escalate"):
        review_out = {
            "evaluation": {
                "met_criteria": None,
                "confidence": 0.0,
                "criteria_results": [],
                "unrecognised_criteria": [],
                "summary": f"Reviewer returned {action}.",
            },
            "recommended_action": action,
        }

        code, written, _, _, _ = _run_main(
            _payload_with("refactor"), monkeypatch, review_out=review_out
        )

        assert code == 1
        assert written["diagnostics"]["error_code"] == "QUALITY_VALIDATION_FAILED"


def test_zero_code_request_reports_findings_without_touching_the_repo(monkeypatch):
    payload = _payload_with("analysis")
    payload["execution_objectives"]["zero_code_modifications"] = True

    review_out = {
        "evaluation": {
            "met_criteria": None, "confidence": 0.0, "criteria_results": [],
            "unrecognised_criteria": [], "summary": "Read-only analysis (advisory): ok",
        },
        "recommended_action": "review",
    }
    analyse_fn = MagicMock(return_value=review_out)

    with patch("review_agent.analyse_read_only_task", analyse_fn):
        code, written, pipeline_mock, backtest_fn, _ = _run_main(payload, monkeypatch)

    assert code == 0
    analyse_fn.assert_called()
    # Nothing was branched, committed, or backtested.
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    assert written["generated_artifacts"]["modified_files"] == []
    assert written["execution_summary"]["zero_code_modifications"] is True
    assert written["recommended_action"] == "review"
    assert "Read-only analysis" in written["evaluation"]["summary"]


def test_request_without_a_repository_defaults_to_current_repo(monkeypatch):
    payload = _payload_with("other")
    payload["repository_details"] = None

    code, written, pipeline_mock, backtest_fn, _ = _run_main(payload, monkeypatch)

    assert code == 0
    pipeline_mock.assert_called_once()
    backtest_fn.assert_not_called()
    normalised = pipeline_mock.call_args.args[0]
    assert normalised["repositories"][0]["repo_full_name"] == (
        "bankingscience/BSLAgenticQuantDevLoop"
    )
    assert normalised["code_free"] is False
    assert written["generated_artifacts"]["modified_files"] == ["x.py"]


def test_zero_code_backtest_submits_the_baseline_without_editing_it(monkeypatch):
    # "Backtest what's already there" — the declared form of the direct-backtest
    # canary, which used to be triggered by sniffing marker strings in the prose.
    payload = _payload_with("backtest")
    payload["execution_objectives"]["zero_code_modifications"] = True

    code, written, pipeline_mock, backtest_fn, _ = _run_main(payload, monkeypatch)

    assert code == 0
    backtest_fn.assert_called()  # the engine still ran
    pipeline_mock.assert_not_called()  # but nothing was rewritten first
    assert written["generated_artifacts"]["modified_files"] == []
    assert written["performance_metrics"]["sharpe_ratio"] == 1.2


def test_force_backtest_env_overrides_strategy_type(monkeypatch):
    monkeypatch.setenv("RAE_FORCE_BACKTEST", "true")
    code, written, _, backtest_fn, review_fn = _run_main(
        _payload_with("refactor"), monkeypatch)
    assert code == 0
    backtest_fn.assert_called()  # forced on despite non-backtest strategy_type
    review_fn.assert_not_called()


def _verified_retrieval_context() -> dict:
    return {
        "enabled": True,
        "status": "ok",
        "query": "Summary: complete the frozen quantitative task",
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
                "source_ticket_id": "SCRUM-4",
                "source_type": "comment",
                "source_id": "comment:10001",
                "source_timestamp": "2026-05-01T00:00:00+00:00",
                "rank": 1,
                "score": 2.5,
                "text": "Historical implementation note for the task.",
            }
        ],
    }


def _verified_retrieval_delivery(*, evidence_hash: str = "c" * 64) -> dict:
    return {
        "delivery_mode": "generator_prompt",
        "prompt_injected": True,
        "evidence_text_sha256": evidence_hash,
        "prompt_template_sha256": "d" * 64,
        "injected_memory_ids": ["MEM-01"],
    }


def _two_attempt_payload(mode: str, *, rag_enabled: bool = False) -> dict:
    payload = _payload_with("other")
    payload["architecture_mode"] = mode
    parameters = payload["execution_objectives"].setdefault(
        "parsed_task_parameters", {}
    )
    parameters["rag_enabled"] = rag_enabled
    if rag_enabled:
        parameters["rag_top_k"] = 5
        payload["retrieval_context"] = _verified_retrieval_context()
    payload["execution_objectives"]["resource_path"] = "src/e3_fixture.py"
    payload["execution_objectives"]["target_path"] = "src/e3_fixture.py"
    payload["iteration_controls"].update(
        allow_iteration=True,
        max_iterations=2,
        max_failed_iterations=2,
        max_agent_turns=20,
        max_token_budget_per_run=700_000,
    )
    return payload


def _attempt_accounting(
    mode: str,
    *,
    commit_count: int = 0,
    rag_enabled: bool = False,
    provider_call_count: int = 0,
) -> dict:
    from exp3.budget import RoleBudgetLedger

    ledger = (
        RoleBudgetLedger.for_single_agent()
        if mode == "single_agent"
        else RoleBudgetLedger()
    )
    result = {
        "architecture_mode": mode,
        "rag_enabled": rag_enabled,
        "provider_identity": _TEST_PROVIDER_CONFIG.identity,
        "identity_sha256": "e" * 64,
        "budget": ledger.snapshot(),
        "provider_calls": [],
        "isolation_preflight": [
            {
                "schema_version": "exp3-negative-ref-preflight-evidence-v1",
                "phase": "pre_orchestration",
                "passed": True,
                "manifest_sha256": "a" * 64,
                "deny_ref_set_sha256": "b" * 64,
                "repository_count": 1,
                "denied_ref_count": 6,
            }
        ],
        "calculation_tool_calls": [],
        "commit_count": commit_count,
    }
    if provider_call_count:
        role = "developer" if mode == "single_agent" else "manager"
        phase = "single_agent" if mode == "single_agent" else "manager_to_architect"
        records = []
        for number in range(1, provider_call_count + 1):
            records.append(
                {
                    "call_id": f"exp3-{number:06d}",
                    "role": role,
                    "phase": phase,
                    "status": "succeeded",
                    "failure_type": None,
                    "input_sha256": "1" * 64,
                    "output_sha256": "2" * 64,
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "total_tokens": 3,
                    "latency_seconds": 0.1,
                }
            )
        for usage in (result["budget"]["shared"], result["budget"]["roles"][role]):
            usage.update(
                calls=provider_call_count,
                prompt_tokens=2 * provider_call_count,
                completion_tokens=provider_call_count,
                total_tokens=3 * provider_call_count,
                latency_seconds=round(0.1 * provider_call_count, 6),
            )
        result["provider_calls"] = records
    return result


def _manager_attempt_result(*, committed: bool) -> dict:
    from exp3.budget import RoleBudgetLedger

    path = "src/e3_fixture.py"
    return {
        "status": "succeeded",
        "failure_phase": None,
        "manager_final": {"summary": "attempt finished"},
        "developer_result": {},
        "committed_paths": [path] if committed else [],
        "commit_count": 1 if committed else 0,
        "audit": [],
        "provider_calls": [],
        "calculation_tool_calls": [],
        "isolation_preflight": _attempt_accounting("manager_star")[
            "isolation_preflight"
        ],
        "manager_acceptance_map": None,
        "budget": RoleBudgetLedger().snapshot(),
        "topology": {"cursor": 5, "sequence_complete": True, "finalized": True},
    }


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_two_attempt_m0_retries_only_when_required_commit_is_missing(monkeypatch):
    payload = _two_attempt_payload("single_agent")
    first_accounting = _attempt_accounting("single_agent", commit_count=0)
    second_accounting = _attempt_accounting("single_agent", commit_count=1)
    first = {
        "status": "succeeded",
        "summary": "no commit",
        "usage": {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "artifacts": {"modified_files": [], "new_files": []},
        "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
        "experiment3_provider_accounting": first_accounting,
    }
    second = {
        "status": "succeeded",
        "summary": "committed",
        "usage": {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "artifacts": {"modified_files": [], "new_files": ["src/e3_fixture.py"]},
        "diagnostics": {
            "retry_safe": {"commit": {"paths": ["src/e3_fixture.py"]}}
        },
        "experiment3_provider_accounting": second_accounting,
    }
    boundaries = [_boundary_mock(), _boundary_mock()]
    boundary_factory = MagicMock(side_effect=boundaries)
    pipeline = MagicMock(side_effect=[first, second])

    code, written, _, backtest_fn, review_fn = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=pipeline,
        exp3_boundary_factory=boundary_factory,
    )

    assert code == 0
    assert pipeline.call_count == 2
    assert boundary_factory.call_count == 2
    assert [
        call.args[0]["_experiment_attempt"]["number"]
        for call in boundary_factory.call_args_list
    ] == [1, 2]
    for boundary, pipeline_call in zip(
        boundaries, pipeline.call_args_list, strict=True
    ):
        attempt_payload = pipeline_call.args[0]
        boundary.start.assert_called_once_with(attempt_payload)
    review_fn.assert_not_called()
    backtest_fn.assert_not_called()
    telemetry = written["telemetry"]["experiment3"]
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_commit_missing",
        "complete",
    ]
    assert telemetry["shared_budget"]["max_calls"] == 40
    assert telemetry["shared_budget"]["max_tokens"] == 700_000


@pytest.mark.parametrize("rag_enabled", [False, True], ids=["C0", "C1"])
def test_e2_rag_contract_runs_two_complete_single_agent_attempts(
    monkeypatch, rag_enabled
):
    payload = _two_attempt_payload("single_agent", rag_enabled=rag_enabled)
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["formal_execution_contract"] = "exp2_single_agent_rag_v1"
    parameters["rag_top_k"] = 5

    first_accounting = _attempt_accounting(
        "single_agent",
        commit_count=0,
        rag_enabled=rag_enabled,
        provider_call_count=1,
    )
    second_accounting = _attempt_accounting(
        "single_agent",
        commit_count=1,
        rag_enabled=rag_enabled,
        provider_call_count=1,
    )
    first = {
        "status": "succeeded",
        "summary": "first complete attempt did not commit",
        "usage": {
            "calls": 1,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
        "artifacts": {"modified_files": [], "new_files": []},
        "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
        "experiment3_provider_accounting": first_accounting,
    }
    second = {
        "status": "succeeded",
        "summary": "second complete attempt committed",
        "usage": {
            "calls": 1,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
        },
        "artifacts": {"modified_files": [], "new_files": ["src/e3_fixture.py"]},
        "diagnostics": {
            "retry_safe": {"commit": {"paths": ["src/e3_fixture.py"]}}
        },
        "experiment3_provider_accounting": second_accounting,
    }
    boundaries = [_boundary_mock(), _boundary_mock()]
    boundary_factory = MagicMock(side_effect=boundaries)
    responses = iter([first, second])

    def run_attempt(attempt_payload, **_kwargs):
        if rag_enabled:
            attempt_payload["_retrieval_delivery"] = _verified_retrieval_delivery()
        return next(responses)

    pipeline = MagicMock(side_effect=run_attempt)

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=pipeline,
        exp3_boundary_factory=boundary_factory,
    )

    assert code == 0
    assert pipeline.call_count == 2
    assert boundary_factory.call_count == 2
    telemetry = written["telemetry"]["experiment3"]
    assert response_errors(written) == []
    assert telemetry["architecture_mode"] == "single_agent"
    assert telemetry["rag_enabled"] is rag_enabled
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_commit_missing",
        "complete",
    ]
    attempt_payloads = [call.args[0] for call in pipeline.call_args_list]
    if rag_enabled:
        assert all(
            item["retrieval_context"]["enabled"] is True
            for item in attempt_payloads
        )
        assert written["telemetry"]["retrieval"]["delivery_mode"] == "generator_prompt"
        assert written["telemetry"]["retrieval"]["prompt_injected"] is True
        assert written["telemetry"]["retrieval"]["injected_memory_ids"] == [
            "MEM-01"
        ]
    else:
        assert all(item.get("retrieval_context") is None for item in attempt_payloads)
        assert written["telemetry"]["retrieval"] == {
            "enabled": False,
            "delivery_mode": "disabled",
            "prompt_injected": False,
        }


def test_e2_rag_contract_fails_closed_when_attempt_delivery_receipt_is_missing(
    monkeypatch,
):
    payload = _two_attempt_payload("single_agent", rag_enabled=True)
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["formal_execution_contract"] = "exp2_single_agent_rag_v1"
    first = {
        "status": "succeeded",
        "summary": "first attempt did not commit",
        "usage": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        "artifacts": {"modified_files": [], "new_files": []},
        "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
        "experiment3_provider_accounting": _attempt_accounting(
            "single_agent",
            commit_count=0,
            rag_enabled=True,
            provider_call_count=1,
        ),
    }
    second = {
        "status": "succeeded",
        "summary": "second attempt committed",
        "usage": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        "artifacts": {"modified_files": [], "new_files": ["src/e3_fixture.py"]},
        "diagnostics": {"retry_safe": {"commit": {"paths": ["src/e3_fixture.py"]}}},
        "experiment3_provider_accounting": _attempt_accounting(
            "single_agent",
            commit_count=1,
            rag_enabled=True,
            provider_call_count=1,
        ),
    }

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=MagicMock(side_effect=[first, second]),
        exp3_boundary_factory=MagicMock(side_effect=[_boundary_mock(), _boundary_mock()]),
    )

    assert code == 1
    assert "did not preserve generator prompt-delivery evidence" in written[
        "diagnostics"
    ]["error_message"]
    assert written["telemetry"]["retrieval"]["delivery_mode"] == "shadow"
    assert written["telemetry"]["retrieval"]["prompt_injected"] is False


def test_e2_rag_contract_fails_closed_when_attempt_delivery_receipts_change(
    monkeypatch,
):
    payload = _two_attempt_payload("single_agent", rag_enabled=True)
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["formal_execution_contract"] = "exp2_single_agent_rag_v1"
    responses = iter(
        [
            {
                "status": "succeeded",
                "summary": "first attempt did not commit",
                "usage": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                "artifacts": {"modified_files": [], "new_files": []},
                "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
                "experiment3_provider_accounting": _attempt_accounting(
                    "single_agent",
                    commit_count=0,
                    rag_enabled=True,
                    provider_call_count=1,
                ),
            },
            {
                "status": "succeeded",
                "summary": "second attempt committed",
                "usage": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                "artifacts": {"modified_files": [], "new_files": ["src/e3_fixture.py"]},
                "diagnostics": {"retry_safe": {"commit": {"paths": ["src/e3_fixture.py"]}}},
                "experiment3_provider_accounting": _attempt_accounting(
                    "single_agent",
                    commit_count=1,
                    rag_enabled=True,
                    provider_call_count=1,
                ),
            },
        ]
    )
    call_number = 0

    def run_attempt(attempt_payload, **_kwargs):
        nonlocal call_number
        call_number += 1
        attempt_payload["_retrieval_delivery"] = _verified_retrieval_delivery(
            evidence_hash=("c" if call_number == 1 else "e") * 64
        )
        return next(responses)

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=MagicMock(side_effect=run_attempt),
        exp3_boundary_factory=MagicMock(side_effect=[_boundary_mock(), _boundary_mock()]),
    )

    assert code == 1
    assert "changed between model-using attempts" in written["diagnostics"][
        "error_message"
    ]


def test_e2_rag_contract_keeps_first_receipt_when_second_attempt_reuses_answers(
    monkeypatch,
):
    payload = _two_attempt_payload("single_agent", rag_enabled=True)
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["formal_execution_contract"] = "exp2_single_agent_rag_v1"
    first = {
        "status": "succeeded",
        "summary": "first model-using attempt saved answers but did not finish",
        "usage": {"calls": 1, "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        "artifacts": {"modified_files": [], "new_files": []},
        "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
        "experiment3_provider_accounting": _attempt_accounting(
            "single_agent",
            commit_count=0,
            rag_enabled=True,
            provider_call_count=1,
        ),
    }
    second = {
        "status": "succeeded",
        "summary": "second zero-call attempt reused the saved answer artifact",
        "usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        "artifacts": {"modified_files": [], "new_files": ["src/e3_fixture.py"]},
        "diagnostics": {"retry_safe": {"commit": {"paths": ["src/e3_fixture.py"]}}},
        "experiment3_provider_accounting": _attempt_accounting(
            "single_agent", commit_count=1, rag_enabled=True
        ),
    }
    responses = iter([first, second])
    call_number = 0

    def run_attempt(attempt_payload, **_kwargs):
        nonlocal call_number
        call_number += 1
        if call_number == 1:
            attempt_payload["_retrieval_delivery"] = _verified_retrieval_delivery()
        return next(responses)

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=MagicMock(side_effect=run_attempt),
        exp3_boundary_factory=MagicMock(
            side_effect=[_boundary_mock(), _boundary_mock()]
        ),
    )

    assert code == 0
    assert response_errors(written) == []
    assert written["telemetry"]["retrieval"]["delivery_mode"] == "generator_prompt"
    assert written["telemetry"]["retrieval"]["prompt_injected"] is True
    assert [
        item["provider_call_count"]
        for item in written["telemetry"]["experiment3"]["attempts"]
    ] == [1, 0]


def test_two_attempt_m1_runs_two_complete_topologies_then_stops(monkeypatch):
    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    boundary_factory = MagicMock(side_effect=[_boundary_mock(), _boundary_mock()])

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=[
            _manager_attempt_result(committed=False),
            _manager_attempt_result(committed=True),
        ],
    ) as manager_star:
        code, written, pipeline_mock, backtest_fn, review_fn = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 0
    assert manager_star.call_count == 2
    assert boundary_factory.call_count == 2
    assert [
        call.args[0]["_experiment_attempt"]["number"]
        for call in boundary_factory.call_args_list
    ] == [1, 2]
    assert [
        call.args[0]["_experiment_attempt"]["number"]
        for call in manager_star.call_args_list
    ] == [1, 2]
    pipeline_mock.assert_not_called()
    backtest_fn.assert_not_called()
    review_fn.assert_not_called()
    telemetry = written["telemetry"]["experiment3"]
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_commit_missing",
        "complete",
    ]
    assert telemetry["shared_budget"]["max_calls"] == 40
    assert telemetry["shared_budget"]["max_tokens"] == 700_000


def test_two_attempt_m1_different_role_transfers_still_emit_telemetry(monkeypatch):
    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    results = [
        _manager_attempt_result(committed=False),
        _manager_attempt_result(committed=True),
    ]
    for result, transferred in zip(results, (2, 1)):
        roles = result["budget"]["roles"]
        roles["architect"].update(
            configured_max_calls=3,
            transferred_calls=-transferred,
            max_calls=3 - transferred,
        )
        roles["developer"].update(
            configured_max_calls=14,
            transferred_calls=transferred,
            max_calls=14 + transferred,
        )

    with patch("exp3.router.run_manager_star_runtime", side_effect=results):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(
                side_effect=[_boundary_mock(), _boundary_mock()]
            ),
        )

    telemetry = written["telemetry"]["experiment3"]
    assert code == 0
    assert telemetry["shared_budget"]["configured_max_calls"] == 40
    assert telemetry["shared_budget"]["transferred_calls"] == 0
    assert telemetry["shared_budget"]["max_calls"] == 40
    assert telemetry["role_usage"]["architect"]["configured_max_calls"] == 6
    assert telemetry["role_usage"]["architect"]["transferred_calls"] == -3
    assert telemetry["role_usage"]["architect"]["max_calls"] == 3
    assert telemetry["role_usage"]["developer"]["configured_max_calls"] == 28
    assert telemetry["role_usage"]["developer"]["transferred_calls"] == 3
    assert telemetry["role_usage"]["developer"]["max_calls"] == 31


def test_two_attempt_m1_second_failure_does_not_reuse_first_role_outputs(
    monkeypatch,
):
    import exp3.telemetry as exp3_telemetry

    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    first_result = _manager_attempt_result(committed=False)
    first_result["manager_final"] = {"summary": "FIRST ATTEMPT ONLY"}
    first_result["developer_result"] = {"marker": "FIRST DEVELOPER OUTPUT"}

    second_accounting = _attempt_accounting("manager_star")
    provider_call = {
        "call_id": "exp3-000001",
        "role": "manager",
        "phase": "manager_to_architect",
        "status": "failed",
        "failure_type": "RuntimeError",
        "input_sha256": "1" * 64,
        "output_sha256": None,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
        "latency_seconds": 0.1,
    }
    second_accounting["provider_calls"] = [provider_call]
    for usage in (
        second_accounting["budget"]["shared"],
        second_accounting["budget"]["roles"]["manager"],
    ):
        usage.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            failures=1,
            latency_seconds=0.1,
        )
    second_boundary = _boundary_mock()
    second_boundary.snapshot.return_value = second_accounting
    captured = {}
    original_builder = exp3_telemetry.build_manager_star_telemetry

    def capture_builder(outcome, **kwargs):
        captured["outcome"] = outcome
        return original_builder(outcome, **kwargs)

    with patch(
        "exp3.telemetry.build_manager_star_telemetry",
        side_effect=capture_builder,
    ), patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=[first_result, RuntimeError("attempt two failed")],
    ):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(
                side_effect=[_boundary_mock(), second_boundary]
            ),
        )

    assert code == 1, written
    final_outcome = captured["outcome"]
    assert final_outcome["source_attempt"] == 2
    assert final_outcome["manager_final"] == {}
    assert final_outcome["developer_result"] == {}
    assert "FIRST ATTEMPT ONLY" not in json.dumps(final_outcome)
    assert "FIRST DEVELOPER OUTPUT" not in json.dumps(final_outcome)
    assert [item["completion"] for item in final_outcome["attempts"]] == [
        "required_commit_missing",
        "runtime_failure",
    ]


def test_two_attempt_m1_first_complete_attempt_does_not_run_a_second(monkeypatch):
    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    boundary_factory = MagicMock(return_value=_boundary_mock())

    with patch(
        "exp3.router.run_manager_star_runtime",
        return_value=_manager_attempt_result(committed=True),
    ) as manager_star:
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 0
    assert manager_star.call_count == 1
    assert boundary_factory.call_count == 1
    assert len(written["telemetry"]["experiment3"]["attempts"]) == 1


def test_two_attempt_m1_snapshot_failure_keeps_complete_result(
    monkeypatch,
):
    import exp3.telemetry as exp3_telemetry

    payload = _two_attempt_payload("manager_star")
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["quant_calculator_enabled"] = True
    parameters["answer_capture_profile"] = "t3_item_results_v2"
    parameters["quant_calculator_schema_path"] = (
        "schemas/t3_quant_artifact.schema.json"
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: {
            "candidate_count": 25,
            "candidates": {str(item_id): {} for item_id in range(1, 26)},
            "candidates_sha256": "a" * 64,
        },
    )

    def fail_snapshot(_path, *, attempt):
        raise RuntimeError(f"snapshot storage unavailable for attempt {attempt}")

    monkeypatch.setattr(
        "exp3.attempt_control.freeze_item_store_attempt",
        fail_snapshot,
    )
    captured = {}
    original_builder = exp3_telemetry.build_manager_star_telemetry

    def capture_builder(outcome, **kwargs):
        captured["outcome"] = outcome
        return original_builder(outcome, **kwargs)

    with patch(
        "exp3.telemetry.build_manager_star_telemetry",
        side_effect=capture_builder,
    ), patch(
        "exp3.router.run_manager_star_runtime",
        return_value=_manager_attempt_result(committed=False),
    ):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(return_value=_boundary_mock()),
        )

    assert code == 0, written
    assert written["telemetry"]["experiment3"]["attempts"][0][
        "completion"
    ] == "complete"
    raw_attempt = captured["outcome"]["attempts"][0]
    assert raw_attempt["snapshot_status"] == "failed"
    assert raw_attempt["snapshot_error"] == (
        "RuntimeError: snapshot storage unavailable for attempt 1"
    )


def test_m1_non_manager_error_still_uses_manager_star_failure_telemetry(
    monkeypatch,
):
    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    boundary = _boundary_mock()
    boundary.snapshot.return_value = _attempt_accounting("manager_star")

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=RuntimeError("pre-call local failure"),
    ):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(return_value=boundary),
        )

    assert code == 1
    assert written["telemetry"]["experiment3"]["architecture_mode"] == (
        "manager_star"
    )


def test_m0_manager_named_error_still_uses_single_agent_failure_telemetry(
    monkeypatch,
):
    from exp3.router import ManagerStarRunError

    payload = _two_attempt_payload("single_agent")
    boundary = _boundary_mock()
    boundary.snapshot.return_value = _attempt_accounting("single_agent")
    error = ManagerStarRunError(
        failure_phase="synthetic_m0_failure",
        failure_type="SyntheticFailure",
        audit=[],
        budget=_attempt_accounting("manager_star")["budget"],
        topology={"cursor": 0, "sequence_complete": False, "finalized": False},
    )

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=MagicMock(side_effect=error),
        exp3_boundary_factory=MagicMock(return_value=boundary),
    )

    assert code == 1
    assert written["telemetry"]["experiment3"]["architecture_mode"] == (
        "single_agent"
    )


def test_two_attempt_m1_saved_25_then_budget_error_keeps_complete_answers(
    monkeypatch,
):
    payload = _two_attempt_payload("manager_star")
    payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_enabled"
    ] = True
    payload["execution_objectives"]["parsed_task_parameters"][
        "answer_capture_profile"
    ] = "t3_item_results_v2"
    payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_schema_path"
    ] = "schemas/t3_quant_artifact.schema.json"
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: {
            "candidate_count": 25,
            "candidates": {str(item_id): {} for item_id in range(1, 26)},
            "candidates_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        "exp3.attempt_control.freeze_item_store_attempt",
        lambda _path, *, attempt: {"attempt": attempt, "candidate_count": 25},
    )

    accounting = _attempt_accounting("manager_star")
    provider_call = {
        "call_id": "exp3-000001",
        "role": "manager",
        "phase": "manager_to_architect",
        "status": "failed",
        "failure_type": "BudgetExceeded",
        "input_sha256": "1" * 64,
        "output_sha256": None,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
        "latency_seconds": 0.1,
    }
    accounting["provider_calls"] = [provider_call]
    for usage in (
        accounting["budget"]["shared"],
        accounting["budget"]["roles"]["manager"],
    ):
        usage.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            failures=1,
            latency_seconds=0.1,
        )
    boundary = _boundary_mock()
    boundary.snapshot.return_value = accounting

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=run.BudgetExceeded("per-attempt provider budget exceeded"),
    ) as manager_star:
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(return_value=boundary),
        )

    assert code == 0, written
    manager_star.assert_called_once()
    attempt = written["telemetry"]["experiment3"]["attempts"][0]
    assert attempt["completion"] == "complete"
    assert attempt["answer_capture_status"] == "complete"
    assert attempt["runtime_exit_status"] == "error"
    assert attempt["failure_type"] == "BudgetExceeded"
    assert attempt["saved_item_count"] == 25


def test_two_attempt_m1_partial_items_then_attempt_budget_error_uses_attempt_two(
    monkeypatch,
):
    payload = _two_attempt_payload("manager_star")
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["quant_calculator_enabled"] = True
    parameters["answer_capture_profile"] = "t3_item_results_v2"
    parameters["quant_calculator_schema_path"] = (
        "schemas/t3_quant_artifact.schema.json"
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    stores = iter(
        [
            {
                "candidate_count": 24,
                "candidates": {str(item_id): {} for item_id in range(1, 25)},
                "candidates_sha256": "a" * 64,
            },
            {
                "candidate_count": 24,
                "candidates": {str(item_id): {} for item_id in range(1, 25)},
                "candidates_sha256": "a" * 64,
            },
            {
                "candidate_count": 25,
                "candidates": {str(item_id): {} for item_id in range(1, 26)},
                "candidates_sha256": "b" * 64,
            },
        ]
    )
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store", lambda _path: next(stores)
    )
    monkeypatch.setattr(
        "exp3.attempt_control.freeze_item_store_attempt",
        lambda _path, *, attempt: {"attempt": attempt},
    )

    first_accounting = _attempt_accounting("manager_star")
    provider_call = {
        "call_id": "exp3-000001",
        "role": "manager",
        "phase": "manager_to_architect",
        "status": "failed",
        "failure_type": "BudgetExceeded",
        "input_sha256": "1" * 64,
        "output_sha256": None,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
        "latency_seconds": 0.1,
    }
    first_accounting["provider_calls"] = [provider_call]
    for usage in (
        first_accounting["budget"]["shared"],
        first_accounting["budget"]["roles"]["manager"],
    ):
        usage.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            failures=1,
            latency_seconds=0.1,
        )
    first_boundary = _boundary_mock()
    first_boundary.snapshot.return_value = first_accounting

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=[
            run.BudgetExceeded("per-attempt provider budget exceeded"),
            _manager_attempt_result(committed=False),
        ],
    ) as manager_star:
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(
                side_effect=[first_boundary, _boundary_mock()]
            ),
        )

    assert code == 0, written
    assert manager_star.call_count == 2
    attempts = written["telemetry"]["experiment3"]["attempts"]
    assert [item["completion"] for item in attempts] == [
        "runtime_failure",
        "complete",
    ]
    assert attempts[0]["saved_item_count"] == 24
    assert attempts[0]["failure_type"] == "BudgetExceeded"
    assert attempts[1]["saved_item_count"] == 25


def test_two_attempt_m1_clean_t3_exit_reports_required_items_missing(
    monkeypatch,
):
    payload = _two_attempt_payload("manager_star")
    payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_enabled"
    ] = True
    payload["execution_objectives"]["parsed_task_parameters"][
        "answer_capture_profile"
    ] = "t3_item_results_v2"
    payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_schema_path"
    ] = "schemas/t3_quant_artifact.schema.json"
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: {
            "candidate_count": 24,
            "candidates": {str(item_id): {} for item_id in range(1, 25)},
            "candidates_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        "exp3.attempt_control.freeze_item_store_attempt",
        lambda _path, *, attempt: {"attempt": attempt, "candidate_count": 24},
    )

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=[
            _manager_attempt_result(committed=True),
            _manager_attempt_result(committed=True),
        ],
    ) as manager_star:
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=MagicMock(
                side_effect=[_boundary_mock(), _boundary_mock()]
            ),
        )

    assert code == 1
    assert manager_star.call_count == 2, written
    telemetry = written["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "required_items_missing"
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_items_missing",
        "required_items_missing",
    ]


def test_two_attempt_m0_clean_t3_exit_reports_required_items_missing(
    monkeypatch,
):
    payload = _two_attempt_payload("single_agent")
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters["quant_calculator_enabled"] = True
    parameters["answer_capture_profile"] = "t3_item_results_v2"
    parameters["quant_calculator_schema_path"] = (
        "schemas/t3_quant_artifact.schema.json"
    )
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: {
            "candidate_count": 24,
            "candidates": {str(item_id): {} for item_id in range(1, 25)},
            "candidates_sha256": "a" * 64,
        },
    )
    monkeypatch.setattr(
        "exp3.attempt_control.freeze_item_store_attempt",
        lambda _path, *, attempt: {"attempt": attempt, "candidate_count": 24},
    )

    def incomplete_attempt():
        return {
            "status": "succeeded",
            "summary": "24 item candidates saved",
            "usage": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "artifacts": {"modified_files": [], "new_files": []},
            "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
            "experiment3_provider_accounting": _attempt_accounting(
                "single_agent"
            ),
        }

    code, written, *_ = _run_main(
        payload,
        monkeypatch,
        pipeline_mock=MagicMock(
            side_effect=[incomplete_attempt(), incomplete_attempt()]
        ),
        exp3_boundary_factory=MagicMock(
            side_effect=[_boundary_mock(), _boundary_mock()]
        ),
    )

    assert code == 1
    telemetry = written["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "required_items_missing"
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_items_missing",
        "required_items_missing",
    ]


def test_two_attempt_m1_resets_attempt_clock_but_keeps_observation_clock(
    monkeypatch,
):
    payload = _two_attempt_payload("manager_star")
    payload["iteration_controls"]["timeout_seconds"] = 60
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    clock = _FakeClock()
    first_boundary = _boundary_mock()
    first_accounting = _attempt_accounting("manager_star")
    for usage in (
        first_accounting["budget"]["shared"],
        first_accounting["budget"]["roles"]["manager"],
    ):
        usage.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            latency_seconds=0.1,
        )
    first_accounting["provider_calls"] = [
        {
            "call_id": "exp3-000001",
            "role": "manager",
            "phase": "manager_to_architect",
            "status": "succeeded",
            "failure_type": None,
            "input_sha256": "1" * 64,
            "output_sha256": "2" * 64,
            "prompt_tokens": 2,
            "completion_tokens": 1,
            "total_tokens": 3,
            "latency_seconds": 0.1,
        }
    ]
    first_boundary.snapshot.return_value = first_accounting
    second_boundary = _boundary_mock()
    boundary_factory = MagicMock(side_effect=[first_boundary, second_boundary])
    outcomes = iter(
        [
            _manager_attempt_result(committed=False),
            _manager_attempt_result(committed=True),
        ]
    )

    def run_attempt(*_args, **_kwargs):
        result = next(outcomes)
        # Attempt 1 exceeds its own 60-second allowance. Attempt 2 receives a
        # fresh 60-second allowance, while the observation still stops at 120.
        clock.advance(61 if clock.now == 0 else 59)
        return result

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=run_attempt,
    ) as manager_star:
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
            exp3_clock=clock,
        )

    assert code == 0
    assert manager_star.call_count == 2
    assert clock.now == 120
    attempts = written["telemetry"]["experiment3"]["attempts"]
    assert [item["failure_type"] for item in attempts[:1]] == ["RunTimeout"]
    assert [item["completion"] for item in attempts] == [
        "runtime_failure",
        "complete",
    ]


def test_two_attempt_m1_later_zero_call_failure_preserves_first_attempt_usage(
    monkeypatch,
):
    from exp3.router import ManagerStarRunError

    payload = _two_attempt_payload("manager_star")
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    first = _manager_attempt_result(committed=False)
    first_budget = _attempt_accounting("manager_star")["budget"]
    provider_record = {
        "call_id": "exp3-000001",
        "role": "manager",
        "phase": "manager_to_architect",
        "status": "succeeded",
        "failure_type": None,
        "input_sha256": "1" * 64,
        "output_sha256": "2" * 64,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
        "latency_seconds": 0.1,
    }
    for item in (
        first_budget["shared"],
        first_budget["roles"]["manager"],
    ):
        item.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            latency_seconds=0.1,
        )
    first["budget"] = first_budget
    first["provider_calls"] = [provider_record]
    second_error = ManagerStarRunError(
        failure_phase="isolation_pre_orchestration",
        failure_type="IsolationPreflightError",
        audit=[],
        budget=_attempt_accounting("manager_star")["budget"],
        topology={"cursor": 0, "sequence_complete": False, "finalized": False},
    )
    first_boundary = _boundary_mock()
    second_boundary = _boundary_mock()
    second_boundary.snapshot.return_value = _attempt_accounting("manager_star")
    boundary_factory = MagicMock(
        side_effect=[first_boundary, second_boundary]
    )

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=[first, second_error],
    ):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    telemetry = written["telemetry"]["experiment3"]
    assert code == 1
    assert telemetry["shared_budget"]["calls"] == 1
    assert telemetry["shared_budget"]["total_tokens"] == 3
    assert len(telemetry["provider_calls"]) == 1
    assert [item["provider_call_count"] for item in telemetry["attempts"]] == [
        1,
        0,
    ]
    assert written["telemetry"]["model_usage"][0]["total_tokens"] == 3


def test_two_attempt_rag_m1_failure_preserves_true_rag_state(monkeypatch):
    from exp3.router import ManagerStarRunError

    payload = _two_attempt_payload("manager_star", rag_enabled=True)
    parameters = payload["execution_objectives"]["parsed_task_parameters"]
    parameters.update(
        {
            "formal_execution_contract": "factorial_rag_architecture_v1",
            "rag_delivery_policy": "all_model_stages_v1",
        }
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")

    provider_record = {
        "call_id": "exp3-000001",
        "role": "manager",
        "phase": "manager_to_architect",
        "status": "succeeded",
        "failure_type": None,
        "input_sha256": "1" * 64,
        "output_sha256": "2" * 64,
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "total_tokens": 3,
        "latency_seconds": 0.1,
    }
    delivery_record = {
        "record_type": "role_retrieval_delivery",
        "attempt": 1,
        "phase": "manager_to_architect",
        "role": "manager",
        "status": "delivered",
        "evidence_text_sha256": "c" * 64,
        "prompt_template_sha256": "d" * 64,
        "injected_memory_count": 1,
        "injected_memory_ids_sha256": "f" * 64,
    }
    first = _manager_attempt_result(committed=False)
    first_budget = _attempt_accounting("manager_star", rag_enabled=True)["budget"]
    for item in (first_budget["shared"], first_budget["roles"]["manager"]):
        item.update(
            calls=1,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            latency_seconds=0.1,
        )
    first.update(
        rag_enabled=True,
        budget=first_budget,
        provider_calls=[provider_record],
        audit=[
            delivery_record,
            {"record_type": "provider_call", **provider_record},
        ],
    )
    second_error = ManagerStarRunError(
        failure_phase="isolation_pre_orchestration",
        failure_type="IsolationPreflightError",
        audit=[],
        budget=_attempt_accounting("manager_star", rag_enabled=True)["budget"],
        topology={"cursor": 0, "sequence_complete": False, "finalized": False},
    )
    second_error.rag_enabled = True

    first_boundary = _boundary_mock()
    first_boundary.rag_enabled = True
    second_boundary = _boundary_mock()
    second_boundary.rag_enabled = True
    second_boundary.snapshot.return_value = _attempt_accounting(
        "manager_star", rag_enabled=True
    )
    boundary_factory = MagicMock(side_effect=[first_boundary, second_boundary])
    outcomes = iter([first, second_error])
    call_number = 0

    def run_attempt(attempt_payload, **_kwargs):
        nonlocal call_number
        call_number += 1
        if call_number == 1:
            attempt_payload["_retrieval_delivery"] = _verified_retrieval_delivery()
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch(
        "exp3.router.run_manager_star_runtime",
        side_effect=run_attempt,
    ):
        code, written, *_ = _run_main(
            payload,
            monkeypatch,
            exp3_boundary_factory=boundary_factory,
        )

    assert code == 1
    telemetry = written["telemetry"]["experiment3"]
    assert telemetry["rag_enabled"] is True
    assert [item["provider_call_count"] for item in telemetry["attempts"]] == [1, 0]
    assert written["telemetry"]["retrieval"]["delivery_mode"] == "generator_prompt"
    assert written["telemetry"]["retrieval"]["prompt_injected"] is True
    assert telemetry["role_retrieval_deliveries"] == [
        {key: value for key, value in delivery_record.items() if key != "record_type"}
    ]
