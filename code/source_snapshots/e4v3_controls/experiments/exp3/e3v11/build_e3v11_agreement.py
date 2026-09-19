#!/usr/bin/env python3
"""Fail-closed builder for the prospective E3V11 T3 pair schedule.

The checked-in configuration cannot pass ``require_agreement_ready``.
Consequently the current builder writes nothing and makes no external call.  A
later reviewed change must resolve every binding and enable agreement generation
while formal model execution remains disabled.
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
    from . import controls
except ImportError:  # pragma: no cover - direct script execution
    import controls  # type: ignore[no-redef]


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
    """Write only after bindings pass while the model-execution gate stays shut."""

    controls.require_agreement_ready(config)
    output = output.resolve()
    if _git_worktree_root(output) is not None:
        raise ValueError("formal agreement output must remain outside Git worktrees")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")

    schedule = controls.build_schedule(config)
    manifest: dict[str, Any] = {
        "schema_version": "exp3-e3v11-agreement-v1",
        "experiment_id": "E3V11",
        "study_id": config["study_id"],
        "status": "frozen_not_executed",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_calls_made": False,
        "task_id": "T3",
        "pair_count": len(schedule),
        "observation_count": sum(
            len(pair["observations"]) for pair in schedule
        ),
        "observations_per_authorization": 2,
        "source": config["source"],
        "runtime": config["runtime"],
        "task_suite": config["task_suite"],
        "tool_contract": config["tool_contract"],
        "shared_budget": config["shared_budget"],
        "manager_star_role_budget": config["manager_star_role_budget"],
        "evaluation": config["evaluation"],
        "control": config["control"],
        "registered_changes_from_e3v10": controls.REGISTERED_CHANGES_FROM_E3V10,
        "execution_policy": {
            "rag_enabled": config["execution"]["rag_enabled"],
            "retrieval_context_included": config["execution"][
                "retrieval_context_included"
            ],
            "serial": config["execution"]["serial"],
            "observations_per_authorization": config["execution"][
                "observations_per_authorization"
            ],
            "advance_only_after_pair_audit": config["execution"][
                "advance_only_after_pair_audit"
            ],
            "formal_model_execution_enabled_at_agreement_build": False,
        },
        "pair_order": [pair["pair_id"] for pair in schedule],
    }
    manifest["agreement_sha256"] = controls.sha256_value(
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
                "schema_version": "exp3-e3v11-agreement-sha256-v1",
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
    parser.add_argument("--config", type=Path, default=controls.CONFIG_PATH)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        config = controls.load_config(args.config)
        manifest = build_agreement(config, args.output)
    except controls.NotReadyError as exc:
        print(
            json.dumps(
                {
                    "schema_version": "exp3-e3v11-builder-status-v1",
                    "status": "unready",
                    "agreement_written": False,
                    "formal_observation": False,
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
