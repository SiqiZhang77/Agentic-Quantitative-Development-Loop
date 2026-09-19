"""get_backtest_results - MCP tool: final metrics for a completed backtest.

Reads the completed run directory, locates and parses resultsTable.csv (via
result_locator + engine_adapter), and returns the normalised metrics in the shape
the final result contract expects. Consolidates locate + parse + validate so the
LLM does not call separate tools or hold intermediate parsing state. Call only
after get_backtest_status reports completed.
"""

from __future__ import annotations

import os
from pathlib import Path

from result_locator import locate_results_table
from engine_adapter import adapt_results_table, validate

SCHEMA_VERSION = "1.0"
DEFAULT_RUNTIME_DIR = os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")

# A usable result must carry at least these. If resultsTable.csv has only a
# header and no data row (the engine's "MISSING REPORT" case), parsing yields an
# empty dict; we must report that as a failure rather than a silent completed.
_REQUIRED_METRICS = ("sharpe_ratio", "max_drawdown", "total_return")


def _summarise(metrics: dict) -> str:
    bits = []
    for key, label in (
        ("sharpe_ratio", "Sharpe"),
        ("total_return", "total return"),
        ("max_drawdown", "max drawdown"),
    ):
        value = metrics.get(key)
        if value is not None:
            bits.append(f"{label} {value}")
    return ", ".join(bits)


def get_backtest_results(job_name: str, runtime_dir: str | Path = DEFAULT_RUNTIME_DIR) -> dict:
    """Locate -> parse -> validate the run's resultsTable.csv into structured metrics."""
    run_directory = Path(runtime_dir) / "simulation-results" / job_name
    base = {
        "schema_version": SCHEMA_VERSION,
        "job_name": job_name,
        "run_directory": str(run_directory),
    }

    if not run_directory.is_dir():
        return {
            **base, "status": "failed", "results_table_path": None,
            "metrics": {}, "summary": "",
            "errors": [f"run directory not found: {run_directory}"],
        }

    try:
        csv_path = locate_results_table(run_directory)
    except FileNotFoundError as exc:
        return {
            **base, "status": "failed", "results_table_path": None,
            "metrics": {}, "summary": "", "errors": [str(exc)],
        }

    metrics = validate(adapt_results_table(csv_path))
    missing = [k for k in _REQUIRED_METRICS if metrics.get(k) is None]
    if missing:
        # resultsTable.csv was found but carries no usable metrics (e.g. header
        # only, no data row). Surface it as a failure with a reason so the agent
        # loop is not fed a silent empty "completed" result.
        return {
            **base, "status": "failed", "results_table_path": str(csv_path),
            "metrics": metrics, "summary": "",
            "errors": [f"no usable metrics parsed (missing {missing}); "
                       "resultsTable.csv may have a header but no data row"],
        }

    return {
        **base, "status": "completed", "results_table_path": str(csv_path),
        "metrics": metrics, "summary": _summarise(metrics), "errors": [],
    }


if __name__ == "__main__":
    import json
    import sys

    print(json.dumps(get_backtest_results(sys.argv[1] if len(sys.argv) > 1 else "test001"), indent=2))
