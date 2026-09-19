"""Strict, content-safe contracts for the three Experiment 3 handoffs.

The handoff body is validated locally against a version-specific Draft 2020-12
schema.  Canonical JSON is used for both character limits and hashing so the
same logical object has the same audit identity regardless of dict insertion
order.  Audit records deliberately contain no handoff body.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


@dataclass(frozen=True)
class _HandoffSpec:
    filename: str
    producer: str
    char_limit: int | None


_HANDOFF_SPECS = {
    "architect_plan_v1": _HandoffSpec(
        filename="architect_plan_v1.schema.json",
        producer="architect",
        char_limit=12_000,
    ),
    "developer_result_v1": _HandoffSpec(
        filename="developer_result_v1.schema.json",
        producer="developer",
        char_limit=24_000,
    ),
    "manager_final_v1": _HandoffSpec(
        filename="manager_final_v1.schema.json",
        producer="manager",
        char_limit=None,
    ),
}


class HandoffValidationError(ValueError):
    """A handoff failed a version, producer, schema, or size boundary.

    Reasons intentionally identify only the contract rule and JSON path.  They
    never include values from the handoff body, so callers may safely record the
    exception without leaking private prompt or result text.
    """

    def __init__(self, expected_schema: str, reasons: list[str] | tuple[str, ...]):
        self.expected_schema = expected_schema
        self.reasons = tuple(reasons)
        summary = "; ".join(self.reasons) or "unknown contract violation"
        super().__init__(f"{expected_schema} handoff validation failed: {summary}")


@dataclass(frozen=True)
class ValidatedHandoff:
    """Validated handoff plus its stable, text-free audit identity."""

    version: str
    producer: str
    hash: str
    char_count: int
    _canonical: str = field(repr=False, compare=False)

    @property
    def canonical(self) -> str:
        """Return the canonical body for the next in-process handoff consumer."""

        return self._canonical

    @property
    def value(self) -> dict[str, Any]:
        """Return a fresh JSON copy so callers cannot mutate stored evidence."""

        parsed = json.loads(self._canonical)
        if not isinstance(parsed, dict):  # Defensive; schemas require an object.
            raise RuntimeError("validated handoff canonical form is not an object")
        return parsed

    @property
    def sha256(self) -> str:
        """Explicit alias for callers that label the digest algorithm."""

        return self.hash

    def audit_record(self) -> dict[str, str | int]:
        """Return only non-content audit metadata."""

        return {
            "version": self.version,
            "hash": self.hash,
            "char_count": self.char_count,
            "producer": self.producer,
        }


def canonical_json(value: Any) -> str:
    """Serialize a JSON-compatible value using the frozen E3 canonical form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Return SHA256 over the UTF-8 bytes of :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@lru_cache(maxsize=None)
def _validator(expected_schema: str) -> Draft202012Validator:
    spec = _HANDOFF_SPECS[expected_schema]
    schema = json.loads((_SCHEMA_DIR / spec.filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_error_reason(error: Any) -> str:
    path = ".".join(str(part) for part in error.absolute_path) or "<root>"
    keyword = str(error.validator or "schema")
    return f"{path}: violates {keyword}"


def validate_handoff(
    value: Any,
    expected_schema: str,
    producer_role: str,
) -> ValidatedHandoff:
    """Validate, canonicalize, bound, and hash one E3 handoff.

    ``expected_schema`` and ``producer_role`` come from the fixed router edge,
    not from model output.  The input object is never modified.
    """

    spec = _HANDOFF_SPECS.get(expected_schema)
    if spec is None:
        raise HandoffValidationError(expected_schema, ["unsupported handoff version"])
    if producer_role != spec.producer:
        raise HandoffValidationError(
            expected_schema,
            ["caller producer role does not match the fixed handoff producer"],
        )
    if not isinstance(value, dict):
        raise HandoffValidationError(expected_schema, ["<root>: violates type"])

    try:
        canonical = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise HandoffValidationError(
            expected_schema,
            ["<root>: is not canonical JSON-compatible"],
        ) from exc

    errors = sorted(
        _validator(expected_schema).iter_errors(value),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            str(error.validator),
        ),
    )
    if errors:
        raise HandoffValidationError(
            expected_schema,
            [_schema_error_reason(error) for error in errors],
        )

    char_count = len(canonical)
    if spec.char_limit is not None and char_count > spec.char_limit:
        raise HandoffValidationError(
            expected_schema,
            [
                f"canonical char count {char_count} exceeds limit "
                f"{spec.char_limit}"
            ],
        )

    return ValidatedHandoff(
        version=expected_schema,
        producer=producer_role,
        hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        char_count=char_count,
        _canonical=canonical,
    )
