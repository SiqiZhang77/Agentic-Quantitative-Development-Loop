from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from experiments.exp3.e3v9 import build_e3v9_agreement, controls, preflight_e3v9


def _resolved_bindings_but_disabled_gates() -> dict:
    config = copy.deepcopy(controls.load_config())
    replacements = {
        "__UNRESOLVED_E3V9_SOURCE_REF__": "exp3/e3v9-immediate-item-source",
        "__UNRESOLVED_E3V9_SOURCE_COMMIT_SHA__": "a" * 40,
        "__UNRESOLVED_E3V9_IMAGE_REF__": "exp3-e3v9-runtime",
        "__UNRESOLVED_E3V9_IMAGE_DIGEST__": "sha256:" + "b" * 64,
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
    suite = config["task_suite"]
    for path_field, hash_field in controls.PUBLIC_FILE_BINDINGS:
        suite[hash_field] = hashlib.sha256(
            (controls.REPOSITORY_ROOT / suite[path_field]).read_bytes()
        ).hexdigest()
    for item in suite["public_inputs"]:
        item["sha256"] = hashlib.sha256(
            (controls.REPOSITORY_ROOT / item["path"]).read_bytes()
        ).hexdigest()
    config["freeze_status"] = "ready_for_freeze"
    controls.validate_config(config)
    assert controls.binding_issues(config) == []
    assert controls.agreement_build_issues(config) == [
        "this preparation version does not enable agreement generation",
    ]
    return config


def test_checked_in_config_is_explicitly_unready_and_t3_v2_only() -> None:
    config = controls.load_config()
    issues = controls.readiness_issues(config)

    assert config["freeze_status"] == "prospective_unready"
    assert config["task_id"] == "T3"
    assert config["task_suite"]["version"] == "t3-quant-suite-v2"
    assert config["task_suite"]["answer_capture_profile"] == (
        "t3_item_results_v2"
    )
    assert config["task_suite"]["root"] == (
        "experiments/shared/t3-quant-suite-v2"
    )
    assert any("source.source_commit" in issue for issue in issues)
    assert any("runtime.image_digest" in issue for issue in issues)
    assert any("evaluation.private_item_scorer_sha256" in issue for issue in issues)
    assert any("task_suite.scorer_result_schema_sha256" in issue for issue in issues)


def test_current_builder_refuses_before_writing_any_agreement() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "agreement"
        with pytest.raises(controls.NotReadyError):
            build_e3v9_agreement.build_agreement(controls.load_config(), output)
        assert not output.exists()


def test_zero_model_preflight_reports_no_observation_or_external_call() -> None:
    result = preflight_e3v9.preflight(controls.load_config(), replicate=1)

    assert result["status"] == "unready"
    assert result["formal_observation"] is False
    assert result["provider_call_count"] == 0
    assert result["external_calls_made"] is False
    assert result["observation_count"] == 0
    assert result["run_ids"] == []


def test_e3v8_pair_count_and_alternating_arm_order_are_retained() -> None:
    config = _resolved_bindings_but_disabled_gates()
    schedule = controls.build_schedule(config)

    assert len(schedule) == 5
    assert sum(len(pair["observations"]) for pair in schedule) == 10
    assert [
        [
            item["architecture_mode"]
            for item in pair["observations"]
        ]
        for pair in schedule
    ] == [
        ["single_agent", "manager_star"],
        ["manager_star", "single_agent"],
        ["single_agent", "manager_star"],
        ["manager_star", "single_agent"],
        ["single_agent", "manager_star"],
    ]


def test_pair_differs_only_in_registered_architecture_fields() -> None:
    pair = controls.build_pair_identities(
        _resolved_bindings_but_disabled_gates(), 3
    )

    assert controls.comparison_projection(pair[0]) == (
        controls.comparison_projection(pair[1])
    )
    assert pair[0]["shared_budget"] == pair[1]["shared_budget"]
    assert pair[0]["tool_contract"] == pair[1]["tool_contract"]
    assert pair[0]["task_suite"] == pair[1]["task_suite"]
    assert {item["answer_capture_profile"] for item in pair} == {
        "t3_item_results_v2"
    }

    drifted = copy.deepcopy(pair)
    drifted[1]["runtime"]["model_alias"] = "different-model"
    with pytest.raises(controls.ConfigError, match="outside"):
        controls.validate_pair(drifted)


def test_shared_two_attempt_and_immediate_item_contract_are_fixed() -> None:
    config = controls.load_config()
    tool = config["tool_contract"]
    outcome = config["task_suite"]

    assert config["shared_budget"]["max_attempts"] == 2
    assert outcome["primary_outcome"] == "correct_items_out_of_25"
    assert outcome["required_item_count"] == 25
    assert outcome["answer_capture_profile"] == "t3_item_results_v2"
    assert outcome["missing_items_reduce_correct_items_out_of_25"] is True
    assert (
        outcome["missing_reported_separately_from_submitted_incorrect"] is True
    )
    assert tool["nonempty_items_per_submission"] == 1
    assert tool["save_timing"] == "immediately_after_each_item_is_available"
    assert tool["write_mode"] == "atomic_per_item"
    assert tool["partial_calculation_status"] == "partial"
    assert tool["partial_retention"] == "explicit_completed_return_ids_only"
    assert tool["partial_continuation_reference"] == "stored_ref"
    assert tool["never_maps_results_to_item_ids"] is True


def test_config_rejects_answer_capture_profile_drift() -> None:
    config = copy.deepcopy(controls.load_config())
    config["task_suite"]["answer_capture_profile"] = "calculator_enabled"

    with pytest.raises(controls.ConfigError, match="violates const"):
        controls.validate_config(config)


def test_bound_public_v2_files_exist_and_publish_single_item_saves() -> None:
    config = controls.load_config()
    repository_root = controls.HERE.parents[2]
    suite = config["task_suite"]
    bound_paths = [
        suite["task_path"],
        suite["rubric_path"],
        suite["item_submission_schema_path"],
        suite["output_schema_path"],
        suite["scorer_result_schema_path"],
        suite["scorer_result_validator_path"],
        *(item["path"] for item in suite["public_inputs"]),
    ]

    assert all((repository_root / path).is_file() for path in bound_paths)
    item_schema = json.loads(
        (repository_root / suite["item_submission_schema_path"]).read_text(
            encoding="utf-8"
        )
    )
    assert item_schema["properties"]["schema_version"]["const"] == (
        "t3-quant-item-submission-v2"
    )
    assert item_schema["properties"]["items"]["maxItems"] == 1
    task = (repository_root / suite["task_path"]).read_text(encoding="utf-8")
    assert "successful or partial `quant_calculate` call" in task
    assert "exactly one item candidate" in task
    assert "written immediately by one atomic file replacement" in task


def test_resolved_public_hash_mismatch_keeps_preflight_unready() -> None:
    config = _resolved_bindings_but_disabled_gates()
    config["task_suite"]["scorer_result_schema_sha256"] = "d" * 64

    result = preflight_e3v9.preflight(config, replicate=1)

    assert (
        "public binding hash mismatch: "
        "task_suite.scorer_result_schema_sha256"
    ) in result["issues"]
    assert result["status"] == "unready"
    assert result["provider_call_count"] == 0
    assert result["external_calls_made"] is False
    assert result["observation_count"] == 0


def test_config_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    rendered = controls.CONFIG_PATH.read_text(encoding="utf-8")
    rendered = rendered.replace(
        '"experiment_id": "E3V9",',
        '"experiment_id": "E3V9",\n  "experiment_id": "E3V9",',
        1,
    )
    path = tmp_path / "duplicate-config.json"
    path.write_text(rendered, encoding="utf-8")

    with pytest.raises(controls.ConfigError, match="duplicate JSON key"):
        controls.load_config(path)


def test_m1_role_call_slices_equal_the_shared_per_attempt_call_cap() -> None:
    config = controls.load_config()
    role_calls = sum(
        config["manager_star_role_budget"][role]["max_calls"]
        for role in ("manager", "architect", "developer")
    )

    assert role_calls == config["shared_budget"]["max_model_calls_per_attempt"]


def test_resolved_bindings_still_cannot_cross_reviewed_gates() -> None:
    config = _resolved_bindings_but_disabled_gates()
    result = preflight_e3v9.preflight(config, replicate=2)

    assert result["status"] == "unready"
    assert result["observation_count"] == 0
    assert result["issues"] == [
        "this preparation version does not enable agreement generation",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "agreement"
        with pytest.raises(controls.NotReadyError):
            build_e3v9_agreement.build_agreement(config, output)
        assert not output.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config["shared_budget"].__setitem__("max_attempts", 3),
        lambda config: config["tool_contract"].__setitem__(
            "nonempty_items_per_submission", 2
        ),
        lambda config: config["execution"].__setitem__(
            "formal_model_execution_enabled", True
        ),
        lambda config: config["execution"].__setitem__(
            "agreement_generation_enabled", True
        ),
    ],
)
def test_preparation_schema_rejects_boundary_widening(mutation) -> None:
    config = copy.deepcopy(controls.load_config())
    mutation(config)
    with pytest.raises(controls.ConfigError):
        controls.validate_config(config)


def test_preparation_contains_no_formal_launcher_or_agreement_directory() -> None:
    assert not (controls.HERE / "run_e3v9_observation.py").exists()
    assert not (controls.HERE / "run_e3v9_pair.py").exists()
    assert not (controls.HERE / "agreement-terra-e3v9").exists()


def test_agreement_stage_never_requires_or_permits_model_execution(monkeypatch) -> None:
    config = _resolved_bindings_but_disabled_gates()
    config["execution"]["agreement_generation_enabled"] = True

    # The checked-in draft schema intentionally fixes the gate to false.  This
    # focused gate test bypasses only that schema lock to verify the prospective
    # stage rule that a later reviewed schema must retain.
    monkeypatch.setattr(controls, "validate_config", lambda _config: None)
    assert controls.agreement_build_issues(config) == []

    config["execution"]["formal_model_execution_enabled"] = True
    assert controls.agreement_build_issues(config) == [
        "formal model execution must remain disabled while building the agreement"
    ]
