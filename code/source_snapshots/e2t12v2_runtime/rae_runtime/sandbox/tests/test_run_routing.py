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
        "max_calls": 0,
        "max_tokens": 0,
        "over_budget": False,
    }
    developer = {
        **zero_role,
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
            "implementation": [{"path": "src/e3_fixture.py", "summary": "done"}]
        },
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
        "exp3-runtime-telemetry-v2"
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
