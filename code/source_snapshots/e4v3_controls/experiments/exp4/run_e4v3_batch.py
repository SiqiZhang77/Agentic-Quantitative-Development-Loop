#!/usr/bin/env python3
"""Run any unused subset of the frozen E4V3 four-condition run pool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import run_e4v2_batch as shared_batch
except ImportError:  # pragma: no cover
    import run_e4v2_batch as shared_batch  # type: ignore[no-redef]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--replicates", type=int, required=True)
    parser.add_argument("--replicate-start", type=int, default=1)
    parser.add_argument(
        "--conditions",
        nargs="+",
        choices=shared_batch.identity.CONDITION_CODES,
        required=True,
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = shared_batch.run_global_batch(
            args.launch_package.resolve(), args.replicate_start,
            args.replicates, args.conditions, args.execute
        )
        return 2 if args.execute and result.get("status") == "completed_with_recorded_errors" else 0
    except Exception as exc:
        print(json.dumps({
            "schema_version": "exp4-e4v3-global-batch-result-v1",
            "status": "failed",
            "formal_observation": bool(args.execute),
            "provider_call_count": 0 if not args.execute else None,
            "failure_type": type(exc).__name__,
            "message": str(exc),
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
