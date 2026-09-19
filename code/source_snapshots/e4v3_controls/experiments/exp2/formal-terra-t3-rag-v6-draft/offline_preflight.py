#!/usr/bin/env python3
"""Report E2V6 local preparation readiness without external or model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_preparation_package import DEFAULT_CONFIG_PATH, offline_preflight


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    result = offline_preflight(args.config)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") == "ready_for_preparation_only" else 2


if __name__ == "__main__":
    raise SystemExit(main())
