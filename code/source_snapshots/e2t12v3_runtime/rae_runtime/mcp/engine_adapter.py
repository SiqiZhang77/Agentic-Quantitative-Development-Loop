"""engine_adapter.py - RAE-10: raw engine output -> result-contract metrics.

PURE data transformation. No polling, no waiting, no run-directory discovery here
(those live on the status/poller side). Given a ``resultsTable.csv``, map its
columns into the metric keys defined by the result schema (``result_schema.py``).

Forward-compatible: known columns are renamed to schema keys; unknown columns
pass through unchanged (new engine metrics are kept, never dropped).
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

# Source column names confirmed from a real resultsTable.csv sample (the metric
# block is the last 6 columns: SHARPE, SORTINO, VOL, PROFIT, ANNUAL-RTN,
# MAX-DRAWDOWN - there is no TOTAL-RETURN column, PROFIT is the total return).
# TODO: confirm the target schema key names against result_schema.py.
_METRIC_MAP = {
    "SHARPE": "sharpe_ratio",
    "SORTINO": "sortino_ratio",
    "VOL": "volatility",
    "PROFIT": "total_return",
    "ANNUAL-RTN": "annual_return",
    "MAX-DRAWDOWN": "max_drawdown",
    "TOTAL-RETURN": "total_return",  # alias kept in case a build emits this name
}

# TODO: replace with the real required-baseline metrics from result_schema.py
_REQUIRED = {"sharpe_ratio", "max_drawdown", "total_return"}


def _to_float(raw: str) -> float | None:
    try:
        return float((raw or "").strip())
    except ValueError:
        return None


def adapt_results_table(results_table_path: str | Path) -> dict:
    """``resultsTable.csv`` -> metrics dict in result_schema.py shape.

    The real resultsTable.csv echoes ~hundreds of config params first, then the
    performance metrics as the last columns. We find where the metric block
    starts (the first known metric column) and take everything from there on:
    config columns ahead of it are skipped, while genuinely new metrics added at
    the tail still pass through (forward-compatible). Known columns are renamed to
    schema keys; unknown trailing columns keep their name.

    Uses ``csv.reader`` (not ``DictReader``) so duplicate config column names in
    the real file don't collapse rows.
    """
    with open(results_table_path, newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        data = next(reader, None)
    if not header or not data:
        return {}

    columns = [c.strip() for c in header]  # real CSV has a space after each comma
    start = next((i for i, c in enumerate(columns) if c in _METRIC_MAP), len(columns))

    metrics: dict = {}
    for i in range(start, len(columns)):
        col = columns[i]
        value = data[i] if i < len(data) else ""
        key = _METRIC_MAP.get(col, col)  # known -> mapped; new trailing metric -> passthrough
        metrics[key] = _to_float(value)

    # resultsTable stores MAX-DRAWDOWN as a positive magnitude, but the contract
    # (and IW's result sample) reports max_drawdown as a negative number.
    # TODO: confirm the drawdown sign convention against the result schema.
    if metrics.get("max_drawdown") is not None:
        metrics["max_drawdown"] = -abs(metrics["max_drawdown"])
    return metrics


def validate(metrics: dict) -> dict:
    """Forward-compatible check: required keys must be present; extras pass through.

    On missing required keys, warn to stderr but DO NOT raise - never break the
    pipeline.
    """
    missing = _REQUIRED - metrics.keys()
    if missing:
        print(
            f"[engine_adapter] WARNING: missing required metrics {sorted(missing)}",
            file=sys.stderr,
        )
    return metrics


if __name__ == "__main__":
    import json

    # Quick manual check against a mock results table, if one exists.
    sample = Path("mock_runtime/simulation-results/test001/resultsTable.csv")
    if sample.is_file():
        print(json.dumps(validate(adapt_results_table(sample)), indent=2))
    else:
        print("No sample resultsTable.csv found; run pytest test_engine_adapter.py instead.")
