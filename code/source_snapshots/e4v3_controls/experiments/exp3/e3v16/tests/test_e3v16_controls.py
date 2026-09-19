from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from experiments.exp3.e3v16 import build_e3v16_agreement, controls, preflight_e3v16


def _resolved_bindings_for_agreement() -> dict:
    config = copy.deepcopy(controls.load_config())
    replacements = {
        "__UNRESOLVED_E3V16_SOURCE_REF__": "exp/shared-t3-runtime-v6",
        "__UNRESOLVED_E3V16_SOURCE_COMMIT_SHA__": "a" * 40,
        "__UNRESOLVED_E3V16_IMAGE_REF__": "exp3-e3v16-runtime",
        "__UNRESOLVED_E3V16_IMAGE_DIGEST__": "sha256:" + "b" * 64,
        "__UNRESOLVED_E3V16_CONTROL_REF__": controls.EXPECTED_CONTROL_REF,
        "__UNRESOLVED_E3V16_CONTROL_COMMIT_SHA__": "d" * 40,
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
    control_path = "experiments/exp3/e3v16/controls.py"
    config["control"]["files_sha256"] = {
        control_path: hashlib.sha256(
            (controls.REPOSITORY_ROOT / control_path).read_bytes()
        ).hexdigest()
    }
    config["control"]["files_fingerprint_sha256"] = controls.sha256_value(
        config["control"]["files_sha256"]
    )
    config["freeze_status"] = "ready_for_freeze"
    config["execution"]["agreement_generation_enabled"] = True
    controls.validate_config(config)
    assert controls.binding_issues(config) == []
    assert controls.agreement_build_issues(config) == []
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
            build_e3v16_agreement.build_agreement(controls.load_config(), output)
        assert not output.exists()


def test_zero_model_preflight_reports_no_observation_or_external_call() -> None:
    result = preflight_e3v16.preflight(controls.load_config(), replicate=1)

    assert result["status"] == "unready"
    assert result["formal_observation"] is False
    assert result["provider_call_count"] == 0
    assert result["external_calls_made"] is False
    assert result["observation_count"] == 0
    assert result["run_ids"] == []


def test_e3v8_pair_count_and_alternating_arm_order_are_retained() -> None:
    config = _resolved_bindings_for_agreement()
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
        _resolved_bindings_for_agreement(), 3
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


def test_registered_change_is_only_the_host_status_reader() -> None:
    change = controls.REGISTERED_CHANGES_FROM_E3V15

    assert change["host_observation_result_status_path"] == {
        "from": "top_level_status",
        "to": "execution_summary.status",
    }
    assert change["host_observation_result_classification_changed"] is True
    assert change["runtime_source_changed"] is False
    assert change["runtime_image_changed"] is False
    assert change["model_visible_task_changed"] is False
    assert change["scoring_rule_changed"] is False


def test_shared_two_attempt_and_immediate_item_contract_are_fixed() -> None:
    config = controls.load_config()
    tool = config["tool_contract"]
    outcome = config["task_suite"]

    assert config["shared_budget"]["max_attempts"] == 2
    assert config["shared_budget"]["max_tokens_per_attempt"] == 500_000
    assert config["shared_budget"]["max_tokens_per_observation"] == 1_000_000
    assert all(
        config["manager_star_role_budget"][role]["max_tokens"] == 500_000
        for role in ("manager", "architect", "developer")
    )
    assert config["manager_star_role_budget"]["transfer_policy"] == (
        controls.EXPECTED_CALL_TRANSFER_POLICY
    )
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
    assert config["evaluation"]["item_store_projection_sha256"].startswith(
        "__UNRESOLVED_"
    )


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
    config = _resolved_bindings_for_agreement()
    config["task_suite"]["scorer_result_schema_sha256"] = "d" * 64

    result = preflight_e3v16.preflight(config, replicate=1)

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
        '"experiment_id": "E3V16",',
        '"experiment_id": "E3V16",\n  "experiment_id": "E3V16",',
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


def test_config_rejects_transfer_that_would_consume_manager_final_review() -> None:
    config = copy.deepcopy(controls.load_config())
    config["manager_star_role_budget"]["transfer_policy"][
        "source_role"
    ] = "manager"

    with pytest.raises(controls.ConfigError, match="violates const"):
        controls.validate_config(config)


def test_ready_config_rejects_a_different_control_branch() -> None:
    config = _resolved_bindings_for_agreement()
    config["control"]["control_ref"] = "exp/e3v16-freeze-controls"

    with pytest.raises(controls.ConfigError, match="dedicated freeze-control ref"):
        controls.validate_config(config)


def test_resolved_bindings_require_the_frozen_agreement_for_preflight() -> None:
    config = _resolved_bindings_for_agreement()
    result = preflight_e3v16.preflight(config, replicate=2)

    assert result["status"] == "unready"
    assert result["observation_count"] == 0
    assert result["issues"] == [
        "frozen agreement directory was not supplied",
        "public binding inventory was not supplied",
        "private evaluator hash manifest was not supplied",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        public_bindings = Path(tmp) / "public-bindings.json"
        private_manifest = Path(tmp) / "private-evaluator.json"
        public_bindings.write_text("public evidence\n", encoding="utf-8")
        private_manifest.write_text("private hash evidence\n", encoding="utf-8")
        config["control"]["public_binding_inventory_sha256"] = hashlib.sha256(
            public_bindings.read_bytes()
        ).hexdigest()
        config["evaluation"]["private_evaluator_manifest_sha256"] = hashlib.sha256(
            private_manifest.read_bytes()
        ).hexdigest()
        output = Path(tmp) / "agreement"
        agreement = build_e3v16_agreement.build_agreement(config, output)
        assert output.is_dir()
        ready = preflight_e3v16.preflight(
            config,
            replicate=2,
            agreement_dir=output,
            public_bindings=public_bindings,
            private_evaluator_manifest=private_manifest,
        )
        assert ready["status"] == "ready"
        assert ready["agreement_sha256"] == agreement["agreement_sha256"]
        assert ready["observation_count"] == 2


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


def test_launch_stage_keeps_agreement_external_and_requires_explicit_authorization() -> None:
    observation = (controls.HERE / "run_e3v16_observation.py").read_text(
        encoding="utf-8"
    )
    pair = (controls.HERE / "run_e3v16_pair.py").read_text(encoding="utf-8")

    assert 'f"{experiment_id}_RUN_AUTHORIZATION"' in observation
    assert 'f"{experiment_id}_PAIR_AUTHORIZATION"' in pair
    assert "--execute" in observation
    assert "--execute" in pair
    assert not (controls.HERE / "agreement-terra-e3v16").exists()


def test_agreement_stage_never_requires_or_permits_model_execution(monkeypatch) -> None:
    config = _resolved_bindings_for_agreement()

    # The checked-in draft schema intentionally fixes the gate to false.  This
    # focused gate test bypasses only that schema lock to verify the prospective
    # stage rule that a later reviewed schema must retain.
    monkeypatch.setattr(controls, "validate_config", lambda _config: None)
    assert controls.agreement_build_issues(config) == []

    config["execution"]["formal_model_execution_enabled"] = True
    assert controls.agreement_build_issues(config) == [
        "formal model execution must remain disabled while building the agreement"
    ]
