"""Server startup seams:
- RAE-23a least-privilege tool surface (tool_policy.json enforcement).
- IW baseline bridge: a Jira .request attachment named in the runtime request
  points BACKTEST_BASELINE at the user's template.
"""
import asyncio
import inspect
import json
import os

import pytest

import server


@pytest.fixture(autouse=True)
def _restore_baseline_env():
    """_apply_request_template_baseline sets os.environ directly (production
    behaviour), so snapshot/restore BACKTEST_BASELINE around each test to avoid
    leaking a temp-path baseline into other test files."""
    saved = os.environ.get("BACKTEST_BASELINE")
    yield
    if saved is None:
        os.environ.pop("BACKTEST_BASELINE", None)
    else:
        os.environ["BACKTEST_BASELINE"] = saved


def _listed_tool_names(mcp):
    tools = mcp.list_tools()
    if inspect.isawaitable(tools):
        tools = asyncio.run(tools)
    return sorted(getattr(t, "name", t) for t in tools)


# --- RAE-23a tool policy ---------------------------------------------------

def test_blocked_tool_names_from_policy(monkeypatch):
    monkeypatch.delenv("RAE_TOOL_POLICY_ENFORCE", raising=False)
    monkeypatch.delenv("RAE_TOOL_POLICY_FILE", raising=False)
    assert set(server._blocked_tool_names()) == {"get_backtest_artifacts", "generate_equity_curve"}


def test_enforcement_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RAE_TOOL_POLICY_ENFORCE", "0")
    assert server._blocked_tool_names() == []


def test_malformed_policy_fails_open(monkeypatch):
    monkeypatch.setenv("RAE_TOOL_POLICY_FILE", "/no/such/policy.json")
    # Must not raise, and must expose everything rather than lock the surface.
    assert server._blocked_tool_names() == []


@pytest.mark.skipif(server.FastMCP is None, reason="fastmcp not installed")
def test_live_server_hides_blocked_but_keeps_allowed(monkeypatch):
    monkeypatch.delenv("RAE_TOOL_POLICY_ENFORCE", raising=False)
    monkeypatch.delenv("RAE_TOOL_POLICY_FILE", raising=False)
    names = _listed_tool_names(server.create_mcp_server())
    # The two redundant post-processing tools are hidden from the LLM...
    assert "get_backtest_artifacts" not in names
    assert "generate_equity_curve" not in names
    # ...while the core surface stays available.
    assert {"submit_backtest", "get_backtest_status", "get_backtest_results",
            "run_backtest_postprocessing", "list_master_indices", "list_jobs"} <= set(names)


@pytest.mark.skipif(server.FastMCP is None, reason="fastmcp not installed")
def test_blocked_tools_underlying_functions_remain(monkeypatch):
    # Blocking removes the MCP *tool* from the surface, but run_backtest_postprocessing
    # calls the underlying functions directly (not via the tool), so they must remain
    # callable — that's why blocking the redundant tools doesn't break post-processing.
    monkeypatch.delenv("RAE_TOOL_POLICY_ENFORCE", raising=False)
    server.create_mcp_server()  # applies the policy
    assert callable(server._get_backtest_artifacts)
    assert callable(server._generate_equity_curve)


def test_default_username_survives_numeric_container_uid(monkeypatch):
    monkeypatch.delenv("BIALOBOG_USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    monkeypatch.setattr(
        server.getpass,
        "getuser",
        lambda: (_ for _ in ()).throw(KeyError("getpwuid(): uid not found: 1041")),
    )

    assert server._default_username() == server.DEFAULT_BACKTEST_USERNAME


# --- IW baseline bridge ----------------------------------------------------

def test_baseline_bridge_sets_env_from_request_template(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKTEST_BASELINE", raising=False)
    tpl = tmp_path / "SCRUM-1.request"
    tpl.write_text("purpose = backtest\n")
    req = tmp_path / "request.json"
    req.write_text(json.dumps({"input_paths": {"request_template_path": str(tpl)}}))
    out = server._apply_request_template_baseline(req)
    assert out == str(tpl)
    assert server.os.environ["BACKTEST_BASELINE"] == str(tpl)


def test_baseline_bridge_never_overrides_explicit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKTEST_BASELINE", "/explicit/baseline.request")
    req = tmp_path / "request.json"
    req.write_text(json.dumps({"input_paths": {"request_template_path": str(tmp_path / "x.request")}}))
    assert server._apply_request_template_baseline(req) is None
    assert server.os.environ["BACKTEST_BASELINE"] == "/explicit/baseline.request"


def test_baseline_bridge_noop_when_no_payload(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKTEST_BASELINE", raising=False)
    assert server._apply_request_template_baseline(tmp_path / "missing.json") is None
    assert "BACKTEST_BASELINE" not in server.os.environ


def test_baseline_bridge_rejects_missing_template(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKTEST_BASELINE", raising=False)
    req = tmp_path / "request.json"
    req.write_text(json.dumps({"input_paths": {"request_template_path": "/gone/x.request"}}))
    with pytest.raises(RuntimeError, match="refusing to use a different baseline"):
        server._apply_request_template_baseline(req)
    assert "BACKTEST_BASELINE" not in server.os.environ
