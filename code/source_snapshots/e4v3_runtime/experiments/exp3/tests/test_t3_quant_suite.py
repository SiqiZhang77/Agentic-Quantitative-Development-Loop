from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_ROOT = REPO_ROOT.parents[1]
CONTROL = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "protocols"
    / "amendments"
    / "t3-quant-suite-v1"
)
Q6_CONTROL = CONTROL.parent / "t3-quant-suite-q6"
PUBLIC = REPO_ROOT / "experiments" / "shared" / "t3-quant-suite-v1"


def _rows() -> list[dict[str, str]]:
    with (PUBLIC / "input" / "synthetic_portfolio_returns_v1.csv").open(
        encoding="utf-8",
        newline="",
    ) as handle:
        return list(csv.DictReader(handle))


def test_control_and_agent_visible_public_bytes_are_identical() -> None:
    pairs = [
        (Q6_CONTROL / "T3_PUBLIC_TASK_Q6_V1.txt", PUBLIC / "TASK.md"),
        (CONTROL / "T3_PUBLIC_RUBRIC_V1.md", PUBLIC / "RUBRIC.md"),
        (CONTROL / "output_schema_v1.json", PUBLIC / "output_schema_v1.json"),
        (
            CONTROL / "synthetic_portfolio_returns_v1.csv",
            PUBLIC / "input" / "synthetic_portfolio_returns_v1.csv",
        ),
        (
            CONTROL / "portfolio_config_v1.json",
            PUBLIC / "input" / "portfolio_config_v1.json",
        ),
    ]

    for canonical, source_copy in pairs:
        assert canonical.read_bytes() == source_copy.read_bytes()


def test_public_dataset_is_120_ordered_finite_synthetic_rows() -> None:
    rows = _rows()
    assert len(rows) == 120
    assert list(rows[0]) == [
        "date",
        "asset_a_return",
        "asset_b_return",
        "asset_c_return",
    ]
    dates = [date.fromisoformat(row["date"]) for row in rows]
    assert dates == sorted(dates)
    assert len(set(dates)) == 120
    values = [
        float(row[column])
        for row in rows
        for column in ("asset_a_return", "asset_b_return", "asset_c_return")
    ]
    assert all(math.isfinite(value) and value > -1 for value in values)
    assert min(values) < -0.02
    assert max(values) > 0.02
    assert all("sanctum" not in value.casefold() for row in rows for value in row.values())


def test_config_weights_dates_and_output_schema_are_deterministic() -> None:
    config = json.loads((PUBLIC / "input" / "portfolio_config_v1.json").read_text())
    assert config["observation_count"] == 120
    assert config["annualization_periods"] == 252
    assert config["initial_wealth"] == 1.0
    assert sum(config["target_weights"].values()) == 1.0
    input_dates = {row["date"] for row in _rows()}
    assert len(config["rebalance_dates"]) == 6
    assert config["rebalance_dates"] == sorted(config["rebalance_dates"])
    assert set(config["rebalance_dates"]).issubset(input_dates)
    assert config["transaction_cost_bps"] == 8.0

    schema = json.loads((PUBLIC / "output_schema_v1.json").read_text())
    Draft202012Validator.check_schema(schema)
    assert schema["properties"]["observation_count"]["const"] == 120
    assert schema["properties"]["level_a"]["properties"]["rows"]["minItems"] == 120
    assert (
        schema["properties"]["level_c"]["properties"]["rebalance_events"]["maxItems"]
        == 6
    )


def test_public_prompt_discloses_conventions_but_not_complete_oracle_formulas() -> None:
    task = (PUBLIC / "TASK.md").read_text(encoding="utf-8")
    for required in (
        "quant_calculate",
        "at most four",
        "ddof=1",
        "252",
        "Apply returns before",
        "earliest input date",
        "Do not round intermediate",
    ):
        assert required in task
    for evaluator_only_formula in (
        "product(1 +",
        "portfolio_wealth[-1] **",
        "portfolio_wealth[t] / running_peak[t]",
    ):
        assert evaluator_only_formula not in task
    evaluator_contract = (CONTROL / "T3_EVALUATOR_CONTRACT_V1.md").read_text(
        encoding="utf-8"
    )
    assert "product(1 + asset_return" in evaluator_contract
    assert "must not be copied into the agent prompt" in evaluator_contract


def test_task_identity_enables_only_the_versioned_calculator_profile() -> None:
    identity = json.loads((CONTROL / "T3_TASK_IDENTITY_V1.json").read_text())
    assert identity["status"] == "local_candidate_not_agreed"
    assert identity["runtime"]["parsed_task_parameters"] == {
        "quant_calculator_enabled": True
    }
    assert identity["runtime"]["allowed_directories"] == [
        "experiments/shared/t3-quant-suite-v1"
    ]
    assert identity["tool_profile"] == {
        "name": "quant_calculate_v1",
        "max_attempted_calls": 4,
        "developer_only": True,
        "counts_toward_model_call_cap": False,
    }


def test_public_data_exercises_drawdown_turnover_and_cost_without_storing_answers() -> None:
    from quant_calculator import execute_quant_calculation

    rows = _rows()
    config = json.loads((PUBLIC / "input" / "portfolio_config_v1.json").read_text())
    returns = [
        [
            float(row["asset_a_return"]),
            float(row["asset_b_return"]),
            float(row["asset_c_return"]),
        ]
        for row in rows
    ]
    dates = [row["date"] for row in rows]
    indices = [dates.index(value) for value in config["rebalance_dates"]]
    response = execute_quant_calculation(
        {
            "schema_version": "quant-calculate-request-v2",
            "operations": [
                {
                    "id": "ledger",
                    "op": "rebalance_path",
                    "args": {
                        "returns": returns,
                        "target_weights": list(config["target_weights"].values()),
                        "rebalance_indices": indices,
                        "initial_nav": config["initial_wealth"],
                        "cost_bps": config["transaction_cost_bps"],
                    },
                },
                {
                    "id": "post_nav",
                    "op": "column",
                    "args": {"matrix": {"ref": "ledger"}, "index": 3},
                },
                {
                    "id": "with_initial",
                    "op": "concat",
                    "args": {
                        "items": [config["initial_wealth"], {"ref": "post_nav"}]
                    },
                },
                {
                    "id": "peaks",
                    "op": "running_max",
                    "args": {"values": {"ref": "with_initial"}},
                },
                {
                    "id": "drawdown",
                    "op": "divide",
                    "args": {
                        "left": {"ref": "with_initial"},
                        "right": {"ref": "peaks"},
                    },
                },
                {
                    "id": "drawdown_pct",
                    "op": "subtract",
                    "args": {"left": {"ref": "drawdown"}, "right": 1.0},
                },
                {
                    "id": "minimum_drawdown",
                    "op": "minimum",
                    "args": {"values": {"ref": "drawdown_pct"}},
                },
            ],
            "return_ids": ["ledger", "minimum_drawdown"],
        }
    )
    ledger = response["results"]["ledger"]
    assert all(row[0] > 0 and row[3] > 0 for row in ledger)
    assert sum(ledger[index][1] for index in indices) > 0
    assert sum(ledger[index][2] for index in indices) > 0
    assert response["results"]["minimum_drawdown"] < 0
