from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from quant_calculator import (
    MAX_LITERAL_CELLS,
    MAX_VECTOR_LENGTH,
    QuantCalculationError,
    execute_quant_calculation,
    quant_calculator_enabled,
    quant_calculator_schema_paths,
    quant_calculator_writable_paths,
)


SOURCE = Path(__file__).resolve().parents[1] / "quant_calculator.py"


def _request(operations, return_ids):
    return {
        "schema_version": "quant-calculate-request-v2",
        "operations": operations,
        "return_ids": return_ids,
    }


def test_reference_graph_composes_weighted_returns_wealth_and_drawdown() -> None:
    response = execute_quant_calculation(
        _request(
            [
                {
                    "id": "daily",
                    "op": "matrix_vector_product",
                    "args": {
                        "matrix": [[0.10, 0.0], [-0.10, 0.20]],
                        "vector": [0.5, 0.5],
                    },
                },
                {
                    "id": "factors",
                    "op": "add",
                    "args": {"left": {"ref": "daily"}, "right": 1.0},
                },
                {
                    "id": "wealth",
                    "op": "cumulative_product",
                    "args": {"values": {"ref": "factors"}},
                },
                {
                    "id": "wealth_with_initial",
                    "op": "concat",
                    "args": {"items": [1.0, {"ref": "wealth"}]},
                },
                {
                    "id": "peaks",
                    "op": "running_max",
                    "args": {"values": {"ref": "wealth_with_initial"}},
                },
                {
                    "id": "ratio",
                    "op": "divide",
                    "args": {
                        "left": {"ref": "wealth_with_initial"},
                        "right": {"ref": "peaks"},
                    },
                },
                {
                    "id": "drawdown",
                    "op": "subtract",
                    "args": {"left": {"ref": "ratio"}, "right": 1.0},
                },
            ],
            ["daily", "wealth", "drawdown"],
        )
    )

    assert response["operation_count"] == 7
    assert response["results"]["daily"] == pytest.approx([0.05, 0.05])
    assert response["results"]["wealth"] == pytest.approx([1.05, 1.1025])
    assert response["results"]["drawdown"] == pytest.approx([0.0, 0.0, 0.0])


def test_explicit_stored_reference_continues_arithmetic_across_calls() -> None:
    first = execute_quant_calculation(
        _request(
            [
                {
                    "id": "series",
                    "op": "add",
                    "args": {"left": [1.0, 2.0, 3.0], "right": 1.0},
                }
            ],
            ["series"],
        )
    )
    second = execute_quant_calculation(
        _request(
            [
                {
                    "id": "total",
                    "op": "sum",
                    "args": {
                        "values": {
                            "stored_ref": {
                                "calculation_id": "calculation-1",
                                "result_id": "series",
                            }
                        }
                    },
                }
            ],
            ["total"],
        ),
        stored_calculations={"calculation-1": first["results"]},
    )

    assert second["results"]["total"] == pytest.approx(9.0)


def test_failed_later_operation_preserves_only_explicit_completed_return_ids() -> None:
    response = execute_quant_calculation(
        _request(
            [
                {
                    "id": "private_intermediate",
                    "op": "add",
                    "args": {"left": [1.0, 2.0, 3.0], "right": 1.0},
                },
                {
                    "id": "saved_series",
                    "op": "multiply",
                    "args": {
                        "left": {"ref": "private_intermediate"},
                        "right": 2.0,
                    },
                },
                {
                    "id": "broken_sum",
                    "op": "sum",
                    "args": {"values": []},
                },
            ],
            ["saved_series", "broken_sum"],
        )
    )

    assert response == {
        "schema_version": "quant-calculate-response-v1",
        "status": "partial",
        "operation_count": 3,
        "completed_operation_count": 2,
        "results": {"saved_series": pytest.approx([4.0, 6.0, 8.0])},
        "error": "nonempty_vector_required",
        "failed_operation_index": 2,
        "failed_operation_id": "broken_sum",
        "failed_operation": "sum",
    }
    assert "private_intermediate" not in response["results"]


def test_later_failure_without_an_explicit_completed_return_stays_failed() -> None:
    with pytest.raises(QuantCalculationError, match="nonempty_vector_required") as caught:
        execute_quant_calculation(
            _request(
                [
                    {
                        "id": "unselected_series",
                        "op": "add",
                        "args": {"left": [1.0, 2.0], "right": 1.0},
                    },
                    {
                        "id": "broken_sum",
                        "op": "sum",
                        "args": {"values": []},
                    },
                ],
                ["broken_sum"],
            )
        )

    assert caught.value.operation_index == 1
    assert caught.value.operation_id == "broken_sum"


def test_plain_ref_cannot_silently_cross_calls() -> None:
    with pytest.raises(QuantCalculationError, match="unknown_or_forward_reference"):
        execute_quant_calculation(
            _request(
                [
                    {
                        "id": "total",
                        "op": "sum",
                        "args": {"values": {"ref": "prior_series"}},
                    }
                ],
                ["total"],
            ),
            stored_calculations={
                "calculation-1": {"prior_series": [1.0, 2.0]}
            },
        )


def test_standard_deviation_requires_explicit_supported_ddof() -> None:
    sample = execute_quant_calculation(
        _request(
            [
                {
                    "id": "value",
                    "op": "standard_deviation",
                    "args": {"values": [1.0, 2.0, 3.0], "ddof": 1},
                }
            ],
            ["value"],
        )
    )
    assert sample["results"]["value"] == pytest.approx(1.0)

    with pytest.raises(QuantCalculationError, match="invalid_ddof"):
        execute_quant_calculation(
            _request(
                [
                    {
                        "id": "value",
                        "op": "standard_deviation",
                        "args": {"values": [1.0, 2.0], "ddof": 2},
                    }
                ],
                ["value"],
            )
        )


def test_rebalance_path_applies_return_before_end_of_day_cost_and_reset() -> None:
    response = execute_quant_calculation(
        _request(
            [
                {
                    "id": "ledger",
                    "op": "rebalance_path",
                    "args": {
                        "returns": [[0.10, 0.0], [0.0, 0.0]],
                        "target_weights": [0.5, 0.5],
                        "rebalance_indices": [0],
                        "initial_nav": 1.0,
                        "cost_bps": 10.0,
                    },
                }
            ],
            ["ledger"],
        )
    )

    first = response["results"]["ledger"][0]
    expected_pre_nav = 1.05
    expected_turnover = 0.5 * (
        abs(0.55 / 1.05 - 0.5) + abs(0.5 / 1.05 - 0.5)
    )
    expected_cost = expected_pre_nav * expected_turnover * 10 / 10_000
    assert first[0] == pytest.approx(expected_pre_nav)
    assert first[1] == pytest.approx(expected_turnover)
    assert first[2] == pytest.approx(expected_cost)
    assert first[3] == pytest.approx(expected_pre_nav - expected_cost)
    assert first[-2:] == pytest.approx([0.5, 0.5])
    assert response["results"]["ledger"][1][0] == pytest.approx(first[3])


def test_rebalance_path_rejects_invalid_weights_and_return_floor() -> None:
    base = {
        "returns": [[0.01, 0.02]],
        "target_weights": [0.5, 0.5],
        "rebalance_indices": [],
        "initial_nav": 1.0,
        "cost_bps": 5.0,
    }
    for changed, code in (
        ({**base, "target_weights": [0.6, 0.5]}, "invalid_target_weights"),
        ({**base, "returns": [[-1.0, 0.0]]}, "return_not_greater_than_minus_one"),
    ):
        with pytest.raises(QuantCalculationError, match=code):
            execute_quant_calculation(
                _request(
                    [{"id": "ledger", "op": "rebalance_path", "args": changed}],
                    ["ledger"],
                )
            )


@pytest.mark.parametrize(
    "indices,code",
    [
        ([[0]], "rebalance_indices_must_be_integers"),
        ([0, 0], "rebalance_indices_must_be_unique"),
        ([1], "rebalance_index_out_of_range"),
    ],
)
def test_rebalance_path_reports_the_exact_index_rule(indices, code) -> None:
    request = _request(
        [
            {
                "id": "ledger",
                "op": "rebalance_path",
                "args": {
                    "returns": [[0.01, 0.02]],
                    "target_weights": [0.5, 0.5],
                    "rebalance_indices": indices,
                    "initial_nav": 1.0,
                    "cost_bps": 5.0,
                },
            }
        ],
        ["ledger"],
    )

    with pytest.raises(QuantCalculationError) as caught:
        execute_quant_calculation(request)

    assert caught.value.code == code
    assert caught.value.argument_name == "rebalance_indices"
    assert caught.value.detail


def test_invalid_inline_operand_reports_the_argument_and_safe_rule() -> None:
    request = _request(
        [
            {
                "id": "drawdown",
                "op": "subtract",
                "args": {
                    "left": {"op": "running_max", "values": [1.0, 1.1]},
                    "right": 1.0,
                },
            }
        ],
        ["drawdown"],
    )

    with pytest.raises(QuantCalculationError) as caught:
        execute_quant_calculation(request)

    assert caught.value.code == "invalid_operand_object"
    assert caught.value.operation_index == 0
    assert caught.value.operation == "subtract"
    assert caught.value.argument_name == "left"
    assert "own earlier operation" in caught.value.detail


def test_filter_comparison_covariance_and_correlation_are_bounded_primitives() -> None:
    response = execute_quant_calculation(
        _request(
            [
                {
                    "id": "mask",
                    "op": "greater_than",
                    "args": {"left": [-1.0, 0.0, 2.0], "right": 0.0},
                },
                {
                    "id": "selected",
                    "op": "filter",
                    "args": {"values": [10.0, 20.0, 30.0], "mask": {"ref": "mask"}},
                },
                {
                    "id": "cov",
                    "op": "covariance",
                    "args": {"left": [1.0, 2.0, 3.0], "right": [2.0, 4.0, 6.0], "ddof": 1},
                },
                {
                    "id": "corr",
                    "op": "correlation",
                    "args": {"left": [1.0, 2.0, 3.0], "right": [2.0, 4.0, 6.0]},
                },
            ],
            ["selected", "cov", "corr"],
        )
    )
    assert response["results"]["selected"] == [30.0]
    assert response["results"]["cov"] == pytest.approx(2.0)
    assert response["results"]["corr"] == pytest.approx(1.0)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -float("inf")])
def test_non_numeric_and_nonfinite_inputs_fail_closed(value) -> None:
    with pytest.raises(QuantCalculationError):
        execute_quant_calculation(
            _request(
                [{"id": "answer", "op": "sum", "args": {"values": [1.0, value]}}],
                ["answer"],
            )
        )


def test_unknown_forward_and_duplicate_references_fail_closed() -> None:
    bad_requests = [
        _request(
            [{"id": "x", "op": "sum", "args": {"values": {"ref": "later"}}}],
            ["x"],
        ),
        _request(
            [
                {"id": "x", "op": "sum", "args": {"values": [1.0]}},
                {"id": "x", "op": "sum", "args": {"values": [2.0]}},
            ],
            ["x"],
        ),
    ]
    for request in bad_requests:
        with pytest.raises(QuantCalculationError):
            execute_quant_calculation(request)


def test_dimension_zero_division_and_overflow_fail_without_raw_values() -> None:
    bad = [
        _request(
            [{"id": "x", "op": "dot", "args": {"left": [1.0], "right": [1.0, 2.0]}}],
            ["x"],
        ),
        _request(
            [{"id": "x", "op": "divide", "args": {"left": 1.0, "right": 0.0}}],
            ["x"],
        ),
        _request(
            [{"id": "x", "op": "power", "args": {"left": 1e12, "right": 100.0}}],
            ["x"],
        ),
    ]
    for request in bad:
        with pytest.raises(QuantCalculationError) as raised:
            execute_quant_calculation(request)
        assert "1e+" not in str(raised.value)


def test_vector_and_total_literal_limits_are_enforced() -> None:
    with pytest.raises(QuantCalculationError, match="literal_vector_too_large"):
        execute_quant_calculation(
            _request(
                [
                    {
                        "id": "x",
                        "op": "sum",
                        "args": {"values": [1.0] * (MAX_VECTOR_LENGTH + 1)},
                    }
                ],
                ["x"],
            )
        )

    # Many legal vectors can still exceed the total request-cell boundary.
    operations = [
        {
            "id": f"x{index}",
            "op": "sum",
            "args": {"values": [1.0] * MAX_VECTOR_LENGTH},
        }
        for index in range(MAX_LITERAL_CELLS // MAX_VECTOR_LENGTH + 1)
    ]
    with pytest.raises(QuantCalculationError, match="too_many_literal_cells"):
        execute_quant_calculation(_request(operations, [operations[-1]["id"]]))


def test_task_profile_flag_is_explicit_boolean_only() -> None:
    assert quant_calculator_enabled({"execution_objectives": {}}) is False
    assert quant_calculator_enabled(
        {
            "execution_objectives": {
                "parsed_task_parameters": {"quant_calculator_enabled": True}
            }
        }
    ) is True
    with pytest.raises(QuantCalculationError, match="invalid_calculator_flag"):
        quant_calculator_enabled(
            {
                "execution_objectives": {
                    "parsed_task_parameters": {"quant_calculator_enabled": "true"}
                }
            }
        )


@pytest.mark.parametrize("malformed", [None, "", [], 0, False])
def test_task_profile_rejects_present_malformed_execution_objectives(
    malformed: object,
) -> None:
    with pytest.raises(QuantCalculationError, match="invalid_execution_objectives"):
        quant_calculator_enabled({"execution_objectives": malformed})


@pytest.mark.parametrize("malformed", [None, "", [], 0, False])
def test_task_profile_rejects_present_malformed_parameters(
    malformed: object,
) -> None:
    with pytest.raises(QuantCalculationError, match="invalid_task_parameters"):
        quant_calculator_enabled(
            {"execution_objectives": {"parsed_task_parameters": malformed}}
        )


def test_enabled_task_profile_derives_one_exact_writable_submission() -> None:
    payload = {
        "execution_objectives": {
            "parsed_task_parameters": {
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": "experiments/t3/output_schema.json",
            }
        },
        "repositories": [{"repo_full_name": "owner/repo"}],
        "strategy": {"target_path": "experiments/t3/submissions/result.json"},
    }

    assert quant_calculator_writable_paths(payload) == {
        "owner/repo": ["experiments/t3/submissions/result.json"]
    }
    assert quant_calculator_schema_paths(payload) == {
        "owner/repo": ["experiments/t3/output_schema.json"]
    }


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda p: p.update(repositories=[]), "calculator_requires_one_repository"),
        (
            lambda p: p.update(repositories=[{}, {}]),
            "calculator_requires_one_repository",
        ),
        (
            lambda p: p["strategy"].update(target_path="../escape.json"),
            "invalid_calculator_target_path",
        ),
    ],
)
def test_enabled_task_profile_write_scope_fails_closed(mutate, expected) -> None:
    payload = {
        "execution_objectives": {
            "parsed_task_parameters": {
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": "experiments/t3/output_schema.json",
            }
        },
        "repositories": [{"repo_full_name": "owner/repo"}],
        "strategy": {"target_path": "experiments/t3/submissions/result.json"},
    }
    mutate(payload)

    with pytest.raises(QuantCalculationError, match=expected):
        quant_calculator_writable_paths(payload)


@pytest.mark.parametrize(
    "schema_path",
    [None, "", "/absolute/schema.json", "../escape.json", "safe/../escape.json"],
)
def test_enabled_task_profile_schema_scope_fails_closed(schema_path: object) -> None:
    payload = {
        "execution_objectives": {
            "parsed_task_parameters": {
                "quant_calculator_enabled": True,
                "quant_calculator_schema_path": schema_path,
            }
        },
        "repositories": [{"repo_full_name": "owner/repo"}],
        "strategy": {"target_path": "experiments/t3/submissions/result.json"},
    }

    with pytest.raises(QuantCalculationError, match="invalid_calculator_schema_path"):
        quant_calculator_schema_paths(payload)


def test_source_contains_no_dynamic_code_or_external_io_surface() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    imported = set()
    called_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)

    assert imported.isdisjoint(
        {
            "asyncio",
            "builtins",
            "importlib",
            "os",
            "pathlib",
            "pickle",
            "requests",
            "socket",
            "subprocess",
            "urllib",
        }
    )
    assert called_names.isdisjoint({"compile", "eval", "exec", "open", "__import__"})
