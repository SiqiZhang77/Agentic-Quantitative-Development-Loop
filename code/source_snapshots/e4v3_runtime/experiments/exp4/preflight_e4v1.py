#!/usr/bin/env python3
"""Zero-model identity preflight for exactly one future E4V1 block."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import identity
except ImportError:  # pragma: no cover - direct script execution
    import identity  # type: ignore[no-redef]


def preflight(
    config: dict[str, Any], *, task_id: str, replicate: int
) -> dict[str, Any]:
    issues = identity.readiness_issues(config)
    result: dict[str, Any] = {
        "schema_version": "exp4-e4v1-block-preflight-v1",
        "experiment_id": "E4V1",
        "block_id": f"{task_id}-K{replicate}",
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
    block = identity.build_block_identities(config, task_id, replicate)
    result.update(
        {
            "observation_count": len(block),
            "run_ids": [item["run_id"] for item in block],
            "target_refs": [item["target_ref"] for item in block],
            "identity_sha256": [item["identity_sha256"] for item in block],
        }
    )
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=identity.CONFIG_PATH)
    parser.add_argument("--task", choices=("T1", "T2", "T3"), required=True)
    parser.add_argument("--replicate", type=int, choices=range(1, 5), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = identity.load_config(args.config)
    result = preflight(config, task_id=args.task, replicate=args.replicate)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
