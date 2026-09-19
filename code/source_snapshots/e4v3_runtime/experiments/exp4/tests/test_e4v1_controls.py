from __future__ import annotations

import copy
import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from experiments.exp4 import build_e4v1_agreement, identity, preflight_e4v1


def _resolved_config() -> dict:
    config = copy.deepcopy(identity.load_config())
    replacements = {
        "__UNRESOLVED_SOURCE_REF__": "exp4/e4v1-shared-source",
        "__UNRESOLVED_SOURCE_COMMIT_SHA__": "a" * 40,
        "__UNRESOLVED_CONTROL_REF__": "exp4/e4v1-controls",
        "__UNRESOLVED_CONTROL_COMMIT_SHA__": "d" * 40,
        "__UNRESOLVED_IMAGE_REF__": "exp4-shared-runtime",
        "__UNRESOLVED_IMAGE_DIGEST__": "sha256:" + "b" * 64,
        "__UNRESOLVED_T1_TASK_TEXT_PATH__": "experiments/shared/t3-quant-suite-v2/TASK.md",
        "__UNRESOLVED_T2_TASK_TEXT_PATH__": "experiments/shared/t3-quant-suite-v2/RUBRIC.md",
        "__UNRESOLVED_RETRIEVER_NAME__": "frozen_jira_memory",
        "__UNRESOLVED_RETRIEVER_VERSION__": "v1",
        "__UNRESOLVED_CORPUS_ID__": "e4v1-corpus",
        "__UNRESOLVED_INDEX_ID__": "e4v1-index",
        "__UNRESOLVED_EXCLUSION_LIST_ID__": "e4v1-exclusions",
        "__UNRESOLVED_RETRIEVAL_CUTOFF__": "2026-09-01T00:00:00+00:00",
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
    repository_root = identity.REPOSITORY_ROOT

    def file_hash(path: str) -> str:
        return hashlib.sha256((repository_root / path).read_bytes()).hexdigest()

    for task_id in ("T1", "T2"):
        task = config["task_bindings"][task_id]
        task["task_text_sha256"] = file_hash(task["task_text_path"])
    t3 = config["task_bindings"]["T3"]
    t3["task_text_sha256"] = file_hash(t3["task_text_path"])
    t3["rubric_sha256"] = file_hash(t3["rubric_path"])
    t3["item_submission_schema_sha256"] = file_hash(
        t3["item_submission_schema_path"]
    )
    t3["output_schema_sha256"] = file_hash(t3["output_schema_path"])
    result_contract = t3["result_contract"]
    result_contract["scorer_result_schema_sha256"] = file_hash(
        result_contract["scorer_result_schema_path"]
    )
    result_contract["scorer_result_validator_sha256"] = file_hash(
        result_contract["scorer_result_validator_path"]
    )
    for item in t3["public_inputs"]:
        item["sha256"] = file_hash(item["path"])
    config["runtime"]["item_submission_schema_sha256"] = t3[
        "item_submission_schema_sha256"
    ]
    for item in config["control"]["control_files"]:
        item["sha256"] = file_hash(item["path"])
    context_path = "experiments/exp4/tests/fixtures/retrieval_context.json"
    query_path = "experiments/exp4/PROTOCOL_DRAFT.md"
    context_bytes = (repository_root / context_path).read_bytes()
    for retrieval in config["retrieval_binding"]["task_query_context"].values():
        retrieval.update(
            query_path=query_path,
            query_sha256=file_hash(query_path),
            retrieval_context_path=context_path,
            retrieval_context_sha256=hashlib.sha256(context_bytes).hexdigest(),
            retrieval_context_byte_count=len(context_bytes),
            memory_count=1,
        )
    config["freeze_status"] = "ready_for_freeze"
    identity.validate_config(config)
    assert identity.readiness_issues(config) == [
        "this prospective version does not enable agreement generation"
    ]
    return config


def test_checked_in_config_is_explicitly_unready() -> None:
    config = identity.load_config()
    issues = identity.readiness_issues(config)
    assert config["freeze_status"] == "prospective_unready"
    assert any("source.source_commit" in issue for issue in issues)
    assert any("runtime.image_digest" in issue for issue in issues)
    assert any("runtime.runtime_response_schema_sha256" in issue for issue in issues)
    assert any("evaluator_bindings.T3" in issue for issue in issues)
    assert any("retrieval_binding.index_sha256" in issue for issue in issues)


def test_current_builder_refuses_before_creating_output() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "agreement"
        with pytest.raises(identity.NotReadyError):
            build_e4v1_agreement.build_agreement(identity.load_config(), output)
        assert not output.exists()


def test_current_preflight_is_zero_call_and_unready() -> None:
    result = preflight_e4v1.preflight(
        identity.load_config(), task_id="T3", replicate=1
    )
    assert result["status"] == "unready"
    assert result["formal_observation"] is False
    assert result["provider_call_count"] == 0
    assert result["external_calls_made"] is False
    assert result["observation_count"] == 0


def test_williams_order_balances_positions_and_transitions() -> None:
    config = identity.load_config()
    rows = [config["williams_order"][str(index)] for index in range(1, 5)]
    expected = set(identity.CONDITION_CODES)
    for position in range(4):
        assert {row[position] for row in rows} == expected
    transitions = {
        (left, right)
        for row in rows
        for left, right in zip(row, row[1:])
    }
    assert transitions == {
        (left, right)
        for left in identity.CONDITION_CODES
        for right in identity.CONDITION_CODES
        if left != right
    }


def test_schedule_contains_twelve_four_observation_blocks() -> None:
    schedule = identity.build_schedule(_resolved_config())
    assert len(schedule) == 12
    assert len({block["block_id"] for block in schedule}) == 12
    assert sum(len(block["observations"]) for block in schedule) == 48
    assert all(len(block["observations"]) == 4 for block in schedule)


def test_block_projection_differs_only_in_treatment_and_logistics() -> None:
    block = identity.build_block_identities(_resolved_config(), "T2", 3)
    projections = [identity.comparison_projection(item) for item in block]
    assert projections[1:] == [projections[0], projections[0], projections[0]]

    drifted = copy.deepcopy(block)
    drifted[2]["runtime"]["model_alias"] = "different-model"
    with pytest.raises(identity.ConfigError, match="outside allowed treatment"):
        identity.validate_block(drifted)

    contract_drift = copy.deepcopy(block)
    contract_drift[3]["runtime"]["rag_delivery_policy"] = "one_stage_only"
    with pytest.raises(identity.ConfigError, match="outside allowed treatment"):
        identity.validate_block(contract_drift)


def test_target_and_run_names_do_not_reveal_condition() -> None:
    block = identity.build_block_identities(_resolved_config(), "T1", 4)
    forbidden = ("M0R0", "M0R1", "M1R0", "M1R1", "single", "manager", "rag")
    for item in block:
        combined = f"{item['run_id']} {item['target_ref']}".casefold()
        assert all(token.casefold() not in combined for token in forbidden)
        assert item["target_ref"].startswith("quant/E4V1-T1-K4-P")


def test_resolved_preflight_still_refuses_without_reviewed_generation_gate() -> None:
    result = preflight_e4v1.preflight(
        _resolved_config(), task_id="T3", replicate=2
    )
    assert result["status"] == "unready"
    assert result["observation_count"] == 0
    assert result["issues"] == [
        "this prospective version does not enable agreement generation"
    ]
    assert result["provider_call_count"] == 0


def test_resolved_builder_still_refuses_and_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "agreement"
        with pytest.raises(identity.NotReadyError, match="does not enable"):
            build_e4v1_agreement.build_agreement(_resolved_config(), output)
        assert not output.exists()


def test_prospective_config_cannot_enable_model_execution() -> None:
    config = copy.deepcopy(identity.load_config())
    config["execution"]["formal_model_execution_enabled"] = True

    schema_errors = identity._schema_errors(config)
    assert any(
        "execution.formal_model_execution_enabled" in error
        and "violates const" in error
        for error in schema_errors
    )
    with pytest.raises(identity.ConfigError, match="violates const"):
        identity.validate_config(config)


def test_prospective_schema_cannot_enable_agreement_generation() -> None:
    config = copy.deepcopy(identity.load_config())
    config["execution"]["agreement_generation_enabled"] = True
    with pytest.raises(identity.ConfigError, match="violates const"):
        identity.validate_config(config)


def test_factorial_runtime_contract_and_two_attempt_budget_are_machine_fixed() -> None:
    config = identity.load_config()
    runtime = config["runtime"]
    budget = config["shared_budget"]

    assert runtime["formal_execution_contract"] == "factorial_rag_architecture_v1"
    assert runtime["rag_delivery_policy"] == "all_model_stages_v1"
    assert budget == {
        "max_attempts": 2,
        "max_model_calls_per_attempt": 20,
        "max_tokens_per_attempt": 350000,
        "max_model_calls_per_observation": 40,
        "max_tokens_per_observation": 700000,
        "attempt_timeout_seconds": 1800,
        "observation_timeout_seconds": 3600,
        "cpu_vcpus": 2,
        "memory_mb": 4096,
        "pids_limit": 256,
    }
    assert sum(
        config["manager_star_role_budget"][role]["max_calls"]
        for role in ("manager", "architect", "developer")
    ) == 20


def test_answer_capture_profile_is_explicit_only_for_t3() -> None:
    config = identity.load_config()
    assert config["task_bindings"]["T1"]["answer_capture_profile"] is None
    assert config["task_bindings"]["T2"]["answer_capture_profile"] is None
    assert config["task_bindings"]["T3"]["answer_capture_profile"] == (
        "t3_item_results_v2"
    )

    t1 = identity.build_run_identity(
        config,
        task_id="T1",
        replicate=1,
        position=1,
        condition_code="M0R0",
    )
    t3 = identity.build_run_identity(
        config,
        task_id="T3",
        replicate=1,
        position=1,
        condition_code="M0R0",
    )
    assert t1["answer_capture_profile"] is None
    assert t3["answer_capture_profile"] == "t3_item_results_v2"


def test_t3_answer_capture_profile_drift_fails_closed() -> None:
    config = copy.deepcopy(identity.load_config())
    config["task_bindings"]["T3"]["answer_capture_profile"] = (
        "calculator_enabled"
    )
    with pytest.raises(identity.ConfigError, match="violates const"):
        identity.validate_config(config)


def test_t3_suite_v2_binds_all_six_public_files_and_item_semantics() -> None:
    config = identity.load_config()
    t3 = config["task_bindings"]["T3"]
    bound_paths = [
        t3["task_text_path"],
        t3["rubric_path"],
        t3["item_submission_schema_path"],
        t3["output_schema_path"],
        *(item["path"] for item in t3["public_inputs"]),
    ]

    bound_paths.extend(
        [
            t3["result_contract"]["scorer_result_schema_path"],
            t3["result_contract"]["scorer_result_validator_path"],
        ]
    )
    assert len(bound_paths) == 8
    assert all((identity.REPOSITORY_ROOT / path).is_file() for path in bound_paths)
    assert t3["suite_version"] == "t3-quant-suite-v2"
    assert t3["answer_capture_profile"] == "t3_item_results_v2"
    outcome = t3["result_contract"]
    assert outcome["required_item_ids"] == list(range(1, 26))
    assert outcome["required_item_count"] == 25
    assert outcome["reported_item_states"] == [
        "accepted",
        "invalid_format",
        "explicit_abstain",
        "tool_failure",
        "not_attempted",
    ]
    assert outcome["saved_accepted_items_survive_runtime_failure"] is True
    assert outcome["scorer_result_schema_version"] == "t3-quant-scorer-result-v2"
    scorer_schema = json.loads(
        (
            identity.REPOSITORY_ROOT / outcome["scorer_result_schema_path"]
        ).read_text(encoding="utf-8")
    )
    assert outcome["reported_item_states"] == scorer_schema["$defs"]["itemResult"][
        "properties"
    ]["submission_state"]["enum"]


def test_condition_schema_rejects_any_nonregistered_cell_field() -> None:
    config = copy.deepcopy(identity.load_config())
    config["conditions"]["M1R1"]["extra_capacity"] = True

    with pytest.raises(identity.ConfigError, match="violates const"):
        identity.validate_config(config)


def test_config_loader_rejects_duplicate_json_keys() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "duplicate.json"
        path.write_text(
            '{"schema_version":"first","schema_version":"second"}',
            encoding="utf-8",
        )

        with pytest.raises(identity.ConfigError, match="duplicate JSON key"):
            identity.load_config(path)


def test_resolved_public_file_hash_mismatch_keeps_preflight_unready() -> None:
    config = _resolved_config()
    config["task_bindings"]["T3"]["rubric_sha256"] = "0" * 64

    issues = identity.readiness_issues(config)

    assert "bound local file hash mismatch: task_bindings.T3.rubric" in issues


def test_each_task_has_one_frozen_query_and_context_identity() -> None:
    config = identity.load_config()
    bindings = config["retrieval_binding"]["task_query_context"]

    assert set(bindings) == {"T1", "T2", "T3"}
    for task_id, binding in bindings.items():
        assert set(binding) == {
            "query_path",
            "query_sha256",
            "retrieval_context_path",
            "retrieval_context_sha256",
            "retrieval_context_byte_count",
            "memory_count",
        }
        assert binding["query_sha256"].startswith(f"__UNRESOLVED_{task_id}_")

    block = identity.build_block_identities(_resolved_config(), "T3", 1)
    rag_on = [item for item in block if item["rag_enabled"]]
    rag_off = [item for item in block if not item["rag_enabled"]]
    assert rag_on[0]["retrieval_binding"] == rag_on[1]["retrieval_binding"]
    assert all(item["retrieval_context_included"] is True for item in rag_on)
    assert all(item["retrieval_context_included"] is False for item in rag_off)
    assert all(item["retrieval_binding"]["task_id"] == "T3" for item in block)


def test_control_ref_commit_and_reviewed_files_are_bound() -> None:
    config = identity.load_config()
    control = config["control"]

    assert control["control_ref"] == "__UNRESOLVED_CONTROL_REF__"
    assert control["control_commit"] == "__UNRESOLVED_CONTROL_COMMIT_SHA__"
    assert tuple(item["path"] for item in control["control_files"]) == (
        identity.EXPECTED_CONTROL_FILES
    )
    assert all(item["sha256"].startswith("__UNRESOLVED_") for item in control["control_files"])


def test_retrieval_context_declared_size_and_memory_count_are_verified() -> None:
    config = _resolved_config()
    binding = config["retrieval_binding"]["task_query_context"]["T2"]
    binding["retrieval_context_byte_count"] += 1
    binding["memory_count"] = 2

    issues = identity.readiness_issues(config)

    assert (
        "retrieval context byte count mismatch: "
        "retrieval_binding.task_query_context.T2"
    ) in issues
    assert (
        "retrieval context memory count mismatch: "
        "retrieval_binding.task_query_context.T2"
    ) in issues
