#!/usr/bin/env python3
"""Build an offline E2 T3 RAG v6 preparation package, never an agreement.

This module deliberately stops before formal execution.  It accepts only a
fully hash-bound configuration, verifies local files without contacting a
provider or remote Git server, and writes a non-runnable preparation package.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Callable, Final


HERE: Final = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH: Final = HERE / "preparation_config.template.json"
UNRESOLVED: Final = "UNRESOLVED"
CONFIG_SCHEMA_VERSION: Final = "exp2-terra-t3-rag-v6-preparation-config-v1"
PREPARATION_SCHEMA_VERSION: Final = "exp2-terra-t3-rag-v6-preparation-v1"
T3_RELATIVE_ROOT: Final = Path("experiments/shared/t3-quant-suite-v2")
PAIR_ORDER: Final = {
    1: ("C0", "C1"),
    2: ("C1", "C0"),
    3: ("C0", "C1"),
    4: ("C1", "C0"),
    5: ("C0", "C1"),
}
CONDITION_OVERLAYS: Final = {
    "C0": {"rag_enabled": False},
    "C1": {"rag_enabled": True},
}
ITERATION_CONTROLS: Final = {
    "allow_iteration": True,
    "max_iterations": 2,
    "max_failed_iterations": 2,
    "max_agent_turns": 20,
    "max_token_budget_per_run": 1_000_000,
    "timeout_seconds": 1_800,
}
SHARED_BUDGET: Final = {
    "max_attempts": 2,
    "max_calls_per_attempt": 20,
    "max_tokens_per_attempt": 500_000,
    "max_calls_per_observation": 40,
    "max_tokens_per_observation": 1_000_000,
    "attempt_timeout_seconds": 1_800,
    "observation_timeout_seconds": 3_600,
}
OUTCOME_CONTRACT: Final = {
    "primary_outcome": "correct_items_out_of_25",
    "required_item_count": 25,
    "missing_items_reduce_correct_items_out_of_25": True,
    "missing_reported_separately_from_submitted_incorrect": True,
}
PUBLIC_SOURCE_FILES: Final = (
    T3_RELATIVE_ROOT / "TASK.md",
    T3_RELATIVE_ROOT / "RUBRIC.md",
    T3_RELATIVE_ROOT / "item_submission_schema_v2.json",
    T3_RELATIVE_ROOT / "output_schema_v1.json",
    T3_RELATIVE_ROOT / "scorer_result_schema_v2.json",
    T3_RELATIVE_ROOT / "scorer_result_validator.py",
    T3_RELATIVE_ROOT / "input" / "synthetic_portfolio_returns_v1.csv",
    T3_RELATIVE_ROOT / "input" / "portfolio_config_v1.json",
    Path("rae_runtime/proxy/exp3/schemas/t3_item_submission_v2.schema.json"),
    Path("rae_runtime/proxy/t3_quant_item_submission.py"),
    Path("rae_runtime/proxy/t3_quant_item_store.py"),
)
_HASH_RE: Final = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_RE: Final = re.compile(r"^[a-f0-9]{40}$")
_IMAGE_RE: Final = re.compile(r"^sha256:[a-f0-9]{64}$")

_TOP_LEVEL_FIELDS: Final = {
    "schema_version",
    "status",
    "study",
    "source",
    "control",
    "image",
    "rag",
    "private_evaluator",
    "blinding",
    "public_task",
}
_STUDY_FIELDS: Final = {
    "answer_capture_profile",
    "execution_epoch",
    "architecture_mode",
    "task_id",
    "replicates",
    "formal_execution_contract",
    "model_alias",
    "provider_mode",
    "provider_base_url",
}
_SOURCE_FIELDS: Final = {"tree", "ref", "commit_sha"}
_CONTROL_FIELDS: Final = {"tree", "ref", "commit_sha"}
_IMAGE_FIELDS: Final = {"digest"}
_RAG_FIELDS: Final = {
    "corpus_manifest_path",
    "corpus_manifest_sha256",
    "corpus_id",
    "corpus_sha256",
    "index_path",
    "index_id",
    "index_sha256",
    "retrieval_context_path",
    "retrieval_context_sha256",
    "exclusion_list_id",
    "exclusion_list_sha256",
    "cutoff_at",
    "top_k",
}
_EVALUATOR_FIELDS: Final = {
    "manifest_path",
    "manifest_sha256",
    "schema_version",
    "private_t3_item_scorer_sha256",
    "private_evaluator_coordinator_sha256",
    "primary_scorer_commitment_sha256",
}
_BLINDING_FIELDS: Final = {"salt_sha256"}
_PUBLIC_TASK_FIELDS: Final = {
    "relative_root",
    "scorer_result_schema_path",
    "scorer_result_schema_sha256",
    "scorer_result_validator_path",
    "scorer_result_validator_sha256",
}


class PreparationBlockedError(RuntimeError):
    """The draft cannot progress because one or more bindings are unresolved."""


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(value, dict):
        raise ValueError("JSON document must be an object")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _require_exact_fields(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{label} fields do not match the v6 preparation contract")
    return value


def validate_config_shape(value: Any) -> dict[str, Any]:
    config = _require_exact_fields(value, _TOP_LEVEL_FIELDS, "config")
    if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError("config schema_version is invalid")
    if config.get("status") != "draft_unresolved":
        raise ValueError("config status must remain draft_unresolved")
    study = _require_exact_fields(config.get("study"), _STUDY_FIELDS, "study")
    _require_exact_fields(config.get("source"), _SOURCE_FIELDS, "source")
    _require_exact_fields(config.get("control"), _CONTROL_FIELDS, "control")
    _require_exact_fields(config.get("image"), _IMAGE_FIELDS, "image")
    rag = _require_exact_fields(config.get("rag"), _RAG_FIELDS, "rag")
    _require_exact_fields(
        config.get("private_evaluator"), _EVALUATOR_FIELDS, "private_evaluator"
    )
    _require_exact_fields(config.get("blinding"), _BLINDING_FIELDS, "blinding")
    public_task = _require_exact_fields(
        config.get("public_task"), _PUBLIC_TASK_FIELDS, "public_task"
    )
    if (
        study.get("execution_epoch") != "E2T3RAGV6"
        or study.get("answer_capture_profile") != "t3_item_results_v2"
        or study.get("architecture_mode") != "single_agent"
        or study.get("task_id") != "T3"
        or study.get("replicates") != 5
        or study.get("formal_execution_contract") != "exp2_single_agent_rag_v2"
        or study.get("model_alias") != "gpt-5.6-terra"
        or study.get("provider_mode") != "openai"
        or study.get("provider_base_url") != "https://api.openai.com/v1"
    ):
        raise ValueError("study constants drifted")
    if public_task.get("relative_root") != str(T3_RELATIVE_ROOT):
        raise ValueError("public task must use t3-quant-suite-v2")
    if public_task.get("scorer_result_schema_path") != str(
        T3_RELATIVE_ROOT / "scorer_result_schema_v2.json"
    ):
        raise ValueError("public scorer-result schema path drifted")
    if public_task.get("scorer_result_validator_path") != str(
        T3_RELATIVE_ROOT / "scorer_result_validator.py"
    ):
        raise ValueError("public scorer-result validator path drifted")
    if rag.get("top_k") != 5:
        raise ValueError("RAG top_k must remain 5 in both conditions")
    return copy.deepcopy(config)


def unresolved_fields(value: Any, prefix: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else key
            result.extend(unresolved_fields(value[key], child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            result.extend(unresolved_fields(item, f"{prefix}[{index}]"))
    elif value == UNRESOLVED:
        result.append(prefix)
    return result


def execution_schedule() -> list[dict[str, Any]]:
    schedule: list[dict[str, Any]] = []
    for replicate in range(1, 6):
        for condition in PAIR_ORDER[replicate]:
            schedule.append(
                {
                    "sequence": len(schedule) + 1,
                    "task_id": "T3",
                    "replicate": replicate,
                    "pair_id": f"T3-R{replicate}",
                    "condition": condition,
                    "rag_enabled": CONDITION_OVERLAYS[condition]["rag_enabled"],
                }
            )
    if len(schedule) != 10 or len(
        {(x["replicate"], x["condition"]) for x in schedule}
    ) != 10:
        raise AssertionError("schedule must contain five complete C0/C1 pairs")
    return schedule


def pair_difference_fields() -> list[str]:
    left = CONDITION_OVERLAYS["C0"]
    right = CONDITION_OVERLAYS["C1"]
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def _git_probe(path: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return {"head": head, "clean": not bool(dirty)}


def _absolute_existing_file(value: Any, label: str) -> Path:
    if type(value) is not str:
        raise ValueError(f"{label} must be an absolute file path")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise ValueError(f"{label} must be an existing canonical absolute file")
    return path


def _absolute_existing_directory(value: Any, label: str) -> Path:
    if type(value) is not str:
        raise ValueError(f"{label} must be an absolute directory path")
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError(f"{label} must be an existing canonical absolute directory")
    return path


def _require_hash(value: Any, label: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return value


def _require_commit(value: Any, label: str) -> str:
    if type(value) is not str or _COMMIT_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 40-character Git SHA")
    return value


def _verify_hash(path: Path, expected: Any, label: str) -> None:
    digest = _require_hash(expected, label)
    if sha256_file(path) != digest:
        raise ValueError(f"{label} does not match {path}")


def _validate_private_evaluator_manifest(
    path: Path, config: dict[str, Any]
) -> dict[str, str]:
    value = load_json(path)
    expected_fields = {
        "schema_version",
        "private_t3_item_scorer_sha256",
        "private_evaluator_coordinator_sha256",
        "primary_scorer_commitment_sha256",
    }
    _require_exact_fields(value, expected_fields, "private evaluator hash manifest")
    if value.get("schema_version") != config["schema_version"]:
        raise ValueError("private evaluator hash manifest schema drifted")
    for field in sorted(expected_fields - {"schema_version"}):
        _require_hash(value.get(field), f"private_evaluator.{field}")
        if value.get(field) != config.get(field):
            raise ValueError(f"private evaluator hash mismatch: {field}")
    return value


def _validate_retrieval_context(path: Path, rag: dict[str, Any]) -> dict[str, Any]:
    value = load_json(path)
    if (
        value.get("enabled") is not True
        or value.get("index_id") != rag["index_id"]
        or value.get("index_sha256") != rag["index_sha256"]
        or value.get("corpus_id") != rag["corpus_id"]
        or value.get("corpus_sha256") != rag["corpus_sha256"]
        or value.get("exclusion_list_id") != rag["exclusion_list_id"]
        or value.get("exclusion_list_sha256") != rag["exclusion_list_sha256"]
        or value.get("cutoff_at") != rag["cutoff_at"]
        or value.get("requested_top_k") != rag["top_k"]
        or not isinstance(value.get("memories"), list)
        or len(value["memories"]) > rag["top_k"]
    ):
        raise ValueError("frozen retrieval context identity or size drifted")
    return value


def _validate_corpus_manifest(path: Path, rag: dict[str, Any]) -> dict[str, Any]:
    value = load_json(path)
    if (
        value.get("corpus_id") != rag["corpus_id"]
        or value.get("corpus_sha256") != rag["corpus_sha256"]
        or value.get("exclusion_list_id") != rag["exclusion_list_id"]
        or value.get("exclusion_list_sha256") != rag["exclusion_list_sha256"]
        or value.get("cutoff_at") != rag["cutoff_at"]
    ):
        raise ValueError("RAG corpus manifest identity drifted")
    return value


def offline_preflight(
    config_path: Path,
    *,
    git_probe: Callable[[Path], dict[str, Any]] = _git_probe,
) -> dict[str, Any]:
    """Verify local preparation inputs without network or model access."""

    base = {
        "schema_version": "exp2-terra-t3-rag-v6-offline-preflight-v1",
        "provider_call_count": 0,
        "network_call_count": 0,
        "agreement_generated": False,
        "formal_execution_authorized": False,
        "formal_run_command_available": False,
        "planned_observation_count": 10,
        "pair_difference_fields": pair_difference_fields(),
    }
    try:
        config = validate_config_shape(load_json(config_path))
        unresolved = unresolved_fields(config)
        if unresolved:
            return {
                **base,
                "status": "blocked",
                "reason": "unresolved_bindings",
                "unresolved_fields": unresolved,
            }
        if pair_difference_fields() != ["rag_enabled"]:
            raise ValueError("C0/C1 overlays differ beyond rag_enabled")

        source = config["source"]
        control = config["control"]
        public_task = config["public_task"]
        source_tree = _absolute_existing_directory(source["tree"], "source.tree")
        control_tree = _absolute_existing_directory(control["tree"], "control.tree")
        source_commit = _require_commit(source["commit_sha"], "source.commit_sha")
        control_commit = _require_commit(control["commit_sha"], "control.commit_sha")
        source_state = git_probe(source_tree)
        control_state = git_probe(control_tree)
        if source_state != {"head": source_commit, "clean": True}:
            raise ValueError("local source tree is not clean at the configured commit")
        if control_state != {"head": control_commit, "clean": True}:
            raise ValueError("local control tree is not clean at the configured commit")
        if type(source["ref"]) is not str or not source["ref"].startswith("exp2/"):
            raise ValueError("source.ref must be a new exp2 branch")
        if type(control["ref"]) is not str or not control["ref"].startswith("exp2/"):
            raise ValueError("control.ref must be a new exp2 branch")
        if type(config["image"]["digest"]) is not str or _IMAGE_RE.fullmatch(
            config["image"]["digest"]
        ) is None:
            raise ValueError("image.digest must be an immutable sha256 digest")

        public_hashes: dict[str, str] = {}
        for relative in PUBLIC_SOURCE_FILES:
            path = source_tree / relative
            if not path.is_file():
                raise ValueError(f"required public source file is missing: {relative}")
            public_hashes[str(relative)] = sha256_file(path)
        public_schema = source_tree / T3_RELATIVE_ROOT / "item_submission_schema_v2.json"
        runtime_schema = (
            source_tree
            / "rae_runtime/proxy/exp3/schemas/t3_item_submission_v2.schema.json"
        )
        if public_schema.read_bytes() != runtime_schema.read_bytes():
            raise ValueError("public and runtime T3 v2 submission schemas differ")
        _verify_hash(
            source_tree / public_task["scorer_result_schema_path"],
            public_task["scorer_result_schema_sha256"],
            "public_task.scorer_result_schema_sha256",
        )
        _verify_hash(
            source_tree / public_task["scorer_result_validator_path"],
            public_task["scorer_result_validator_sha256"],
            "public_task.scorer_result_validator_sha256",
        )

        rag = config["rag"]
        corpus_manifest = _absolute_existing_file(
            rag["corpus_manifest_path"], "rag.corpus_manifest_path"
        )
        index_path = _absolute_existing_file(rag["index_path"], "rag.index_path")
        retrieval_path = _absolute_existing_file(
            rag["retrieval_context_path"], "rag.retrieval_context_path"
        )
        _verify_hash(
            corpus_manifest,
            rag["corpus_manifest_sha256"],
            "rag.corpus_manifest_sha256",
        )
        _validate_corpus_manifest(corpus_manifest, rag)
        _verify_hash(index_path, rag["index_sha256"], "rag.index_sha256")
        retrieval = _validate_retrieval_context(retrieval_path, rag)
        if sha256_bytes(canonical_bytes(retrieval)) != _require_hash(
            rag["retrieval_context_sha256"], "rag.retrieval_context_sha256"
        ):
            raise ValueError("rag.retrieval_context_sha256 does not match its JSON")

        evaluator = config["private_evaluator"]
        evaluator_manifest = _absolute_existing_file(
            evaluator["manifest_path"], "private_evaluator.manifest_path"
        )
        _verify_hash(
            evaluator_manifest,
            evaluator["manifest_sha256"],
            "private_evaluator.manifest_sha256",
        )
        evaluator_hashes = _validate_private_evaluator_manifest(
            evaluator_manifest, evaluator
        )
        _require_hash(config["blinding"]["salt_sha256"], "blinding.salt_sha256")
        _require_hash(rag["exclusion_list_sha256"], "rag.exclusion_list_sha256")

        return {
            **base,
            "status": "ready_for_preparation_only",
            "unresolved_fields": [],
            "source_commit": source_commit,
            "control_commit": control_commit,
            "public_source_files_sha256": public_hashes,
            "public_source_set_sha256": sha256_bytes(canonical_bytes(public_hashes)),
            "retrieval_context_sha256": sha256_bytes(canonical_bytes(retrieval)),
            "private_evaluator_hashes_sha256": sha256_bytes(
                canonical_bytes(evaluator_hashes)
            ),
            "remote_ref_check": "not_performed_offline",
            "image_presence_check": "not_performed_offline",
        }
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        return {
            **base,
            "status": "blocked",
            "reason": "offline_preflight_failed",
            "errors": [str(exc)],
            "unresolved_fields": [],
        }


def git_worktree_root(path: Path) -> Path | None:
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def require_outside_git(path: Path, label: str) -> None:
    if git_worktree_root(path) is not None:
        raise ValueError(f"{label} must remain outside every Git worktree")


def build_preparation_package(
    *,
    config_path: Path,
    output: Path,
    git_probe: Callable[[Path], dict[str, Any]] = _git_probe,
) -> dict[str, Any]:
    """Write a non-runnable package after the local offline preflight passes."""

    preflight = offline_preflight(config_path, git_probe=git_probe)
    if preflight.get("status") != "ready_for_preparation_only":
        unresolved = preflight.get("unresolved_fields") or []
        suffix = f": {', '.join(unresolved)}" if unresolved else ""
        raise PreparationBlockedError("E2V6 preparation is blocked" + suffix)
    config = validate_config_shape(load_json(config_path))
    output = output.resolve()
    require_outside_git(output, "preparation output")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    try:
        common = {
            "task_id": "T3",
            "answer_capture_profile": config["study"]["answer_capture_profile"],
            "architecture_mode": "single_agent",
            "public_task_root": str(T3_RELATIVE_ROOT),
            "formal_execution_contract": config["study"]["formal_execution_contract"],
            "model_alias": config["study"]["model_alias"],
            "provider_mode": config["study"]["provider_mode"],
            "provider_base_url": config["study"]["provider_base_url"],
            "source_ref": config["source"]["ref"],
            "source_commit": config["source"]["commit_sha"],
            "image_digest": config["image"]["digest"],
            "rag_binding": {
                key: config["rag"][key]
                for key in (
                    "corpus_id",
                    "corpus_sha256",
                    "index_id",
                    "index_sha256",
                    "retrieval_context_sha256",
                    "exclusion_list_id",
                    "exclusion_list_sha256",
                    "cutoff_at",
                    "top_k",
                )
            },
            "iteration_controls": copy.deepcopy(ITERATION_CONTROLS),
            "shared_budget": copy.deepcopy(SHARED_BUDGET),
            "outcome_contract": {
                **copy.deepcopy(OUTCOME_CONTRACT),
                "scorer_result_schema_path": config["public_task"][
                    "scorer_result_schema_path"
                ],
                "scorer_result_schema_sha256": config["public_task"][
                    "scorer_result_schema_sha256"
                ],
                "scorer_result_validator_path": config["public_task"][
                    "scorer_result_validator_path"
                ],
                "scorer_result_validator_sha256": config["public_task"][
                    "scorer_result_validator_sha256"
                ],
            },
        }
        pair_template = {
            "schema_version": "exp2-terra-t3-rag-v6-pair-template-v1",
            "status": "not_runnable",
            "common_request_fields": common,
            "condition_overlays": copy.deepcopy(CONDITION_OVERLAYS),
            "allowed_pair_difference_fields": ["rag_enabled"],
            "schedule": execution_schedule(),
        }
        manifest = {
            "schema_version": PREPARATION_SCHEMA_VERSION,
            "status": "prepared_not_frozen_not_authorized",
            "execution_epoch": "E2T3RAGV6",
            "scope": "T3_only_RAG_vs_no_RAG",
            "planned_observation_count": 10,
            "pair_count": 5,
            "provider_call_count": 0,
            "network_call_count": 0,
            "agreement_generated": False,
            "formal_execution_authorized": False,
            "formal_run_command_available": False,
            "preflight_sha256": sha256_bytes(canonical_bytes(preflight)),
            "pair_template_sha256": sha256_bytes(canonical_bytes(pair_template)),
            "bindings": {
                "source": config["source"],
                "control": config["control"],
                "image": config["image"],
                "rag": {
                    key: value
                    for key, value in config["rag"].items()
                    if not key.endswith("_path")
                },
                "private_evaluator": {
                    key: value
                    for key, value in config["private_evaluator"].items()
                    if key != "manifest_path"
                },
                "blinding": config["blinding"],
            },
            "public_source_files_sha256": preflight["public_source_files_sha256"],
            "next_required_gate": (
                "commit and publish source/control, build immutable image, then create "
                "a separate prospective agreement and ten-observation zero-model preflight"
            ),
        }
        write_json(staging / "offline_preflight.json", preflight)
        write_json(staging / "pair_template.json", pair_template)
        write_json(staging / "preparation_manifest.json", manifest)
        checksum_rows = [
            f"{sha256_file(path)}  {path.relative_to(staging)}"
            for path in sorted(staging.rglob("*.json"))
        ]
        (staging / "checksums.sha256").write_text(
            "\n".join(checksum_rows) + "\n", encoding="utf-8"
        )
        os.replace(staging, output)
        return {
            "status": "prepared_not_frozen_not_authorized",
            "output": str(output),
            "planned_observation_count": 10,
            "provider_call_count": 0,
            "network_call_count": 0,
            "agreement_generated": False,
            "formal_run_command_available": False,
        }
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a non-runnable E2V6 preparation package only."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    preflight = offline_preflight(args.config)
    if preflight.get("status") != "ready_for_preparation_only":
        print(json.dumps(preflight, sort_keys=True))
        return 2
    result = build_preparation_package(config_path=args.config, output=args.output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
