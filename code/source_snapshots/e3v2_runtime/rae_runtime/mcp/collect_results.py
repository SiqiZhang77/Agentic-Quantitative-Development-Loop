"""collect_results - RAE-10 end-to-end results pipeline.

Ties the three pieces together: locate (unzip + find resultsTable.csv) -> adapt
(parse into metric keys) -> validate (forward-compatible check). Returns a metrics
dict shaped exactly for ``result_builder._metrics`` (keys: total_return,
sharpe_ratio, max_drawdown, ...), so RAE-08 can pass it straight into
``build_response(backtest={"metrics": <this>, "time_series_data_path": ...})``.
"""

from __future__ import annotations

from pathlib import Path

from result_locator import locate_results_table
from engine_adapter import adapt_results_table, validate


def collect_results(results_dir: str | Path) -> dict:
    """Locate -> adapt -> validate. Returns the validated metrics dict."""
    csv_path = locate_results_table(results_dir)
    return validate(adapt_results_table(csv_path))


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1:
        print(json.dumps(collect_results(sys.argv[1]), indent=2))
    else:
        print("usage: python collect_results.py <results_dir>")
