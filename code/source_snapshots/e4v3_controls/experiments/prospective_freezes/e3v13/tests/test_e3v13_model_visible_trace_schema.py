from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA_PATH = Path(__file__).parents[1] / "model_visible_trace_schema_v1.json"


def _validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_accepts_redacted_visible_output_record() -> None:
    _validator().validate(
        {
            "schema_version": "model-visible-transcript-v1",
            "record_type": "model_visible_output",
            "architecture_mode": "single_agent",
            "role": "developer",
            "phase": "single_agent_developer",
            "attempt": 1,
            "output_sha256": "a" * 64,
            "output_char_count": 19,
            "output": {"answer": "visible"},
        }
    )


def test_rejects_record_with_prompt_or_secret_field() -> None:
    errors = list(
        _validator().iter_errors(
            {
                "schema_version": "model-visible-transcript-v1",
                "record_type": "model_visible_output",
                "architecture_mode": "single_agent",
                "role": "developer",
                "phase": "single_agent_developer",
                "attempt": 1,
                "output_sha256": "a" * 64,
                "output_char_count": 19,
                "output": {"answer": "visible"},
                "prompt": "must not be captured",
            }
        )
    )
    assert errors
