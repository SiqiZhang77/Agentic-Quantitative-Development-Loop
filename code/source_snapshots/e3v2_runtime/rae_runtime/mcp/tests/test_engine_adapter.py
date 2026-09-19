"""Deterministic contract tests for engine_adapter (RAE-10).

For now uses small fabricated resultsTable.csv fixtures. Swap in the FISH
golden dataset + its expected contract JSON when available, for a real
no-regression check on the mock -> real cutover.
"""

from engine_adapter import adapt_results_table, validate


def test_maps_known_columns(tmp_path):
    p = tmp_path / "resultsTable.csv"
    p.write_text(
        "SHARPE,SORTINO,VOL,ANNUAL-RTN,MAX-DRAWDOWN,TOTAL-RETURN\n"
        "1.20,1.60,0.20,0.15,-0.08,0.45\n"
    )
    out = validate(adapt_results_table(p))
    assert out["sharpe_ratio"] == 1.20
    assert out["sortino_ratio"] == 1.60
    assert out["max_drawdown"] == -0.08
    assert out["total_return"] == 0.45


def test_passes_through_new_columns(tmp_path):
    p = tmp_path / "resultsTable.csv"
    p.write_text("SHARPE,NEW_METRIC\n1.0,3.3\n")
    out = adapt_results_table(p)
    assert out["sharpe_ratio"] == 1.0
    assert out["NEW_METRIC"] == 3.3  # forward-compatible: new field kept, not dropped


def test_skips_leading_config_columns(tmp_path):
    """Real resultsTable.csv echoes config first, metrics last - only metrics out."""
    p = tmp_path / "resultsTable.csv"
    # mirrors the real layout: jobID/from/to + config params, then the 6 metrics
    p.write_text(
        "jobID, from, to, CARRY_RESET, DYNMCAPTOPN, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
        "FISH1, 2007-02-08, 2025-01-02, true, 40, 1.42, 2.01, 0.18, 0.45, 0.15, -0.08\n"
    )
    out = adapt_results_table(p)
    assert set(out) == {
        "sharpe_ratio", "sortino_ratio", "volatility",
        "total_return", "annual_return", "max_drawdown",
    }
    assert out["sharpe_ratio"] == 1.42
    assert out["total_return"] == 0.45        # from PROFIT
    assert out["max_drawdown"] == -0.08
    assert "jobID" not in out and "CARRY_RESET" not in out  # config excluded


def test_new_trailing_metric_passes_through(tmp_path):
    """A new metric added after the known block is kept (forward-compatible)."""
    p = tmp_path / "resultsTable.csv"
    p.write_text(
        "jobID, CARRY_RESET, SHARPE, MAX-DRAWDOWN, CALMAR\n"
        "FISH1, true, 1.4, -0.08, 2.6\n"
    )
    out = adapt_results_table(p)
    assert out["sharpe_ratio"] == 1.4
    assert out["CALMAR"] == 2.6               # new trailing metric kept
    assert "CARRY_RESET" not in out           # leading config still excluded


def test_missing_required_warns_but_does_not_raise(tmp_path, capsys):
    p = tmp_path / "resultsTable.csv"
    p.write_text("SORTINO\n1.6\n")  # no sharpe/max_drawdown/total_return
    out = validate(adapt_results_table(p))  # must not raise
    assert out["sortino_ratio"] == 1.6
    assert "missing required metrics" in capsys.readouterr().err
