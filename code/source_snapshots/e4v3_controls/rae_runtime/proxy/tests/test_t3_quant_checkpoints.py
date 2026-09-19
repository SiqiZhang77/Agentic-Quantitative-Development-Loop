from __future__ import annotations

import json
from pathlib import Path

import pytest

from t3_quant_checkpoints import (
    T3QuantCheckpointError,
    checkpoint_path_from_audit,
    load_checkpoint_sections,
    save_checkpoint_section,
)


def test_checkpoint_path_is_next_to_the_content_free_audit(tmp_path: Path) -> None:
    path = checkpoint_path_from_audit(str(tmp_path / "mcp-audit.jsonl"))

    assert path == tmp_path / "t3_quant_checkpoints.json"
    assert load_checkpoint_sections(path) == {}


def test_sections_are_saved_atomically_and_can_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "t3_quant_checkpoints.json"

    first = save_checkpoint_section(
        path,
        section="level_a",
        document={"rows": [{"value": 1.0}]},
        calculation_reference_count=1,
    )
    save_checkpoint_section(
        path,
        section="level_b",
        document={"metric": 2.0},
        calculation_reference_count=2,
    )
    replacement = save_checkpoint_section(
        path,
        section="level_a",
        document={"rows": [{"value": 3.0}]},
        calculation_reference_count=1,
    )

    assert first["saved_sections"] == ["level_a"]
    assert replacement["saved_sections"] == ["level_a", "level_b"]
    assert load_checkpoint_sections(path) == {
        "level_a": {"rows": [{"value": 3.0}]},
        "level_b": {"metric": 2.0},
    }
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("section", "document", "references", "code"),
    [
        ("level_d", {"value": 1.0}, 1, "checkpoint_section_invalid"),
        ("level_a", {"value": float("inf")}, 1, "checkpoint_non_json_or_nonfinite"),
        ("level_a", {"value": 1.0}, 0, "checkpoint_reference_count_invalid"),
    ],
)
def test_invalid_checkpoint_inputs_fail_closed(
    tmp_path: Path,
    section: str,
    document: dict,
    references: int,
    code: str,
) -> None:
    with pytest.raises(T3QuantCheckpointError, match=code):
        save_checkpoint_section(
            tmp_path / "t3_quant_checkpoints.json",
            section=section,
            document=document,
            calculation_reference_count=references,
        )


def test_tampered_checkpoint_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "t3_quant_checkpoints.json"
    save_checkpoint_section(
        path,
        section="level_a",
        document={"value": 1.0},
        calculation_reference_count=1,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sections"]["level_a"]["document"]["value"] = 999.0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(T3QuantCheckpointError, match="checkpoint_record_invalid"):
        load_checkpoint_sections(path)
