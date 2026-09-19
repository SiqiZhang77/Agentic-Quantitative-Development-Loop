from __future__ import annotations

from copy import deepcopy

from jsonschema import Draft202012Validator
import pytest

import github_mcp_server as server
from quant_calculator import (
    OPERATION_ARGUMENT_KINDS,
    OPTIONAL_OPERATION_ARGUMENTS,
    SUPPORTED_OPERATIONS,
    QuantCalculationError,
    execute_quant_calculation,
)


def _request(operation: str = "sum", args: dict | None = None) -> dict:
    return {
        "schema_version": "quant-calculate-request-v2",
        "operations": [
            {
                "id": "answer",
                "op": operation,
                "args": args if args is not None else {"values": [1.0, 2.0]},
            }
        ],
        "return_ids": ["answer"],
    }


def _operation_variants(schema: dict) -> dict[str, dict]:
    variants = schema["properties"]["operations"]["items"]["anyOf"]
    by_operation = {}
    for variant in variants:
        operation_schema = variant["properties"]["op"]
        operations = (
            [operation_schema["const"]]
            if "const" in operation_schema
            else operation_schema["enum"]
        )
        for operation in operations:
            by_operation[operation] = variant
    return by_operation


def test_model_facing_schema_is_valid_and_matches_backend_argument_contract() -> None:
    schema = server._quant_calculate_request_json_schema()
    Draft202012Validator.check_schema(schema)

    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["schema_version", "operations", "return_ids"]
    assert set(schema["properties"]) == {
        "schema_version",
        "operations",
        "return_ids",
    }
    assert schema["properties"]["schema_version"] == {
        "type": "string",
        "const": "quant-calculate-request-v2",
    }

    variants = _operation_variants(schema)
    assert set(variants) == set(SUPPORTED_OPERATIONS)
    for operation, argument_kinds in OPERATION_ARGUMENT_KINDS.items():
        variant = variants[operation]
        assert variant["additionalProperties"] is False
        assert variant["required"] == ["id", "op", "args"]
        args_schema = variant["properties"]["args"]
        assert args_schema["additionalProperties"] is False
        assert set(args_schema["properties"]) == set(argument_kinds)
        assert set(args_schema["required"]) == set(argument_kinds) - set(
            OPTIONAL_OPERATION_ARGUMENTS.get(operation, frozenset())
        )


VALID_ARGUMENTS = {
    "add": {"left": [1.0, 2.0], "right": 1.0},
    "subtract": {"left": [1.0, 2.0], "right": 1.0},
    "multiply": {"left": [1.0, 2.0], "right": 2.0},
    "divide": {"left": [1.0, 2.0], "right": 2.0},
    "power": {"left": [1.0, 2.0], "right": 2.0},
    "absolute": {"value": [-1.0, 2.0]},
    "negate": {"value": [-1.0, 2.0]},
    "sqrt": {"value": [1.0, 4.0]},
    "sum": {"values": [1.0, 2.0]},
    "product": {"values": [1.0, 2.0]},
    "mean": {"values": [1.0, 2.0]},
    "minimum": {"values": [1.0, 2.0]},
    "maximum": {"values": [1.0, 2.0]},
    "argmin": {"values": [1.0, 2.0]},
    "argmax": {"values": [1.0, 2.0]},
    "length": {"values": [1.0, 2.0]},
    "first": {"values": [1.0, 2.0]},
    "last": {"values": [1.0, 2.0]},
    "standard_deviation": {"values": [1.0, 2.0, 3.0], "ddof": 1},
    "variance": {"values": [1.0, 2.0, 3.0], "ddof": 0},
    "covariance": {"left": [1.0, 2.0], "right": [2.0, 4.0]},
    "correlation": {"left": [1.0, 2.0, 3.0], "right": [2.0, 4.0, 7.0]},
    "dot": {"left": [1.0, 2.0], "right": [2.0, 4.0]},
    "matrix_vector_product": {
        "matrix": [[1.0, 2.0], [3.0, 4.0]],
        "vector": [1.0, 1.0],
    },
    "column": {"matrix": [[1.0, 2.0], [3.0, 4.0]], "index": 0},
    "cumulative_product": {"values": [1.0, 2.0]},
    "cumulative_sum": {"values": [1.0, 2.0]},
    "running_max": {"values": [1.0, 2.0]},
    "concat": {"items": [1.0, [2.0, 3.0]]},
    "slice": {"values": [1.0, 2.0, 3.0], "start": 0, "stop": 2},
    "greater_than": {"left": [1.0, 2.0], "right": 1.0},
    "less_than": {"left": [1.0, 2.0], "right": 2.0},
    "filter": {"values": [1.0, 2.0], "mask": [True, False]},
    "rebalance_path": {
        "returns": [[0.01, 0.02], [0.0, -0.01]],
        "target_weights": [0.5, 0.5],
        "rebalance_indices": [0],
        "initial_nav": 1.0,
        "cost_bps": 5.0,
    },
}


@pytest.mark.parametrize("operation", sorted(SUPPORTED_OPERATIONS))
def test_every_published_operation_has_a_schema_valid_success_path(
    operation: str,
) -> None:
    schema = server._quant_calculate_request_json_schema()
    request = _request(operation, deepcopy(VALID_ARGUMENTS[operation]))

    Draft202012Validator(schema).validate(request)
    assert execute_quant_calculation(request)["status"] == "succeeded"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("standard_deviation", {"data": [1.0, 2.0, 3.0], "ddof": 1}),
        ("standard_deviation", {"values": [1.0, 2.0, 3.0]}),
        ("add", {"left": 1.0, "right": 2.0, "extra": 3.0}),
        ("sum", {"left": 1.0, "right": 2.0}),
        (
            "rebalance_path",
            {
                "returns": [[0.01, 0.02]],
                "weights": [0.5, 0.5],
                "rebalance_indices": [],
                "initial_nav": 1.0,
                "cost_bps": 5.0,
            },
        ),
    ],
)
def test_model_facing_schema_rejects_missing_wrong_or_extra_operation_fields(
    operation: str,
    args: dict,
) -> None:
    validator = Draft202012Validator(server._quant_calculate_request_json_schema())
    assert list(validator.iter_errors(_request(operation, args)))


def test_rebalance_indices_schema_accepts_one_whole_ref_but_rejects_wrapped_list_ref() -> None:
    validator = Draft202012Validator(server._quant_calculate_request_json_schema())
    whole_ref = deepcopy(VALID_ARGUMENTS["rebalance_path"])
    whole_ref["rebalance_indices"] = {"ref": "indices"}
    wrapped_ref = deepcopy(VALID_ARGUMENTS["rebalance_path"])
    wrapped_ref["rebalance_indices"] = [{"ref": "indices"}]

    assert list(validator.iter_errors(_request("rebalance_path", whole_ref))) == []
    assert list(validator.iter_errors(_request("rebalance_path", wrapped_ref)))


def test_matching_request_succeeds_and_extra_outer_field_still_fails_closed() -> None:
    request = _request()
    result = execute_quant_calculation(request)
    assert result["status"] == "succeeded"
    assert result["results"]["answer"] == pytest.approx(3.0)

    request["timeout_seconds"] = 300
    with pytest.raises(QuantCalculationError, match="invalid_request_fields"):
        execute_quant_calculation(request)


def test_model_facing_schema_publishes_explicit_cross_call_stored_refs() -> None:
    request = _request(
        "sum",
        {
            "values": {
                "stored_ref": {
                    "calculation_id": "calculation-1",
                    "result_id": "prior_series",
                }
            }
        },
    )
    Draft202012Validator(server._quant_calculate_request_json_schema()).validate(
        request
    )

    result = execute_quant_calculation(
        request,
        stored_calculations={
            "calculation-1": {"prior_series": [1.0, 2.0, 3.0]}
        },
    )
    assert result["results"]["answer"] == pytest.approx(6.0)
