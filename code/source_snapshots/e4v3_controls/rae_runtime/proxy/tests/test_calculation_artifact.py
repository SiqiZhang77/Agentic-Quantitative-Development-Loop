from __future__ import annotations

import json
import hashlib
import math

import pytest

from calculation_artifact import (
    CalculationArtifactError,
    assemble_calculation_document,
    summarize_calculation_response,
)


def _calculations() -> dict:
    return {
        "calculation-1": {
            "path": [float(index) / 1000.0 for index in range(120)],
            "matrix": [
                [float(index), float(index) + 0.25, float(index) + 0.5]
                for index in range(120)
            ],
            "terminal": 1.2345,
        }
    }


def test_large_results_are_retained_by_reference_not_returned_in_full() -> None:
    values = [float(index) + 0.123456789 for index in range(120)]
    response = summarize_calculation_response(
        {
            "status": "succeeded",
            "operation_count": 2,
            "results": {"series": values, "terminal": values[-1]},
        },
        "calculation-1",
    )

    assert response["schema_version"] == "quant-calculate-mcp-response-v2"
    assert response["calculation_id"] == "calculation-1"
    assert response["results"]["series"]["shape"] == [120]
    assert response["results"]["terminal"] == {
        "kind": "scalar",
        "value": values[-1],
    }
    rendered = json.dumps(response, sort_keys=True)
    assert str(values[60]) not in rendered
    assert len(response["results"]["series"]["sha256"]) == 64
    assert len(response["stored_results_sha256"]) == 64


def test_assembles_120_rows_nested_objects_subsets_and_scalars() -> None:
    dates = [f"2026-01-{(index % 28) + 1:02d}-{index:03d}" for index in range(120)]
    template = {
        "schema_version": "example-v1",
        "rows": {
            "$zip_rows": {
                "date": dates,
                "value": {
                    "$calc": {
                        "calculation_id": "calculation-1",
                        "result_id": "path",
                    }
                },
                "weights": {
                    "a": {
                        "$calc": {
                            "calculation_id": "calculation-1",
                            "result_id": "matrix",
                            "column": 1,
                        }
                    },
                    "b": {
                        "$calc": {
                            "calculation_id": "calculation-1",
                            "result_id": "matrix",
                            "column": 2,
                        }
                    },
                },
                "constant_vector": {"$repeat": [0.4, 0.6]},
            }
        },
        "events": {
            "$zip_rows": {
                "date": [dates[0], dates[59], dates[119]],
                "nav": {
                    "$calc": {
                        "calculation_id": "calculation-1",
                        "result_id": "matrix",
                        "column": 0,
                        "indices": [0, 59, 119],
                    }
                },
            }
        },
        "last_path_value": {
            "$calc": {
                "calculation_id": "calculation-1",
                "result_id": "path",
                "index": 119,
            }
        },
        "terminal": {
            "$calc": {
                "calculation_id": "calculation-1",
                "result_id": "terminal",
            }
        },
    }

    artifact = assemble_calculation_document(template, _calculations())

    assert len(artifact.document["rows"]) == 120
    assert artifact.document["rows"][59] == {
        "date": dates[59],
        "value": 0.059,
        "weights": {"a": 59.25, "b": 59.5},
        "constant_vector": [0.4, 0.6],
    }
    assert [item["nav"] for item in artifact.document["events"]] == [0.0, 59.0, 119.0]
    assert artifact.document["last_path_value"] == 0.119
    assert artifact.document["terminal"] == 1.2345
    assert json.loads(artifact.content) == artifact.document
    assert artifact.byte_count > 0
    assert len(artifact.sha256) == 64
    assert artifact.sha256 == hashlib.sha256(artifact.content.encode("utf-8")).hexdigest()
    assert artifact.reference_count == 6
    assert artifact.checkpoint_reference_count == 0


def test_saved_sections_can_be_assembled_into_a_full_document() -> None:
    artifact = assemble_calculation_document(
        {
            "schema_version": "example-v1",
            "level_a": {"$checkpoint": "level_a"},
            "level_b": {"$checkpoint": "level_b"},
        },
        {},
        checkpoints={
            "level_a": {"rows": [{"value": 1.0}]},
            "level_b": {"metric": 2.0},
        },
    )

    assert artifact.document["level_a"] == {"rows": [{"value": 1.0}]}
    assert artifact.document["level_b"] == {"metric": 2.0}
    assert artifact.reference_count == 0
    assert artifact.checkpoint_reference_count == 2


@pytest.mark.parametrize(
    ("document", "error_code"),
    [
        (
            {
                "value": {
                    "$calc": {
                        "calculation_id": "calculation-1",
                        "result_id": "missing",
                    }
                }
            },
            "unknown_calculation_reference",
        ),
        (
            {
                "rows": {
                    "$zip_rows": {
                        "a": [1, 2],
                        "b": [1, 2, 3],
                    }
                }
            },
            "row_column_length_mismatch",
        ),
        (
            {"value": {"$calc": {}, "extra": 1}},
            "malformed_reserved_assembly_marker",
        ),
        (
            {
                "value": {
                    "$calc": {
                        "calculation_id": "calculation-1",
                        "result_id": "path",
                        "index": 120,
                    }
                }
            },
            "result_index_unavailable",
        ),
        (
            {"level_a": {"$checkpoint": "level_a"}},
            "unknown_checkpoint_reference",
        ),
    ],
)
def test_invalid_assembly_specs_fail_closed(document: dict, error_code: str) -> None:
    with pytest.raises(CalculationArtifactError, match=error_code):
        assemble_calculation_document(document, _calculations())


def test_nonfinite_values_cannot_enter_an_artifact() -> None:
    calculations = {"calculation-1": {"bad": math.inf}}
    document = {
        "value": {
            "$calc": {
                "calculation_id": "calculation-1",
                "result_id": "bad",
            }
        }
    }

    with pytest.raises(CalculationArtifactError, match="non_json_or_nonfinite_value"):
        assemble_calculation_document(document, calculations)


def test_repeat_accepts_only_a_literal_array_not_a_calculation_marker() -> None:
    document = {
        "rows": {
            "$zip_rows": {
                "value": [1, 2],
                "repeated": {
                    "$repeat": [
                        {
                            "$calc": {
                                "calculation_id": "calculation-1",
                                "result_id": "path",
                            }
                        }
                    ]
                },
            }
        }
    }

    with pytest.raises(CalculationArtifactError, match="repeat_requires_literal_array"):
        assemble_calculation_document(document, _calculations())


def test_row_expansion_is_rejected_before_it_can_exceed_the_artifact_cap() -> None:
    document = {
        "rows": {
            "$zip_rows": {
                "index": list(range(512)),
                "repeated": {"$repeat": ["x" * 3000]},
            }
        }
    }

    with pytest.raises(CalculationArtifactError, match="assembled_artifact_too_large"):
        assemble_calculation_document(document, _calculations())


def test_template_depth_reference_count_and_input_bytes_are_bounded() -> None:
    nested: dict = {"value": 1}
    for _ in range(18):
        nested = {"nested": nested}
    with pytest.raises(CalculationArtifactError, match="assembly_nesting_too_deep"):
        assemble_calculation_document(nested, _calculations())

    selector = {
        "$calc": {
            "calculation_id": "calculation-1",
            "result_id": "terminal",
        }
    }
    with pytest.raises(CalculationArtifactError, match="too_many_calculation_references"):
        assemble_calculation_document({"values": [selector] * 513}, _calculations())

    with pytest.raises(CalculationArtifactError, match="artifact_template_too_large"):
        assemble_calculation_document({"value": "x" * 100_000}, _calculations())
