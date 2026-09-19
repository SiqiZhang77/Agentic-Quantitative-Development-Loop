#!/usr/bin/env python3
"""Resolve the E3V17 draft from reviewed public and hash-only inputs.

This stage creates a configuration that may build an agreement, while the
formal-model-execution gate remains false. It performs no model, evaluator,
container or network call.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[2]
E3V17_ROOT = CONTROL_ROOT / "experiments" / "exp3" / "e3v17"
PROSPECTIVE_CONFIG = E3V17_ROOT / "config" / "e3v17.prospective.json"
CONTROL_FILES = (
    "experiments/exp3/e3v17/controls.py",
    "experiments/exp3/e3v17/build_e3v17_agreement.py",
    "experiments/exp3/e3v17/preflight_e3v17.py",
    "experiments/exp3/e3v17/item_store_projection.py",
    "experiments/exp3/e3v17/build_e3v17_launch_package.py",
    "experiments/exp3/e3v17/run_e3v17_observation.py",
    "experiments/exp3/e3v17/run_e3v17_pair.py",
    "experiments/exp3/e3v17/schemas/e3v17_config.schema.json",
    "experiments/prospective_freezes/e3v17/collect_public_bindings.py",
    "experiments/prospective_freezes/e3v17/collect_private_evaluator_bindings.py",
    "experiments/prospective_freezes/e3v17/build_freeze_config.py",
    "experiments/prospective_freezes/e3v17/build_scorer_result_v2.py",
    "experiments/prospective_freezes/e3v17/model_visible_trace_schema_v1.json",
)


class FreezeConfigError(RuntimeError):
    """The supplied evidence cannot safely resolve the E3V17 draft."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FreezeConfigError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeConfigError(f"cannot read JSON evidence: {path}") from exc
    if not isinstance(value, dict):
        raise FreezeConfigError(f"JSON evidence root must be an object: {path}")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _run_git(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=CONTROL_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise FreezeConfigError(f"git identity check failed: {detail}")
    return completed.stdout.strip()


def _controls_module():
    path = E3V17_ROOT / "controls.py"
    spec = importlib.util.spec_from_file_location("e3v17_freeze_controls", path)
    if spec is None or spec.loader is None:
        raise FreezeConfigError("cannot load E3V17 controls")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FreezeConfigError(f"{label} fields differ; missing={missing}, extra={extra}")


def _require_hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise FreezeConfigError(f"{label} is not one SHA-256 value")
    if any(char not in "0123456789abcdef" for char in value):
        raise FreezeConfigError(f"{label} is not lowercase hexadecimal")
    return value


def _validate_public(value: dict[str, Any]) -> None:
    if value.get("schema_version") != "shared-t3-e3v17-public-binding-inventory-v1":
        raise FreezeConfigError("public binding inventory schema is not E3V17 v1")
    if value.get("status") != "public_bindings_verified_private_bindings_unresolved":
        raise FreezeConfigError("public binding inventory did not finish successfully")
    if value.get("provider_call_count") != 0 or value.get("external_model_calls_made") is not False:
        raise FreezeConfigError("public binding inventory does not prove zero model calls")


def _validate_private(value: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "task_suite_version",
        "private_item_scorer_sha256",
        "private_base_scorer_sha256",
        "evaluator_coordinator_sha256",
        "provider_call_count",
        "external_model_calls_made",
        "contains_private_paths",
        "contains_evaluator_source",
        "contains_candidate_or_expected_answers",
    }
    _require_exact_keys(value, expected, "private evaluator manifest")
    if value["schema_version"] != "exp3-e3v17-private-evaluator-hashes-v1":
        raise FreezeConfigError("private evaluator manifest schema is not E3V17 v1")
    if value["task_suite_version"] != "t3-quant-suite-v2":
        raise FreezeConfigError("private evaluator is not reviewed for T3 suite v2")
    for field in (
        "private_item_scorer_sha256",
        "private_base_scorer_sha256",
        "evaluator_coordinator_sha256",
    ):
        _require_hash(value[field], f"private evaluator manifest {field}")
    if value["provider_call_count"] != 0 or value["external_model_calls_made"] is not False:
        raise FreezeConfigError("private evaluator manifest does not prove zero model calls")
    for field in (
        "contains_private_paths",
        "contains_evaluator_source",
        "contains_candidate_or_expected_answers",
    ):
        if value[field] is not False:
            raise FreezeConfigError(f"private evaluator manifest unsafe field is true: {field}")


def _public_file(public: dict[str, Any], relative: str) -> str:
    files = public.get("public_files")
    if not isinstance(files, dict) or relative not in files:
        raise FreezeConfigError(f"public binding is missing: {relative}")
    return _require_hash(files[relative], f"public_files.{relative}")


def _control_binding(control_ref: str, control_commit: str) -> dict[str, Any]:
    if _run_git(["rev-parse", "HEAD"]) != control_commit:
        raise FreezeConfigError("control checkout HEAD does not match --control-commit")
    if _run_git(["status", "--porcelain"]):
        raise FreezeConfigError("control checkout is dirty; publish and bind a clean commit")
    files: dict[str, str] = {}
    for relative in CONTROL_FILES:
        path = CONTROL_ROOT / relative
        if not path.is_file():
            raise FreezeConfigError(f"reviewed control file is missing: {relative}")
        files[relative] = _sha256_file(path)
    return {
        "repository": "bankingscience/BSLAgenticQuantDevLoop",
        "control_ref": control_ref,
        "control_commit": control_commit,
        "files_sha256": files,
        "files_fingerprint_sha256": _sha256_bytes(_canonical(files)),
    }


def build(
    *,
    public_inventory_path: Path,
    private_manifest_path: Path,
    control_ref: str,
    control_commit: str,
) -> dict[str, Any]:
    public = _load_json(public_inventory_path)
    private = _load_json(private_manifest_path)
    _validate_public(public)
    _validate_private(private)
    config = copy.deepcopy(_load_json(PROSPECTIVE_CONFIG))
    config["freeze_status"] = "ready_for_freeze"
    config["source"] = {
        "repository": "bankingscience/BSLAgenticQuantDevLoop",
        "source_ref": public["source"]["source_ref"],
        "source_commit": public["source"]["source_commit"],
    }
    runtime = public["runtime"]
    config["runtime"].update(
        {
            "image_ref": runtime["image_ref"],
            "image_digest": runtime["image_digest"],
            "runtime_request_schema_sha256": runtime["runtime_request_schema_sha256"],
            "runtime_response_schema_sha256": runtime["runtime_response_schema_sha256"],
            "tool_implementation_sha256": runtime["tool_implementation_sha256"],
            "execution_control_sha256": runtime["execution_control_sha256"],
            "model_visible_tool_fingerprint_sha256": runtime[
                "model_visible_tool_fingerprint_sha256"
            ],
            "provider_identity_sha256": runtime["provider_identity_sha256"],
        }
    )
    suite = config["task_suite"]
    public_field_paths = {
        "task_sha256": suite["task_path"],
        "rubric_sha256": suite["rubric_path"],
        "item_submission_schema_sha256": suite["item_submission_schema_path"],
        "output_schema_sha256": suite["output_schema_path"],
        "scorer_result_schema_sha256": suite["scorer_result_schema_path"],
        "scorer_result_validator_sha256": suite["scorer_result_validator_path"],
    }
    for field, relative in public_field_paths.items():
        suite[field] = _public_file(public, relative)
    for item in suite["public_inputs"]:
        item["sha256"] = _public_file(public, item["path"])
    config["evaluation"] = {
        "private_item_scorer_sha256": private["private_item_scorer_sha256"],
        "private_base_scorer_sha256": private["private_base_scorer_sha256"],
        "evaluator_coordinator_sha256": private["evaluator_coordinator_sha256"],
        "private_evaluator_manifest_sha256": _sha256_file(private_manifest_path),
        "visible_response_trace_schema_sha256": _require_hash(
            public["control"]["visible_response_trace_schema_sha256"],
            "visible response trace schema hash",
        ),
        "item_store_projection_sha256": _sha256_file(
            E3V17_ROOT / "item_store_projection.py"
        ),
    }
    config["control"] = {
        **_control_binding(control_ref, control_commit),
        "public_binding_inventory_sha256": _sha256_file(public_inventory_path),
    }
    config["execution"]["agreement_generation_enabled"] = True
    config["execution"]["formal_model_execution_enabled"] = False
    controls = _controls_module()
    controls.validate_config(config)
    issues = controls.agreement_build_issues(config)
    if issues:
        raise FreezeConfigError("resolved configuration remains unready: " + "; ".join(issues))
    return config


def _write(path: Path, value: dict[str, Any]) -> None:
    resolved = path.resolve()
    if resolved.exists():
        raise FreezeConfigError(f"refusing to overwrite existing output: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(_canonical(value) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-bindings", type=Path, required=True)
    parser.add_argument("--private-evaluator-manifest", type=Path, required=True)
    parser.add_argument("--control-ref", required=True)
    parser.add_argument("--control-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = build(
            public_inventory_path=args.public_bindings.resolve(),
            private_manifest_path=args.private_evaluator_manifest.resolve(),
            control_ref=args.control_ref,
            control_commit=args.control_commit,
        )
        _write(args.output, config)
    except FreezeConfigError as exc:
        print(json.dumps({"status": "blocked", "provider_call_count": 0, "issue": str(exc)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "status": "ready_for_agreement_generation",
                "provider_call_count": 0,
                "external_model_calls_made": False,
                "control_commit": config["control"]["control_commit"],
                "source_commit": config["source"]["source_commit"],
                "image_digest": config["runtime"]["image_digest"],
                "output": str(args.output.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
