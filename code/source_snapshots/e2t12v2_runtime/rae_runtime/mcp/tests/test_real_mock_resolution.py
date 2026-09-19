"""Real vs mock backtester dir resolution (USE_REAL_BACKTESTER).

The requests dir, results root, and status polling must all resolve to the same
explicit mounted location when submitting for real, and stay on the mock dir
otherwise. Real completion is a per-job <job_name>/ directory under the results
root holding the request, a job-*-log.zip, and resultsTable.csv — the same shape
the mock produces.
"""
from datetime import datetime, timezone
from pathlib import Path

import get_backtest_status as gbs
from submit_backtest import get_transport, results_root


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _past_iso():
    return datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()


def test_mock_dirs(monkeypatch):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "false")
    monkeypatch.setenv("MOCK_RUNTIME_DIR", "/tmp/mock_runtime")
    monkeypatch.setenv("BACKTEST_REQUESTS_DIR", "/tmp/mock_runtime/backtest-requests")
    assert str(results_root()) == "/tmp/mock_runtime/simulation-results"
    assert str(get_transport().directory) == "/tmp/mock_runtime/backtest-requests"


def test_real_dirs_require_explicit_absolute_paths(monkeypatch):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.delenv("SIMULATION_REQUESTS_DIR", raising=False)
    monkeypatch.delenv("SIMULATION_RESULTS_DIR", raising=False)
    import pytest
    with pytest.raises(ValueError, match="SIMULATION_RESULTS_DIR must be set"):
        results_root()
    with pytest.raises(ValueError, match="SIMULATION_REQUESTS_DIR must be set"):
        get_transport()


def test_real_dirs_reject_tilde(monkeypatch):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", "~/simulation-requests")
    import pytest
    with pytest.raises(ValueError, match="~"):
        get_transport()


def test_real_results_dir_override(monkeypatch):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    import pytest
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", "/data/sim-results")
    with pytest.raises(OSError, match="does not exist"):
        results_root()


def test_real_dirs_use_explicit_absolute_paths(monkeypatch, tmp_path):
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", str(requests))
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(results))
    assert results_root() == results
    assert get_transport().directory == requests


def test_mock_completion_is_a_per_job_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "false")
    monkeypatch.setenv("MOCK_RUNTIME_DIR", str(tmp_path))
    submitted_at = _past_iso()
    job = "BT-1-i1"
    job_dir = tmp_path / "simulation-results" / job
    job_dir.mkdir(parents=True)
    (job_dir / "resultsTable.csv").write_text("SHARPE\n1.0\n")
    out = gbs.get_backtest_status(job, submitted_at)
    assert out["status"] == "completed"
    assert "resultsTable.csv" in out["output_files"]


def test_real_completion_is_a_per_job_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    submitted_at = _past_iso()
    job = "BT-1-i1"
    # Confirmed engine output layout: <results-root>/<job>/ with these 3 files.
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / f"{job}.request").write_text("purpose = backtest\n")
    (job_dir / "job-73-log.zip").write_text("zip")
    (job_dir / "resultsTable.csv").write_text("SHARPE\n1.0\n")
    out = gbs.get_backtest_status(job, submitted_at)
    assert out["status"] == "completed"
    assert out["results_dir"] == str(job_dir)
    assert "resultsTable.csv" in out["output_files"]
    assert "job-73-log.zip" in out["output_files"]


def test_real_result_directory_without_results_table_is_still_running(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    submitted_at = _past_iso()
    job = "BT-1-i1"
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / f"{job}.request").write_text("purpose = backtest\n")
    (job_dir / "job-73-log.zip").write_text("zip")

    out = gbs.get_backtest_status(job, submitted_at, timeout_seconds=999999999)

    assert out["status"] == "running"
    assert out["waiting_for"] == "resultsTable.csv"
    assert out["expected_results_path"] == str(job_dir)
    assert "job-73-log.zip" in out["output_files"]


def test_real_result_directory_without_results_table_times_out(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    submitted_at = "2020-01-01T00:00:00+00:00"
    job = "BT-1-i1"
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / f"{job}.request").write_text("purpose = backtest\n")

    out = gbs.get_backtest_status(job, submitted_at, timeout_seconds=1)

    assert out["status"] == "timeout"
    assert "resultsTable.csv" in out["error"]
    assert out["expected_results_path"] == str(job_dir)


def test_real_header_only_results_table_is_failed(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    submitted_at = _past_iso()
    job = "BT-1-i1"
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / "resultsTable.csv").write_text(
        "jobID, GROWTH_MAXEPS5YGROWTHRATE, SHARPE, MAX-DRAWDOWN\n"
    )

    out = gbs.get_backtest_status(job, submitted_at, timeout_seconds=5400)

    assert out["status"] == "failed"
    assert out["error"] == "resultsTable.csv exists but contains no data row"
    assert out["results_table_path"] == str(job_dir / "resultsTable.csv")


def test_stale_real_completion_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    job = "BT-1-i1"
    job_dir = tmp_path / job
    job_dir.mkdir()
    (job_dir / "resultsTable.csv").write_text("SHARPE\n1.0\n")
    old = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
    import os
    os.utime(job_dir, (old, old))
    out = gbs.get_backtest_status(job, _now_iso())
    assert out["status"] == "running"
    assert out["ignored_stale_result"] is True


def test_real_still_running_when_no_output_yet(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path))
    out = gbs.get_backtest_status("BT-1-i1", _now_iso(), timeout_seconds=5400)
    assert out["status"] == "running"
