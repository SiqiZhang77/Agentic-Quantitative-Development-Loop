"""
Parses the fake ``resultsTable.csv`` written by :mod:`dummy_backtester` into the
key performance metrics a client cares about. In the real engine this step unzips
a multi-GB archive and runs a cluster parse script; here it is a simple CSV read
against the same on-disk layout, so the caller-facing contract is identical.

Structured statuses (returned as data, never raised):

* ``missing_results_table``  — no ``resultsTable.csv`` in the job's result dir
* ``empty_results_table``    — the CSV has a header but no data rows
* ``metrics_missing``        — required metric columns are absent
* ``completed``              — success, with a ``metrics`` block

Status vocabulary (shared across the three tools): ``completed`` (parse ok) /
``failed`` (missing/empty/invalid results).
"""

from __future__ import annotations

import csv
from pathlib import Path

from dummy_backtester import (
    DEFAULT_RUNTIME_DIR,
    JOB_NAME_RE,
    is_valid_job_name,
    results_root,
)

_REQUIRED_METRICS = {
    "SHARPE": "sharpe",
    "SORTINO": "sortino",
    "VOL": "volatility",
    "ANNUAL-RTN": "annual_return",
    "MAX-DRAWDOWN": "max_drawdown",
}
_TOTAL_RETURN_COLS = ("TOTAL-RETURN", "PROFIT")


def _validation_error(message: str) -> dict:
    return {"status": "failed", "error_type": "validation_error", "message": message}


def _to_float(raw: str) -> float | None:
    try:
        return float((raw or "").strip())
    except (ValueError, AttributeError):
        return None


def get_backtest_logs(
    job_name: str,
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
) -> dict:
    if not is_valid_job_name(job_name):
        return _validation_error(
            f"invalid job_name {job_name!r}: must match {JOB_NAME_RE.pattern}"
        )

    result_dir = results_root(runtime_dir) / job_name
    results_table = result_dir / "resultsTable.csv"
    equity_curve_csv = result_dir / f"ZQQ_{job_name}.csv"

    if not results_table.is_file():
        return {
            "job_name": job_name,
            "status": "failed",
            "error_type": "missing_results_table",
            "message": f"resultsTable.csv not found at {results_table}",
        }

    with results_table.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        return {
            "job_name": job_name,
            "status": "failed",
            "error_type": "empty_results_table",
            "message": "resultsTable.csv has a header but no data rows",
        }

    row = rows[0]

    missing = [col for col in _REQUIRED_METRICS if col not in row]
    has_total_return = any(col in row for col in _TOTAL_RETURN_COLS)
    if not has_total_return:
        missing.append("TOTAL-RETURN")
    if missing:
        return {
            "job_name": job_name,
            "status": "failed",
            "error_type": "metrics_missing",
            "missing_fields": missing,
        }

    metrics = {out: _to_float(row[col]) for col, out in _REQUIRED_METRICS.items()}
    total_col = next(c for c in _TOTAL_RETURN_COLS if c in row)
    metrics["total_return"] = _to_float(row[total_col])

    return {
        "job_name": job_name,
        "status": "completed",
        "metrics": metrics,
        "files": {
            "results_table": str(results_table),
            "equity_curve_csv": str(equity_curve_csv),
        },
    }


if __name__ == "__main__":
    import json

    print(json.dumps(get_backtest_logs("mock_alpha_test"), indent=2))
