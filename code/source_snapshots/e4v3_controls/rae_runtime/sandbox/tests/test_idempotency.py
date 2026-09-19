"""Unit tests for RAE-16: retry idempotency (safe re-runs) and LLM cost tracking.

Covers:
  - BudgetGuard.record_llm_response / usage_dict (cost tracking extension)
  - BudgetGuard enforcement still works after recording
  - github_client idempotent branch creation + push (offline mode)
  - pipeline.run_pipeline retry diagnostics in result payload
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

_SANDBOX = Path(__file__).parent.parent
for _p in (str(_SANDBOX), str(_SANDBOX / "proxy")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from budget_guard import BudgetGuard, BudgetExceeded, RunTimeout


class TestBudgetGuardRecording:
    def _litellm_response(self, prompt_tokens=100, completion_tokens=50, cost=0.003):
        return {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "response_cost": cost,
        }

    def test_records_single_call(self):
        guard = BudgetGuard()
        guard.record_llm_response(self._litellm_response(), model="nova-micro")
        usage = guard.usage_dict()
        assert usage["calls"] == 1
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150
        assert usage["cost_usd"] == pytest.approx(0.003)
        assert usage["model"] == "nova-micro"

    def test_accumulates_across_calls(self):
        guard = BudgetGuard()
        guard.record_llm_response(self._litellm_response(100, 50, 0.001), model="m")
        guard.record_llm_response(self._litellm_response(200, 80, 0.002), model="m")
        usage = guard.usage_dict()
        assert usage["calls"] == 2
        assert usage["prompt_tokens"] == 300
        assert usage["completion_tokens"] == 130
        assert usage["total_tokens"] == 430
        assert usage["cost_usd"] == pytest.approx(0.003)

    def test_extracts_cost_from_hidden_params(self):
        resp = {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "_hidden_params": {"response_cost": 0.0042},
        }
        guard = BudgetGuard()
        guard.record_llm_response(resp, model="m")
        assert guard.usage_dict()["cost_usd"] == pytest.approx(0.0042)

    def test_handles_missing_usage_gracefully(self):
        resp = {"choices": [{"message": {"content": "x"}}]}
        guard = BudgetGuard()
        guard.record_llm_response(resp, model="m")
        usage = guard.usage_dict()
        assert usage["calls"] == 1
        assert usage["total_tokens"] == 0
        assert usage["cost_usd"] == 0.0

    def test_uses_input_output_token_keys(self):
        resp = {
            "usage": {"input_tokens": 40, "output_tokens": 20},
            "cost": 0.001,
        }
        guard = BudgetGuard()
        guard.record_llm_response(resp, model="m")
        assert guard.usage_dict()["prompt_tokens"] == 40
        assert guard.usage_dict()["completion_tokens"] == 20

    def test_initial_usage_dict_is_zeroed(self):
        guard = BudgetGuard()
        usage = guard.usage_dict()
        assert usage["calls"] == 0
        assert usage["total_tokens"] == 0
        assert usage["cost_usd"] == 0.0
        assert usage["model"] is None


class TestBudgetGuardEnforcement:
    def test_record_triggers_budget_exceeded(self):
        guard = BudgetGuard(max_token_budget=100)
        resp = {
            "usage": {"prompt_tokens": 60, "completion_tokens": 50},
        }
        with pytest.raises(BudgetExceeded, match="token budget"):
            guard.record_llm_response(resp, model="m")

    def test_record_under_budget_does_not_raise(self):
        guard = BudgetGuard(max_token_budget=200)
        resp = {"usage": {"prompt_tokens": 60, "completion_tokens": 50}}
        guard.record_llm_response(resp, model="m")
        assert guard.usage_dict()["total_tokens"] == 110

    def test_existing_tick_iteration_still_works(self):
        guard = BudgetGuard(max_iterations=2)
        guard.tick_iteration()
        guard.tick_iteration()
        with pytest.raises(RunTimeout, match="max_iterations"):
            guard.tick_iteration()

    def test_existing_add_tokens_still_works(self):
        guard = BudgetGuard(max_token_budget=50)
        guard.add_tokens(30)
        with pytest.raises(BudgetExceeded):
            guard.add_tokens(30)


class TestGithubClientIdempotency:
    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RAE_OFFLINE", "1")
        monkeypatch.setenv("OFFLINE_PUSH_DIR", str(tmp_path))
        self.tmp_path = tmp_path

    def test_push_skips_when_content_identical(self):
        from github_client import push_strategy_code

        code = "def foo(): pass"
        target = self.tmp_path / "pushed_strategy.py"
        target.write_text(code, "utf-8")

        result = push_strategy_code(code, "b", "p", "msg")
        assert "no changes" in result.lower()

    def test_push_writes_when_content_differs(self):
        from github_client import push_strategy_code

        target = self.tmp_path / "pushed_strategy.py"
        target.write_text("old code", "utf-8")

        result = push_strategy_code("new code", "b", "p", "msg")
        assert "wrote" in result.lower()
        assert target.read_text("utf-8") == "new code"

    def test_push_creates_file_when_absent(self):
        from github_client import push_strategy_code

        result = push_strategy_code("brand new", "b", "p", "msg")
        assert "wrote" in result.lower()
        assert (self.tmp_path / "pushed_strategy.py").read_text("utf-8") == "brand new"

    def test_create_branch_offline_is_idempotent(self):
        from github_client import create_feature_branch

        r1 = create_feature_branch("quant/X")
        r2 = create_feature_branch("quant/X")
        assert r1 == r2
        assert "skipped" in r1.lower() or "offline" in r1.lower()


class TestLlmClientBudgetGuard:
    def test_call_llm_offline_ignores_guard(self, monkeypatch):
        monkeypatch.setenv("RAE_OFFLINE", "1")
        from llm_client import call_llm

        guard = BudgetGuard()
        result = call_llm("test prompt", budget_guard=guard)
        assert "generate_signals" in result
        assert guard.usage_dict()["calls"] == 0

    def test_call_llm_records_when_guard_provided(self, monkeypatch):
        monkeypatch.delenv("RAE_OFFLINE", raising=False)
        monkeypatch.setenv("API_KEY", "dummy")
        from llm_client import call_llm

        fake_body = {
            "choices": [{"message": {"content": "modified code"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 40},
            "response_cost": 0.005,
        }
        fake_resp = MagicMock()
        fake_resp.json.return_value = fake_body
        fake_resp.status_code = 200

        guard = BudgetGuard()
        with patch("llm_client.requests.post", return_value=fake_resp):
            result = call_llm("test prompt", budget_guard=guard)

        assert result == "modified code"
        assert guard.usage_dict()["calls"] == 1
        assert guard.usage_dict()["prompt_tokens"] == 80
        assert guard.usage_dict()["cost_usd"] == pytest.approx(0.005)

    def test_call_llm_works_without_guard(self, monkeypatch):
        monkeypatch.delenv("RAE_OFFLINE", raising=False)
        monkeypatch.setenv("API_KEY", "dummy")
        from llm_client import call_llm

        fake_body = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        fake_resp = MagicMock()
        fake_resp.json.return_value = fake_body
        fake_resp.status_code = 200

        with patch("llm_client.requests.post", return_value=fake_resp):
            result = call_llm("test")
        assert result == "ok"


def _base_payload(**overrides):
    p = {
        "run_id": "TEST-1-001",
        "issue_key": "TEST-1",
        "command": "backtest",
        "args": {"strategy": "add RSI filter"},
        "strategy": {
            "repo_url": "https://github.com/example/repo",
            "ref": "main",
            "path": "strategy.py",
        },
    }
    p.update(overrides)
    return p


class TestPipelineRetryDiagnostics:
    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, tmp_path):
        monkeypatch.setenv("RAE_OFFLINE", "1")
        monkeypatch.setenv("OFFLINE_PUSH_DIR", str(tmp_path))
        self.tmp_path = tmp_path

    def test_result_has_diagnostics_block(self):
        from pipeline import run_pipeline

        result = run_pipeline(_base_payload())
        assert "diagnostics" in result
        assert "retry_safe" in result["diagnostics"]
        rs = result["diagnostics"]["retry_safe"]
        assert rs["ticket_id"] == "TEST-1"
        assert rs["run_id"] == "TEST-1-001"
        assert "branch" in rs
        assert "commit" in rs

    def test_branch_action_on_first_run(self):
        from pipeline import run_pipeline

        result = run_pipeline(_base_payload())
        rs = result["diagnostics"]["retry_safe"]
        assert rs["branch"]["name"] == "quant/TEST-1"

    def test_commit_action_reports_committed(self):
        from pipeline import run_pipeline

        result = run_pipeline(_base_payload())
        rs = result["diagnostics"]["retry_safe"]
        assert rs["commit"]["action"] == "committed"
        assert rs["commit"]["changed"] is True

    def test_commit_action_reports_skipped_on_rerun(self):
        from pipeline import run_pipeline

        result1 = run_pipeline(_base_payload())
        assert result1["diagnostics"]["retry_safe"]["commit"]["action"] == "committed"

        result2 = run_pipeline(_base_payload())
        assert result2["diagnostics"]["retry_safe"]["commit"]["action"] == "skipped"
        assert result2["diagnostics"]["retry_safe"]["commit"]["changed"] is False

    def test_usage_block_present(self):
        from pipeline import run_pipeline

        result = run_pipeline(_base_payload())
        assert "usage" in result
        usage = result["usage"]
        assert "calls" in usage
        assert "total_tokens" in usage
        assert "cost_usd" in usage

    def test_usage_with_iteration_controls(self):
        from pipeline import run_pipeline

        payload = _base_payload(iteration_controls={"max_token_budget": 100000})
        result = run_pipeline(payload)
        assert result["usage"]["total_tokens"] == 0

    def test_tracer_records_steps(self):
        from pipeline import run_pipeline
        from trace import TraceCollector

        tracer = TraceCollector()
        run_pipeline(_base_payload(), tracer=tracer)
        entries = tracer.as_list()
        tools = [e["tool_call"] for e in entries]
        assert "create_feature_branch" in tools
        assert "get_strategy_code" in tools
        assert "call_llm" in tools
        assert "push_strategy_code" in tools
