"""metrics_patch - RAE-10: emit a performance_metrics helper patch.

Wraps collect_results() into the helper-patch shape that
after_backtest/write_runtime_output.py merges (key: performance_metrics_update),
trimmed to the closed result-contract metric fields. The patch JSON is written
under RAE_OUTPUT_DIR (default /workspace/output), like the other helpers.

alpha/beta and time_series_data_path are filled by the artefacts helper, not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from collect_results import collect_results

# Metric fields this tool is allowed to set in the closed performance_metrics
# block (additionalProperties: false). Extras like sortino/vol are dropped here.
_CONTRACT_METRICS = ("total_return", "sharpe_ratio", "max_drawdown")

DEFAULT_OUTPUT_DIR = Path(os.environ.get("RAE_OUTPUT_DIR", "/workspace/output"))


def build_metrics_patch(job_name: str, results_dir: str | Path) -> dict:
    """Run the results pipeline and wrap it as a write_runtime_output helper patch."""
    metrics = collect_results(results_dir)
    update = {key: metrics.get(key) for key in _CONTRACT_METRICS}
    return {
        "tool_name": "get_backtest_results",
        "status": "completed",
        "job_name": job_name,
        "performance_metrics_update": update,
    }


def write_metrics_patch(
    job_name: str,
    results_dir: str | Path,
    output_dir: str | Path | None = None,
) -> Path:
    """Write the patch JSON under the output dir (RAE_OUTPUT_DIR); return its path."""
    patch = build_metrics_patch(job_name, results_dir)
    out = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{job_name}_metrics_patch.json"
    path.write_text(json.dumps(patch, indent=2))
    return path
