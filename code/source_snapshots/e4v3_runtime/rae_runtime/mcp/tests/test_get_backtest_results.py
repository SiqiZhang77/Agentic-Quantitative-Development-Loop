"""Tests for the get_backtest_results MCP tool."""

from get_backtest_results import get_backtest_results


def _make_run(tmp_path, job="JOB1"):
    run = tmp_path / "simulation-results" / job
    run.mkdir(parents=True)
    (run / "resultsTable.csv").write_text(
        "jobID, CARRY_RESET, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
        "JOB1, true, 1.42, 2.01, 0.18, 0.45, 0.15, 0.08\n"
    )
    return tmp_path


def test_completed_returns_metrics(tmp_path):
    runtime = _make_run(tmp_path)
    out = get_backtest_results("JOB1", runtime_dir=runtime)

    assert out["status"] == "completed"
    assert out["schema_version"] == "1.0"
    assert out["job_name"] == "JOB1"
    assert out["metrics"]["sharpe_ratio"] == 1.42
    assert out["metrics"]["total_return"] == 0.45
    assert out["metrics"]["max_drawdown"] == -0.08
    assert out["results_table_path"].endswith("resultsTable.csv")
    assert out["errors"] == []
    assert "Sharpe" in out["summary"]


def test_missing_run_dir_returns_error(tmp_path):
    out = get_backtest_results("NOPE", runtime_dir=tmp_path)
    assert out["status"] == "failed"
    assert out["metrics"] == {}
    assert out["results_table_path"] is None
    assert out["errors"]


def test_header_only_results_table_is_failed_not_completed(tmp_path):
    # The engine "MISSING REPORT" case: resultsTable.csv exists but has only a
    # header row and no data. Must report failed (with a reason), never a silent
    # completed/empty result that the agent loop would treat as success.
    run = tmp_path / "simulation-results" / "JOB1"
    run.mkdir(parents=True)
    (run / "resultsTable.csv").write_text(
        "jobID, CARRY_RESET, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
    )
    out = get_backtest_results("JOB1", runtime_dir=tmp_path)

    assert out["status"] == "failed"
    assert out["metrics"] == {}
    assert out["summary"] == ""
    assert out["errors"]
    assert out["results_table_path"].endswith("resultsTable.csv")
