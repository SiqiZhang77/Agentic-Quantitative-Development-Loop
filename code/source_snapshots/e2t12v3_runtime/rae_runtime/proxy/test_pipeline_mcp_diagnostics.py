import importlib
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _install_agent_stubs(monkeypatch):
    agents = types.ModuleType("agents")
    agents.Agent = object
    agents.Runner = object
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
        sys.modules, "agents.extensions.models.litellm_model", litellm_model
    )
    monkeypatch.setitem(sys.modules, "agents.mcp", mcp)


def _load_pipeline_mcp(monkeypatch):
    _install_agent_stubs(monkeypatch)
    sys.modules.pop("pipeline_mcp", None)
    return importlib.import_module("pipeline_mcp")


class FakeResult:
    final_output = "I cannot access the GitHub tools."
    new_items = []


def test_missing_mcp_tool_calls_do_not_default_to_committed(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)

    diagnostics = pipeline_mcp._extract_commit_diagnostics(
        FakeResult(), "quant/SCRUM-9"
    )

    assert diagnostics["commit"]["action"] == "unknown"
    assert diagnostics["commit"]["changed"] is False
    assert diagnostics["tool_calls"] == []
    with pytest.raises(pipeline_mcp.MCPWritebackError):
        pipeline_mcp._raise_if_mcp_write_failed(diagnostics)


def test_commit_tool_failure_is_preserved(monkeypatch):
    pipeline_mcp = _load_pipeline_mcp(monkeypatch)

    diagnostics = {
        "commit": {"action": "failed", "changed": False},
        "tool_calls": [
            {
                "tool": "commit_and_push",
                "status": "failed",
                "message": "RuntimeError: missing GITHUB_TOKEN",
            }
        ],
    }

    with pytest.raises(pipeline_mcp.MCPWritebackError) as exc:
        pipeline_mcp._raise_if_mcp_write_failed(diagnostics)

    assert "missing GITHUB_TOKEN" in str(exc.value)
