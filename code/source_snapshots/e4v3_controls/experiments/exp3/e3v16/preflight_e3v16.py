#!/usr/bin/env python3
"""Zero-model preflight for one future E3V16 M0/M1 pair."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    from . import controls
except ImportError:  # pragma: no cover - direct script execution
    import controls  # type: ignore[no-redef]


def _load_json(path: Path, issues: list[str]) -> Any:
    if not path.is_file():
        issues.append(f"agreement file is missing: {path.name}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        issues.append(f"agreement file is invalid JSON: {path.name}")
        return None
    return value


def _agreement_issues(
    config: dict[str, Any], agreement_dir: Path | None
) -> tuple[list[str], str | None]:
    issues: list[str] = []
    if agreement_dir is None:
        return ["frozen agreement directory was not supplied"], None
    root = agreement_dir.resolve()
    agreement = _load_json(root / "agreement.json", issues)
    schedule_record = _load_json(root / "schedule.json", issues)
    digest_record = _load_json(root / "agreement.sha256.json", issues)
    if issues or agreement is None or schedule_record is None or digest_record is None:
        return issues, None
    if not isinstance(agreement, dict):
        return ["agreement.json root is not an object"], None
    if not isinstance(schedule_record, list):
        return ["schedule.json root is not an array"], None
    if not isinstance(digest_record, dict):
        return ["agreement.sha256.json root is not an object"], None
    expected_schedule = controls.build_schedule(config)
    if schedule_record != expected_schedule:
        issues.append("frozen schedule does not match the resolved E3V16 configuration")
    claimed = agreement.get("agreement_sha256")
    if not isinstance(claimed, str):
        issues.append("agreement.json does not contain one agreement_sha256")
        return issues, None
    unsigned = copy.deepcopy(agreement)
    unsigned.pop("agreement_sha256", None)
    actual = controls.sha256_value(
        {"manifest": unsigned, "schedule": schedule_record}
    )
    if actual != claimed:
        issues.append("agreement logical SHA-256 does not match its manifest and schedule")
    if digest_record.get("agreement_sha256") != claimed:
        issues.append("agreement.sha256.json does not match agreement.json")
    expected_sections = {
        "source": config["source"],
        "runtime": config["runtime"],
        "task_suite": config["task_suite"],
        "tool_contract": config["tool_contract"],
        "shared_budget": config["shared_budget"],
        "manager_star_role_budget": config["manager_star_role_budget"],
        "evaluation": config["evaluation"],
        "control": config["control"],
    }
    for field, expected in expected_sections.items():
        if agreement.get(field) != expected:
            issues.append(f"agreement binding differs from config: {field}")
    if agreement.get("registered_changes_from_e3v15") != (
        controls.REGISTERED_CHANGES_FROM_E3V15
    ):
        issues.append("agreement E3V15-to-E3V16 change declaration is invalid")
    return issues, claimed


def _evidence_issues(
    config: dict[str, Any],
    public_bindings: Path | None,
    private_evaluator_manifest: Path | None,
) -> list[str]:
    issues: list[str] = []
    checks = (
        (
            public_bindings,
            config["control"]["public_binding_inventory_sha256"],
            "public binding inventory",
        ),
        (
            private_evaluator_manifest,
            config["evaluation"]["private_evaluator_manifest_sha256"],
            "private evaluator hash manifest",
        ),
    )
    for path, expected, label in checks:
        if path is None:
            issues.append(f"{label} was not supplied")
            continue
        resolved = path.resolve()
        if not resolved.is_file():
            issues.append(f"{label} file is missing")
            continue
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected:
            issues.append(f"{label} SHA-256 does not match the agreement binding")
    return issues


def preflight(
    config: dict[str, Any],
    *,
    replicate: int,
    agreement_dir: Path | None = None,
    public_bindings: Path | None = None,
    private_evaluator_manifest: Path | None = None,
) -> dict[str, Any]:
    issues = controls.readiness_issues(config)
    agreement_sha256: str | None = None
    if not issues:
        agreement_problems, agreement_sha256 = _agreement_issues(
            config, agreement_dir
        )
        issues.extend(agreement_problems)
        issues.extend(
            _evidence_issues(
                config, public_bindings, private_evaluator_manifest
            )
        )
    result: dict[str, Any] = {
        "schema_version": "exp3-e3v16-pair-preflight-v1",
        "experiment_id": "E3V16",
        "pair": f"T3_R{replicate}",
        "status": "unready" if issues else "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_calls_made": False,
        "observation_count": 0,
        "run_ids": [],
        "target_refs": [],
        "issues": issues,
        "agreement_sha256": agreement_sha256,
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
    parser.add_argument("--agreement", type=Path, required=True)
    parser.add_argument("--public-bindings", type=Path, required=True)
    parser.add_argument("--private-evaluator-manifest", type=Path, required=True)
    parser.add_argument("--replicate", type=int, choices=range(1, 6), required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config = controls.load_config(args.config)
    result = preflight(
        config,
        replicate=args.replicate,
        agreement_dir=args.agreement,
        public_bindings=args.public_bindings,
        private_evaluator_manifest=args.private_evaluator_manifest,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
