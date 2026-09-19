import json
import math

import pytest

from dataset_analysis import DatasetAnalysisError, analyse_dataset_task


def _payload(dataset, *, text="Analyse the complete dataset"):
    return {
        "issue_key": "SCRUM-83",
        "command": "analysis",
        "execution_objectives": {
            "strategy_type": "analysis",
            "zero_code_modifications": True,
        },
        "jira_context": {
            "ticket_id": "SCRUM-83",
            "summary": "Dataset analysis",
            "description": text,
            "triggering_comment": {"timestamp": "t", "author": "user", "text": text},
            "events_history": [],
        },
        "input_datasets": [
            {
                "dataset_id": "experiment_canary",
                "source_kind": "jira_attachment",
                "original_filename": dataset.name,
                "source_uri": "jira-attachment://11347",
                "container_path": str(dataset),
                "format": "csv",
                "read_only": True,
                "size_bytes": dataset.stat().st_size,
                "sha256": "a" * 64,
            }
        ],
    }


def _llm_replies(*items):
    replies = iter(items)

    def call(prompt):
        return next(replies)

    return call


def test_canary_profile_is_calculated_from_complete_csv(tmp_path):
    dataset = tmp_path / "experiment_canary.csv"
    dataset.write_text(
        "Date,ZQQ_Return,Benchmark_Return,Risk_Free_Rate,Peer_A_Return,Peer_B_Return,Regime\n"
        "2022-01-01,0.01,0.008,0.0015,0.009,0.007,calm\n"
        "2022-02-01,-0.02,-0.015,0.0016,-0.018,-0.012,crisis\n"
        "2022-03-01,0.03,0.025,0.0017,0.028,,recovery\n",
        encoding="utf-8",
    )
    plan = {
        "operations": [
            {"type": "row_count", "dataset_id": "experiment_canary"},
            {"type": "column_names", "dataset_id": "experiment_canary"},
            {"type": "date_range", "dataset_id": "experiment_canary", "column": "Date"},
            {"type": "missing_counts", "dataset_id": "experiment_canary"},
            {"type": "describe_numeric", "dataset_id": "experiment_canary"},
        ]
    }
    out = analyse_dataset_task(
        _payload(dataset),
        llm=_llm_replies(
            json.dumps(plan),
            '{"summary":"The dataset has three rows; Peer_B_Return has one missing value."}',
        ),
    )

    results = out["analysis_results"]["operations"]
    assert results[0]["result"]["row_count"] == 3
    assert results[1]["result"]["column_names"][0] == "Date"
    assert results[2]["result"] == {
        "column": "Date",
        "start": "2022-01-01",
        "end": "2022-03-01",
        "count": 3,
        "missing_count": 0,
    }
    assert results[3]["result"]["missing_counts"]["Peer_A_Return"] == 0
    assert results[3]["result"]["missing_counts"]["Peer_B_Return"] == 1
    assert results[4]["result"]["columns"]["Peer_B_Return"]["count"] == 2
    assert "Measured read-only dataset analysis" in out["evaluation"]["summary"]
    assert out["recommended_action"] == "review"


def test_financial_operations_are_measured_deterministically(tmp_path):
    dataset = tmp_path / "experiment_canary.csv"
    dataset.write_text(
        "Date,Fund,Benchmark,RF,Regime\n"
        "2024-01-31,0.02,0.01,0.001,calm\n"
        "2024-02-29,-0.01,-0.02,0.001,crisis\n"
        "2024-03-31,0.03,0.025,0.001,recovery\n"
        "2024-04-30,-0.02,-0.01,0.001,crisis\n"
        "2024-05-31,0.01,0.015,0.001,calm\n",
        encoding="utf-8",
    )
    plan = {
        "operations": [
            {"type": "annualized_return", "dataset_id": "experiment_canary", "column": "Fund"},
            {"type": "annualized_volatility", "dataset_id": "experiment_canary", "column": "Fund"},
            {"type": "sharpe_ratio", "dataset_id": "experiment_canary", "return_column": "Fund", "risk_free_column": "RF"},
            {"type": "sortino_ratio", "dataset_id": "experiment_canary", "return_column": "Fund", "risk_free_column": "RF"},
            {"type": "max_drawdown", "dataset_id": "experiment_canary", "return_column": "Fund"},
            {"type": "beta", "dataset_id": "experiment_canary", "return_column": "Fund", "benchmark_column": "Benchmark"},
            {"type": "capture_ratio", "dataset_id": "experiment_canary", "return_column": "Fund", "benchmark_column": "Benchmark", "market": "up"},
            {"type": "group_summary", "dataset_id": "experiment_canary", "group_column": "Regime", "value_columns": ["Fund", "Benchmark"]},
            {"type": "henriksson_merton", "dataset_id": "experiment_canary", "return_column": "Fund", "benchmark_column": "Benchmark", "risk_free_column": "RF"},
        ]
    }
    out = analyse_dataset_task(
        _payload(dataset),
        llm=_llm_replies(json.dumps(plan), '{"summary":"Measured financial metrics were calculated."}'),
    )
    operations = {item["operation"]: item["result"] for item in out["analysis_results"]["operations"]}

    assert operations["annualized_return"]["observations"] == 5
    assert operations["annualized_volatility"]["annualized_volatility"] > 0
    assert math.isfinite(operations["sharpe_ratio"]["sharpe_ratio"])
    assert math.isfinite(operations["sortino_ratio"]["sortino_ratio"])
    assert operations["max_drawdown"]["max_drawdown"] < 0
    assert operations["beta"]["observations"] == 5
    assert operations["capture_ratio"]["market"] == "up"
    assert set(operations["group_summary"]["groups"]) == {"calm", "crisis", "recovery"}
    assert math.isfinite(operations["henriksson_merton"]["gamma"])


def test_unknown_operation_is_rejected_before_execution(tmp_path):
    dataset = tmp_path / "experiment_canary.csv"
    dataset.write_text("Date,Return\n2024-01-31,0.01\n", encoding="utf-8")

    with pytest.raises(DatasetAnalysisError, match="unsupported analysis operation"):
        analyse_dataset_task(
            _payload(dataset),
            llm=_llm_replies('{"operations":[{"type":"execute_python","dataset_id":"experiment_canary"}]}'),
        )


def test_non_csv_dataset_is_rejected_without_falling_back_to_preview(tmp_path):
    dataset = tmp_path / "input.json"
    dataset.write_text('{"value":1}', encoding="utf-8")
    payload = _payload(dataset)
    payload["input_datasets"][0]["format"] = "json"

    with pytest.raises(DatasetAnalysisError, match="currently supports CSV"):
        analyse_dataset_task(payload, llm=lambda prompt: pytest.fail("LLM should not run"))
