"""Condition-neutral identities for the prospective E4V1 four-cell study.

This module performs deterministic, offline validation only. It does not read
private evaluator contents, retrieve memories, call a provider or launch a
container.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
CONFIG_PATH = HERE / "config" / "e4v1.prospective.json"
SCHEMA_PATH = HERE / "schemas" / "e4v1_config.schema.json"

CONDITION_CODES = ("M0R0", "M0R1", "M1R0", "M1R1")
EXPECTED_CONDITIONS = {
    "M0R0": {
        "architecture_mode": "single_agent",
        "rag_enabled": False,
        "retrieval_context_included": False,
        "role_topology": "single_agent",
    },
    "M0R1": {
        "architecture_mode": "single_agent",
        "rag_enabled": True,
        "retrieval_context_included": True,
        "role_topology": "single_agent",
    },
    "M1R0": {
        "architecture_mode": "manager_star",
        "rag_enabled": False,
        "retrieval_context_included": False,
        "role_topology": "manager_star",
    },
    "M1R1": {
        "architecture_mode": "manager_star",
        "rag_enabled": True,
        "retrieval_context_included": True,
        "role_topology": "manager_star",
    },
}
EXPECTED_CONTROL_FILES = (
    "experiments/exp4/README.md",
    "experiments/exp4/schemas/e4v1_config.schema.json",
    "experiments/exp4/identity.py",
    "experiments/exp4/build_e4v1_agreement.py",
    "experiments/exp4/preflight_e4v1.py",
    "experiments/exp4/PROTOCOL_DRAFT.md",
)

TREATMENT_FIELDS = {
    "architecture_mode",
    "rag_enabled",
    "retrieval_context_included",
    "role_topology",
}
LOGISTICAL_FIELDS = {"run_id", "target_ref", "block_position"}
_PLACEHOLDER = re.compile(r"^__UNRESOLVED_[A-Z0-9_]+__$")
_NEUTRAL_TARGET = re.compile(r"^quant/E4V1-T[123]-K[1-4]-P[1-4]$")
_TREATMENT_LEAK = re.compile(r"M[01]R[01]|single|manager|rag", re.IGNORECASE)


class ConfigError(ValueError):
    """The prospective configuration violates a structural design rule."""


class NotReadyError(RuntimeError):
    """Formal bindings are incomplete, so no agreement may be produced."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("E4V1 is not ready: " + "; ".join(self.issues))


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ConfigError(f"duplicate JSON key is forbidden: {key}")
        value[key] = child
    return value


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if not isinstance(value, dict):
        raise ConfigError("JSON document root must be an object")
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
    schema = _load_json_object(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    value = _load_json_object(path)
    validate_config(value)
    return value


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


def _validate_williams_order(config: dict[str, Any]) -> None:
    orders = config["williams_order"]
    rows = [tuple(orders[str(replicate)]) for replicate in range(1, 5)]
    expected = set(CONDITION_CODES)
    if any(set(row) != expected for row in rows):
        raise ConfigError("each Williams row must contain every condition exactly once")
    for position in range(4):
        if {row[position] for row in rows} != expected:
            raise ConfigError("each condition must occupy every Williams position once")
    transitions = [
        (left, right)
        for row in rows
        for left, right in zip(row, row[1:])
    ]
    expected_transitions = {
        (left, right)
        for left in CONDITION_CODES
        for right in CONDITION_CODES
        if left != right
    }
    if len(transitions) != 12 or set(transitions) != expected_transitions:
        raise ConfigError("Williams rows must balance all 12 directed transitions")


def validate_config(config: dict[str, Any]) -> None:
    errors = _schema_errors(config)
    if errors:
        raise ConfigError("configuration: " + "; ".join(errors))
    if config["conditions"] != EXPECTED_CONDITIONS:
        raise ConfigError("the four condition definitions do not match the E4V1 factorial")
    if tuple(
        item["path"] for item in config["control"]["control_files"]
    ) != EXPECTED_CONTROL_FILES:
        raise ConfigError("control_files must bind the six reviewed E4 control files")
    _validate_williams_order(config)
    t3 = config["task_bindings"]["T3"]
    if (
        config["runtime"]["item_submission_schema_sha256"]
        != t3["item_submission_schema_sha256"]
    ):
        raise ConfigError(
            "runtime and T3 task binding must name the same item-submission schema hash"
        )
    budget = config["shared_budget"]
    if budget["max_model_calls_per_observation"] != (
        budget["max_attempts"] * budget["max_model_calls_per_attempt"]
    ) or budget["max_tokens_per_observation"] != (
        budget["max_attempts"] * budget["max_tokens_per_attempt"]
    ):
        raise ConfigError("observation budget must equal two complete attempt caps")
    role_calls = sum(
        config["manager_star_role_budget"][role]["max_calls"]
        for role in ("manager", "architect", "developer")
    )
    if role_calls != budget["max_model_calls_per_attempt"]:
        raise ConfigError("manager-star role call slices must equal the shared attempt cap")
    if config["execution"]["formal_model_execution_enabled"]:
        raise ConfigError(
            "the prospective control file must never enable formal model execution"
        )


def unresolved_paths(value: Any, prefix: str = "") -> list[str]:
    """Return paths to visible placeholders without exposing any private value."""

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


def _local_file_bindings(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    bindings: list[tuple[str, str, str]] = [
        (
            f"control.control_files[{index}]",
            item["path"],
            item["sha256"],
        )
        for index, item in enumerate(config["control"]["control_files"])
    ]
    for task_id in ("T1", "T2"):
        task = config["task_bindings"][task_id]
        bindings.append(
            (
                f"task_bindings.{task_id}.task_text",
                task["task_text_path"],
                task["task_text_sha256"],
            )
        )
        bindings.extend(
            (
                f"task_bindings.{task_id}.public_inputs[{index}]",
                item["path"],
                item["sha256"],
            )
            for index, item in enumerate(task["public_inputs"])
        )
    t3 = config["task_bindings"]["T3"]
    bindings.extend(
        [
            (
                "task_bindings.T3.task_text",
                t3["task_text_path"],
                t3["task_text_sha256"],
            ),
            (
                "task_bindings.T3.rubric",
                t3["rubric_path"],
                t3["rubric_sha256"],
            ),
            (
                "task_bindings.T3.item_submission_schema",
                t3["item_submission_schema_path"],
                t3["item_submission_schema_sha256"],
            ),
            (
                "task_bindings.T3.output_schema",
                t3["output_schema_path"],
                t3["output_schema_sha256"],
            ),
            (
                "task_bindings.T3.result_contract.scorer_result_schema",
                t3["result_contract"]["scorer_result_schema_path"],
                t3["result_contract"]["scorer_result_schema_sha256"],
            ),
            (
                "task_bindings.T3.result_contract.scorer_result_validator",
                t3["result_contract"]["scorer_result_validator_path"],
                t3["result_contract"]["scorer_result_validator_sha256"],
            ),
        ]
    )
    bindings.extend(
        (
            f"task_bindings.T3.public_inputs[{index}]",
            item["path"],
            item["sha256"],
        )
        for index, item in enumerate(t3["public_inputs"])
    )
    task_retrieval = config["retrieval_binding"]["task_query_context"]
    for task_id in ("T1", "T2", "T3"):
        retrieval = task_retrieval[task_id]
        bindings.extend(
            [
                (
                    f"retrieval_binding.task_query_context.{task_id}.query",
                    retrieval["query_path"],
                    retrieval["query_sha256"],
                ),
                (
                    f"retrieval_binding.task_query_context.{task_id}.retrieval_context",
                    retrieval["retrieval_context_path"],
                    retrieval["retrieval_context_sha256"],
                ),
            ]
        )
    return bindings


def local_binding_issues(
    config: dict[str, Any], *, repository_root: Path = REPOSITORY_ROOT
) -> list[str]:
    """Verify bytes for resolved public/controlled local bindings only."""

    issues: list[str] = []
    for label, configured_path, expected_hash in _local_file_bindings(config):
        if _PLACEHOLDER.fullmatch(configured_path) or _PLACEHOLDER.fullmatch(
            expected_hash
        ):
            continue
        path = Path(configured_path)
        local_path = path if path.is_absolute() else repository_root / path
        if label.startswith(("task_bindings.", "control.")):
            try:
                local_path.resolve().relative_to(repository_root.resolve())
            except ValueError:
                issues.append(f"bound public file escapes repository: {label}")
                continue
        if not local_path.is_file():
            issues.append(f"bound local file is missing: {label}")
            continue
        actual_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            issues.append(f"bound local file hash mismatch: {label}")

    for task_id, binding in config["retrieval_binding"][
        "task_query_context"
    ].items():
        path_value = binding["retrieval_context_path"]
        byte_count = binding["retrieval_context_byte_count"]
        memory_count = binding["memory_count"]
        if (
            _PLACEHOLDER.fullmatch(path_value)
            or (isinstance(byte_count, str) and _PLACEHOLDER.fullmatch(byte_count))
            or (isinstance(memory_count, str) and _PLACEHOLDER.fullmatch(memory_count))
        ):
            continue
        path = Path(path_value)
        local_path = path if path.is_absolute() else repository_root / path
        if not local_path.is_file():
            continue
        content = local_path.read_bytes()
        if len(content) != byte_count:
            issues.append(
                "retrieval context byte count mismatch: "
                f"retrieval_binding.task_query_context.{task_id}"
            )
        try:
            context = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ConfigError):
            issues.append(
                "retrieval context is not strict JSON: "
                f"retrieval_binding.task_query_context.{task_id}"
            )
            continue
        memories = context.get("memories") if isinstance(context, dict) else None
        if not isinstance(context, dict) or context.get("enabled") is not True:
            issues.append(
                "retrieval context is not enabled: "
                f"retrieval_binding.task_query_context.{task_id}"
            )
        if not isinstance(memories, list):
            issues.append(
                "retrieval context memories are not an array: "
                f"retrieval_binding.task_query_context.{task_id}"
            )
        elif len(memories) != memory_count:
            issues.append(
                "retrieval context memory count mismatch: "
                f"retrieval_binding.task_query_context.{task_id}"
            )
    return issues


def readiness_issues(config: dict[str, Any]) -> list[str]:
    """List every fail-closed reason; this function never contacts a provider."""

    validate_config(config)
    issues = [f"unresolved formal binding: {path}" for path in unresolved_paths(config)]
    issues.extend(local_binding_issues(config))
    if config["freeze_status"] != "ready_for_freeze":
        issues.append("freeze_status is not ready_for_freeze")
    if not config["execution"]["agreement_generation_enabled"]:
        issues.append("this prospective version does not enable agreement generation")
    return issues


def require_ready(config: dict[str, Any]) -> None:
    issues = readiness_issues(config)
    if issues:
        raise NotReadyError(issues)


def block_order(config: dict[str, Any]) -> list[tuple[str, int]]:
    validate_config(config)
    seed = config["block_order_seed"]
    blocks = [
        (task_id, replicate)
        for task_id in config["task_ids"]
        for replicate in range(1, config["replicate_count"] + 1)
    ]
    return sorted(
        blocks,
        key=lambda item: hashlib.sha256(
            f"{seed}\0{item[0]}\0{item[1]}".encode("utf-8")
        ).hexdigest(),
    )


def neutral_run_id(task_id: str, replicate: int, position: int) -> str:
    return f"E4V1-{task_id}-K{replicate}-P{position}"


def neutral_target_ref(task_id: str, replicate: int, position: int) -> str:
    value = f"quant/{neutral_run_id(task_id, replicate, position)}"
    if not _NEUTRAL_TARGET.fullmatch(value) or _TREATMENT_LEAK.search(value):
        raise ConfigError("target ref is not condition-neutral")
    return value


def build_run_identity(
    config: dict[str, Any],
    *,
    task_id: str,
    replicate: int,
    position: int,
    condition_code: str,
) -> dict[str, Any]:
    validate_config(config)
    if task_id not in config["task_ids"]:
        raise ConfigError(f"unknown task_id: {task_id}")
    if replicate not in range(1, config["replicate_count"] + 1):
        raise ConfigError("replicate is outside 1..4")
    if position not in range(1, 5):
        raise ConfigError("block position is outside 1..4")
    expected_code = config["williams_order"][str(replicate)][position - 1]
    if condition_code != expected_code:
        raise ConfigError("condition does not match the frozen Williams position")

    condition = config["conditions"][condition_code]
    task_binding = config["task_bindings"][task_id]
    retrieval_binding = copy.deepcopy(config["retrieval_binding"])
    task_query_context = retrieval_binding.pop("task_query_context")
    retrieval_binding["task_id"] = task_id
    retrieval_binding["query_context"] = task_query_context[task_id]
    value = {
        "schema_version": "exp4-e4v1-run-identity-v1",
        "experiment_id": config["experiment_id"],
        "run_id": neutral_run_id(task_id, replicate, position),
        "target_ref": neutral_target_ref(task_id, replicate, position),
        "task_id": task_id,
        "answer_capture_profile": task_binding["answer_capture_profile"],
        "replicate": replicate,
        "block_position": position,
        **copy.deepcopy(condition),
        "source": copy.deepcopy(config["source"]),
        "control": copy.deepcopy(config["control"]),
        "runtime": copy.deepcopy(config["runtime"]),
        "task_binding": copy.deepcopy(task_binding),
        "evaluator_sha256": config["evaluator_bindings"][task_id],
        "evaluator_coordinator_sha256": config["evaluator_bindings"][
            "coordinator_sha256"
        ],
        "retrieval_binding": retrieval_binding,
        "shared_budget": copy.deepcopy(config["shared_budget"]),
        "manager_star_role_budget": copy.deepcopy(
            config["manager_star_role_budget"]
        ),
    }
    value["identity_sha256"] = sha256_value(value)
    return value


def build_block_identities(
    config: dict[str, Any], task_id: str, replicate: int
) -> list[dict[str, Any]]:
    if task_id not in config.get("task_ids", []):
        raise ConfigError(f"unknown task_id: {task_id}")
    if replicate not in range(1, int(config.get("replicate_count", 0)) + 1):
        raise ConfigError("replicate is outside 1..4")
    order = config["williams_order"][str(replicate)]
    identities = [
        build_run_identity(
            config,
            task_id=task_id,
            replicate=replicate,
            position=position,
            condition_code=condition_code,
        )
        for position, condition_code in enumerate(order, start=1)
    ]
    validate_block(identities)
    return identities


def comparison_projection(identity_value: dict[str, Any]) -> dict[str, Any]:
    """Remove only treatment and neutral storage/schedule fields for equality."""

    value = copy.deepcopy(identity_value)
    value.pop("identity_sha256", None)
    for field in TREATMENT_FIELDS | LOGISTICAL_FIELDS:
        value.pop(field, None)
    return value


def validate_block(identities: list[dict[str, Any]]) -> None:
    if len(identities) != 4:
        raise ConfigError("one block must contain exactly four observations")
    if len({item["run_id"] for item in identities}) != 4:
        raise ConfigError("block run IDs must be unique")
    if len({item["target_ref"] for item in identities}) != 4:
        raise ConfigError("block target refs must be unique")
    observed_treatments = {
        (
            item["architecture_mode"],
            item["rag_enabled"],
            item["retrieval_context_included"],
            item["role_topology"],
        )
        for item in identities
    }
    expected_treatments = {
        (
            value["architecture_mode"],
            value["rag_enabled"],
            value["retrieval_context_included"],
            value["role_topology"],
        )
        for value in EXPECTED_CONDITIONS.values()
    }
    if observed_treatments != expected_treatments:
        raise ConfigError("block does not contain the four factorial treatments")
    projection = comparison_projection(identities[0])
    if any(comparison_projection(item) != projection for item in identities[1:]):
        raise ConfigError("block identities differ outside allowed treatment fields")


def build_schedule(config: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for execution_position, (task_id, replicate) in enumerate(block_order(config), start=1):
        identities = build_block_identities(config, task_id, replicate)
        records.append(
            {
                "block_execution_position": execution_position,
                "block_id": f"{task_id}-K{replicate}",
                "task_id": task_id,
                "replicate": replicate,
                "observations": identities,
            }
        )
    if len(records) != 12 or sum(len(item["observations"]) for item in records) != 48:
        raise AssertionError("E4V1 schedule must contain 12 blocks and 48 observations")
    return records
