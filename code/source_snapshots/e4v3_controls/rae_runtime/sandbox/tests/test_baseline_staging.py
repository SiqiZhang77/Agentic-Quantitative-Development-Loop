"""stage_strategy_baseline: the backtest runs the strategy .request the edit
agent committed to the ticket's quant branch (the edit -> engine connection).
It fetches quant/<ticket>:<path>, stages it locally, and returns that path for
submit_backtest's baseline — degrading to the default template (None) when it
can't, so a fetch problem never fails the backtest.
"""
import sys
import types
from pathlib import Path

import pytest

import mcp_client


def _payload(tmp_path, path="rae_runtime/proxy/strategy.request", issue_key="SCRUM-9"):
    return {
        "issue_key": issue_key,
        "result_path": str(tmp_path / "result.json"),
        "strategy": {"path": path},
    }


def _fake_github(monkeypatch, fn):
    mod = types.ModuleType("github_client")
    mod.get_strategy_code = fn
    monkeypatch.setitem(sys.modules, "github_client", mod)


def test_none_when_path_is_not_a_request(tmp_path, monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    assert mcp_client.stage_strategy_baseline(
        _payload(tmp_path, path="rae_runtime/proxy/strategy.py")) is None


def test_none_when_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("RAE_OFFLINE", "1")
    assert mcp_client.stage_strategy_baseline(_payload(tmp_path)) is None


def test_fetches_from_quant_branch_and_stages(tmp_path, monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    seen = {}

    def fake(branch, path):
        seen["branch"], seen["path"] = branch, path
        return "purpose = backtest\nNPORT = 80\n"

    _fake_github(monkeypatch, fake)
    staged = mcp_client.stage_strategy_baseline(_payload(tmp_path))

    assert seen == {"branch": "quant/SCRUM-9", "path": "rae_runtime/proxy/strategy.request"}
    assert staged is not None
    assert Path(staged).read_text(encoding="utf-8") == "purpose = backtest\nNPORT = 80\n"


def test_mock_mode_falls_back_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    monkeypatch.setenv("USE_REAL_BACKTESTER", "false")

    def boom(branch, path):
        raise RuntimeError("404 not found")

    _fake_github(monkeypatch, boom)
    # Mock: no real engine, so degrade to the default template rather than fail.
    assert mcp_client.stage_strategy_baseline(_payload(tmp_path)) is None


def test_real_mode_raises_on_fetch_error(tmp_path, monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")

    def boom(branch, path):
        raise RuntimeError("404 not found")

    _fake_github(monkeypatch, boom)
    # Real: must NOT silently use the default template — fail with diagnostics.
    with pytest.raises(mcp_client.BacktestBaselineError) as exc:
        mcp_client.stage_strategy_baseline(_payload(tmp_path))
    assert "quant/SCRUM-9" in str(exc.value) and "strategy.request" in str(exc.value)
