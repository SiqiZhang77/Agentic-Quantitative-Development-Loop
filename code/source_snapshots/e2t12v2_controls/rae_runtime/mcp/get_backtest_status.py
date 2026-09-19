"""Mock ``get_backtest_status`` tool (Week 2 walking skeleton).

Derives a job's status purely from what is on disk under ``mock_runtime/`` or
the configured real results root: a directory named after the job containing
``resultsTable.csv`` -> ``completed``; a plain file -> ``rejected`` (its
contents are the reason); neither, past the deadline -> ``timeout``; otherwise
-> ``running``. One-shot and non-blocking; the client polls until the status is
terminal. stdlib only.

Status vocabulary (shared across the three tools):
``running`` / ``completed`` / ``rejected`` / ``timeout`` / ``failed`` (bad input).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from submit_backtest import results_root as _resolved_results_root

JOB_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
DEFAULT_RUNTIME_DIR = os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")


def _has_results_table_data_row(results_table: Path) -> bool:
    try:
        with results_table.open("r", encoding="utf-8-sig", errors="replace") as handle:
            non_empty_lines = [line for line in handle if line.strip()]
    except OSError:
        return False
    return len(non_empty_lines) >= 2


def _parse_submitted_at(submitted_at: str) -> datetime:
    """Parse ISO-8601 timestamps returned by submit_backtest."""
    if not isinstance(submitted_at, str) or not submitted_at.strip():
        raise ValueError("submitted_at must be a non-empty ISO-8601 timestamp string")

    cleaned = submitted_at.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    parsed = datetime.fromisoformat(cleaned)

    # If the timestamp has no timezone, treat it as UTC to keep behaviour stable.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _elapsed_seconds_since(submitted_at: str) -> float:
    submitted_dt = _parse_submitted_at(submitted_at)
    now_dt = datetime.now(timezone.utc)
    return max(0.0, (now_dt - submitted_dt).total_seconds())


def _results_root(runtime_dir: str | Path | None = None) -> Path:
    """Results root. An explicit ``runtime_dir`` (mock tests) wins; otherwise
    resolve real/mock centrally from USE_REAL_BACKTESTER so real-mode polling hits
    the explicit mounted SIMULATION_RESULTS_DIR instead of the mock dir."""
    if runtime_dir is not None:
        return Path(runtime_dir) / "simulation-results"
    return _resolved_results_root()


def get_backtest_status(
    job_name: str,
    submitted_at: str,
    timeout_seconds: int = 5400,
    runtime_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return the current status for a submitted backtest job."""
    if not isinstance(job_name, str) or not JOB_NAME_RE.fullmatch(job_name):
        return {
            "status": "failed",
            "job_name": job_name,
            "error": "Invalid job_name. Use only letters, digits, underscores, or hyphens.",
        }

    try:
        timeout_seconds = int(timeout_seconds)
    except (TypeError, ValueError):
        return {
            "status": "failed",
            "job_name": job_name,
            "error": "timeout_seconds must be an integer.",
        }

    if timeout_seconds < 1:
        return {
            "status": "failed",
            "job_name": job_name,
            "error": "timeout_seconds must be at least 1.",
        }

    try:
        submitted_dt = _parse_submitted_at(submitted_at)
    except ValueError as exc:
        return {
            "status": "failed",
            "job_name": job_name,
            "error": str(exc),
        }
    now_dt = datetime.now(timezone.utc)
    elapsed_seconds = max(0.0, (now_dt - submitted_dt).total_seconds())

    # Completion signal is the same shape in both modes: the engine writes a
    # per-job <job_name>/ directory under the results root. _results_root()
    # resolves the real vs mock root.
    try:
        result_path = _results_root(runtime_dir) / job_name
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "job_name": job_name,
            "submitted_at": submitted_at,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "error": f"cannot resolve backtest results directory: {exc}",
        }

    if result_path.exists():
        result_mtime = datetime.fromtimestamp(result_path.stat().st_mtime, timezone.utc)
        if result_mtime < submitted_dt:
            return {
                "status": "running",
                "job_name": job_name,
                "submitted_at": submitted_at,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "timeout_seconds": timeout_seconds,
                "expected_results_path": str(result_path),
                "ignored_stale_result": True,
            }

    if result_path.is_dir():
        output_files = sorted(p.name for p in result_path.iterdir() if p.is_file())
        if "resultsTable.csv" not in output_files:
            if elapsed_seconds > timeout_seconds:
                return {
                    "status": "timeout",
                    "job_name": job_name,
                    "submitted_at": submitted_at,
                    "elapsed_seconds": round(elapsed_seconds, 3),
                    "timeout_seconds": timeout_seconds,
                    "expected_results_path": str(result_path),
                    "output_files": output_files,
                    "error": "result directory exists but resultsTable.csv was not written before timeout",
                }
            return {
                "status": "running",
                "job_name": job_name,
                "submitted_at": submitted_at,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "timeout_seconds": timeout_seconds,
                "expected_results_path": str(result_path),
                "output_files": output_files,
                "waiting_for": "resultsTable.csv",
            }
        results_table = result_path / "resultsTable.csv"
        if not _has_results_table_data_row(results_table):
            return {
                "status": "failed",
                "job_name": job_name,
                "submitted_at": submitted_at,
                "elapsed_seconds": round(elapsed_seconds, 3),
                "results_dir": str(result_path),
                "results_table_path": str(results_table),
                "output_files": output_files,
                "error": "resultsTable.csv exists but contains no data row",
            }
        return {
            "status": "completed",
            "job_name": job_name,
            "submitted_at": submitted_at,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "results_dir": str(result_path),
            "result_path": str(result_path / "result.json"),
            "output_files": output_files,
        }

    if result_path.is_file():
        reason = result_path.read_text(encoding="utf-8", errors="replace").strip()
        return {
            "status": "rejected",
            "job_name": job_name,
            "submitted_at": submitted_at,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "rejection_file": str(result_path),
            "reason": reason or "Backtest rejected; no reason was provided.",
        }

    if elapsed_seconds > timeout_seconds:
        return {
            "status": "timeout",
            "job_name": job_name,
            "submitted_at": submitted_at,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "timeout_seconds": timeout_seconds,
            "expected_results_path": str(result_path),
        }

    return {
        "status": "running",
        "job_name": job_name,
        "submitted_at": submitted_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "timeout_seconds": timeout_seconds,
        "expected_results_path": str(result_path),
    }


async def poll_until_done(job_name, submitted_at, *, timeout_seconds=3600):
    """Poll until the job is terminal or the caller's timeout is hit.

    Non-blocking (``await asyncio.sleep``), exponential backoff 5s -> 60s.
    ``timeout_seconds`` is the shared IW contract field
    (``iteration_controls.timeout_seconds`` in runtime_request.schema.json,
    range 60-10800s); default 3600 (60 min) is just a fallback.
    """
    import asyncio
    import time

    delay, start = 5, time.monotonic()
    while True:
        snapshot = get_backtest_status(job_name, submitted_at)
        if snapshot["status"] in {"completed", "rejected", "failed"}:
            return snapshot
        if time.monotonic() - start > timeout_seconds:
            return {
                "status": "timeout",
                "job_name": job_name,
                "elapsed_seconds": round(time.monotonic() - start, 1),
            }
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)  # 5 -> 10 -> 20 -> 40 -> 60 (capped)


# Default demo values so this file can be run directly from VS Code without
# typing command-line arguments. These match the successful local test job.
DEFAULT_DEMO_JOB_NAME = os.getenv("DEMO_BACKTEST_JOB_NAME", "test001")
DEFAULT_DEMO_SUBMITTED_AT = os.getenv(
    "DEMO_BACKTEST_SUBMITTED_AT",
    "2026-06-09T10:02:36.841660+00:00",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Check status for a submitted backtest job.")
    parser.add_argument(
        "job_name",
        nargs="?",
        default=DEFAULT_DEMO_JOB_NAME,
        help=f"Job name returned by submit_backtest. Default: {DEFAULT_DEMO_JOB_NAME}",
    )
    parser.add_argument(
        "submitted_at",
        nargs="?",
        default=DEFAULT_DEMO_SUBMITTED_AT,
        help=f"ISO-8601 timestamp returned by submit_backtest. Default: {DEFAULT_DEMO_SUBMITTED_AT}",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=5400,
        help="Seconds before a missing result is treated as timed out. Default: 5400.",
    )
    parser.add_argument(
        "--runtime-dir",
        default=DEFAULT_RUNTIME_DIR,
        help="Runtime root containing simulation-results/. Default: MOCK_RUNTIME_DIR or mock_runtime.",
    )
    args = parser.parse_args()

    status = get_backtest_status(
        job_name=args.job_name,
        submitted_at=args.submitted_at,
        timeout_seconds=args.timeout_seconds,
        runtime_dir=args.runtime_dir,
    )
    print(json.dumps(status, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
