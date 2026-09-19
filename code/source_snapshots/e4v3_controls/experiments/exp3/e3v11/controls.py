"""Offline-only identity and readiness checks for the E3V11 draft.

This module never calls a model, evaluator, container, repository host or any
other external system.  It keeps the future M0/M1 comparison inspectable while
every formal binding remains deliberately unresolved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator, FormatChecker


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[2]
CONFIG_PATH = HERE / "config" / "e3v11.prospective.json"
SCHEMA_PATH = HERE / "schemas" / "e3v11_config.schema.json"

ARMS = ("M0", "M1")
EXPECTED_CONDITIONS = {
    "M0": {
        "architecture_mode": "single_agent",
        "role_topology": "single_agent",
    },
    "M1": {
        "architecture_mode": "manager_star",
        "role_topology": "manager_star",
    },
}
EXPECTED_ARM_ORDER = {
    "1": ["M0", "M1"],
    "2": ["M1", "M0"],
    "3": ["M0", "M1"],
    "4": ["M1", "M0"],
    "5": ["M0", "M1"],
}
EXPECTED_PUBLIC_INPUTS = {
    "experiments/shared/t3-quant-suite-v2/input/portfolio_config_v1.json",
    "experiments/shared/t3-quant-suite-v2/input/synthetic_portfolio_returns_v1.csv",
}
REGISTERED_CHANGES_FROM_E3V10 = {
    "reason": "increase_equal_capacity_and_repair_answer_blind_scorer_input_compatibility",
    "per_attempt_token_cap": {"from": 350000, "to": 500000},
    "per_observation_token_cap": {"from": 700000, "to": 1000000},
    "model_visible_task_changed": False,
    "public_inputs_changed": False,
    "scoring_rule_changed": False,
    "candidate_values_changed_by_projection": False,
    "same_change_applies_to_both_arms": True,
}
PUBLIC_FILE_BINDINGS = (
    ("task_path", "task_sha256"),
    ("rubric_path", "rubric_sha256"),
    ("item_submission_schema_path", "item_submission_schema_sha256"),
    ("output_schema_path", "output_schema_sha256"),
    ("scorer_result_schema_path", "scorer_result_schema_sha256"),
    ("scorer_result_validator_path", "scorer_result_validator_sha256"),
)
_PLACEHOLDER = re.compile(r"^__UNRESOLVED_[A-Z0-9_]+__$")


class ConfigError(ValueError):
    """The prospective E3V11 configuration violates a design constraint."""


class NotReadyError(RuntimeError):
    """Formal bindings or reviewed generation gates are incomplete."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("E3V11 is not ready: " + "; ".join(self.issues))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ConfigError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ConfigError(f"non-finite JSON constant: {value}")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )
    if not isinstance(value, dict):
        raise ConfigError("JSON root must be an object")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = _load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _schema_errors(value: Any) -> list[str]:
    errors = sorted(
        _validator().iter_errors(value),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"violates {error.validator or 'schema'}"
        for error in errors
    ]


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    value = _load_json(path)
    validate_config(value)
    return value


def validate_config(config: dict[str, Any]) -> None:
    errors = _schema_errors(config)
    if errors:
        raise ConfigError("configuration: " + "; ".join(errors))
    if config["conditions"] != EXPECTED_CONDITIONS:
        raise ConfigError("M0 and M1 do not match the E3V11 architecture contrast")
    if config["arm_order"] != EXPECTED_ARM_ORDER:
        raise ConfigError("E3V11 must retain the alternating E3V8 pair order")
    public_paths = {
        item["path"] for item in config["task_suite"]["public_inputs"]
    }
    if public_paths != EXPECTED_PUBLIC_INPUTS:
        raise ConfigError("T3 v2 public input inventory is incomplete or changed")
    role_calls = sum(
        config["manager_star_role_budget"][role]["max_calls"]
        for role in ("manager", "architect", "developer")
    )
    if role_calls != config["shared_budget"]["max_model_calls_per_attempt"]:
        raise ConfigError("M1 role-call slices must equal the shared per-attempt cap")
    if config["execution"]["formal_model_execution_enabled"]:
        raise ConfigError("the E3V11 preparation file must not enable model execution")
    if (
        config["freeze_status"] == "prospective_unready"
        and config["execution"]["agreement_generation_enabled"]
    ):
        raise ConfigError("an unresolved prospective config cannot enable agreement generation")
    if (
        config["freeze_status"] == "ready_for_freeze"
        and not config["execution"]["agreement_generation_enabled"]
    ):
        raise ConfigError("a freeze-ready config must enable agreement generation")
    if (
        config["freeze_status"] == "ready_for_freeze"
        and config["source"]["source_ref"] != "exp/shared-t3-runtime-v2"
    ):
        raise ConfigError("E3V11 must bind the published shared runtime v2 source ref")
    if (
        config["freeze_status"] == "ready_for_freeze"
        and config["control"]["control_ref"] != "exp/e3v11-freeze-controls"
    ):
        raise ConfigError("E3V11 must bind the dedicated freeze-control ref")


def unresolved_paths(value: Any, prefix: str = "") -> list[str]:
    """Return placeholder paths without reading or revealing private material."""

    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.extend(unresolved_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(unresolved_paths(child, f"{prefix}[{index}]"))
    elif isinstance(value, str) and _PLACEHOLDER.fullmatch(value):
        result.append(prefix or "<root>")
    return result


def binding_issues(config: dict[str, Any]) -> list[str]:
    """List unresolved or mismatched inputs needed before agreement freezing.

    This stage is deliberately independent of both execution gates.  It checks
    only whether the source, image, task, tool and evaluator identities are
    fully bound and whether the prospective review marked the configuration as
    ready for freezing.
    """

    validate_config(config)
    issues = [
        f"unresolved formal binding: {path}"
        for path in unresolved_paths(config)
    ]
    suite = config["task_suite"]
    public_bindings = [
        (suite[path_field], suite[hash_field], hash_field)
        for path_field, hash_field in PUBLIC_FILE_BINDINGS
    ]
    public_bindings.extend(
        (item["path"], item["sha256"], f"public_inputs[{index}].sha256")
        for index, item in enumerate(suite["public_inputs"])
    )
    for relative, expected, label in public_bindings:
        if isinstance(expected, str) and _PLACEHOLDER.fullmatch(expected):
            continue
        path = REPOSITORY_ROOT / relative
        if not path.is_file():
            issues.append(f"public binding file is missing: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            issues.append(f"public binding hash mismatch: task_suite.{label}")
    control = config["control"]
    control_files = control["files_sha256"]
    if config["freeze_status"] == "ready_for_freeze" and not control_files:
        issues.append("control.files_sha256 is empty")
    for relative, expected in control_files.items():
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            issues.append(f"control binding path is unsafe: {relative}")
            continue
        path = REPOSITORY_ROOT / candidate
        if not path.is_file():
            issues.append(f"control binding file is missing: {relative}")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            issues.append(f"control binding hash mismatch: {relative}")
    if control_files:
        actual_fingerprint = sha256_value(control_files)
        expected_fingerprint = control["files_fingerprint_sha256"]
        if not _PLACEHOLDER.fullmatch(expected_fingerprint) and (
            actual_fingerprint != expected_fingerprint
        ):
            issues.append("control.files_fingerprint_sha256 does not match files_sha256")
    if config["freeze_status"] != "ready_for_freeze":
        issues.append("freeze_status is not ready_for_freeze")
    return issues


def agreement_build_issues(config: dict[str, Any]) -> list[str]:
    """Return reasons an agreement cannot be built with model calls disabled.

    Agreement generation records exactly what a later run would use; it is not
    itself permission to contact Terra.  Requiring the execution gate to remain
    false prevents a configuration review or builder invocation from becoming
    an accidental model-run authorization.
    """

    issues = binding_issues(config)
    if not config["execution"]["agreement_generation_enabled"]:
        issues.append("this preparation version does not enable agreement generation")
    if config["execution"]["formal_model_execution_enabled"]:
        issues.append(
            "formal model execution must remain disabled while building the agreement"
        )
    return issues


def readiness_issues(config: dict[str, Any]) -> list[str]:
    """Backward-compatible name for the agreement-building readiness stage."""

    return agreement_build_issues(config)


def require_agreement_ready(config: dict[str, Any]) -> None:
    issues = agreement_build_issues(config)
    if issues:
        raise NotReadyError(issues)


def require_ready(config: dict[str, Any]) -> None:
    """Backward-compatible wrapper for agreement-building readiness."""

    require_agreement_ready(config)


def _run_id(replicate: int, arm: str) -> str:
    return f"E3V11-T3-R{replicate}-{arm}"


def build_run_identity(
    config: dict[str, Any], *, replicate: int, arm: str, sequence: int
) -> dict[str, Any]:
    """Build one inspectable identity without asserting formal readiness."""

    validate_config(config)
    if replicate not in range(1, config["replicate_count"] + 1):
        raise ConfigError("replicate must be between 1 and 5")
    if arm not in ARMS:
        raise ConfigError("arm must be M0 or M1")
    order = config["arm_order"][str(replicate)]
    if sequence not in (1, 2) or order[sequence - 1] != arm:
        raise ConfigError("arm does not match the registered pair sequence")
    paired_arm = "M1" if arm == "M0" else "M0"
    run_id = _run_id(replicate, arm)
    identity = {
        "schema_version": "exp3-e3v11-run-identity-draft-v1",
        "experiment_id": config["experiment_id"],
        "study_id": config["study_id"],
        "run_id": run_id,
        "target_ref": f"quant/{run_id}",
        "paired_target_ref": f"quant/{_run_id(replicate, paired_arm)}",
        "task_id": config["task_id"],
        "answer_capture_profile": config["task_suite"][
            "answer_capture_profile"
        ],
        "replicate": replicate,
        "sequence_in_pair": sequence,
        **copy.deepcopy(config["conditions"][arm]),
        "rag_enabled": config["execution"]["rag_enabled"],
        "retrieval_context_included": config["execution"][
            "retrieval_context_included"
        ],
        "source": copy.deepcopy(config["source"]),
        "runtime": copy.deepcopy(config["runtime"]),
        "task_suite": copy.deepcopy(config["task_suite"]),
        "tool_contract": copy.deepcopy(config["tool_contract"]),
        "shared_budget": copy.deepcopy(config["shared_budget"]),
        "role_budget": (
            copy.deepcopy(config["manager_star_role_budget"])
            if arm == "M1"
            else None
        ),
        "evaluation": copy.deepcopy(config["evaluation"]),
        "control": copy.deepcopy(config["control"]),
    }
    identity["identity_sha256"] = sha256_value(identity)
    return identity


def comparison_projection(identity: dict[str, Any]) -> dict[str, Any]:
    """Remove only architecture treatment and pair-logistics fields."""

    value = copy.deepcopy(identity)
    for field in (
        "identity_sha256",
        "run_id",
        "target_ref",
        "paired_target_ref",
        "sequence_in_pair",
        "architecture_mode",
        "role_topology",
        "role_budget",
    ):
        value.pop(field, None)
    return value


def validate_pair(pair: list[dict[str, Any]]) -> None:
    if len(pair) != 2:
        raise ConfigError("an E3V11 pair must contain exactly two observations")
    modes = {item.get("architecture_mode") for item in pair}
    if modes != {"single_agent", "manager_star"}:
        raise ConfigError("a pair must contain one M0 and one M1 observation")
    if comparison_projection(pair[0]) != comparison_projection(pair[1]):
        raise ConfigError("the pair differs outside the registered architecture treatment")
    targets = {item["target_ref"] for item in pair}
    if len(targets) != 2:
        raise ConfigError("paired target refs must be distinct")
    for item in pair:
        if item["source"]["source_ref"] in {
            item["target_ref"],
            item["paired_target_ref"],
        }:
            raise ConfigError("source and target refs must be distinct")


def build_pair_identities(
    config: dict[str, Any], replicate: int
) -> list[dict[str, Any]]:
    validate_config(config)
    if replicate not in range(1, config["replicate_count"] + 1):
        raise ConfigError("replicate must be between 1 and 5")
    pair = [
        build_run_identity(
            config,
            replicate=replicate,
            arm=arm,
            sequence=sequence,
        )
        for sequence, arm in enumerate(
            config["arm_order"][str(replicate)], start=1
        )
    ]
    validate_pair(pair)
    return pair


def build_schedule(config: dict[str, Any]) -> list[dict[str, Any]]:
    validate_config(config)
    return [
        {
            "pair_id": f"T3_R{replicate}",
            "replicate": replicate,
            "observations": build_pair_identities(config, replicate),
        }
        for replicate in range(1, config["replicate_count"] + 1)
    ]
