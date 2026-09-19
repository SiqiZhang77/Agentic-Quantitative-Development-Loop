#!/usr/bin/env python3
"""Build a T3-only E4 four-cell agreement after all bindings resolve.

The builder writes the full 20-observation schedule.  It makes no model,
retriever or evaluator call and does not authorize execution by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

try:
    from . import identity_e4v2 as identity
except ImportError:  # pragma: no cover - direct script execution
    import identity_e4v2 as identity  # type: ignore[no-redef]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _git_worktree_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def build_agreement(config: dict[str, Any], output: Path) -> dict[str, Any]:
    """Atomically write a frozen schedule, or refuse before creating output."""

    identity.require_ready(config)
    output = output.resolve()
    if _git_worktree_root(output) is not None:
        raise ValueError("formal agreement output must remain outside every Git worktree")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")

    schedule = identity.build_schedule(config)
    manifest: dict[str, Any] = {
        "schema_version": f"exp4-{config['experiment_id'].lower()}-agreement-v1",
        "experiment_id": config["experiment_id"],
        "status": "frozen_not_executed",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_calls_made": False,
        "block_count": len(schedule),
        "batch_count": len(config["execution"]["phase_order"]),
        "observation_count": sum(len(block["observations"]) for block in schedule),
        "observations_per_authorization": config["execution"]["observations_per_authorization"],
        "source": config["source"],
        "control": config["control"],
        "runtime": config["runtime"],
        "task_bindings": config["task_bindings"],
        "evaluator_bindings": config["evaluator_bindings"],
        "retrieval_binding": config["retrieval_binding"],
        "shared_budget": config["shared_budget"],
        "manager_star_role_budget": config["manager_star_role_budget"],
        "execution": config["execution"],
        "block_order": [block["block_id"] for block in schedule],
    }
    if config["experiment_id"] == "E4V3":
        manifest.update({
            "replicate_count": config["replicate_count"],
            "registered_conditions": list(identity.selected_condition_codes(config)),
            "registered_observation_count": config["execution"]["registered_observation_count"],
            "selection_mode": config["execution"]["selection_mode"],
        })
    manifest["agreement_sha256"] = identity.sha256_value(
        {"manifest": manifest, "schedule": schedule}
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        _write_json(staging / "agreement.json", manifest)
        _write_json(staging / "schedule.json", schedule)
        _write_json(
            staging / "agreement.sha256.json",
            {
                "schema_version": f"exp4-{config['experiment_id'].lower()}-agreement-sha256-v1",
                "agreement_sha256": manifest["agreement_sha256"],
            },
        )
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=identity.CONFIG_PATH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        config = identity.load_config(args.config)
        manifest = build_agreement(config, args.output)
    except identity.NotReadyError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "exp4-e4-builder-status-v1",
                    "status": "unready",
                    "agreement_written": False,
                    "provider_call_count": 0,
                    "external_calls_made": False,
                    "issues": list(exc.issues),
                },
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
