"""Agent-driven backtest driver (proxy/backtest_mcp.py): the LLM drives the
tools, then we deterministically postprocess so the result matches the scripted
contract and drives the iterate loop. The LLM/MCP server are patched out here;
the real agent run is exercised against the cluster.
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# backtest_mcp imports mcp_client from sandbox/, which proxy/tests/conftest.py
# does not add to the path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sandbox"))

# Dependency-light offline collection: production imports remain unchanged and
# every Agent/MCP/provider constructor is replaced by a fake in each test.
if "agents" not in sys.modules:
    agents = types.ModuleType("agents")
    agents.Agent = object
    agents.Runner = SimpleNamespace(run=None)
    agents.ModelSettings = lambda **kwargs: SimpleNamespace(**kwargs)
    agents.set_tracing_disabled = lambda disabled: None
    extensions = types.ModuleType("agents.extensions")
    models = types.ModuleType("agents.extensions.models")
    litellm_model = types.ModuleType("agents.extensions.models.litellm_model")
    litellm_model.LitellmModel = object
    mcp = types.ModuleType("agents.mcp")
    mcp.MCPServerStdio = object
    sys.modules.update(
        {
            "agents": agents,
            "agents.extensions": extensions,
            "agents.extensions.models": models,
            "agents.extensions.models.litellm_model": litellm_model,
            "agents.mcp": mcp,
        }
    )
if "fastmcp" not in sys.modules:
    fastmcp = types.ModuleType("fastmcp")
    fastmcp.Client = object
    fastmcp_client = types.ModuleType("fastmcp.client")
    fastmcp_transports = types.ModuleType("fastmcp.client.transports")
    fastmcp_transports.PythonStdioTransport = object
    sys.modules.update(
        {
            "fastmcp": fastmcp,
            "fastmcp.client": fastmcp_client,
            "fastmcp.client.transports": fastmcp_transports,
        }
    )

import backtest_mcp
from mcp_client import job_name_for
from provider_config import ProviderConfig


_PROVIDER_CONFIG = ProviderConfig(
    provider_mode="company_litellm",
    base_url="http://weles.cs.ucl.ac.uk:4000",
    model_alias="qwen3-coder",
    transport_model="litellm_proxy/qwen3-coder",
    adapter="litellm_chat_completions",
    api_key="test-secret",
    api_key_source="test",
    explicit_provider=True,
)


class _DummyServer:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_CONTRACT = {
    "summary": "Backtest completed for ALPHA-101 (via MCP).",
    "metrics": {"sharpe_ratio": 1.2, "max_drawdown": "-8.0%", "total_return": "45.0%"},
    "equity_curve": None,
    "evaluation": {"met_criteria": True, "summary": "meets criteria"},
    "recommended_action": "iterate",
}


def _run(payload):
    with patch.object(backtest_mcp, "MCPServerStdio", return_value=_DummyServer()), \
         patch.object(backtest_mcp, "Agent", MagicMock()), \
         patch.object(backtest_mcp, "build_agents_model", return_value=MagicMock()), \
         patch.object(backtest_mcp.Runner, "run",
                      AsyncMock(return_value=SimpleNamespace(final_output="Sharpe 1.2. Warnings: none."))), \
         patch.object(backtest_mcp, "postprocess_via_mcp_async",
                      AsyncMock(return_value=dict(_CONTRACT))) as mock_post:
        result = backtest_mcp.run_backtest_mcp(
            payload,
            provider_config=_PROVIDER_CONFIG,
        )
    return result, mock_post


def test_agent_driver_returns_structured_contract():
    payload = {"run_id": "ALPHA-101-1", "issue_key": "ALPHA-101", "iteration": 1,
               "args": {"start": "2020-01-01", "end": "2020-12-31"}}
    result, _ = _run(payload)

    # The structured contract the iterate loop + result_builder consume flows through.
    assert result["metrics"] == _CONTRACT["metrics"]
    assert result["evaluation"] == _CONTRACT["evaluation"]
    assert result["recommended_action"] == "iterate"  # so the loop can iterate
    # The agent's own narrative is preserved but does not overwrite the contract.
    assert result["agent_summary"] == "Sharpe 1.2. Warnings: none."
    assert result["status"] == "succeeded"


def test_postprocess_scores_the_exact_job_the_agent_submitted():
    payload = {"run_id": "ALPHA-101-1", "issue_key": "ALPHA-101", "iteration": 3,
               "args": {"start": "2020-01-01", "end": "2020-12-31"}}
    _, mock_post = _run(payload)

    # Same per-iteration-unique name the prompt pins and mcp_client submits under —
    # iteration 3 must not reuse an earlier job dir.
    expected = job_name_for(payload)
    assert expected.endswith("-i3")
    mock_post.assert_awaited_once()
    assert mock_post.await_args.args[1] == expected


def test_falls_back_to_contract_summary_when_agent_is_silent():
    payload = {"run_id": "X-1", "issue_key": "X", "iteration": 1, "args": {}}
    with patch.object(backtest_mcp, "MCPServerStdio", return_value=_DummyServer()), \
         patch.object(backtest_mcp, "Agent", MagicMock()), \
         patch.object(backtest_mcp, "build_agents_model", return_value=MagicMock()), \
         patch.object(backtest_mcp.Runner, "run",
                      AsyncMock(return_value=SimpleNamespace(final_output=""))), \
         patch.object(backtest_mcp, "postprocess_via_mcp_async",
                      AsyncMock(return_value=dict(_CONTRACT))):
        result = backtest_mcp.run_backtest_mcp(
            payload,
            provider_config=_PROVIDER_CONFIG,
        )
    assert result["summary"] == _CONTRACT["summary"]


class TestParseParams:
    """The Jira `params:` raw string -> extra_params dict (IW seam)."""

    def test_empty_is_noop(self):
        assert backtest_mcp._parse_params(None) == {}
        assert backtest_mcp._parse_params("") == {}

    def test_simple_pairs(self):
        assert backtest_mcp._parse_params("NFREQ=4, NPORT=50") == {"NFREQ": "4", "NPORT": "50"}

    def test_comma_inside_value_is_rejoined(self):
        # active-rule lists carry their own commas; they belong to the previous key.
        assert backtest_mcp._parse_params("VALUE_ACTIVE_RULES=4,5") == {"VALUE_ACTIVE_RULES": "4,5"}

    def test_mixed_scalars_and_list_value(self):
        assert backtest_mcp._parse_params("NFREQ=4, VALUE_ACTIVE_RULES=1,2,3, NPORT=50") == {
            "NFREQ": "4", "VALUE_ACTIVE_RULES": "1,2,3", "NPORT": "50",
        }


class TestParamsClauseInPrompt:
    def test_clause_present_only_when_params_supplied(self):
        with_clause = backtest_mcp._build_prompt(
            "J", "2020-01-01", "2020-12-31", None,
            ' Also pass extra_params={"NFREQ": "4"} to submit_backtest ...')
        without = backtest_mcp._build_prompt("J", "2020-01-01", "2020-12-31", None)
        assert "extra_params" in with_clause
        assert "extra_params" not in without
