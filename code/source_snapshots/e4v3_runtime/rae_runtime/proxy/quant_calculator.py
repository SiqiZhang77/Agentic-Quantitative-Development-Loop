"""Deterministic, bounded numerical primitives for ``quant_calculate``.

This module is deliberately *not* a Python execution surface.  It imports no
filesystem, process, environment, network or dynamic-import APIs and accepts
only a small JSON operation graph.  The model must still select and compose the
operations that match the task; the calculator only performs the arithmetic.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import re
import statistics
from collections.abc import Mapping
from typing import Any, Final


REQUEST_SCHEMA_VERSION: Final = "quant-calculate-request-v2"
RESPONSE_SCHEMA_VERSION: Final = "quant-calculate-response-v1"
TOOL_PROFILE: Final = "quant_calculate_with_artifact_v3"
TOOL_NAME: Final = "quant_calculate"
MAX_CALLS_PER_RUN: Final = 4
MAX_OPERATIONS: Final = 64
MAX_LITERAL_CELLS: Final = 8_192
MAX_VECTOR_LENGTH: Final = 512
MAX_MATRIX_COLUMNS: Final = 16
MAX_RESULT_CELLS: Final = 32_768
MAX_REQUEST_BYTES: Final = 256 * 1024
MAX_RESPONSE_BYTES: Final = 256 * 1024
MAX_ABS_INPUT: Final = 1e12
MAX_ABS_RESULT: Final = 1e300

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CALCULATION_ID_RE = re.compile(r"^calculation-[1-4]$")
_BINARY_OPERATIONS = frozenset(
    {"add", "subtract", "multiply", "divide", "power"}
)
_UNARY_OPERATIONS = frozenset({"absolute", "negate", "sqrt"})
_REDUCTIONS = frozenset(
    {
        "sum",
        "product",
        "mean",
        "minimum",
        "maximum",
        "argmin",
        "argmax",
        "length",
        "first",
        "last",
    }
)
SUPPORTED_OPERATIONS: Final = frozenset(
    {
        *_BINARY_OPERATIONS,
        *_UNARY_OPERATIONS,
        *_REDUCTIONS,
        "standard_deviation",
        "variance",
        "covariance",
        "correlation",
        "dot",
        "matrix_vector_product",
        "column",
        "cumulative_product",
        "cumulative_sum",
        "running_max",
        "concat",
        "slice",
        "greater_than",
        "less_than",
        "filter",
        "rebalance_path",
    }
)
CALCULATION_ERROR_DETAILS: Final[dict[str, str]] = {
    "numeric_vector_required": (
        "The named argument must be one JSON array of numeric values. Correct "
        "that argument and call quant_calculate again; earlier retained results "
        "remain available inside this run."
    ),
    "nonempty_vector_required": (
        "The named argument requires at least one numeric value: expected minimum "
        "length 1 and received actual length 0. Correct that argument and call "
        "quant_calculate again; earlier retained results remain available inside "
        "this run."
    ),
    "flat_numeric_vector_required": (
        "The named argument must be one flat numeric array, but it contained a "
        "nested array. Select one vector or use a matrix operation, then call "
        "quant_calculate again; earlier retained results remain available."
    ),
    "numeric_matrix_required": (
        "The named argument must be a JSON array of numeric rows. Correct that "
        "argument and call quant_calculate again; earlier retained results remain "
        "available inside this run."
    ),
    "nonempty_matrix_required": (
        "The named argument requires at least one numeric row: expected minimum "
        "row count 1 and received actual row count 0. Correct that argument and "
        "call quant_calculate again; earlier retained results remain available "
        "inside this run."
    ),
    "invalid_operand_object": (
        "An object-valued operand must be exactly {'ref': 'earlier_operation_id'} "
        "or {'stored_ref': {'calculation_id': 'calculation-N', "
        "'result_id': 'returned_result_id'}}. Put nested arithmetic in its own "
        "earlier operation and reference that operation ID."
    ),
    "rebalance_indices_must_be_list": (
        "rebalance_indices must resolve to one flat list of zero-based input-row "
        "positions. Use one whole-field ref when an earlier operation returns the list."
    ),
    "rebalance_indices_must_be_integers": (
        "Every resolved rebalance_indices entry must be an integer input-row position; "
        "nested lists and non-integer values are not accepted."
    ),
    "rebalance_indices_must_be_unique": (
        "rebalance_indices must not contain the same input-row position more than once."
    ),
    "rebalance_index_out_of_range": (
        "Every rebalance index must satisfy 0 <= index < number of return rows."
    ),
}

# Single source of truth for operation argument names and their model-visible
# JSON kinds. The backend validator and FastMCP input schema are both derived
# from this contract so published fields cannot drift from enforced fields.
OPERATION_ARGUMENT_KINDS: Final[dict[str, dict[str, str]]] = {
    **{
        operation: {"left": "numeric_shape", "right": "numeric_shape"}
        for operation in _BINARY_OPERATIONS
    },
    **{
        operation: {"value": "numeric_shape"}
        for operation in _UNARY_OPERATIONS
    },
    **{
        operation: {"values": "numeric_vector"}
        for operation in _REDUCTIONS
    },
    "standard_deviation": {"values": "numeric_vector", "ddof": "ddof"},
    "variance": {"values": "numeric_vector", "ddof": "ddof"},
    "covariance": {
        "left": "numeric_vector",
        "right": "numeric_vector",
        "ddof": "ddof",
    },
    "correlation": {"left": "numeric_vector", "right": "numeric_vector"},
    "dot": {"left": "numeric_vector", "right": "numeric_vector"},
    "matrix_vector_product": {
        "matrix": "numeric_matrix",
        "vector": "numeric_vector",
    },
    "column": {"matrix": "numeric_matrix", "index": "nonnegative_integer"},
    "cumulative_product": {"values": "numeric_vector"},
    "cumulative_sum": {"values": "numeric_vector"},
    "running_max": {"values": "numeric_vector"},
    "concat": {"items": "concat_items"},
    "slice": {
        "values": "numeric_vector",
        "start": "nonnegative_integer",
        "stop": "nonnegative_integer",
    },
    "greater_than": {"left": "scalar_or_vector", "right": "scalar_or_vector"},
    "less_than": {"left": "scalar_or_vector", "right": "scalar_or_vector"},
    "filter": {"values": "numeric_vector", "mask": "boolean_vector"},
    "rebalance_path": {
        "returns": "return_matrix",
        "target_weights": "target_weights",
        "rebalance_indices": "rebalance_indices",
        "initial_nav": "positive_number",
        "cost_bps": "cost_bps",
    },
}
OPTIONAL_OPERATION_ARGUMENTS: Final[dict[str, frozenset[str]]] = {
    "covariance": frozenset({"ddof"}),
}

if set(OPERATION_ARGUMENT_KINDS) != set(SUPPORTED_OPERATIONS):  # pragma: no cover
    raise RuntimeError("operation argument contract does not cover supported operations")


def _reference_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["ref"],
        "properties": {
            "ref": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{0,63}$",
                "description": "ID of an earlier operation in this request only.",
            }
        },
    }


def _stored_reference_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["stored_ref"],
        "properties": {
            "stored_ref": {
                "type": "object",
                "additionalProperties": False,
                "required": ["calculation_id", "result_id"],
                "properties": {
                    "calculation_id": {
                        "type": "string",
                        "pattern": "^calculation-[1-4]$",
                        "description": (
                            "ID returned by an earlier successful or partial "
                            "quant_calculate call in this run."
                        ),
                    },
                    "result_id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]{0,63}$",
                        "description": (
                            "Returned result ID from that earlier calculation."
                        ),
                    },
                },
            }
        },
    }


def _number_json_schema() -> dict[str, Any]:
    return {
        "type": "number",
        "minimum": -MAX_ABS_INPUT,
        "maximum": MAX_ABS_INPUT,
    }


def _with_reference(literal_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "anyOf": [
            deepcopy(literal_schema),
            _reference_json_schema(),
            _stored_reference_json_schema(),
        ]
    }


def _numeric_vector_literal_json_schema(
    *, max_items: int = MAX_VECTOR_LENGTH
) -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": max_items,
        "items": _with_reference(_number_json_schema()),
    }


def _numeric_matrix_literal_json_schema() -> dict[str, Any]:
    return {
        "type": "array",
        "minItems": 1,
        "maxItems": MAX_VECTOR_LENGTH,
        "items": _with_reference(
            _numeric_vector_literal_json_schema(max_items=MAX_MATRIX_COLUMNS)
        ),
        "description": (
            "Non-empty rectangular row-major numeric matrix with at most "
            f"{MAX_MATRIX_COLUMNS} columns."
        ),
    }


def _argument_json_schema(kind: str) -> dict[str, Any]:
    number = _number_json_schema()
    vector = _numeric_vector_literal_json_schema()
    matrix = _numeric_matrix_literal_json_schema()

    if kind == "numeric_shape":
        return {
            "anyOf": [
                number,
                vector,
                matrix,
                _reference_json_schema(),
                _stored_reference_json_schema(),
            ],
            "description": (
                "A finite number, numeric vector, numeric matrix, or a reference "
                "to an earlier compatible result in this request or an earlier "
                "successful calculation call. Binary operands must have matching "
                "shapes, except scalar broadcasting is allowed."
            ),
        }
    if kind == "scalar_or_vector":
        return {
            "anyOf": [
                number,
                vector,
                _reference_json_schema(),
                _stored_reference_json_schema(),
            ],
            "description": (
                "A finite number, numeric vector, or a reference to an earlier "
                "compatible result in this request or an earlier successful "
                "calculation call."
            ),
        }
    if kind == "numeric_vector":
        schema = _with_reference(vector)
        schema["description"] = (
            "A non-empty numeric vector, or a reference to an earlier vector result."
        )
        return schema
    if kind == "numeric_matrix":
        schema = _with_reference(matrix)
        schema["description"] = (
            "A non-empty rectangular numeric matrix, or a reference to an earlier "
            "matrix result."
        )
        return schema
    if kind == "nonnegative_integer":
        return _with_reference({"type": "integer", "minimum": 0})
    if kind == "ddof":
        schema = _with_reference({"type": "integer", "enum": [0, 1]})
        schema["description"] = (
            "Delta degrees of freedom: 0 for population or 1 for sample statistics."
        )
        return schema
    if kind == "boolean_vector":
        schema = _with_reference(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_VECTOR_LENGTH,
                "items": {
                    "anyOf": [
                        {"type": "boolean"},
                        _reference_json_schema(),
                        _stored_reference_json_schema(),
                    ]
                },
            }
        )
        schema["description"] = (
            "A non-empty boolean mask, or a reference to an earlier boolean-vector "
            "result; its length must match values."
        )
        return schema
    if kind == "concat_items":
        schema = _with_reference(
            {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_VECTOR_LENGTH,
                "items": {
                    "anyOf": [
                        number,
                        vector,
                        _reference_json_schema(),
                        _stored_reference_json_schema(),
                    ]
                },
            }
        )
        schema["description"] = (
            "A non-empty list of numbers, numeric vectors, or references to earlier "
            "compatible results."
        )
        return schema
    if kind == "rebalance_indices":
        schema = _with_reference(
            {
                "type": "array",
                "maxItems": MAX_VECTOR_LENGTH,
                "uniqueItems": True,
                "items": {"type": "integer", "minimum": 0},
            }
        )
        schema["description"] = (
            "One flat list of unique zero-based input-row positions at which to "
            "rebalance, or one whole-field reference to an earlier compatible list. "
            "Do not wrap a list-valued reference inside an array. An empty list means "
            "no rebalancing."
        )
        return schema
    if kind == "return_matrix":
        schema = _with_reference(matrix)
        schema["description"] = (
            "Period-by-asset return matrix; every return must be greater than -1, "
            "or a reference to an earlier compatible matrix result."
        )
        return schema
    if kind == "target_weights":
        schema = _with_reference(vector)
        schema["description"] = (
            "Non-negative target weights that sum to 1, or a reference to an "
            "earlier compatible vector result."
        )
        return schema
    if kind == "positive_number":
        schema = _with_reference(
            {
                "type": "number",
                "exclusiveMinimum": 0,
                "maximum": MAX_ABS_INPUT,
            }
        )
        schema["description"] = "A positive finite number."
        return schema
    if kind == "cost_bps":
        schema = _with_reference(
            {"type": "number", "minimum": 0, "maximum": 10_000}
        )
        schema["description"] = "Non-negative transaction cost in basis points."
        return schema
    raise RuntimeError(f"unknown operation argument kind: {kind}")


def _operation_contract_groups() -> list[
    tuple[list[str], dict[str, str], frozenset[str]]
]:
    grouped: dict[
        tuple[tuple[tuple[str, str], ...], tuple[str, ...]], list[str]
    ] = {}
    for operation, argument_kinds in OPERATION_ARGUMENT_KINDS.items():
        optional = OPTIONAL_OPERATION_ARGUMENTS.get(operation, frozenset())
        key = (tuple(sorted(argument_kinds.items())), tuple(sorted(optional)))
        grouped.setdefault(key, []).append(operation)
    return [
        (sorted(operations), dict(argument_items), frozenset(optional))
        for (argument_items, optional), operations in sorted(
            grouped.items(), key=lambda item: sorted(item[1])[0]
        )
    ]


def _operation_record_json_schema(
    operations: list[str],
    argument_kinds: dict[str, str],
    optional: frozenset[str],
) -> dict[str, Any]:
    args_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(set(argument_kinds) - set(optional)),
        "properties": {
            name: _argument_json_schema(kind)
            for name, kind in sorted(argument_kinds.items())
        },
    }
    if operations == ["covariance"]:
        args_schema["description"] = "ddof is optional and defaults to 1."
    if operations == ["slice"]:
        args_schema["description"] = (
            "Uses Python-style half-open bounds and requires 0 <= start < stop <= "
            "len(values)."
        )

    operation_schema: dict[str, Any] = {"type": "string"}
    if len(operations) == 1:
        operation_schema["const"] = operations[0]
    else:
        operation_schema["enum"] = operations

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "op", "args"],
        "properties": {
            "id": {
                "type": "string",
                "pattern": "^[a-z][a-z0-9_]{0,63}$",
                "description": "Unique operation ID used by return_ids and later refs.",
            },
            "op": operation_schema,
            "args": args_schema,
        },
    }


def quant_calculate_request_json_schema() -> dict[str, Any]:
    """Return the exact model-facing request contract enforced by this module."""

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "operations", "return_ids"],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": REQUEST_SCHEMA_VERSION,
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OPERATIONS,
                "items": {
                    "anyOf": [
                        _operation_record_json_schema(
                            operations,
                            argument_kinds,
                            optional,
                        )
                        for operations, argument_kinds, optional in (
                            _operation_contract_groups()
                        )
                    ]
                },
                "description": (
                    "Ordered numerical operations. Use {'ref': 'operation_id'} "
                    "only for an earlier operation in this request. To use a "
                    "returned result from an earlier successful or partial "
                    "quant_calculate call in this run, use {'stored_ref': "
                    "{'calculation_id': "
                    "'calculation-1', 'result_id': 'returned_result_id'}}. Across "
                    f"the request, literal inputs are limited to {MAX_LITERAL_CELLS} "
                    "cells."
                ),
            },
            "return_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OPERATIONS,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "pattern": "^[a-z][a-z0-9_]{0,63}$",
                },
                "description": "Unique completed operation IDs to return.",
            },
        },
    }


class QuantCalculationError(ValueError):
    """A content-free failure from the constrained calculation boundary."""

    def __init__(self, code: str, *, argument_name: str | None = None):
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "calculation_failed"
        self.code = code
        self.operation_index: int | None = None
        self.operation_id: str | None = None
        self.operation: str | None = None
        self.argument_name = (
            argument_name
            if type(argument_name) is str and _ID_RE.fullmatch(argument_name) is not None
            else None
        )
        self.detail: str | None = CALCULATION_ERROR_DETAILS.get(code)
        super().__init__(code)


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise QuantCalculationError("non_json_value") from exc


def quant_calculator_enabled(payload: Mapping[str, Any]) -> bool:
    """Return the explicit task-profile flag; malformed values fail closed."""

    if not isinstance(payload, Mapping):
        raise QuantCalculationError("invalid_runtime_payload")
    objectives = payload.get("execution_objectives", {})
    if not isinstance(objectives, Mapping):
        raise QuantCalculationError("invalid_execution_objectives")
    parameters = objectives.get("parsed_task_parameters", {})
    if not isinstance(parameters, Mapping):
        raise QuantCalculationError("invalid_task_parameters")
    value = parameters.get("quant_calculator_enabled", False)
    if type(value) is not bool:
        raise QuantCalculationError("invalid_calculator_flag")
    return value


def quant_calculator_writable_paths(
    payload: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return the single exact submission path for an enabled task profile."""

    if not quant_calculator_enabled(payload):
        return {}
    repositories = payload.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise QuantCalculationError("calculator_requires_one_repository")
    repository = repositories[0]
    if not isinstance(repository, Mapping):
        raise QuantCalculationError("invalid_calculator_repository")
    repo_name = repository.get("repo_full_name")
    if type(repo_name) is not str or not repo_name.strip():
        raise QuantCalculationError("invalid_calculator_repository")
    strategy = payload.get("strategy")
    if not isinstance(strategy, Mapping):
        raise QuantCalculationError("invalid_calculator_strategy")
    target_path = strategy.get("target_path")
    if type(target_path) is not str or not target_path.strip():
        raise QuantCalculationError("invalid_calculator_target_path")
    candidate = target_path.strip()
    if candidate.startswith("/") or "\\" in candidate or ".." in candidate.split("/"):
        raise QuantCalculationError("invalid_calculator_target_path")
    normalised = candidate.strip("/")
    if not normalised:
        raise QuantCalculationError("invalid_calculator_target_path")
    return {repo_name: [normalised]}


def quant_calculator_schema_paths(
    payload: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Return the exact repository schema path frozen by the task profile."""

    if not quant_calculator_enabled(payload):
        return {}
    repositories = payload.get("repositories")
    if not isinstance(repositories, list) or len(repositories) != 1:
        raise QuantCalculationError("calculator_requires_one_repository")
    repository = repositories[0]
    if not isinstance(repository, Mapping):
        raise QuantCalculationError("invalid_calculator_repository")
    repo_name = repository.get("repo_full_name")
    if type(repo_name) is not str or not repo_name.strip():
        raise QuantCalculationError("invalid_calculator_repository")
    objectives = payload.get("execution_objectives")
    parameters = (
        objectives.get("parsed_task_parameters")
        if isinstance(objectives, Mapping)
        else None
    )
    schema_path = (
        parameters.get("quant_calculator_schema_path")
        if isinstance(parameters, Mapping)
        else None
    )
    if type(schema_path) is not str or not schema_path.strip():
        raise QuantCalculationError("invalid_calculator_schema_path")
    candidate = schema_path.strip()
    if candidate.startswith("/") or "\\" in candidate or ".." in candidate.split("/"):
        raise QuantCalculationError("invalid_calculator_schema_path")
    normalised = candidate.strip("/")
    if not normalised:
        raise QuantCalculationError("invalid_calculator_schema_path")
    return {repo_name: [normalised]}


def _number(value: Any, *, input_value: bool = False) -> float:
    if type(value) not in (int, float):
        raise QuantCalculationError("numeric_value_required")
    parsed = float(value)
    limit = MAX_ABS_INPUT if input_value else MAX_ABS_RESULT
    if not math.isfinite(parsed) or abs(parsed) > limit:
        raise QuantCalculationError(
            "input_number_out_of_range" if input_value else "result_out_of_range"
        )
    return parsed


def _count_literal_cells(value: Any, *, depth: int = 0) -> int:
    if depth > 3:
        raise QuantCalculationError("literal_nesting_too_deep")
    if type(value) in (int, float, bool):
        if type(value) is not bool:
            _number(value, input_value=True)
        return 1
    if isinstance(value, list):
        if len(value) > MAX_VECTOR_LENGTH:
            raise QuantCalculationError("literal_vector_too_large")
        return sum(_count_literal_cells(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        if set(value) == {"ref"}:
            ref = value.get("ref")
            if type(ref) is not str or _ID_RE.fullmatch(ref) is None:
                raise QuantCalculationError("invalid_reference")
            return 0
        if set(value) == {"stored_ref"}:
            selector = value.get("stored_ref")
            if not isinstance(selector, Mapping) or set(selector) != {
                "calculation_id",
                "result_id",
            }:
                raise QuantCalculationError("invalid_stored_reference")
            calculation_id = selector.get("calculation_id")
            result_id = selector.get("result_id")
            if (
                type(calculation_id) is not str
                or _CALCULATION_ID_RE.fullmatch(calculation_id) is None
                or type(result_id) is not str
                or _ID_RE.fullmatch(result_id) is None
            ):
                raise QuantCalculationError("invalid_stored_reference")
            return 0
        raise QuantCalculationError("invalid_operand_object")
    raise QuantCalculationError("unsupported_literal_type")


def _result_cells(value: Any, *, depth: int = 0) -> int:
    if depth > 4:
        raise QuantCalculationError("result_nesting_too_deep")
    if type(value) in (int, float, bool):
        if type(value) is not bool:
            _number(value)
        return 1
    if isinstance(value, list):
        return sum(_result_cells(item, depth=depth + 1) for item in value)
    raise QuantCalculationError("unsupported_result_type")


def _resolve(
    value: Any,
    results: Mapping[str, Any],
    stored_calculations: Mapping[str, Mapping[str, Any]],
) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"ref"}:
            ref = value.get("ref")
            if ref not in results:
                raise QuantCalculationError("unknown_or_forward_reference")
            return results[ref]
        if set(value) == {"stored_ref"}:
            selector = value.get("stored_ref")
            if not isinstance(selector, Mapping):
                raise QuantCalculationError("invalid_stored_reference")
            calculation_id = selector.get("calculation_id")
            result_id = selector.get("result_id")
            calculation = stored_calculations.get(calculation_id)
            if not isinstance(calculation, Mapping):
                raise QuantCalculationError("unknown_stored_calculation")
            if result_id not in calculation:
                raise QuantCalculationError("unknown_stored_result")
            return deepcopy(calculation[result_id])
        raise QuantCalculationError("invalid_operand_object")
    if isinstance(value, list):
        return [
            _resolve(item, results, stored_calculations)
            for item in value
        ]
    return value


def _vector(
    value: Any,
    *,
    booleans: bool = False,
    argument_name: str | None = None,
) -> list[Any]:
    if not isinstance(value, list):
        raise QuantCalculationError(
            "numeric_vector_required", argument_name=argument_name
        )
    if not value:
        raise QuantCalculationError(
            "nonempty_vector_required", argument_name=argument_name
        )
    if any(isinstance(item, list) for item in value):
        raise QuantCalculationError(
            "flat_numeric_vector_required", argument_name=argument_name
        )
    if len(value) > MAX_VECTOR_LENGTH:
        raise QuantCalculationError("vector_too_large", argument_name=argument_name)
    if booleans:
        if any(type(item) is not bool for item in value):
            raise QuantCalculationError(
                "boolean_mask_required", argument_name=argument_name
            )
        return list(value)
    try:
        return [_number(item) for item in value]
    except QuantCalculationError as exc:
        if exc.argument_name is None:
            exc.argument_name = argument_name
        raise


def _matrix(value: Any, *, argument_name: str | None = None) -> list[list[float]]:
    if not isinstance(value, list):
        raise QuantCalculationError(
            "numeric_matrix_required", argument_name=argument_name
        )
    if not value:
        raise QuantCalculationError(
            "nonempty_matrix_required", argument_name=argument_name
        )
    if len(value) > MAX_VECTOR_LENGTH:
        raise QuantCalculationError("matrix_too_large", argument_name=argument_name)
    rows = [_vector(row, argument_name=argument_name) for row in value]
    width = len(rows[0])
    if width > MAX_MATRIX_COLUMNS or any(len(row) != width for row in rows):
        raise QuantCalculationError(
            "invalid_matrix_shape", argument_name=argument_name
        )
    return rows


def _same_shape_binary(left: Any, right: Any, fn) -> Any:
    if type(left) in (int, float) and type(right) in (int, float):
        try:
            return _number(fn(_number(left), _number(right)))
        except (ArithmeticError, ValueError, OverflowError) as exc:
            raise QuantCalculationError("arithmetic_domain_error") from exc
    if isinstance(left, list) and type(right) in (int, float):
        return [_same_shape_binary(item, right, fn) for item in left]
    if type(left) in (int, float) and isinstance(right, list):
        return [_same_shape_binary(left, item, fn) for item in right]
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            raise QuantCalculationError("shape_mismatch")
        return [_same_shape_binary(a, b, fn) for a, b in zip(left, right)]
    raise QuantCalculationError("numeric_shape_required")


def _unary(value: Any, fn) -> Any:
    if type(value) in (int, float):
        try:
            return _number(fn(_number(value)))
        except (ArithmeticError, ValueError, OverflowError) as exc:
            raise QuantCalculationError("arithmetic_domain_error") from exc
    if isinstance(value, list):
        return [_unary(item, fn) for item in value]
    raise QuantCalculationError("numeric_shape_required")


def _require_operation_args(
    operation: str,
    args: Mapping[str, Any],
) -> None:
    if not isinstance(args, Mapping):
        raise QuantCalculationError("operation_args_required")
    argument_kinds = OPERATION_ARGUMENT_KINDS.get(operation)
    if argument_kinds is None:
        raise QuantCalculationError("unsupported_operation")
    optional = OPTIONAL_OPERATION_ARGUMENTS.get(operation, frozenset())
    allowed = set(argument_kinds)
    required = allowed - set(optional)
    if not required.issubset(args) or not set(args).issubset(allowed):
        raise QuantCalculationError("invalid_operation_args")


def _sample_covariance(left: list[float], right: list[float], ddof: int) -> float:
    if len(left) != len(right):
        raise QuantCalculationError("shape_mismatch")
    if type(ddof) is not int or ddof not in {0, 1} or len(left) <= ddof:
        raise QuantCalculationError("invalid_ddof")
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    return _number(
        sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
        / (len(left) - ddof)
    )


def _rebalance_path(args: Mapping[str, Any]) -> list[list[float]]:
    _require_operation_args("rebalance_path", args)
    returns = _matrix(args["returns"], argument_name="returns")
    target = _vector(args["target_weights"], argument_name="target_weights")
    if len(target) != len(returns[0]):
        raise QuantCalculationError("shape_mismatch")
    if any(weight < 0 for weight in target) or not math.isclose(
        sum(target), 1.0, rel_tol=0.0, abs_tol=1e-12
    ):
        raise QuantCalculationError("invalid_target_weights")
    raw_indices = args["rebalance_indices"]
    if not isinstance(raw_indices, list):
        raise QuantCalculationError(
            "rebalance_indices_must_be_list", argument_name="rebalance_indices"
        )
    if any(type(item) is not int for item in raw_indices):
        raise QuantCalculationError(
            "rebalance_indices_must_be_integers", argument_name="rebalance_indices"
        )
    if len(set(raw_indices)) != len(raw_indices):
        raise QuantCalculationError(
            "rebalance_indices_must_be_unique", argument_name="rebalance_indices"
        )
    if any(item < 0 or item >= len(returns) for item in raw_indices):
        raise QuantCalculationError(
            "rebalance_index_out_of_range", argument_name="rebalance_indices"
        )
    rebalance_indices = set(raw_indices)
    initial_nav = _number(args["initial_nav"])
    cost_bps = _number(args["cost_bps"])
    if initial_nav <= 0 or cost_bps < 0 or cost_bps > 10_000:
        raise QuantCalculationError("invalid_portfolio_configuration")

    holdings = [initial_nav * weight for weight in target]
    rows: list[list[float]] = []
    for index, period_returns in enumerate(returns):
        if any(value <= -1.0 for value in period_returns):
            raise QuantCalculationError("return_not_greater_than_minus_one")
        holdings = [
            _number(value * (1.0 + period_return))
            for value, period_return in zip(holdings, period_returns)
        ]
        pre_nav = _number(sum(holdings))
        if pre_nav <= 0:
            raise QuantCalculationError("nonpositive_portfolio_nav")
        pre_weights = [_number(value / pre_nav) for value in holdings]
        turnover = 0.0
        cost = 0.0
        post_nav = pre_nav
        post_weights = list(pre_weights)
        if index in rebalance_indices:
            turnover = _number(
                0.5 * sum(abs(current - wanted) for current, wanted in zip(pre_weights, target))
            )
            cost = _number(pre_nav * turnover * cost_bps / 10_000.0)
            post_nav = _number(pre_nav - cost)
            if post_nav <= 0:
                raise QuantCalculationError("nonpositive_portfolio_nav")
            holdings = [_number(post_nav * weight) for weight in target]
            post_weights = list(target)
        rows.append(
            [
                pre_nav,
                turnover,
                cost,
                post_nav,
                *pre_weights,
                *post_weights,
            ]
        )
    return rows


def _execute_operation(operation: str, args: Mapping[str, Any]) -> Any:
    if operation in _BINARY_OPERATIONS:
        _require_operation_args(operation, args)
        functions = {
            "add": lambda a, b: a + b,
            "subtract": lambda a, b: a - b,
            "multiply": lambda a, b: a * b,
            "divide": lambda a, b: a / b,
            "power": lambda a, b: a**b,
        }
        return _same_shape_binary(args["left"], args["right"], functions[operation])

    if operation in _UNARY_OPERATIONS:
        _require_operation_args(operation, args)
        functions = {
            "absolute": abs,
            "negate": lambda value: -value,
            "sqrt": math.sqrt,
        }
        return _unary(args["value"], functions[operation])

    if operation in _REDUCTIONS:
        _require_operation_args(operation, args)
        values = _vector(args["values"], argument_name="values")
        if operation == "sum":
            return _number(sum(values))
        if operation == "product":
            return _number(math.prod(values))
        if operation == "mean":
            return _number(statistics.fmean(values))
        if operation == "minimum":
            return _number(min(values))
        if operation == "maximum":
            return _number(max(values))
        if operation == "argmin":
            return values.index(min(values))
        if operation == "argmax":
            return values.index(max(values))
        if operation == "length":
            return len(values)
        if operation == "first":
            return values[0]
        return values[-1]

    if operation in {"standard_deviation", "variance"}:
        _require_operation_args(operation, args)
        values = _vector(args["values"], argument_name="values")
        ddof = args["ddof"]
        if type(ddof) is not int or ddof not in {0, 1} or len(values) <= ddof:
            raise QuantCalculationError("invalid_ddof")
        fn = (
            statistics.stdev
            if operation == "standard_deviation" and ddof == 1
            else statistics.pstdev
            if operation == "standard_deviation"
            else statistics.variance
            if ddof == 1
            else statistics.pvariance
        )
        return _number(fn(values))

    if operation in {"covariance", "correlation"}:
        _require_operation_args(operation, args)
        left = _vector(args["left"], argument_name="left")
        right = _vector(args["right"], argument_name="right")
        if operation == "covariance":
            return _sample_covariance(left, right, args.get("ddof", 1))
        covariance = _sample_covariance(left, right, 1)
        denominator = statistics.stdev(left) * statistics.stdev(right)
        if denominator == 0:
            raise QuantCalculationError("zero_denominator")
        return _number(covariance / denominator)

    if operation == "dot":
        _require_operation_args(operation, args)
        left = _vector(args["left"], argument_name="left")
        right = _vector(args["right"], argument_name="right")
        if len(left) != len(right):
            raise QuantCalculationError("shape_mismatch")
        return _number(sum(a * b for a, b in zip(left, right)))

    if operation == "matrix_vector_product":
        _require_operation_args(operation, args)
        matrix = _matrix(args["matrix"], argument_name="matrix")
        vector = _vector(args["vector"], argument_name="vector")
        if len(matrix[0]) != len(vector):
            raise QuantCalculationError("shape_mismatch")
        return [_number(sum(a * b for a, b in zip(row, vector))) for row in matrix]

    if operation == "column":
        _require_operation_args(operation, args)
        matrix = _matrix(args["matrix"], argument_name="matrix")
        index = args["index"]
        if type(index) is not int or not 0 <= index < len(matrix[0]):
            raise QuantCalculationError("invalid_column_index")
        return [row[index] for row in matrix]

    if operation in {"cumulative_product", "cumulative_sum", "running_max"}:
        _require_operation_args(operation, args)
        values = _vector(args["values"], argument_name="values")
        output: list[float] = []
        current = 1.0 if operation == "cumulative_product" else 0.0
        if operation == "running_max":
            current = -math.inf
        for value in values:
            if operation == "cumulative_product":
                current *= value
            elif operation == "cumulative_sum":
                current += value
            else:
                current = max(current, value)
            output.append(_number(current))
        return output

    if operation == "concat":
        _require_operation_args(operation, args)
        if not isinstance(args["items"], list) or not args["items"]:
            raise QuantCalculationError("concat_items_required")
        output: list[float] = []
        for item in args["items"]:
            if type(item) in (int, float):
                output.append(_number(item))
            elif isinstance(item, list):
                output.extend(_vector(item, argument_name="items"))
            else:
                raise QuantCalculationError("invalid_concat_item")
        if len(output) > MAX_VECTOR_LENGTH:
            raise QuantCalculationError("vector_too_large")
        return output

    if operation == "slice":
        _require_operation_args(operation, args)
        values = _vector(args["values"], argument_name="values")
        start, stop = args["start"], args["stop"]
        if type(start) is not int or type(stop) is not int or not 0 <= start <= stop <= len(values):
            raise QuantCalculationError("invalid_slice")
        if start == stop:
            raise QuantCalculationError("empty_slice")
        return values[start:stop]

    if operation in {"greater_than", "less_than"}:
        _require_operation_args(operation, args)
        comparator = (
            (lambda a, b: a > b) if operation == "greater_than" else (lambda a, b: a < b)
        )
        left, right = args["left"], args["right"]
        if isinstance(left, list) and type(right) in (int, float):
            return [
                comparator(value, _number(right))
                for value in _vector(left, argument_name="left")
            ]
        if type(left) in (int, float) and isinstance(right, list):
            return [
                comparator(_number(left), value)
                for value in _vector(right, argument_name="right")
            ]
        if isinstance(left, list) and isinstance(right, list):
            a = _vector(left, argument_name="left")
            b = _vector(right, argument_name="right")
            if len(a) != len(b):
                raise QuantCalculationError("shape_mismatch")
            return [comparator(x, y) for x, y in zip(a, b)]
        return comparator(_number(left), _number(right))

    if operation == "filter":
        _require_operation_args(operation, args)
        values = _vector(args["values"], argument_name="values")
        mask = _vector(args["mask"], booleans=True, argument_name="mask")
        if len(values) != len(mask):
            raise QuantCalculationError("shape_mismatch")
        output = [value for value, keep in zip(values, mask) if keep]
        if not output:
            raise QuantCalculationError("empty_filter_result")
        return output

    if operation == "rebalance_path":
        return _rebalance_path(args)

    raise QuantCalculationError("unsupported_operation")


def execute_quant_calculation(
    request: Mapping[str, Any],
    *,
    stored_calculations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one bounded operation graph with explicit run-local stored refs."""

    if not isinstance(request, Mapping):
        raise QuantCalculationError("request_object_required")
    if set(request) != {"schema_version", "operations", "return_ids"}:
        raise QuantCalculationError("invalid_request_fields")
    if request.get("schema_version") != REQUEST_SCHEMA_VERSION:
        raise QuantCalculationError("invalid_request_schema_version")
    if stored_calculations is None:
        stored_calculations = {}
    if not isinstance(stored_calculations, Mapping):
        raise QuantCalculationError("invalid_stored_calculations")
    if len(canonical_json(request).encode("utf-8")) > MAX_REQUEST_BYTES:
        raise QuantCalculationError("request_too_large")
    operations = request.get("operations")
    return_ids = request.get("return_ids")
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise QuantCalculationError("invalid_operation_count")
    if not isinstance(return_ids, list) or not return_ids:
        raise QuantCalculationError("return_ids_required")
    if len(return_ids) > MAX_OPERATIONS or any(
        type(item) is not str or _ID_RE.fullmatch(item) is None for item in return_ids
    ):
        raise QuantCalculationError("invalid_return_ids")
    if len(set(return_ids)) != len(return_ids):
        raise QuantCalculationError("unknown_or_duplicate_return_id")

    literal_cells = 0
    results: dict[str, Any] = {}
    current_operation_ids: set[str] = set()
    result_cells = 0
    for operation_index, item in enumerate(operations):
        operation_id = item.get("id") if isinstance(item, Mapping) else None
        operation = item.get("op") if isinstance(item, Mapping) else None
        try:
            if not isinstance(item, Mapping) or set(item) != {"id", "op", "args"}:
                raise QuantCalculationError("invalid_operation_record")
            args = item.get("args")
            if type(operation_id) is not str or _ID_RE.fullmatch(operation_id) is None:
                raise QuantCalculationError("invalid_operation_id")
            if operation_id in results:
                raise QuantCalculationError("duplicate_operation_id")
            if operation not in SUPPORTED_OPERATIONS:
                raise QuantCalculationError("unsupported_operation")
            if not isinstance(args, Mapping):
                raise QuantCalculationError("operation_args_required")
            for argument_name, value in args.items():
                try:
                    literal_cells += _count_literal_cells(value)
                except QuantCalculationError as exc:
                    if exc.argument_name is None:
                        exc.argument_name = argument_name
                    raise
            if literal_cells > MAX_LITERAL_CELLS:
                raise QuantCalculationError("too_many_literal_cells")
            resolved = {}
            for argument_name, value in args.items():
                try:
                    resolved[argument_name] = _resolve(
                        value, results, stored_calculations
                    )
                except QuantCalculationError as exc:
                    if exc.argument_name is None:
                        exc.argument_name = argument_name
                    raise
            result = _execute_operation(operation, resolved)
            result_cells += _result_cells(result)
            if result_cells > MAX_RESULT_CELLS:
                raise QuantCalculationError("too_many_result_cells")
            results[operation_id] = result
            current_operation_ids.add(operation_id)
        except QuantCalculationError as exc:
            exc.operation_index = operation_index
            if type(operation_id) is str and _ID_RE.fullmatch(operation_id) is not None:
                exc.operation_id = operation_id
            if type(operation) is str and operation in SUPPORTED_OPERATIONS:
                exc.operation = operation
            # Preserve only results the caller explicitly selected in
            # ``return_ids`` and which completed before this failing step.  An
            # unselected intermediate remains private, and this boundary never
            # guesses which calculation belongs to a task item.
            completed_return_ids = [
                result_id for result_id in return_ids if result_id in results
            ]
            if not completed_return_ids or exc.code in {
                "invalid_operation_record",
                "invalid_operation_id",
                "duplicate_operation_id",
            }:
                raise
            response = {
                "schema_version": RESPONSE_SCHEMA_VERSION,
                "status": "partial",
                "operation_count": len(operations),
                "completed_operation_count": len(results),
                "results": {
                    name: results[name] for name in completed_return_ids
                },
                "error": exc.code,
                "failed_operation_index": operation_index,
            }
            if exc.detail is not None:
                response["error_detail"] = exc.detail
            if exc.operation_id is not None:
                response["failed_operation_id"] = exc.operation_id
            if exc.operation is not None:
                response["failed_operation"] = exc.operation
            if exc.argument_name is not None:
                response["failed_argument"] = exc.argument_name
            if len(canonical_json(response).encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise QuantCalculationError("response_too_large") from exc
            return response

    if any(item not in current_operation_ids for item in return_ids):
        raise QuantCalculationError("unknown_or_duplicate_return_id")
    response = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "status": "succeeded",
        "operation_count": len(operations),
        "results": {name: results[name] for name in return_ids},
    }
    if len(canonical_json(response).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise QuantCalculationError("response_too_large")
    return response
