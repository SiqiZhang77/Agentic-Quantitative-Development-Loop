"""
Writes fake outputs under ``mock_runtime/`` that mirror the real engine's layout:
success -> a ``<job_name>/`` directory holding ``resultsTable.csv`` + ``ZQQ_<job_name>.csv``;
rejection -> a plain ``<job_name>`` file whose contents are the reason. stdlib only;
downstream tools read the same layout the real engine produces.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
import os

DEFAULT_RUNTIME_DIR = os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")

JOB_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
#DEFAULT_RUNTIME_DIR = "mock_runtime"

_RESULTS_TABLE = {
    "SHARPE": "1.20", "SORTINO": "1.60", "VOL": "0.20", "PROFIT": "0.45",
    "ANNUAL-RTN": "0.15", "MAX-DRAWDOWN": "-0.08", "TOTAL-RETURN": "0.45",
}
_EQUITY_CURVE_ROWS = [
    ("2020-01-01", "1.00"), ("2020-02-01", "1.03"), ("2020-03-01", "0.98"),
    ("2020-04-01", "1.07"), ("2020-05-01", "1.12"),
]


def is_valid_job_name(job_name: str) -> bool:
    return isinstance(job_name, str) and bool(JOB_NAME_RE.fullmatch(job_name))


def results_root(runtime_dir: str | Path = DEFAULT_RUNTIME_DIR) -> Path:
    return Path(runtime_dir) / "simulation-results"


def run_dummy_backtest(
    job_name: str,
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
    simulate_rejected: bool = False,
    rejection_reason: str = "Mock rejection: invalid request format",
) -> dict:
    if not is_valid_job_name(job_name):
        raise ValueError(f"invalid job_name {job_name!r}: must match {JOB_NAME_RE.pattern}")

    result_path = results_root(runtime_dir) / job_name

    if simulate_rejected:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(rejection_reason, encoding="utf-8")
        return {"job_name": job_name, "status": "rejected", "reason": rejection_reason}

    result_path.mkdir(parents=True, exist_ok=True)
    with (result_path / "resultsTable.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(_RESULTS_TABLE))
        writer.writeheader()
        writer.writerow(_RESULTS_TABLE)
    with (result_path / f"ZQQ_{job_name}.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "equity"])
        writer.writerows(_EQUITY_CURVE_ROWS)

    return {"job_name": job_name, "status": "completed", "results_dir": str(result_path)}


if __name__ == "__main__":
    print(run_dummy_backtest("mock_dummy_demo"))
