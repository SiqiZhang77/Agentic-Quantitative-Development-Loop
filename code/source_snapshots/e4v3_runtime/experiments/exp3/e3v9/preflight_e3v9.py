#!/usr/bin/env python3
"""Zero-model preflight for one future E3V9 M0/M1 pair."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import controls
except ImportError:  # pragma: no cover - direct script execution
    import controls  # type: ignore[no-redef]


def preflight(config: dict[str, Any], *, replicate: int) -> dict[str, Any]:
    issues = controls.readiness_issues(config)
    result: dict[str, Any] = {
        "schema_version": "exp3-e3v9-pair-preflight-v1",
        "experiment_id": "E3V9",
        "pair": f"T3_R{replicate}",
        "status": "unready" if issues else "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_calls_made": False,
        "observation_count": 0,
        "run_ids": [],
        "target_refs": [],
        "issues": issues,
    }
    if issues:
        return result
    pair = controls.build_pair_identities(config, replicate)
    result.update(
        {
            "observation_count": 2,
            "run_ids": [item["run_id"] for item in pair],
            "target_refs": [item["target_ref"] for item in pair],
            "identity_sha256": [item["identity_sha256"] for item in pair],
        }
    )
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=controls.CONFIG_PATH)
    parser.add_argument("--replicate", type=int, choices=range(1, 6), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = controls.load_config(args.config)
    result = preflight(config, replicate=args.replicate)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
