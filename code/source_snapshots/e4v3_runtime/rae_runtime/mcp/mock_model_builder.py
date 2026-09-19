"""Mock model-builder for the Week 2 walking skeleton.

Stands in for the real model-builder service. submit_backtest drops a
``<job_name>.request`` file into the backtest-requests directory; this module
picks those files up and triggers the (mock) backtest engine so that results
appear under ``simulation-results/``, which get_backtest_status / get_backtest_logs
then read.

In production the real model-builder + Jenkins do this step. Here we simply call
dummy_backtester directly. stdlib only.

Run as a script to process everything currently pending (single pass):

    python backtester/mock_model_builder.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# dummy_backtester lives next to this file. Add this directory to sys.path so the
# import works whether this module is run as a script or imported elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import dummy_backtester  # noqa: E402

# Read the same env var submit_backtest writes to, so the two always agree on
# where requests live. Falls back to the shared mock_runtime/ root.
DEFAULT_REQUESTS_DIR = os.getenv("BACKTEST_REQUESTS_DIR", "mock_runtime/backtest-requests")
# Runtime root for outputs, matching dummy_backtester / get_backtest_status.
DEFAULT_RUNTIME_DIR = os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")


def process_request(
    request_path: str | Path,
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
) -> dict:
    """Consume one .request file and trigger the mock backtest for it.

    The job_name is taken from the filename stem (``alpha_v1.request`` ->
    ``alpha_v1``) — the same key submit_backtest used and that
    get_backtest_status / get_backtest_logs will look for under
    simulation-results/. The consumed request is moved into a ``processed/``
    subfolder so re-runs do not pick it up again.
    """
    request_path = Path(request_path)
    job_name = request_path.stem

    if not dummy_backtester.is_valid_job_name(job_name):
        return {
            "request_file": str(request_path),
            "status": "skipped",
            "reason": f"invalid job_name {job_name!r} derived from filename",
        }

    # Run first; only move the request aside once it has been handled, so a
    # failure leaves the request in place to retry.
    result = dummy_backtester.run_dummy_backtest(job_name, runtime_dir=runtime_dir)

    processed_dir = request_path.parent / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    request_path.replace(processed_dir / request_path.name)

    return {
        "request_file": str(request_path),
        "job_name": job_name,
        "status": result.get("status", "completed"),
        "results_dir": result.get("results_dir"),
    }


def process_all_pending(
    requests_dir: str | Path = DEFAULT_REQUESTS_DIR,
    runtime_dir: str | Path = DEFAULT_RUNTIME_DIR,
) -> list[dict]:
    """Process every pending .request file once (single pass, PoC style)."""
    requests_dir = Path(requests_dir)
    if not requests_dir.is_dir():
        return []

    return [
        process_request(request_file, runtime_dir=runtime_dir)
        for request_file in sorted(requests_dir.glob("*.request"))
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mock model-builder: turn pending .request files into mock backtest results."
    )
    parser.add_argument(
        "--requests-dir",
        default=DEFAULT_REQUESTS_DIR,
        help="Directory submit_backtest writes .request files into. "
        "Defaults to BACKTEST_REQUESTS_DIR or mock_runtime/backtest-requests.",
    )
    parser.add_argument(
        "--runtime-dir",
        default=DEFAULT_RUNTIME_DIR,
        help="Runtime root where results are written. "
        "Defaults to MOCK_RUNTIME_DIR or mock_runtime.",
    )
    args = parser.parse_args()

    processed = process_all_pending(args.requests_dir, args.runtime_dir)
    print(json.dumps(processed, indent=2, ensure_ascii=False))
    print(f"Processed {len(processed)} request(s).")


if __name__ == "__main__":
    main()
