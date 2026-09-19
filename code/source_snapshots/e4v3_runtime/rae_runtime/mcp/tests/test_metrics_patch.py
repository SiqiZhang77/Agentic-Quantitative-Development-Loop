"""Tests for metrics_patch: helper-patch shape + contract-field trimming."""

import json
import zipfile

from metrics_patch import build_metrics_patch, write_metrics_patch


def _make_run(tmp_path, job="JOB1"):
    run = tmp_path / job
    run.mkdir()
    inner = f"opt/simulations-service/deployed-jobs/{job}/resultsTable.csv"
    with zipfile.ZipFile(run / f"{job}-log.zip", "w") as zf:
        zf.writestr(
            inner,
            "jobID, CARRY_RESET, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
            f"{job}, true, 1.42, 2.01, 0.18, 0.45, 0.15, 0.08\n",
        )
    return run


def test_patch_shape_and_contract_fields(tmp_path):
    patch = build_metrics_patch("JOB1", _make_run(tmp_path))
    assert patch["tool_name"] == "get_backtest_results"
    assert patch["status"] == "completed"
    assert patch["job_name"] == "JOB1"

    pm = patch["performance_metrics_update"]
    # trimmed to the closed contract fields only - no sortino/vol/annual
    assert set(pm) == {"total_return", "sharpe_ratio", "max_drawdown"}
    assert pm["sharpe_ratio"] == 1.42
    assert pm["total_return"] == 0.45      # from PROFIT
    assert pm["max_drawdown"] == -0.08     # negated


def test_write_patch_to_output_dir(tmp_path):
    out = tmp_path / "output"
    path = write_metrics_patch("JOB1", _make_run(tmp_path), output_dir=out)
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["performance_metrics_update"]["sharpe_ratio"] == 1.42
