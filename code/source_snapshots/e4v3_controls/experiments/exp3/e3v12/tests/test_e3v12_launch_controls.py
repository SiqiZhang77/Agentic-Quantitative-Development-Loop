from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator, FormatChecker

from experiments.exp3.e3v12 import build_e3v12_launch_package as launch
from experiments.exp3.e3v12 import controls
from experiments.exp3.e3v12 import run_e3v12_observation as observation


ROOT = controls.REPOSITORY_ROOT


def _resolved_config() -> dict:
    config = controls.load_config()
    config["source"]["source_ref"] = "exp/shared-t3-runtime-v3"
    config["source"]["source_commit"] = "b" * 40
    config["runtime"].update(
        {
            "image_ref": f"exp-shared-t3-runtime@sha256:{'d' * 64}",
            "image_digest": f"sha256:{'d' * 64}",
            "provider_identity_sha256": "1" * 64,
            "runtime_request_schema_sha256": "2" * 64,
        }
    )
    return config


def _identity(arm: str = "M0") -> dict:
    architecture = "single_agent" if arm == "M0" else "manager_star"
    paired_arm = "M1" if arm == "M0" else "M0"
    value = {
        "run_id": f"E3V12-T3-R1-{arm}",
        "replicate": 1,
        "sequence_in_pair": 1 if arm == "M0" else 2,
        "arm": arm,
        "architecture_mode": architecture,
        "target_ref": f"quant/E3V12-T3-R1-{arm}",
        "paired_target_ref": f"quant/E3V12-T3-R1-{paired_arm}",
        "source": {
            "source_ref": "exp/shared-t3-runtime-v3",
            "source_commit": "b" * 40,
        },
    }
    value["identity_sha256"] = controls.sha256_value(value)
    return value


def test_request_contains_complete_task_and_matches_runtime_schema() -> None:
    config = _resolved_config()
    task = (ROOT / config["task_suite"]["task_path"]).read_text(encoding="utf-8")
    request = launch._request(
        identity=_identity(), sequence=1, task_text=task, config=config
    )
    schema = json.loads(
        (ROOT / "rae_runtime/sandbox/schemas/runtime_request.schema.json").read_text(
            encoding="utf-8"
        )
    )

    errors = list(
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(request)
    )
    assert errors == []
    assert request["execution_objectives"]["parsed_task_parameters"]["objective"] == task
    assert request["iteration_controls"]["max_iterations"] == 2
    assert request["iteration_controls"]["max_token_budget_per_run"] == 1_000_000


def test_negative_manifest_matches_runtime_parser() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "rae_runtime/proxy"))
    try:
        from exp3.isolation import NegativeRefManifest
    finally:
        sys.path.pop(0)

    identity = _identity()
    other = _identity("M1")
    manifest, manifest_sha, deny_sha = launch._negative_manifest(
        identity, {identity["target_ref"], other["target_ref"]}
    )
    parsed = NegativeRefManifest.from_dict(manifest)

    assert parsed.sha256 == manifest_sha
    assert parsed.deny_ref_set_sha256 == deny_sha
    policy = parsed.policy_for("bankingscience/BSLAgenticQuantDevLoop")
    assert identity["paired_target_ref"] in policy.complete_deny_set


def test_launch_scripts_do_not_import_provider_clients() -> None:
    for name in (
        "build_e3v12_launch_package.py",
        "run_e3v12_observation.py",
        "run_e3v12_pair.py",
    ):
        text = (controls.HERE / name).read_text(encoding="utf-8")
        assert "from openai" not in text
        assert "import openai" not in text


def test_credential_helper_username_is_not_exported(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)
    monkeypatch.setattr(
        observation,
        "_credential_helper",
        lambda: ("114008358", "github-test-token"),
    )

    openai_key, github_token, github_username = observation._credentials()

    assert openai_key == "sk-test-only"
    assert github_token == "github-test-token"
    assert github_username is None


def test_explicit_github_username_is_preserved(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-only")
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-token")
    monkeypatch.setenv("GITHUB_USERNAME", "SiqiZhang77")

    assert observation._credentials() == (
        "sk-test-only",
        "github-test-token",
        "SiqiZhang77",
    )


def test_execute_creates_item_store_parent_before_container(
    monkeypatch, tmp_path: Path
) -> None:
    negative = tmp_path / "negative.json"
    negative.write_text("{}\n", encoding="utf-8")
    state = {
        "manifest": {"experiment_id": "E3V10"},
        "run_id": "E3V10-T3-R1-M0",
        "agreement_sha256": "a" * 64,
        "launch_package_sha256": "b" * 64,
        "target_ref": "quant/E3V10-T3-R1-M0",
        "architecture_mode": "single_agent",
        "image_digest": f"sha256:{'d' * 64}",
        "negative_path": negative,
        "selected": {
            "identity_sha256": "c" * 64,
            "negative_ref_manifest_sha256": "e" * 64,
            "negative_ref_set_sha256": "f" * 64,
        },
        "config": {
            "source": {"source_commit": "1" * 40},
            "runtime": {
                "provider_base_url": "https://api.openai.com/v1",
                "model_alias": "gpt-5.6-terra",
                "image_digest": f"sha256:{'d' * 64}",
            },
        },
        "request": {},
    }
    monkeypatch.setenv(
        "E3V10_RUN_AUTHORIZATION", "AUTHORIZE_E3V10-T3-R1-M0"
    )
    monkeypatch.setattr(
        observation,
        "_credentials",
        lambda: ("sk-test-only", "github-test-token", None),
    )

    def fake_run(*args, **kwargs):
        output_mount = next(
            value
            for index, value in enumerate(args[0])
            if args[0][index - 1] == "-v" and value.endswith(":/workspace/output:rw")
        )
        host_output = Path(output_mount.split(":/workspace/output:rw", 1)[0])
        assert (host_output / "artifacts").is_dir()
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(observation.subprocess, "run", fake_run)

    result = observation.execute(state, tmp_path / "observations")

    assert result["container_exit_code"] == 1
    assert Path(result["output_directory"]).joinpath("output/artifacts").is_dir()
