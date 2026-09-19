from __future__ import annotations

import pytest

from exp3.attempt_control import (
    AttemptControlError,
    attempt_record,
    audit_filename,
    deterministic_evaluation,
    manager_star_commit_evidence,
    pipeline_commit_evidence,
    validate_two_attempt_controls,
    with_attempt_context,
)


def _payload() -> dict:
    return {
        "architecture_mode": "single_agent",
        "strategy": {"target_path": "out/result.json"},
        "iteration_controls": {
            "allow_iteration": True,
            "max_iterations": 2,
            "max_failed_iterations": 2,
            "max_agent_turns": 20,
            "max_token_budget_per_run": 700_000,
        },
    }


def _t3_payload(tmp_path) -> dict:
    payload = _payload()
    payload["execution_objectives"] = {
        "parsed_task_parameters": {
            "quant_calculator_enabled": True,
            "answer_capture_profile": "t3_item_results_v2",
        }
    }
    payload["output_paths"] = {"artifact_dir": str(tmp_path)}
    return payload


def _item_store(count: int) -> dict:
    return {
        "candidate_count": count,
        "candidates": {str(item_id): {} for item_id in range(1, count + 1)},
        "candidates_sha256": "a" * 64,
    }


def test_two_attempt_contract_is_exact_and_attempt_context_is_content_free():
    payload = _payload()
    validate_two_attempt_controls(payload)
    second = with_attempt_context(payload, 2, prior_status="required_commit_missing")

    assert second["_experiment_attempt"]["number"] == 2
    assert "answer correctness information is available" in second[
        "_experiment_attempt"
    ]["feedback"]
    assert "answer" not in payload
    assert audit_filename(second, "mcp_tool_audit.jsonl") == (
        "mcp_tool_audit_attempt_2.jsonl"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("allow_iteration", False),
        ("max_iterations", 1),
        ("max_failed_iterations", 1),
        ("max_agent_turns", 21),
        ("max_token_budget_per_run", 700_001),
    ],
)
def test_two_attempt_contract_rejects_any_budget_drift(field, value):
    payload = _payload()
    payload["iteration_controls"][field] = value
    with pytest.raises(AttemptControlError, match=field):
        validate_two_attempt_controls(payload)


def test_both_architectures_use_the_same_audited_target_commit_rule():
    payload = _payload()
    pipeline = {
        "experiment3_provider_accounting": {"commit_count": 1},
        "diagnostics": {
            "retry_safe": {"commit": {"paths": ["out/result.json"]}}
        },
        "artifacts": {"validation_issues": []},
    }
    manager = {"commit_count": 1, "committed_paths": ["out/result.json"]}

    m0 = pipeline_commit_evidence(payload, pipeline)
    m1 = manager_star_commit_evidence(payload, manager)

    assert m0["complete"] is True
    assert m1["complete"] is True
    assert deterministic_evaluation(m0)["recommended_action"] == "accept"
    assert deterministic_evaluation(m1)["recommended_action"] == "accept"


def test_calculator_availability_does_not_select_t3_answer_capture(tmp_path):
    payload = _payload()
    payload["execution_objectives"] = {
        "parsed_task_parameters": {"quant_calculator_enabled": True}
    }
    payload["output_paths"] = {"artifact_dir": str(tmp_path)}

    evidence = manager_star_commit_evidence(
        payload,
        {"commit_count": 1, "committed_paths": ["out/result.json"]},
    )

    assert evidence["completion_basis"] == "audited_target_commit"
    assert evidence["required_t3_items"] == 0
    assert evidence["complete"] is True


def test_t3_answer_capture_profile_is_explicit_and_fail_closed(tmp_path):
    payload = _payload()
    payload["execution_objectives"] = {
        "parsed_task_parameters": {
            "quant_calculator_enabled": False,
            "answer_capture_profile": "t3_item_results_v2",
        }
    }
    payload["output_paths"] = {"artifact_dir": str(tmp_path)}

    assert manager_star_commit_evidence(
        payload, {"commit_count": 0, "committed_paths": []}
    )["completion_basis"] == "t3_items_missing"

    payload["execution_objectives"]["parsed_task_parameters"][
        "answer_capture_profile"
    ] = "unknown_profile"
    with pytest.raises(AttemptControlError, match="answer_capture_profile"):
        manager_star_commit_evidence(
            payload, {"commit_count": 0, "committed_paths": []}
        )


def test_model_claim_without_audited_commit_requires_second_attempt():
    payload = _payload()
    pipeline = {
        "summary": "the model says it finished",
        "experiment3_provider_accounting": {"commit_count": 0},
        "diagnostics": {
            "retry_safe": {"commit": {"paths": ["out/result.json"]}}
        },
    }

    evidence = pipeline_commit_evidence(payload, pipeline)

    assert evidence["complete"] is False
    assert deterministic_evaluation(evidence)["recommended_action"] == "iterate"


def test_all_25_verified_t3_items_end_attempt_without_answer_scoring(
    tmp_path, monkeypatch
):
    payload = _t3_payload(tmp_path)
    pipeline = {
        "experiment3_provider_accounting": {"commit_count": 0},
        "diagnostics": {"retry_safe": {"commit": {"paths": []}}},
    }
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: _item_store(25),
    )

    m0 = pipeline_commit_evidence(payload, pipeline)
    m1 = manager_star_commit_evidence(
        payload, {"commit_count": 0, "committed_paths": []}
    )

    assert m0["completion_basis"] == "all_t3_items_saved"
    assert m1["completion_basis"] == "all_t3_items_saved"
    assert m0["complete"] is True
    assert m1["complete"] is True
    assert deterministic_evaluation(m0)["recommended_action"] == "accept"
    assert "correctness was not inspected" in deterministic_evaluation(m1)[
        "evaluation"
    ]["summary"]


def test_partial_t3_item_store_allows_the_second_attempt(tmp_path, monkeypatch):
    payload = _t3_payload(tmp_path)
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: _item_store(24),
    )

    evidence = manager_star_commit_evidence(
        payload, {"commit_count": 0, "committed_paths": []}
    )

    assert evidence["saved_t3_items"] == 24
    assert evidence["saved_t3_item_ids"] == list(range(1, 25))
    assert evidence["missing_t3_item_ids"] == [25]
    assert evidence["complete"] is False
    assert deterministic_evaluation(evidence)["recommended_action"] == "iterate"

    second = with_attempt_context(
        payload,
        2,
        prior_status="required_items_missing",
    )
    assert second["_experiment_attempt"]["prior_status"] == (
        "required_items_missing"
    )
    assert "item IDs [25] are still missing" in second["_experiment_attempt"][
        "feedback"
    ]


def test_t3_commit_cannot_substitute_for_missing_primary_items(
    tmp_path, monkeypatch
):
    payload = _t3_payload(tmp_path)
    monkeypatch.setattr(
        "exp3.attempt_control.load_item_store",
        lambda _path: _item_store(0),
    )

    m0 = pipeline_commit_evidence(
        payload,
        {
            "experiment3_provider_accounting": {"commit_count": 1},
            "diagnostics": {
                "retry_safe": {"commit": {"paths": ["out/result.json"]}}
            },
            "artifacts": {"validation_issues": []},
        },
    )
    m1 = manager_star_commit_evidence(
        payload,
        {"commit_count": 1, "committed_paths": ["out/result.json"]},
    )

    for evidence in (m0, m1):
        assert evidence["commit_count"] == 1
        assert evidence["completion_basis"] == "t3_items_missing"
        assert evidence["complete"] is False
        evaluation = deterministic_evaluation(evidence)
        assert evaluation["recommended_action"] == "iterate"
        assert "repository artifact does not replace" in evaluation["evaluation"][
            "summary"
        ].lower()
        record = attempt_record(
            number=1,
            runtime_status="succeeded",
            provider_call_count=1,
            evidence=evidence,
        )
        assert record["completion"] == "required_items_missing"


def test_attempt_record_separates_saved_answers_commit_and_runtime_exit():
    record = attempt_record(
        number=1,
        runtime_status="failed",
        provider_call_count=7,
        evidence={
            "complete": True,
            "completion_basis": "all_t3_items_saved",
            "saved_t3_items": 25,
            "required_t3_items": 25,
            "commit_count": 0,
            "committed_paths": [],
            "target_path": "out/result.json",
        },
        failure_type="BudgetExceeded",
    )

    assert record["completion"] == "complete"
    assert record["answer_capture_status"] == "complete"
    assert record["artifact_delivery_status"] == "not_committed"
    assert record["runtime_exit_status"] == "error"
    assert record["failure_type"] == "BudgetExceeded"
