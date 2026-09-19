#!/usr/bin/env python3
"""Preflight or execute one serial E3V10 M0/M1 pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from experiments.exp3.e3v9 import run_e3v9_pair as common


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--replicate", type=int, choices=range(1, 6), required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        result = common.run_pair(
            args.launch_package.resolve(), args.replicate, args.execute
        )
        if args.execute and result.get("status") == "observations_completed_scoring_incomplete":
            return 2
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "exp3-e3v10-pair-result-v1",
                    "status": "failed",
                    "provider_call_count": 0 if not args.execute else None,
                    "formal_observation": bool(args.execute),
                    "failure_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
