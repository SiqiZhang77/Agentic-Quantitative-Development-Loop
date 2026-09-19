"""End-to-end test for the RAE-10 results pipeline (collect_results).

Builds a results directory containing a ZIP with the real nested layout
(opt/.../deployed-jobs/<job>/resultsTable.csv), then runs the full chain
locate -> adapt -> validate and checks the metrics come out clean.
"""

import zipfile

from collect_results import collect_results


def _make_results_dir(tmp_path, job="JOB1"):
    run = tmp_path / job
    run.mkdir()
    inner = f"opt/simulations-service/deployed-jobs/{job}/resultsTable.csv"
    with zipfile.ZipFile(run / f"{job}-log.zip", "w") as zf:
        zf.writestr(
            inner,
            "jobID, from, to, CARRY_RESET, DYNMCAPTOPN, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
            f"{job}, 2007-02-08, 2025-01-02, true, 40, 1.42, 2.01, 0.18, 0.45, 0.15, 0.08\n",
        )
    return run


def test_end_to_end_locate_adapt_validate(tmp_path):
    run = _make_results_dir(tmp_path)
    metrics = collect_results(run)

    # only the metric block, no config columns
    assert set(metrics) == {
        "sharpe_ratio", "sortino_ratio", "volatility",
        "total_return", "annual_return", "max_drawdown",
    }
    assert metrics["sharpe_ratio"] == 1.42
    assert metrics["total_return"] == 0.45          # from PROFIT
    assert metrics["max_drawdown"] == -0.08         # negated to match the contract


def test_end_to_end_keys_match_result_contract(tmp_path):
    """The three contract metrics result_builder reads must be present + numeric."""
    metrics = collect_results(_make_results_dir(tmp_path))
    for key in ("total_return", "sharpe_ratio", "max_drawdown"):
        assert isinstance(metrics[key], float)


def test_results_table_at_top_level(tmp_path):
    """Real layout: resultsTable.csv sits at the results-dir top level (next to the
    zip), no unzip needed. The locator should find it directly."""
    run = tmp_path / "JOB1"
    run.mkdir()
    (run / "job-10327-log.zip").write_bytes(b"PK\x03\x04not-a-real-zip")  # present but unused
    (run / "resultsTable.csv").write_text(
        "jobID, CARRY_RESET, SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN, MAX-DRAWDOWN\n"
        "JOB1, true, 1.42, 2.01, 0.18, 0.45, 0.15, 0.08\n"
    )
    metrics = collect_results(run)
    assert metrics["sharpe_ratio"] == 1.42
    assert metrics["max_drawdown"] == -0.08
