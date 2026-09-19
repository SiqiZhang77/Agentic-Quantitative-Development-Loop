import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "get_backtest_results.py"
SPEC = importlib.util.spec_from_file_location("after_backtest_get_backtest_results", MODULE_PATH)
assert SPEC is not None
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
get_backtest_results = MODULE.get_backtest_results


def _make_run(tmp_path, job_name="JOB1"):
    run_dir = tmp_path / job_name
    run_dir.mkdir()
    return run_dir


def test_header_only_results_table_fails_without_fake_metrics(tmp_path):
    run_dir = _make_run(tmp_path)
    (run_dir / "resultsTable.csv").write_text(
        "jobID, GROWTH_MAXEPS5YGROWTHRATE, SHARPE, MAX-DRAWDOWN\n",
        encoding="utf-8",
    )

    out = get_backtest_results("JOB1", ticket_id="SCRUM-9", results_dir=str(tmp_path))

    assert out["execution_summary"]["status"] == "FAILED"
    assert out["performance_metrics"]["sharpe_ratio"] is None
    assert out["performance_metrics"]["max_drawdown"] is None
    assert "header but no data row" in out["diagnostics"]["error_message"]


def test_wide_results_table_parses_real_metric_row(tmp_path):
    run_dir = _make_run(tmp_path)
    (run_dir / "resultsTable.csv").write_text(
        "jobID, GROWTH_MAXEPS5YGROWTHRATE, SHARPE, PROFIT, MAX-DRAWDOWN\n"
        "JOB1, 0.5, 1.42, 0.45, 0.08\n",
        encoding="utf-8",
    )

    out = get_backtest_results("JOB1", ticket_id="SCRUM-9", results_dir=str(tmp_path))

    assert out["execution_summary"]["status"] == "SUCCESS"
    assert out["performance_metrics"]["sharpe_ratio"] == 1.42
    assert out["performance_metrics"]["total_return"] == 0.45
    assert out["performance_metrics"]["max_drawdown"] == 0.08
