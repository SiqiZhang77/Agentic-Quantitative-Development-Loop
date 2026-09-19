from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _load(name: str, filename: str):
    path = ROOT / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRIVATE = _load("e3v13_private_bindings", "collect_private_evaluator_bindings.py")
FREEZE = _load("e3v13_freeze_config", "build_freeze_config.py")


def _public_inventory() -> dict:
    config = json.loads(FREEZE.PROSPECTIVE_CONFIG.read_text(encoding="utf-8"))
    suite = config["task_suite"]
    paths = [
        suite["task_path"],
        suite["rubric_path"],
        suite["item_submission_schema_path"],
        suite["output_schema_path"],
        suite["scorer_result_schema_path"],
        suite["scorer_result_validator_path"],
        *(item["path"] for item in suite["public_inputs"]),
    ]
    public_files = {
        relative: FREEZE._sha256_file(FREEZE.CONTROL_ROOT / relative)
        for relative in paths
    }
    return {
        "schema_version": "shared-t3-e3v13-public-binding-inventory-v1",
        "status": "public_bindings_verified_private_bindings_unresolved",
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "source": {
            "source_ref": "exp/shared-t3-runtime-v4",
            "source_commit": "a" * 40,
        },
        "runtime": {
            "image_ref": "exp-shared-t3-runtime@sha256:" + "b" * 64,
            "image_digest": "sha256:" + "b" * 64,
            "runtime_request_schema_sha256": "c" * 64,
            "runtime_response_schema_sha256": "d" * 64,
            "tool_implementation_sha256": "e" * 64,
            "execution_control_sha256": "9" * 64,
            "model_visible_tool_fingerprint_sha256": "f" * 64,
            "provider_identity_sha256": "1" * 64,
        },
        "public_files": public_files,
        "control": {
            "visible_response_trace_schema_sha256": "2" * 64,
        },
    }


def test_private_manifest_contains_hashes_but_no_paths_or_contents(tmp_path: Path) -> None:
    scorer = tmp_path / "private_scorer.py"
    base = tmp_path / "private_base.py"
    coordinator = tmp_path / "coordinator.py"
    scorer.write_text("private expected answer marker", encoding="utf-8")
    base.write_text("private base marker", encoding="utf-8")
    coordinator.write_text("coordinator marker", encoding="utf-8")

    result = PRIVATE.collect(
        private_item_scorer=scorer,
        private_base_scorer=base,
        evaluator_coordinator=coordinator,
    )
    rendered = json.dumps(result, sort_keys=True)

    assert result["provider_call_count"] == 0
    assert result["external_model_calls_made"] is False
    assert str(tmp_path) not in rendered
    assert "expected answer marker" not in rendered
    assert len(result["private_item_scorer_sha256"]) == 64


def test_freeze_config_resolves_only_reviewed_inventories(
    tmp_path: Path, monkeypatch
) -> None:
    public_path = tmp_path / "public.json"
    private_path = tmp_path / "private.json"
    public_path.write_text(json.dumps(_public_inventory()), encoding="utf-8")
    private_path.write_text(
        json.dumps(
            {
                "schema_version": "exp3-e3v13-private-evaluator-hashes-v1",
                "task_suite_version": "t3-quant-suite-v2",
                "private_item_scorer_sha256": "3" * 64,
                "private_base_scorer_sha256": "4" * 64,
                "evaluator_coordinator_sha256": "5" * 64,
                "provider_call_count": 0,
                "external_model_calls_made": False,
                "contains_private_paths": False,
                "contains_evaluator_source": False,
                "contains_candidate_or_expected_answers": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    relative = "experiments/exp3/e3v13/controls.py"
    files = {relative: FREEZE._sha256_file(FREEZE.CONTROL_ROOT / relative)}
    monkeypatch.setattr(
        FREEZE,
        "_control_binding",
        lambda control_ref, control_commit: {
            "repository": "bankingscience/BSLAgenticQuantDevLoop",
            "control_ref": control_ref,
            "control_commit": control_commit,
            "files_sha256": files,
            "files_fingerprint_sha256": FREEZE._sha256_bytes(
                FREEZE._canonical(files)
            ),
        },
    )

    result = FREEZE.build(
        public_inventory_path=public_path,
        private_manifest_path=private_path,
        control_ref="exp/e3v13-transfer-telemetry-controls",
        control_commit="6" * 40,
    )

    assert result["freeze_status"] == "ready_for_freeze"
    assert result["execution"]["agreement_generation_enabled"] is True
    assert result["execution"]["formal_model_execution_enabled"] is False
    assert result["evaluation"]["private_item_scorer_sha256"] == "3" * 64
    assert result["control"]["control_commit"] == "6" * 40
    assert not FREEZE._controls_module().unresolved_paths(result)


def test_freeze_config_rejects_manifest_that_contains_private_paths(
    tmp_path: Path,
) -> None:
    value = {
        "schema_version": "exp3-e3v13-private-evaluator-hashes-v1",
        "task_suite_version": "t3-quant-suite-v2",
        "private_item_scorer_sha256": "3" * 64,
        "private_base_scorer_sha256": "4" * 64,
        "evaluator_coordinator_sha256": "5" * 64,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "contains_private_paths": True,
        "contains_evaluator_source": False,
        "contains_candidate_or_expected_answers": False,
    }

    try:
        FREEZE._validate_private(value)
    except FREEZE.FreezeConfigError as exc:
        assert "contains_private_paths" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unsafe private manifest was accepted")
