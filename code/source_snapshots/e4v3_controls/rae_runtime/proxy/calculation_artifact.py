"""Bounded JSON assembly from run-local ``quant_calculate`` results.

This module performs no arithmetic and has no filesystem, network, process or
environment access.  It only copies already-computed JSON values, selects
positions or matrix columns requested by the caller, zips equal-length columns
into named rows, and serializes the resulting JSON document.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Final


ASSEMBLY_SCHEMA_VERSION: Final = "calculation-artifact-template-v1"
PUBLIC_RESULT_SCHEMA_VERSION: Final = "quant-calculate-mcp-response-v2"
MAX_ASSEMBLY_SPEC_BYTES: Final = 96 * 1024
MAX_ASSEMBLED_ARTIFACT_BYTES: Final = 1024 * 1024
MAX_ASSEMBLY_DEPTH: Final = 16
MAX_ASSEMBLY_REFERENCES: Final = 512
MAX_CHECKPOINT_REFERENCES: Final = 3
MAX_ZIPPED_ROWS: Final = 512
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CALCULATION_ID_RE = re.compile(r"^calculation-[1-4]$")
_CHECKPOINT_NAMES = frozenset({"level_a", "level_b", "level_c"})
_RESERVED_KEYS = frozenset({"$calc", "$checkpoint", "$zip_rows", "$repeat"})


class CalculationArtifactError(ValueError):
    """A stable, content-free assembly failure."""

    def __init__(self, code: str):
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            code = "calculation_artifact_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AssembledArtifact:
    document: dict[str, Any]
    content: str
    byte_count: int
    sha256: str
    reference_count: int
    checkpoint_reference_count: int


@dataclass(frozen=True)
class _Column:
    value: Any
    length: int | None


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CalculationArtifactError("non_json_or_nonfinite_value") from exc


def _shape(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    if not value:
        return [0]
    child_shapes = [_shape(item) for item in value]
    if all(shape == child_shapes[0] for shape in child_shapes):
        return [len(value), *child_shapes[0]]
    return [len(value)]


def _preview(value: list[Any]) -> dict[str, Any]:
    if len(value) <= 6:
        return {"first": deepcopy(value), "last": []}
    return {
        "first": deepcopy(value[:3]),
        "last": deepcopy(value[-3:]),
    }


def summarize_calculation_response(
    response: Mapping[str, Any],
    calculation_id: str,
) -> dict[str, Any]:
    """Return a compact model-facing description while full results stay local."""

    results = response.get("results")
    if not isinstance(results, Mapping) or not _CALCULATION_ID_RE.fullmatch(calculation_id):
        raise CalculationArtifactError("invalid_stored_calculation")
    summaries: dict[str, Any] = {}
    for result_id, value in results.items():
        if type(result_id) is not str or _ID_RE.fullmatch(result_id) is None:
            raise CalculationArtifactError("invalid_stored_result_id")
        if type(value) in (int, float, bool):
            if type(value) is not bool and not math.isfinite(float(value)):
                raise CalculationArtifactError("non_json_or_nonfinite_value")
            summaries[result_id] = {"kind": "scalar", "value": value}
        elif isinstance(value, list):
            canonical = _canonical_json(value)
            summaries[result_id] = {
                "kind": "array",
                "shape": _shape(value),
                "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "preview": _preview(value),
            }
        else:
            raise CalculationArtifactError("unsupported_stored_result")
    stored_canonical = _canonical_json(dict(results))
    return {
        "schema_version": PUBLIC_RESULT_SCHEMA_VERSION,
        "status": "succeeded",
        "operation_count": response.get("operation_count"),
        "calculation_id": calculation_id,
        "stored_results_sha256": hashlib.sha256(
            stored_canonical.encode("utf-8")
        ).hexdigest(),
        "results": summaries,
    }


class _Assembler:
    def __init__(
        self,
        calculations: Mapping[str, Mapping[str, Any]],
        checkpoints: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self._calculations = calculations
        self._checkpoints = checkpoints
        self.reference_count = 0
        self.checkpoint_reference_count = 0

    @staticmethod
    def _check_depth(depth: int) -> None:
        if depth > MAX_ASSEMBLY_DEPTH:
            raise CalculationArtifactError("assembly_nesting_too_deep")

    def _resolve_calculation(self, selector: Any) -> Any:
        if not isinstance(selector, Mapping):
            raise CalculationArtifactError("calculation_selector_object_required")
        allowed = {"calculation_id", "result_id", "index", "indices", "column"}
        if not set(selector).issubset(allowed) or not {
            "calculation_id",
            "result_id",
        }.issubset(selector):
            raise CalculationArtifactError("invalid_calculation_selector_fields")
        if "index" in selector and "indices" in selector:
            raise CalculationArtifactError("conflicting_calculation_selectors")
        calculation_id = selector.get("calculation_id")
        result_id = selector.get("result_id")
        if (
            type(calculation_id) is not str
            or _CALCULATION_ID_RE.fullmatch(calculation_id) is None
            or type(result_id) is not str
            or _ID_RE.fullmatch(result_id) is None
        ):
            raise CalculationArtifactError("invalid_calculation_reference")
        calculation = self._calculations.get(calculation_id)
        if calculation is None or result_id not in calculation:
            raise CalculationArtifactError("unknown_calculation_reference")

        self.reference_count += 1
        if self.reference_count > MAX_ASSEMBLY_REFERENCES:
            raise CalculationArtifactError("too_many_calculation_references")
        value = deepcopy(calculation[result_id])

        if "column" in selector:
            column = selector.get("column")
            if type(column) is not int or column < 0:
                raise CalculationArtifactError("invalid_matrix_column")
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(row, list) or column >= len(row) for row in value)
            ):
                raise CalculationArtifactError("matrix_column_unavailable")
            value = [row[column] for row in value]

        if "indices" in selector:
            indices = selector.get("indices")
            if (
                not isinstance(indices, list)
                or not indices
                or len(indices) > MAX_ZIPPED_ROWS
                or any(type(index) is not int or index < 0 for index in indices)
            ):
                raise CalculationArtifactError("invalid_result_indices")
            if not isinstance(value, list) or any(index >= len(value) for index in indices):
                raise CalculationArtifactError("result_index_unavailable")
            value = [value[index] for index in indices]

        if "index" in selector:
            index = selector.get("index")
            if type(index) is not int or index < 0:
                raise CalculationArtifactError("invalid_result_index")
            if not isinstance(value, list) or index >= len(value):
                raise CalculationArtifactError("result_index_unavailable")
            value = value[index]
        return value

    def _resolve_checkpoint(self, checkpoint_name: Any) -> dict[str, Any]:
        if type(checkpoint_name) is not str or checkpoint_name not in _CHECKPOINT_NAMES:
            raise CalculationArtifactError("invalid_checkpoint_reference")
        checkpoint = self._checkpoints.get(checkpoint_name)
        if not isinstance(checkpoint, Mapping):
            raise CalculationArtifactError("unknown_checkpoint_reference")
        self.checkpoint_reference_count += 1
        if self.checkpoint_reference_count > MAX_CHECKPOINT_REFERENCES:
            raise CalculationArtifactError("too_many_checkpoint_references")
        return deepcopy(dict(checkpoint))

    def materialize(self, value: Any, *, depth: int = 0) -> Any:
        self._check_depth(depth)
        if isinstance(value, Mapping):
            reserved = _RESERVED_KEYS.intersection(value)
            if reserved:
                if set(value) == {"$calc"}:
                    return self._resolve_calculation(value["$calc"])
                if set(value) == {"$checkpoint"}:
                    return self._resolve_checkpoint(value["$checkpoint"])
                if set(value) == {"$zip_rows"}:
                    return self._zip_rows(value["$zip_rows"], depth=depth + 1)
                raise CalculationArtifactError("malformed_reserved_assembly_marker")
            if any(type(key) is not str for key in value):
                raise CalculationArtifactError("json_object_key_must_be_string")
            return {
                key: self.materialize(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.materialize(item, depth=depth + 1) for item in value]
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise CalculationArtifactError("unsupported_assembly_value")

    def _literal_value(self, value: Any, *, depth: int) -> Any:
        """Copy ordinary JSON for ``$repeat`` without resolving any marker."""

        self._check_depth(depth)
        if isinstance(value, Mapping):
            if _RESERVED_KEYS.intersection(value):
                raise CalculationArtifactError("repeat_requires_literal_array")
            if any(type(key) is not str for key in value):
                raise CalculationArtifactError("json_object_key_must_be_string")
            return {
                key: self._literal_value(item, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self._literal_value(item, depth=depth + 1) for item in value]
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise CalculationArtifactError("unsupported_assembly_value")

    @staticmethod
    def _check_projected_rows(columns: Mapping[str, _Column], length: int) -> None:
        """Reject an oversized row expansion before allocating all row copies."""

        per_row = 2 + max(0, len(columns) - 1)
        array_bytes = 0
        for key, column in columns.items():
            per_row += len(_canonical_json(key).encode("utf-8")) + 1
            rendered = len(_canonical_json(column.value).encode("utf-8"))
            if column.length is None:
                per_row += rendered
            else:
                array_bytes += rendered
        projected = 2 + length * per_row + array_bytes
        if projected > MAX_ASSEMBLED_ARTIFACT_BYTES:
            raise CalculationArtifactError("assembled_artifact_too_large")

    def _column(self, value: Any, *, depth: int) -> _Column:
        self._check_depth(depth)
        if isinstance(value, Mapping):
            reserved = _RESERVED_KEYS.intersection(value)
            if reserved:
                if set(value) == {"$calc"}:
                    resolved = self._resolve_calculation(value["$calc"])
                    return _Column(resolved, len(resolved) if isinstance(resolved, list) else None)
                if set(value) == {"$repeat"}:
                    repeated_source = value["$repeat"]
                    if not isinstance(repeated_source, list):
                        raise CalculationArtifactError("repeat_requires_literal_array")
                    repeated = self._literal_value(
                        repeated_source,
                        depth=depth + 1,
                    )
                    return _Column(repeated, None)
                raise CalculationArtifactError("invalid_row_column_marker")
            if any(type(key) is not str for key in value):
                raise CalculationArtifactError("json_object_key_must_be_string")
            columns = {
                key: self._column(item, depth=depth + 1)
                for key, item in value.items()
            }
            length = self._common_length(columns.values())
            if length is None:
                return _Column({key: column.value for key, column in columns.items()}, None)
            self._check_projected_rows(columns, length)
            return _Column(
                [
                    {
                        key: column.value[index] if column.length is not None else deepcopy(column.value)
                        for key, column in columns.items()
                    }
                    for index in range(length)
                ],
                length,
            )
        if isinstance(value, list):
            if not value or len(value) > MAX_ZIPPED_ROWS:
                raise CalculationArtifactError("invalid_literal_row_column")
            materialized = [self.materialize(item, depth=depth + 1) for item in value]
            return _Column(materialized, len(materialized))
        if value is None or type(value) in (str, int, float, bool):
            return _Column(value, None)
        raise CalculationArtifactError("unsupported_row_column")

    @staticmethod
    def _common_length(columns: Any) -> int | None:
        lengths = {column.length for column in columns if column.length is not None}
        if not lengths:
            return None
        if len(lengths) != 1:
            raise CalculationArtifactError("row_column_length_mismatch")
        length = lengths.pop()
        if not 1 <= length <= MAX_ZIPPED_ROWS:
            raise CalculationArtifactError("invalid_zipped_row_count")
        return length

    def _zip_rows(self, fields: Any, *, depth: int) -> list[dict[str, Any]]:
        if not isinstance(fields, Mapping) or not fields:
            raise CalculationArtifactError("zip_rows_fields_required")
        if any(type(key) is not str or not key for key in fields):
            raise CalculationArtifactError("invalid_zip_rows_field")
        columns = {
            key: self._column(value, depth=depth + 1)
            for key, value in fields.items()
        }
        length = self._common_length(columns.values())
        if length is None:
            raise CalculationArtifactError("zip_rows_requires_array_column")
        self._check_projected_rows(columns, length)
        return [
            {
                key: column.value[index] if column.length is not None else deepcopy(column.value)
                for key, column in columns.items()
            }
            for index in range(length)
        ]


def assemble_calculation_document(
    template: Mapping[str, Any],
    calculations: Mapping[str, Mapping[str, Any]],
    *,
    checkpoints: Mapping[str, Mapping[str, Any]] | None = None,
) -> AssembledArtifact:
    """Materialize one bounded top-level JSON object from stored results."""

    if not isinstance(template, Mapping):
        raise CalculationArtifactError("artifact_template_object_required")
    raw_template = _canonical_json(template)
    if len(raw_template.encode("utf-8")) > MAX_ASSEMBLY_SPEC_BYTES:
        raise CalculationArtifactError("artifact_template_too_large")
    if not isinstance(calculations, Mapping):
        raise CalculationArtifactError("stored_calculations_required")
    if checkpoints is not None and not isinstance(checkpoints, Mapping):
        raise CalculationArtifactError("stored_checkpoints_invalid")

    assembler = _Assembler(calculations, checkpoints or {})
    document = assembler.materialize(dict(template))
    if not isinstance(document, dict):
        raise CalculationArtifactError("assembled_document_object_required")
    _canonical_json(document)
    content = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_ASSEMBLED_ARTIFACT_BYTES:
        raise CalculationArtifactError("assembled_artifact_too_large")
    return AssembledArtifact(
        document=document,
        content=content,
        byte_count=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        reference_count=assembler.reference_count,
        checkpoint_reference_count=assembler.checkpoint_reference_count,
    )
