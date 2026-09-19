from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from experiments.exp4 import (
    build_e4v2_agreement,
    build_e4v2_launch_package as launch_builder,
    e4v3_plan,
    identity_e4v2 as identity,
    run_e4v2_batch,
    run_e4v2_observation,
)


def _resolved_e4v3_config(context_path: Path) -> dict:
    config = e4v3_plan.build_prospective_config()
    replacements = {
        "__UNRESOLVED_SOURCE_REF__": "exp4/e4v3-shared-source",
        "__UNRESOLVED_SOURCE_COMMIT_SHA__": "a" * 40,
        "__UNRESOLVED_CONTROL_REF__": "exp4/e4v3-controls",
        "__UNRESOLVED_CONTROL_COMMIT_SHA__": "d" * 40,
        "__UNRESOLVED_IMAGE_REF__": "exp4-shared-runtime",
        "__UNRESOLVED_IMAGE_DIGEST__": "sha256:" + "b" * 64,
        "__UNRESOLVED_RETRIEVER_NAME__": "frozen_jira_memory",
        "__UNRESOLVED_RETRIEVER_VERSION__": "v1",
        "__UNRESOLVED_CORPUS_ID__": "e4v3-corpus",
        "__UNRESOLVED_INDEX_ID__": "e4v3-index",
        "__UNRESOLVED_EXCLUSION_LIST_ID__": "e4v3-exclusions",
        "__UNRESOLVED_RETRIEVAL_CUTOFF__": "2026-09-03T00:00:00+00:00",
    }

    def resolve(value):
        if isinstance(value, dict):
            return {key: resolve(child) for key, child in value.items()}
        if isinstance(value, list):
            return [resolve(child) for child in value]
        if isinstance(value, str) and value in replacements:
            return replacements[value]
        if isinstance(value, str) and value.startswith("__UNRESOLVED_"):
            return "c" * 64
        return value

    config = resolve(config)
    root = identity.REPOSITORY_ROOT
    file_hash = lambda path: hashlib.sha256((root / path).read_bytes()).hexdigest()
    t3 = config["task_bindings"]["T3"]
    for field, path_field in (
        ("task_text_sha256", "task_text_path"),
        ("rubric_sha256", "rubric_path"),
        ("item_submission_schema_sha256", "item_submission_schema_path"),
        ("output_schema_sha256", "output_schema_path"),
    ):
        t3[field] = file_hash(t3[path_field])
    for item in t3["public_inputs"]:
        item["sha256"] = file_hash(item["path"])
    contract = t3["result_contract"]
    contract["scorer_result_schema_sha256"] = file_hash(contract["scorer_result_schema_path"])
    contract["scorer_result_validator_sha256"] = file_hash(contract["scorer_result_validator_path"])
    config["runtime"]["item_submission_schema_sha256"] = t3["item_submission_schema_sha256"]
    config["runtime"]["runtime_request_schema_sha256"] = file_hash("rae_runtime/sandbox/schemas/runtime_request.schema.json")
    for item in config["control"]["control_files"]:
        item["sha256"] = file_hash(item["path"])
    context_bytes = json.dumps(
        json.loads(context_path.read_text(encoding="utf-8")),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    retrieval = config["retrieval_binding"]["task_query_context"]["T3"]
    retrieval.update(
        query_path="experiments/exp4/E4V3_PROTOCOL.md",
        query_sha256=file_hash("experiments/exp4/E4V3_PROTOCOL.md"),
        retrieval_context_path=str(context_path),
        retrieval_context_sha256=hashlib.sha256(context_bytes).hexdigest(),
        retrieval_context_byte_count=len(context_bytes),
        memory_count=1,
    )
    config["freeze_status"] = "ready_for_freeze"
    config["execution"]["agreement_generation_enabled"] = True
    identity.validate_config(config)
    return config


def test_e4v3_plan_registers_four_conditions_by_fifteen_replicates() -> None:
    config = e4v3_plan.build_prospective_config()

    assert config["experiment_id"] == "E4V3"
    assert config["replicate_count"] == 15
    assert config["execution"]["observations_per_authorization"] is None
    assert config["execution"]["registered_observation_count"] == 60
    assert config["execution"]["selection_mode"] == "runtime_explicit_subset"
    schedule = identity.build_schedule(config)
    flattened = identity.build_global_batch(config)
    assert len(schedule) == 15
    assert len(flattened) == 60
    assert len({row["run_id"] for row in flattened}) == 60
    assert {
        condition: sum(row["condition_code"] == condition for row in flattened)
        for condition in identity.CONDITION_CODES
    } == {condition: 15 for condition in identity.CONDITION_CODES}


def test_e4v3_pool_size_is_frozen_but_batch_size_is_not() -> None:
    with pytest.raises(ValueError, match="exactly 15"):
        e4v3_plan.build_prospective_config(10)

    config = copy.deepcopy(e4v3_plan.build_prospective_config())
    config["replicate_count"] = 10
    with pytest.raises(identity.ConfigError, match="replicate_count|15 available repeats"):
        identity.validate_config(config)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (["M1R1"], ("M1R1",)),
        (["M1R1", "M0R0"], ("M0R0", "M1R1")),
        (list(reversed(identity.CONDITION_CODES)), identity.CONDITION_CODES),
    ],
)
def test_runtime_condition_selection_is_canonical(values, expected) -> None:
    assert identity.normalize_selected_conditions(values) == expected


def test_runtime_replicate_range_accepts_one_or_many_and_rejects_overflow() -> None:
    assert run_e4v2_batch._selected_replicates(1, 1, 15) == (1,)
    assert run_e4v2_batch._selected_replicates(2, 9, 15) == tuple(range(2, 11))
    assert run_e4v2_batch._selected_replicates(11, 5, 15) == tuple(range(11, 16))
    with pytest.raises(run_e4v2_batch.BatchLaunchError, match="exceeds"):
        run_e4v2_batch._selected_replicates(11, 6, 15)


def test_runtime_authorization_names_the_exact_selection() -> None:
    value = run_e4v2_batch._global_authorization_value(
        2, 9, ("M0R0", "M1R1")
    )
    assert value == "AUTHORIZE_E4V3_T3_M0R0_M1R1_K2_TO_K10_18_OBSERVATIONS"


def test_execution_environment_accepts_exact_authorization(monkeypatch) -> None:
    selected = ("M1R1",)
    monkeypatch.setenv(
        "E4V3_BATCH_AUTHORIZATION",
        run_e4v2_batch._global_authorization_value(1, 1, selected),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-offline-shape-only")
    environment = run_e4v2_batch._global_execution_environment(1, 1, selected)
    assert environment["OPENAI_API_KEY"] == "sk-offline-shape-only"


def _fake_launch_manifest() -> dict:
    runs = []
    for replicate in range(1, 16):
        for condition in identity.CONDITION_CODES:
            runs.append({
                "run_id": f"E4V3-T3-K{replicate}-{condition}",
                "replicate": replicate,
                "condition_code": condition,
            })
    return {
        "experiment_id": "E4V3",
        "replicate_count": 15,
        "registered_conditions": list(identity.CONDITION_CODES),
        "registered_observation_count": 60,
        "selection_mode": "runtime_explicit_subset",
        "observations_per_authorization": None,
        "runs": runs,
    }


def test_global_preflight_selects_only_requested_conditions_and_range(
    monkeypatch, tmp_path: Path
) -> None:
    package = tmp_path / "launch"
    package.mkdir()
    manifest = _fake_launch_manifest()
    (package / "launch_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fake_preflight(_package, run_id):
        row = next(row for row in manifest["runs"] if row["run_id"] == run_id)
        condition = row["condition_code"]
        return {
            "config": {"experiment_id": "E4V3"},
            "agreement_sha256": "a" * 64,
            "launch_package_sha256": "b" * 64,
            "run_id": run_id,
            "target_ref": f"quant/{run_id}",
            "condition_code": condition,
            "architecture_mode": "manager_star" if condition.startswith("M1") else "single_agent",
            "rag_enabled": condition.endswith("R1"),
            "image_digest": "sha256:" + "c" * 64,
            "status": "ready",
            "provider_call_count": 0,
        }

    monkeypatch.setattr(run_e4v2_batch.observation, "preflight", fake_preflight)
    monkeypatch.setattr(run_e4v2_batch, "_scorer_preflight", lambda config: {
        "status": "ready", "provider_call_count": 0, "total_items": 25,
    })
    ready = run_e4v2_batch.preflight_global_batch(package, 2, 3, ["M1R1", "M0R0"])
    assert ready["replicate_start"] == 2
    assert ready["replicate_end"] == 4
    assert ready["replicate_count"] == 3
    assert ready["selected_conditions"] == ["M0R0", "M1R1"]
    assert ready["condition_counts"] == {"M0R0": 3, "M1R1": 3}
    assert ready["observation_count"] == 6
    assert all("-K1-" not in run_id and "-K5-" not in run_id for run_id in ready["run_ids"])


def test_runtime_selection_is_recorded_before_key_prompt(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(run_e4v2_batch, "E4V3_SELECTION_ROOT", tmp_path)
    ready = {
        "agreement_sha256": "a" * 64,
        "launch_package_sha256": "b" * 64,
        "registered_replicate_count": 15,
        "replicate_start": 1,
        "replicate_count": 1,
        "replicate_end": 1,
        "selected_conditions": ["M1R1"],
        "condition_counts": {"M1R1": 1},
        "observation_count": 1,
        "run_ids": ["E4V3-T3-K1-P3"],
    }
    path = run_e4v2_batch._record_runtime_selection(ready)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["status"] == "selection_recorded_before_model_key"
    assert value["provider_call_count"] == 0
    assert value["run_ids"] == ready["run_ids"]
    unsigned = dict(value)
    claimed = unsigned.pop("selection_sha256")
    assert claimed == identity.sha256_value(unsigned)


def test_e4v3_agreement_launch_pool_and_observation_preflight_connect(
    monkeypatch, tmp_path: Path
) -> None:
    fixture = identity.REPOSITORY_ROOT / "experiments/exp4/tests/fixtures/retrieval_context.json"
    context_path = tmp_path / "retrieval_context.json"
    context_path.write_bytes(fixture.read_bytes())
    config = _resolved_e4v3_config(context_path)
    context_path.write_bytes(json.dumps(
        json.loads(context_path.read_text(encoding="utf-8")),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8"))
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    (freeze / "retrieval_context.json").write_bytes(context_path.read_bytes())
    (freeze / "freeze_config.json").write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    agreement_dir = tmp_path / "agreement"
    agreement = build_e4v2_agreement.build_agreement(config, agreement_dir)
    assert agreement["experiment_id"] == "E4V3"
    assert agreement["observation_count"] == 60
    assert agreement["registered_conditions"] == list(identity.CONDITION_CODES)
    monkeypatch.setattr(launch_builder, "_launch_control", lambda ref, commit: {
        "repository": "bankingscience/BSLAgenticQuantDevLoop", "control_ref": ref,
        "control_commit": commit, "files_sha256": {"synthetic": "a" * 64},
        "files_fingerprint_sha256": "b" * 64,
    })
    launch_dir = tmp_path / "launch"
    manifest = launch_builder.build(
        freeze_package=freeze, agreement_dir=agreement_dir,
        control_ref=config["control"]["control_ref"], control_commit=config["control"]["control_commit"], output=launch_dir,
    )
    assert manifest["replicate_count"] == 15
    assert manifest["registered_observation_count"] == 60
    assert manifest["observations_per_authorization"] is None
    assert len(manifest["runs"]) == 60
    monkeypatch.setattr(run_e4v2_observation, "_validate_control", lambda value: None)
    monkeypatch.setattr(run_e4v2_observation, "_image_available", lambda value: None)
    monkeypatch.setattr(run_e4v2_observation, "_remote_head", lambda ref: config["source"]["source_commit"] if ref == config["source"]["source_ref"] else None)
    monkeypatch.setattr(run_e4v2_observation, "PROJECT_ROOT", tmp_path)
    state = run_e4v2_observation.preflight(launch_dir, manifest["runs"][0]["run_id"])
    assert state["config"]["experiment_id"] == "E4V3"
    assert state["provider_call_count"] == 0
