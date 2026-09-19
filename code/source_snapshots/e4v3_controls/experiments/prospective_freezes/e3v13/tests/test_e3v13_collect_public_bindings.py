from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "collect_public_bindings.py"
SPEC = importlib.util.spec_from_file_location("collect_public_bindings", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def _source_tree(tmp_path: Path) -> Path:
    for relative in (
        *collector.PUBLIC_FILES,
        *collector.RUNTIME_FILES,
        *collector.EXECUTION_CONTROL_FILES,
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    return tmp_path


def test_collect_is_public_only_and_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _source_tree(tmp_path)
    monkeypatch.setattr(collector, "_clean_commit", lambda *_: None)
    monkeypatch.setattr(collector, "_image_id", lambda _: "sha256:" + "a" * 64)

    result = collector.collect(
        source_tree=root,
        source_ref="exp/shared-t3-runtime-v4",
        source_commit="b" * 40,
        image_ref="example@sha256:" + "a" * 64,
        image_lookup_ref="sha256:" + "a" * 64,
    )

    assert result["status"] == "public_bindings_verified_private_bindings_unresolved"
    assert result["provider_call_count"] == 0
    assert result["external_model_calls_made"] is False
    assert len(result["public_files"]) == 8
    assert result["runtime"]["image_digest"] == "sha256:" + "a" * 64
    assert result["runtime"]["image_lookup_ref"] == "sha256:" + "a" * 64
    assert len(result["runtime"]["execution_control_sha256"]) == 64
    assert set(result["runtime"]["execution_control_files"]) == set(
        collector.EXECUTION_CONTROL_FILES
    )
    assert result["control"]["visible_response_trace_schema_path"] == collector.TRACE_SCHEMA_PATH
    assert len(result["control"]["visible_response_trace_schema_sha256"]) == 64
    assert "private evaluator hash-only manifest" in result["still_required_before_agreement"][0]


def test_collect_rejects_missing_public_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _source_tree(tmp_path)
    (root / collector.PUBLIC_FILES[0]).unlink()
    monkeypatch.setattr(collector, "_clean_commit", lambda *_: None)

    with pytest.raises(collector.BindingError, match="required public file is missing"):
        collector.collect(
            source_tree=root,
            source_ref="exp/shared-t3-runtime-v4",
            source_commit="b" * 40,
            image_ref="example:fixed",
        )


def test_collect_rejects_lookup_id_that_differs_from_canonical_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _source_tree(tmp_path)
    monkeypatch.setattr(collector, "_clean_commit", lambda *_: None)
    monkeypatch.setattr(collector, "_image_id", lambda _: "sha256:" + "c" * 64)

    with pytest.raises(collector.BindingError, match="Docker image ID mismatch"):
        collector.collect(
            source_tree=root,
            source_ref="exp/shared-t3-runtime-v4",
            source_commit="b" * 40,
            image_ref="example@sha256:" + "a" * 64,
            image_lookup_ref="sha256:" + "c" * 64,
        )
