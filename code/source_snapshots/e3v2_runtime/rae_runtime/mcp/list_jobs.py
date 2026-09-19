"""list_jobs - MCP tool: list all backtest jobs and their status (read-only).

Scans the results location: a directory named after the job means completed; a
same-named file means rejected; a pending .request means running. Lets the caller
see every run at a glance, or recover a job_name, without polling each one.
"""

from __future__ import annotations

import os
from pathlib import Path

from submit_backtest import results_root as _resolved_results_root

DEFAULT_RUNTIME_DIR = os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")


def _use_real() -> bool:
    return os.getenv("USE_REAL_BACKTESTER", "false").strip().lower() == "true"


def _results_root(runtime_dir: str | Path | None = None) -> Path:
    if _use_real():
        return _resolved_results_root()
    root = Path(runtime_dir) if runtime_dir is not None else Path(os.getenv("MOCK_RUNTIME_DIR", DEFAULT_RUNTIME_DIR))
    return root.expanduser() / "simulation-results"


def _real_requests_root() -> Path:
    raw = os.getenv("SIMULATION_REQUESTS_DIR")
    if not raw or not raw.strip():
        raise ValueError("SIMULATION_REQUESTS_DIR must be set to an explicit absolute path in real backtester mode")
    raw = raw.strip()
    if raw.startswith("~"):
        raise ValueError("SIMULATION_REQUESTS_DIR must be an explicit absolute path; '~' expansion is not allowed")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"SIMULATION_REQUESTS_DIR must be an explicit absolute path, got {raw!r}")
    return path


def list_jobs(runtime_dir: str | Path | None = None, requests_dir: str | Path | None = None) -> dict:
    results_root = _results_root(runtime_dir)
    if requests_dir is not None:
        requests_root = Path(requests_dir).expanduser()
    elif _use_real():
        requests_root = _real_requests_root()
    else:
        requests_root = Path(os.environ.get("BACKTEST_REQUESTS_DIR", "mock_runtime/backtest-requests")).expanduser()

    jobs = []
    seen = set()

    if results_root.is_dir():
        for path in sorted(results_root.iterdir()):
            jobs.append({"job_name": path.name, "status": "completed" if path.is_dir() else "rejected"})
            seen.add(path.name)

    if requests_root.is_dir():
        for request in sorted(requests_root.glob("*.request")):
            if request.stem not in seen:
                jobs.append({"job_name": request.stem, "status": "running"})

    return {"count": len(jobs), "jobs": jobs}


if __name__ == "__main__":
    import json

    print(json.dumps(list_jobs(), indent=2))
