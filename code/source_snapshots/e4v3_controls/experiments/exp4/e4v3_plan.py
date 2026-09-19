"""Create a prospective E4V3 configuration before any model execution.

The future identity registers fifteen run IDs for each of four conditions.
The operator chooses an unused subset at launch time.  This tool deliberately
creates a new, unresolved template; it never creates an agreement or makes an
external call.
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

try:
    from . import identity_e4v2 as identity
except ImportError:  # pragma: no cover
    import identity_e4v2 as identity  # type: ignore[no-redef]


E4V3_CONTROL_FILES = (
    "experiments/exp4/E4V3_PROTOCOL.md",
    "experiments/exp4/schemas/e4v2_config.schema.json",
    "experiments/exp4/identity_e4v2.py",
    "experiments/exp4/e4v3_plan.py",
    "experiments/exp4/build_e4v2_freeze_config.py",
    "experiments/exp4/build_e4v2_agreement.py",
    "experiments/exp4/preflight_e4v2.py",
    "experiments/exp4/build_e4v2_launch_package.py",
    "experiments/exp4/run_e4v2_observation.py",
    "experiments/exp4/run_e4v2_batch.py",
    "experiments/exp4/run_e4v3_batch.py",
)


REGISTERED_REPLICATES = 15


def build_prospective_config(replicates: int = REGISTERED_REPLICATES) -> dict[str, Any]:
    """Return the unresolved four-condition E4V3 run pool."""

    if replicates != REGISTERED_REPLICATES:
        raise ValueError("E4V3 registers exactly 15 available replicates per condition")
    observation_count = len(identity.CONDITION_CODES) * replicates
    config = copy.deepcopy(identity.load_config(identity.CONFIG_PATH))
    config.update(
        {
            "schema_version": "exp4-e4v3-prospective-config-v1",
            "experiment_id": "E4V3",
            "replicate_count": replicates,
            "block_order_seed": "E4V3-four-cell-registered-pool-15-v2",
        }
    )
    base_rows = [
        ["M0R0", "M0R1", "M1R1", "M1R0"],
        ["M0R1", "M1R0", "M0R0", "M1R1"],
        ["M1R0", "M1R1", "M0R1", "M0R0"],
        ["M1R1", "M0R0", "M1R0", "M0R1"],
    ]
    config["williams_order"] = {
        str(index): list(base_rows[(index - 1) % len(base_rows)])
        for index in range(1, replicates + 1)
    }
    config["control"]["control_files"] = [
        {"path": path, "sha256": f"__UNRESOLVED_E4V3_CONTROL_{index}_SHA256__"}
        for index, path in enumerate(E4V3_CONTROL_FILES, start=1)
    ]
    config["execution"] = {
        "serial": True,
        "observations_per_authorization": None,
        "registered_observation_count": observation_count,
        "selection_mode": "runtime_explicit_subset",
        "phase_order": ["runtime_selected_conditions_interleaved"],
        "first_authorized_condition": "runtime_selected_subset",
        "continue_after_observation_failure": True,
        "advance_only_after_block_audit": False,
        "agreement_generation_enabled": False,
        "formal_model_execution_enabled": False,
    }
    identity.validate_config(config)
    return config


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pool-replicates", type=int, default=REGISTERED_REPLICATES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": f"refusing to overwrite: {output}"}, sort_keys=True))
        return 2
    try:
        value = build_prospective_config(args.pool_replicates)
    except ValueError as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": str(exc)}, sort_keys=True))
        return 2
    _write_json(output, value)
    print(json.dumps({
        "status": "prospective_config_written",
        "experiment_id": "E4V3",
        "registered_replicate_count": value["replicate_count"],
        "registered_conditions": list(identity.CONDITION_CODES),
        "registered_observation_count": value["execution"]["registered_observation_count"],
        "provider_call_count": 0,
        "output": str(output),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
