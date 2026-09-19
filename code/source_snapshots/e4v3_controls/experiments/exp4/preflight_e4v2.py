#!/usr/bin/env python3
"""Zero-model identity preflight for one frozen E4 condition."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from . import identity_e4v2 as identity
except ImportError:  # pragma: no cover
    import identity_e4v2 as identity  # type: ignore[no-redef]


def preflight(config: dict[str, Any], *, condition_code: str) -> dict[str, Any]:
    issues = identity.readiness_issues(config)
    result: dict[str, Any] = {
        "schema_version": f"exp4-{config['experiment_id'].lower()}-condition-preflight-v1",
        "experiment_id": config["experiment_id"],
        "task_id": "T3",
        "condition_code": condition_code,
        "status": "unready" if issues else "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "external_calls_made": False,
        "observation_count": 0,
        "run_ids": [],
        "target_refs": [],
        "issues": issues,
    }
    if issues:
        return result
    batch = identity.build_condition_batch(config, condition_code)
    result.update(
        {
            "observation_count": len(batch),
            "run_ids": [item["run_id"] for item in batch],
            "target_refs": [item["target_ref"] for item in batch],
            "identity_sha256": [item["identity_sha256"] for item in batch],
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=identity.CONFIG_PATH)
    parser.add_argument("--condition", choices=identity.CONDITION_CODES, required=True)
    args = parser.parse_args()
    config = identity.load_config(args.config)
    result = preflight(config, condition_code=args.condition)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
