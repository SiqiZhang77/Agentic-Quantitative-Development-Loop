from __future__ import annotations

import ast
import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import pipeline
from exp3.production_provider import (
    ProductionProviderBoundaryError,
    ProductionProviderBoundaryUnavailable,
)


ROOT = Path(__file__).resolve().parents[1]
PIPELINE_MCP_SOURCE = ROOT / "pipeline_mcp.py"


def _payload(*, explicit: bool = True) -> dict:
    value = {
        "run_id": "E3-M0-T1-R1",
        "issue_key": "SCRUM-390",
        "command": "refactor",
        "execution_objectives": {
            "strategy_type": "refactor",
            "parsed_task_parameters": {"rag_enabled": False},
        },
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "frozen-source",
                "target_branch": "quant/E3-M0-T1-R1",
            }
        ],
        "strategy": {
            "ref": "frozen-source",
            "path": "rae_runtime/proxy/budget_guard.py",
            "source_path": "rae_runtime/proxy/budget_guard.py",
            "target_path": "rae_runtime/proxy/budget_guard.py",
            "target_branch": "quant/E3-M0-T1-R1",
        },
        "retrieval_context": None,
    }
    if explicit:
        value["architecture_mode"] = "single_agent"
    return value


def _install_agent_stubs(monkeypatch):
    agents = types.ModuleType("agents")
    agents.Agent = object
    agents.Runner = object
    agents.ModelSettings = lambda **kwargs: types.SimpleNamespace(**kwargs)
    agents.set_tracing_disabled = lambda disabled: None
    extensions = types.ModuleType("agents.extensions")
    models = types.ModuleType("agents.extensions.models")
    litellm_model = types.ModuleType("agents.extensions.models.litellm_model")
    litellm_model.LitellmModel = object
    mcp = types.ModuleType("agents.mcp")
    mcp.MCPServerStdio = object
    monkeypatch.setitem(sys.modules, "agents", agents)
    monkeypatch.setitem(sys.modules, "agents.extensions", extensions)
    monkeypatch.setitem(sys.modules, "agents.extensions.models", models)
    monkeypatch.setitem(
        sys.modules,
        "agents.extensions.models.litellm_model",
        litellm_model,
    )
    monkeypatch.setitem(sys.modules, "agents.mcp", mcp)


def _load_pipeline_mcp(monkeypatch):
    _install_agent_stubs(monkeypatch)
    sys.modules.pop("pipeline_mcp", None)
    return importlib.import_module("pipeline_mcp")


def test_explicit_m0_rejects_missing_boundary_and_scripted_bypass(monkeypatch):
    monkeypatch.setenv("RAE_OFFLINE", "1")
    monkeypatch.setenv("USE_MCP_GITHUB", "true")

    with pytest.raises(ProductionProviderBoundaryUnavailable, match="requires"):
        pipeline.run_pipeline_auto(_payload())
    with pytest.raises(ProductionProviderBoundaryUnavailable, match="scripted"):
        pipeline.run_pipeline_auto(
            _payload(),
            exp3_provider_boundary=object(),
        )


def test_legacy_omission_keeps_scripted_path_unchanged(monkeypatch):
    expected = {"status": "legacy"}
    monkeypatch.setenv("RAE_OFFLINE", "1")
    monkeypatch.setattr(pipeline, "run_pipeline", lambda payload, tracer=None: expected)

    assert pipeline.run_pipeline_auto(_payload(explicit=False)) is expected
    with pytest.raises(ProductionProviderBoundaryError, match="legacy"):
        pipeline.run_pipeline_auto(
            _payload(explicit=False),
            exp3_provider_boundary=object(),
        )


def test_online_m0_forwards_same_boundary_to_pipeline_mcp(monkeypatch):
    boundary = object()
    captured = {}
    module = types.ModuleType("pipeline_mcp")

    def run_pipeline_mcp(payload, tracer=None, exp3_provider_boundary=None):
        captured.update(
            payload=payload,
            tracer=tracer,
            boundary=exp3_provider_boundary,
        )
        return {"status": "ok"}

    module.run_pipeline_mcp = run_pipeline_mcp
    monkeypatch.setitem(sys.modules, "pipeline_mcp", module)
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    monkeypatch.setenv("USE_MCP_GITHUB", "true")

    result = pipeline.run_pipeline_auto(
        _payload(),
        tracer="trace",
        exp3_provider_boundary=boundary,
    )

    assert result == {"status": "ok"}
    assert captured["boundary"] is boundary
    assert captured["tracer"] == "trace"


def test_pipeline_mcp_checks_boundary_before_branch_preparation(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    prepared = []
    monkeypatch.setattr(
        pipeline_mcp,
        "_prepare_repository_branches",
        lambda *args, **kwargs: prepared.append(True),
    )

    with pytest.raises(ProductionProviderBoundaryUnavailable, match="requires"):
        asyncio.run(pipeline_mcp._run_pipeline_mcp_async(_payload()))

    assert prepared == []


def test_pipeline_mcp_source_wraps_model_below_runner_and_before_runner_run():
    tree = ast.parse(
        PIPELINE_MCP_SOURCE.read_text(encoding="utf-8"),
        filename=str(PIPELINE_MCP_SOURCE),
    )
    async_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_pipeline_mcp_async"
    )
    calls = {}
    for node in ast.walk(async_function):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name in {
            "exp3_provider_boundary.start",
            "_prepare_repository_branches",
            "build_agents_model",
            "exp3_provider_boundary.wrap_agents_model",
            "Runner.run",
            "_close_owned_agents_model_client",
        }:
            calls[name] = node.lineno

    assert calls["exp3_provider_boundary.start"] < calls["_prepare_repository_branches"]
    assert calls["build_agents_model"] < calls["exp3_provider_boundary.wrap_agents_model"]
    assert calls["exp3_provider_boundary.wrap_agents_model"] < calls["Runner.run"]
    assert calls["Runner.run"] < calls["_close_owned_agents_model_client"]


def test_pipeline_mcp_awaits_owned_openai_client_before_loop_closes(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    events = []

    class FakeAsyncClient:
        async def close(self):
            events.append(("closed", asyncio.get_running_loop().is_closed()))

    model = types.SimpleNamespace(_rae_owned_async_client=FakeAsyncClient())

    asyncio.run(pipeline_mcp._close_owned_agents_model_client(model))

    assert events == [("closed", False)]


def test_pipeline_mcp_cleanup_is_noop_for_models_without_owned_client(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)

    asyncio.run(
        pipeline_mcp._close_owned_agents_model_client(types.SimpleNamespace())
    )


def test_m0_transcript_capture_failure_is_diagnostic_only(monkeypatch, tmp_path):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    captured = []
    monkeypatch.setattr(
        pipeline_mcp,
        "capture_model_visible_output",
        lambda *_args, **kwargs: captured.append(kwargs)
        or {
            "capture_status": "failed",
            "capture_error": "model_visible_transcript_write_failed",
            "capture_evidence_status": "persisted",
        },
    )

    evidence = pipeline_mcp._capture_single_agent_visible_output(
        tmp_path / "audit.jsonl",
        _payload(),
        {"summary": "valid model result"},
    )

    assert evidence["capture_status"] == "failed"
    assert captured == [
        {
            "architecture_mode": "single_agent",
            "role": "developer",
            "phase": "single_agent_developer",
            "attempt": None,
            "output": {"summary": "valid model result"},
        }
    ]


def test_explicit_m0_mcp_session_receives_same_preflight_binding() -> None:
    tree = ast.parse(
        PIPELINE_MCP_SOURCE.read_text(encoding="utf-8"),
        filename=str(PIPELINE_MCP_SOURCE),
    )
    async_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_pipeline_mcp_async"
    )
    lines: dict[str, int] = {}
    for node in ast.walk(async_function):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            if name in {
                "exp3_provider_boundary.start",
                "exp3_provider_boundary.preflight.mcp_environment_binding",
                "MCPServerStdio",
            }:
                lines[name] = node.lineno

    assert lines["exp3_provider_boundary.start"] < lines[
        "exp3_provider_boundary.preflight.mcp_environment_binding"
    ]
    assert lines[
        "exp3_provider_boundary.preflight.mcp_environment_binding"
    ] < lines["MCPServerStdio"]


def test_sync_wrapper_reconciles_pass_delta_and_attaches_cumulative_snapshot(
    monkeypatch,
):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    baseline = {
        "calls": 2,
        "prompt_tokens": 20,
        "completion_tokens": 4,
        "total_tokens": 24,
    }
    boundary = MagicMock()
    boundary.ledger.snapshot.return_value = {"shared": baseline}
    boundary.snapshot.return_value = {
        "architecture_mode": "single_agent",
        "provider_calls": [],
    }

    async def fake_async(*args, **kwargs):
        del args, kwargs
        return {
            "status": "succeeded",
            "usage": {
                "calls": 1,
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
            },
            "diagnostics": {
                "retry_safe": {
                    "tool_calls": [
                        {"tool": "commit_and_push", "status": "success"},
                        {"tool": "replace_in_file", "status": "success"},
                    ]
                }
            },
        }

    monkeypatch.setattr(pipeline_mcp, "_run_pipeline_mcp_async", fake_async)
    result = pipeline_mcp.run_pipeline_mcp(
        _payload(),
        exp3_provider_boundary=boundary,
    )

    boundary.reconcile_runner_usage.assert_called_once_with(
        result["usage"],
        before=baseline,
    )
    assert result["experiment3_provider_accounting"]["commit_count"] == 2


def test_sync_wrapper_attaches_boundary_snapshot_to_provider_failure(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    boundary = MagicMock()
    boundary.ledger.snapshot.return_value = {"shared": {"calls": 0}}
    safe_snapshot = {"architecture_mode": "single_agent", "provider_calls": []}
    boundary.snapshot.return_value = safe_snapshot

    async def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("PRIVATE provider body")

    monkeypatch.setattr(pipeline_mcp, "_run_pipeline_mcp_async", fail)
    with pytest.raises(RuntimeError) as raised:
        pipeline_mcp.run_pipeline_mcp(
            _payload(),
            exp3_provider_boundary=boundary,
        )

    assert raised.value.exp3_provider_accounting == safe_snapshot


def test_m0_calculator_profile_is_request_bound_and_provider_secrets_are_removed(
    monkeypatch,
):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-remain-parent-only")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "proxy-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "aws-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "github-required-by-mcp")
    monkeypatch.setenv("RAE_ENABLE_QUANT_CALCULATOR", "host-must-not-authorize")
    monkeypatch.setenv("MAX_QUANT_CALCULATE_CALLS", "99")
    monkeypatch.setenv("RAE_EXPERIMENT_ATTEMPT", "99")
    monkeypatch.setenv(
        "CALCULATION_SCHEMA_PATHS_MAP",
        '{"attacker/repo":["wrong.json"]}',
    )

    disabled = pipeline_mcp._mcp_child_environment(_payload())
    enabled_payload = _payload()
    enabled_payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_enabled"
    ] = True
    enabled_payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_schema_path"
    ] = "rae_runtime/proxy/output_schema.json"
    enabled_payload["_experiment_attempt"] = {"number": 2, "maximum": 2}
    enabled = pipeline_mcp._mcp_child_environment(enabled_payload)

    for child in (disabled, enabled):
        assert "OPENAI_API_KEY" not in child
        assert "LITELLM_PROXY_API_KEY" not in child
        assert "AWS_SESSION_TOKEN" not in child
        assert child["GITHUB_TOKEN"] == "github-required-by-mcp"
    assert "RAE_ENABLE_QUANT_CALCULATOR" not in disabled
    assert "MAX_QUANT_CALCULATE_CALLS" not in disabled
    assert "WRITABLE_PATHS_MAP" not in disabled
    assert "CALCULATION_SCHEMA_PATHS_MAP" not in disabled
    assert "RAE_EXPERIMENT_ATTEMPT" not in disabled
    assert enabled["RAE_ENABLE_QUANT_CALCULATOR"] == "true"
    assert enabled["MAX_QUANT_CALCULATE_CALLS"] == "4"
    assert enabled["RAE_EXPERIMENT_ATTEMPT"] == "2"
    assert enabled["WRITABLE_PATHS_MAP"] == (
        '{"bankingscience/BSLAgenticQuantDevLoop": '
        '["rae_runtime/proxy/budget_guard.py"]}'
    )
    assert enabled["CALCULATION_SCHEMA_PATHS_MAP"] == (
        '{"bankingscience/BSLAgenticQuantDevLoop": '
        '["rae_runtime/proxy/output_schema.json"]}'
    )


def test_m0_calculator_profile_rejects_string_boolean(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)
    payload = _payload()
    payload["execution_objectives"]["parsed_task_parameters"][
        "quant_calculator_enabled"
    ] = "true"

    with pytest.raises(ValueError, match="invalid_calculator_flag"):
        pipeline_mcp._mcp_child_environment(payload)
