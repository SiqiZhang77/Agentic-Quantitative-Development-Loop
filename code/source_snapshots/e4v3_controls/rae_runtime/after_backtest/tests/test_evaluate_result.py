from evaluate_result import (
    apply_evaluation,
    evaluate_from_runtime_response,
    evaluate_result,
    extract_target_criteria,
    score_criteria,
)


def _request(target_criteria=None):
    execution_objectives = {"strategy_type": "backtest"}
    if target_criteria is not None:
        execution_objectives["target_criteria"] = target_criteria
    return {"execution_objectives": execution_objectives}


def _metrics(**overrides):
    base = {
        "total_return": 0.18,
        "sharpe_ratio": 1.42,
        "max_drawdown": -0.087,
        "alpha": 0.04,
        "beta": 0.91,
        "time_series_data_path": None,
    }
    base.update(overrides)
    return base


# --- extract_target_criteria -------------------------------------------------


def test_extract_target_criteria_returns_dict_when_present():
    request_payload = _request({"min_sharpe_ratio": 1.0})

    assert extract_target_criteria(request_payload) == {"min_sharpe_ratio": 1.0}


def test_extract_target_criteria_returns_none_when_omitted():
    request_payload = _request(None)

    assert extract_target_criteria(request_payload) is None


def test_extract_target_criteria_returns_none_when_null():
    request_payload = {"execution_objectives": {"target_criteria": None}}

    assert extract_target_criteria(request_payload) is None


# --- score_criteria ------------------------------------------------------------


def test_score_criteria_min_comparator_pass_and_fail():
    results, unrecognised = score_criteria(_metrics(sharpe_ratio=1.42), {"min_sharpe_ratio": 1.0})

    assert unrecognised == []
    assert results == [
        {"criterion": "min_sharpe_ratio", "metric": "sharpe_ratio", "threshold": 1.0, "actual": 1.42, "passed": True}
    ]


def test_score_criteria_max_drawdown_is_a_floor_not_a_ceiling():
    # max_drawdown is the worst acceptable (most negative) drawdown; -0.087 is
    # better (less negative) than the -0.15 floor, so it should pass.
    results, _ = score_criteria(_metrics(max_drawdown=-0.087), {"max_drawdown": -0.15})
    assert results[0]["passed"] is True

    results, _ = score_criteria(_metrics(max_drawdown=-0.21), {"max_drawdown": -0.15})
    assert results[0]["passed"] is False


def test_score_criteria_max_comparator():
    results, _ = score_criteria(_metrics(beta=0.91), {"max_beta": 1.0})
    assert results[0]["passed"] is True

    results, _ = score_criteria(_metrics(beta=1.2), {"max_beta": 1.0})
    assert results[0]["passed"] is False


def test_score_criteria_missing_metric_is_indeterminate():
    results, _ = score_criteria({}, {"min_sharpe_ratio": 1.0})

    assert results == [
        {"criterion": "min_sharpe_ratio", "metric": "sharpe_ratio", "threshold": 1.0, "actual": None, "passed": None}
    ]


def test_score_criteria_none_performance_metrics_is_indeterminate():
    results, _ = score_criteria(None, {"min_sharpe_ratio": 1.0})

    assert results[0]["actual"] is None
    assert results[0]["passed"] is None


def test_score_criteria_skips_null_thresholds():
    results, unrecognised = score_criteria(_metrics(), {"min_sharpe_ratio": None})

    assert results == []
    assert unrecognised == []


def test_score_criteria_reports_unrecognised_keys():
    results, unrecognised = score_criteria(_metrics(), {"min_sortino_ratio": 1.0})

    assert results == []
    assert unrecognised == ["min_sortino_ratio"]


# --- evaluate_result -----------------------------------------------------------


def test_evaluate_result_all_criteria_met():
    out = evaluate_result(
        performance_metrics=_metrics(sharpe_ratio=1.42, max_drawdown=-0.087),
        target_criteria={"min_sharpe_ratio": 1.0, "max_drawdown": -0.15},
        status="SUCCESS",
    )

    assert out["evaluation"]["met_criteria"] is True
    assert out["evaluation"]["confidence"] == 1.0
    assert out["recommended_action"] == "accept"


def test_evaluate_result_criterion_failed():
    out = evaluate_result(
        performance_metrics=_metrics(sharpe_ratio=0.71),
        target_criteria={"min_sharpe_ratio": 1.0},
        status="SUCCESS",
    )

    assert out["evaluation"]["met_criteria"] is False
    assert out["recommended_action"] == "iterate"


def test_evaluate_result_missing_metric_is_indeterminate_not_a_failure():
    out = evaluate_result(
        performance_metrics={},
        target_criteria={"min_sharpe_ratio": 1.0},
        status="SUCCESS",
    )

    assert out["evaluation"]["met_criteria"] is None
    assert out["evaluation"]["confidence"] == 0.0
    assert out["recommended_action"] == "review"


def test_evaluate_result_no_target_criteria_defaults_to_review():
    out = evaluate_result(performance_metrics=_metrics(), target_criteria=None, status="SUCCESS")

    assert out["evaluation"]["met_criteria"] is None
    assert out["evaluation"]["confidence"] == 0.0
    assert out["evaluation"]["criteria_results"] == []
    assert out["recommended_action"] == "review"


def test_evaluate_result_failed_status_escalates_without_scoring():
    out = evaluate_result(
        performance_metrics=None,
        target_criteria={"min_sharpe_ratio": 1.0},
        status="FAILED",
    )

    assert out["evaluation"]["met_criteria"] is None
    assert out["evaluation"]["criteria_results"] == []
    assert out["recommended_action"] == "escalate"


def test_evaluate_result_timeout_status_escalates():
    out = evaluate_result(performance_metrics=_metrics(), target_criteria=None, status="TIMEOUT")

    assert out["recommended_action"] == "escalate"


def test_evaluate_result_unrecognised_criteria_do_not_block_recognised_ones():
    out = evaluate_result(
        performance_metrics=_metrics(sharpe_ratio=1.42),
        target_criteria={"min_sharpe_ratio": 1.0, "min_sortino_ratio": 0.5},
        status="SUCCESS",
    )

    assert out["evaluation"]["met_criteria"] is True
    assert out["evaluation"]["unrecognised_criteria"] == ["min_sortino_ratio"]


# --- evaluate_from_runtime_response / apply_evaluation -------------------------


def test_evaluate_from_runtime_response_reads_status_and_metrics():
    request_payload = _request({"min_sharpe_ratio": 1.0})
    runtime_response = {
        "execution_summary": {"status": "SUCCESS"},
        "performance_metrics": _metrics(sharpe_ratio=1.42),
    }

    out = evaluate_from_runtime_response(request_payload, runtime_response)

    assert out["evaluation"]["met_criteria"] is True
    assert out["recommended_action"] == "accept"


def test_apply_evaluation_merges_into_a_copy_of_the_response():
    request_payload = _request({"min_sharpe_ratio": 1.0})
    runtime_response = {
        "schema_version": "1.0",
        "run_id": "run-001",
        "execution_summary": {"status": "SUCCESS"},
        "performance_metrics": _metrics(sharpe_ratio=1.42),
    }

    merged = apply_evaluation(request_payload, runtime_response)

    assert merged["run_id"] == "run-001"
    assert merged["evaluation"]["met_criteria"] is True
    assert merged["recommended_action"] == "accept"
    assert "evaluation" not in runtime_response
