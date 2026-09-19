"""Golden test: the adapter against a real vendor resultsTable.csv sample.

Pins the adapter output to the known-correct values from a real engine run, so
the mock -> real cutover (and any later change) cannot silently regress. The
fixture (golden_resultsTable.csv) is a real sample with ~388 columns, mostly
config, metrics at the end, duplicate config column names, and spaces after
commas - all of which the adapter must handle.
"""

from pathlib import Path

from engine_adapter import adapt_results_table, validate

GOLDEN = Path(__file__).parent / "golden_resultsTable.csv"


def test_golden_real_sample():
    metrics = validate(adapt_results_table(GOLDEN))
    assert metrics == {
        "sharpe_ratio": 0.5084,
        "sortino_ratio": 0.7877,
        "volatility": 0.1731,
        "total_return": 3.1012,   # from PROFIT
        "annual_return": 0.0759,
        "max_drawdown": -0.5745,  # raw 0.5745 negated to match the contract
    }
