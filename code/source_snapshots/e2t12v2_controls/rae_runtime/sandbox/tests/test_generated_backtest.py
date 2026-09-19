"""Zero-code tickets with engine params run the deterministic generate–run path:
the .request is built from the declared baseline + the ticket's params (no LLM in
the config path) and submitted as-is.

Nothing is committed: the ticket declared zero_code_modifications, and run.py
reports the run that way. The config stays auditable without a commit because the
generation is deterministic — baseline + the ticket's params are both already
recorded, so it can be re-derived from the ticket.
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest

import mcp_client


def test_engine_params_parses_the_raw_params_string():
    # The validator forwards every ticket OPTION in parsed_task_parameters and
    # leaves the params value as one raw string — engine keys live only there.
    payload = {"args": {
        "strategy": "Backtest the volatility expert",
        "start": "2000-01-17",
        "end": "2025-01-02",
        "strategy_type": "backtest",
        "model": "qwen3-coder",
        "max_iterations": 1,
        "zero_code_modifications": True,
        "params": "atrade_sifting_pretrade=volatility-variants-hz, "
                  "VOLATILITY_ACTIVE_RULES=0,1",
    }}
    assert mcp_client._engine_params(payload) == {
        "atrade_sifting_pretrade": "volatility-variants-hz",
        "VOLATILITY_ACTIVE_RULES": "0,1",  # value commas survive the split
    }


def test_engine_params_empty_when_no_params_option():
    assert mcp_client._engine_params(
        {"args": {"strategy": "x", "model": "qwen3-coder"}}) == {}
    assert mcp_client._engine_params({"args": {"params": "   "}}) == {}


def test_zero_code_routing_mirrors_run_py():
    assert mcp_client._zero_code(
        {"execution_objectives": {"zero_code_modifications": True}})
    assert mcp_client._zero_code({"code_free": True})
    assert not mcp_client._zero_code({"execution_objectives": {}, "code_free": False})


def test_local_baseline_path_resolves_next_to_the_server(tmp_path, monkeypatch):
    template = tmp_path / "mcp" / "templates" / "volatility_factor.request"
    template.parent.mkdir(parents=True)
    template.write_text("STARTDATE = 2000-01-17\n")
    monkeypatch.setenv("MCP_SERVER_PATH", str(tmp_path / "mcp" / "server.py"))
    monkeypatch.chdir(tmp_path)  # the as-is CWD lookup must miss
    assert mcp_client._local_baseline_path(
        "rae_runtime/mcp/templates/volatility_factor.request") == str(template)
    assert mcp_client._local_baseline_path(
        "rae_runtime/mcp/templates/other.request") is None


def _fake_github(monkeypatch, **fns):
    mod = types.ModuleType("github_client")
    for name, fn in fns.items():
        setattr(mod, name, fn)
    monkeypatch.setitem(sys.modules, "github_client", mod)


def test_stage_baseline_optional_degrades_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")

    def boom(branch, path):
        raise RuntimeError("branch does not exist")

    _fake_github(monkeypatch, get_strategy_code=boom)
    payload = {"issue_key": "SCRUM-1", "result_path": str(tmp_path / "r.json"),
               "strategy": {"path": "rae_runtime/proxy/strategy.request"}}
    assert mcp_client.stage_strategy_baseline(payload, required=False) is None
    with pytest.raises(mcp_client.BacktestBaselineError):
        mcp_client.stage_strategy_baseline(payload)  # default stays strict


class _FakeClient:
    """Stands in for the MCP client: returns submit_backtest's dry_run generation."""

    def __init__(self, request_text="NPORT = 5\n"):
        self.request_text = request_text
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return types.SimpleNamespace(
            data={"status": "dry_run", "request": self.request_text, "warnings": []}
        )


def test_generate_baseline_never_commits_on_a_zero_code_run(tmp_path, monkeypatch):
    """A zero-code run must not write to the ticket branch.

    run.py reports this run as zero_code_modifications=true with empty
    modified_files, so a commit here would make the Jira write-back state
    something untrue. The generation is deterministic (baseline + the ticket's
    params), so the config stays reproducible from the ticket without one.
    """
    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")

    def must_not_commit(*args, **kwargs):
        raise AssertionError("a zero-code run must never commit to the ticket branch")

    def no_committed_baseline(branch, path):
        raise RuntimeError("branch does not exist")

    _fake_github(
        monkeypatch,
        get_strategy_code=no_committed_baseline,
        create_feature_branch=must_not_commit,
        push_strategy_code=must_not_commit,
    )

    payload = {
        "issue_key": "SCRUM-2",
        "result_path": str(tmp_path / "result.json"),
        "execution_objectives": {"zero_code_modifications": True},
        "strategy": {"path": "rae_runtime/proxy/strategy.request"},
        "args": {"params": "NPORT=5"},
    }
    client = _FakeClient()

    staged = asyncio.run(
        mcp_client._generate_baseline(client, payload, {"NPORT": "5"})
    )

    # The generated config is staged for inspection, not pushed to the branch.
    assert Path(staged).read_text() == "NPORT = 5\n"
    assert Path(staged).parent == tmp_path
    # It was generated deterministically over MCP, with no LLM in the config path.
    assert client.calls[0][0] == "submit_backtest"
    assert client.calls[0][1]["dry_run"] is True
    assert client.calls[0][1]["extra_params"] == {"NPORT": "5"}


def test_response_reports_the_executed_request_so_it_is_uploaded(tmp_path):
    """Removing the commit only stays honest if the config survives the run.

    The staging directory is a temporary mount the orchestrator deletes after
    ingesting the run, so the .request has to be referenced in the response to be
    uploaded as an attachment.
    """
    from result_builder import build_response

    resp = build_response(
        run_id="run_SCRUM-2_1",
        ticket_id="SCRUM-2",
        start_time="2026-07-17T10:00:00",
        end_time="2026-07-17T10:05:00",
        outcome="success",
        traces=[],
        backtest={
            "metrics": {"sharpe_ratio": 1.1},
            "executed_request_path": "/workspace/output/quant-SCRUM-2.generated.request",
        },
        pipeline_out={"status": "no_changes", "artifacts": {"no_code_changes": True}},
    )

    assert resp["generated_artifacts"]["executed_request_path"] == (
        "/workspace/output/quant-SCRUM-2.generated.request"
    )
    # Still a zero-code run: the config is an attachment, not a commit.
    assert resp["execution_summary"]["zero_code_modifications"] is True
    assert resp["generated_artifacts"]["modified_files"] == []


def test_executed_request_is_null_when_no_request_was_staged():
    from result_builder import build_response

    resp = build_response(
        run_id="r", ticket_id="SCRUM-2",
        start_time="2026-07-17T10:00:00", end_time="2026-07-17T10:05:00",
        outcome="success", traces=[], backtest={"metrics": {}},
    )

    assert resp["generated_artifacts"]["executed_request_path"] is None


def test_unreadable_params_are_reported_not_silently_dropped(capsys):
    # The JSON-object form carries no KEY=value pairs. Dropping it in silence would
    # run the baseline's own (heavier) parameters while the ticket looks like it
    # asked for something lighter.
    assert mcp_client._engine_params(
        {"args": {"params": '{"NPORT": 5, "NFREQ": 1}'}}) == {}

    warning = capsys.readouterr().err
    assert "Engine params ignored" in warning
    assert "KEY=value" in warning


def test_readable_params_do_not_warn(capsys):
    assert mcp_client._engine_params({"args": {"params": "NPORT=5"}}) == {"NPORT": "5"}
    assert "Engine params ignored" not in capsys.readouterr().err
