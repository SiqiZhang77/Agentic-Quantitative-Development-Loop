"""Backtest-window guard: a window too short to clear the engine warmup
(max(NDELAY, LTU_WINDOW_SIZE) trading days) produces an all-zero resultsTable, so
submit_backtest must fail fast instead of shipping the degenerate run. The guard
evaluates the *effective* dates (override else baseline), so inheriting the
baseline's long window is never flagged.
"""
import pytest

from submit_backtest import submit_backtest, _window_guard_error, _baseline_value


def _real(monkeypatch, tmp_path):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", str(tmp_path / "req"))
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(tmp_path / "res"))
    (tmp_path / "req").mkdir()
    (tmp_path / "res").mkdir()


def test_one_month_window_fails(monkeypatch, tmp_path):
    _real(monkeypatch, tmp_path)
    out = submit_backtest("j", start_date="2024-12-01", end_date="2024-12-31")
    assert out["status"] == "failed"
    assert "too short" in out["error"] and "warmup" in out["error"]
    # nothing was written
    assert not any((tmp_path / "req").iterdir())


def test_one_year_window_fails(monkeypatch, tmp_path):
    # The old scripted-driver default (2020-01-01..2020-12-31) can't clear the
    # 756-day LTU warmup and would silently return zeros.
    _real(monkeypatch, tmp_path)
    out = submit_backtest("j", start_date="2020-01-01", end_date="2020-12-31")
    assert out["status"] == "failed"


def test_inverted_window_fails(monkeypatch, tmp_path):
    _real(monkeypatch, tmp_path)
    out = submit_backtest("j", start_date="2020-12-31", end_date="2020-01-01")
    assert out["status"] == "failed"
    assert "inverted" in out["error"] or "after STARTDATE" in out["error"]


def test_inherited_long_baseline_window_passes(monkeypatch, tmp_path):
    # No date overrides -> uses the baseline's 2000..2025 window -> guard is silent.
    _real(monkeypatch, tmp_path)
    out = submit_backtest("j")
    assert out["status"] == "submitted"


def test_start_only_uses_baseline_enddate_and_passes(monkeypatch, tmp_path):
    _real(monkeypatch, tmp_path)
    out = submit_backtest("j", start_date="2020-01-01")  # end from baseline (2025)
    assert out["status"] == "submitted"


def test_dry_run_surfaces_window_as_warning_not_failure():
    out = submit_backtest("j", start_date="2024-12-01", end_date="2024-12-31", dry_run=True)
    assert out["status"] == "dry_run"
    assert any("too short" in w for w in out["warnings"])


def test_min_sim_days_env_is_respected(monkeypatch):
    # A generous simulation-day requirement rejects a window the default would pass.
    template = "STARTDATE=2018-01-01\nENDDATE=2025-01-01\nNDELAY=208\nLTU_WINDOW_SIZE=756\n"
    assert _window_guard_error({}, template) is None
    monkeypatch.setenv("BACKTEST_MIN_SIM_TRADING_DAYS", "3000")
    assert _window_guard_error({}, template) is not None


def test_guard_skips_when_no_warmup_configured():
    template = "STARTDATE=2020-01-01\nENDDATE=2020-02-01\n"  # no NDELAY / LTU
    assert _window_guard_error({}, template) is None
