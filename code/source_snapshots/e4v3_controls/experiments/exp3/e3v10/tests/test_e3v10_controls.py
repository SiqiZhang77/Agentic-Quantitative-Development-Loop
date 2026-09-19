from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from experiments.exp3.e3v9 import controls
from experiments.exp3.e3v10 import build_e3v10_launch_package as launch
from experiments.exp3.e3v10 import build_e3v10_replacement as replacement


def _identity(arm: str = "M0") -> dict:
    paired = "M1" if arm == "M0" else "M0"
    value = {
        "run_id": f"E3V10-T3-R1-{arm}",
        "replicate": 1,
        "sequence_in_pair": 1 if arm == "M0" else 2,
        "architecture_mode": "single_agent" if arm == "M0" else "manager_star",
        "target_ref": f"quant/E3V10-T3-R1-{arm}",
        "paired_target_ref": f"quant/E3V10-T3-R1-{paired}",
        "source": {"source_ref": "exp/shared-t3-runtime-v1"},
    }
    value["identity_sha256"] = controls.sha256_value(value)
    return value


def _config() -> dict:
    value = controls.load_config()
    value["source"].update(
        {"source_ref": "exp/shared-t3-runtime-v1", "source_commit": "b" * 40}
    )
    value["runtime"].update(
        {
            "provider_identity_sha256": "1" * 64,
            "runtime_request_schema_sha256": "2" * 64,
        }
    )
    return value


def test_e3v10_request_uses_new_identity_and_runtime_schema() -> None:
    config = _config()
    task = (controls.REPOSITORY_ROOT / config["task_suite"]["task_path"]).read_text(
        encoding="utf-8"
    )
    request = launch.common._request(
        identity=_identity(),
        sequence=1,
        task_text=task,
        config=config,
        experiment_id="E3V10",
    )
    schema = json.loads(
        (
            controls.REPOSITORY_ROOT
            / "rae_runtime/sandbox/schemas/runtime_request.schema.json"
        ).read_text(encoding="utf-8")
    )

    assert list(
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(request)
    ) == []
    assert request["run_id"] == "E3V10-T3-R1-M0"
    assert request["jira_metadata"]["ticket_id"] == "E3V10-001"


def test_e3v10_negative_manifest_denies_all_e3v9_targets() -> None:
    first = _identity("M0")
    second = _identity("M1")
    manifest, _, _ = launch.common._negative_manifest(
        first,
        {first["target_ref"], second["target_ref"]},
        experiment_id="E3V10",
        prior_study_refs=launch.E3V9_TARGETS,
    )

    prior = manifest["repositories"][0]["deny_refs"]["prior_study_refs"]
    assert set(prior) == set(launch.E3V9_TARGETS)
    assert manifest["experiment_id"] == "E3V10"


def test_zero_call_failure_requires_empty_model_usage(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    base = {
        "run_id": "E3V9-T3-R1-M0",
        "execution_summary": {"status": "failed"},
        "generated_artifacts": {"modified_files": [], "new_files": []},
        "diagnostics": {"error_message": "IsolationPreflightError: blocked"},
        "telemetry": {"model_usage": []},
    }
    path.write_text(json.dumps(base), encoding="utf-8")
    assert replacement._validate_zero_call_failure(path)["run_id"].endswith("M0")

    base["telemetry"]["model_usage"] = [{"calls": 1}]
    path.write_text(json.dumps(base), encoding="utf-8")
    with pytest.raises(replacement.ReplacementError):
        replacement._validate_zero_call_failure(path)


def test_replacement_version_changes_only_versioned_strings() -> None:
    value = {
        "run_id": "E3V9-T3-R1-M0",
        "path": "experiments/exp3/e3v9/run_e3v9_pair.py",
        "budget": 700000,
    }

    assert replacement._replace_version(value) == {
        "run_id": "E3V10-T3-R1-M0",
        "path": "experiments/exp3/e3v10/run_e3v10_pair.py",
        "budget": 700000,
    }
