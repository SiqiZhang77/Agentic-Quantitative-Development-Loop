from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_agreed_observation.py"
SPEC = importlib.util.spec_from_file_location("run_agreed_observation", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

T3_BUILDER_PATH = Path(__file__).resolve().parents[1] / "build_t3_quant_agreement.py"
T3_SPEC = importlib.util.spec_from_file_location(
    "build_t3_quant_agreement_for_launcher_test", T3_BUILDER_PATH
)
assert T3_SPEC is not None and T3_SPEC.loader is not None
T3_BUILDER = importlib.util.module_from_spec(T3_SPEC)
T3_SPEC.loader.exec_module(T3_BUILDER)


def test_launcher_defaults_to_v2_epoch() -> None:
    assert MODULE.DEFAULT_AGREEMENT_DIR.name == "agreement-terra-v2"
    assert MODULE.EXPECTED_EXECUTION_EPOCH == "E3V2"


def test_launcher_accepts_only_the_exact_original_and_t3_amendment_profiles() -> None:
    original = {
        "schema_version": "exp3-terra-agreement-v2",
        "execution_epoch": "E3V2",
        "formal_run_count": 18,
    }
    assert MODULE._agreement_profile(original)["kind"] == "original_e3v2"

    t3 = {
        "schema_version": "exp3-t3-quant-agreement-v1",
        "execution_epoch": "E3V2T3Q1",
        "scope": "prospective_T3_only",
        "formal_run_count": 6,
        "target_ref_prefix": "quant/E3V2T3Q1-",
        "tool_profile": dict(MODULE.T3_QUANT_TOOL_PROFILE),
        "runs": [{"task_id": "T3"} for _ in range(6)],
    }
    assert MODULE._agreement_profile(t3)["kind"] == "t3_quant_v1"

    t3["tool_profile"]["max_attempted_calls"] = 5
    try:
        MODULE._agreement_profile(t3)
    except MODULE.LaunchError as exc:
        assert "tool profile" in str(exc)
    else:
        raise AssertionError("expected widened calculator profile to fail closed")


def test_launcher_rejects_unknown_agreement_schema() -> None:
    try:
        MODULE._agreement_profile({"schema_version": "future-unapproved-v9"})
    except MODULE.LaunchError as exc:
        assert "not an approved" in str(exc)
    else:
        raise AssertionError("expected unknown agreement to fail closed")


def test_generated_t3_agreement_reaches_zero_call_launcher_preflight(
    tmp_path: Path,
) -> None:
    source_commit = "a" * 40
    image_digest = "sha256:" + "b" * 64
    agreement_dir = tmp_path / "agreement"
    T3_BUILDER.build(
        agreement_dir,
        source_ref=T3_BUILDER.DEFAULT_SOURCE_REF,
        source_commit=source_commit,
        image_digest=image_digest,
        remote_heads={
            T3_BUILDER.DEFAULT_SOURCE_REF: source_commit,
            "main": "1" * 40,
            "V5-reference": "2" * 40,
            "experiment-2-data": "3" * 40,
            "quant/E2-T1-C0": "4" * 40,
            "quant/E3V2-T2-R1-M0": "5" * 40,
        },
    )

    def remote_head(ref: str):
        if ref == T3_BUILDER.DEFAULT_SOURCE_REF:
            return source_commit
        return None

    with patch.object(MODULE, "_remote_head", side_effect=remote_head), patch.object(
        MODULE,
        "_image_ref",
        return_value=f"{MODULE.IMAGE_REPOSITORY}@{image_digest}",
    ):
        state = MODULE.preflight(agreement_dir, "E3-T3-R1-M0")

    assert state["agreement_profile"]["kind"] == "t3_quant_v1"
    assert state["selected"]["target_ref"] == "quant/E3V2T3Q1-T3-R1-M0"
    parameters = state["request"]["execution_objectives"][
        "parsed_task_parameters"
    ]
    assert parameters["quant_calculator_enabled"] is True
    assert parameters["rag_enabled"] is False


def test_docker_command_attaches_stdin_without_allocating_tty(tmp_path: Path) -> None:
    image_ref = "exp3-manager-star-formal@sha256:" + "a" * 64
    image_digest = "sha256:" + "a" * 64
    state = {
        "agreement": {"image_digest": image_digest},
        "selected": {
            "manifest_sha256": "b" * 64,
            "deny_ref_set_sha256": "c" * 64,
            "identity_sha256": "d" * 64,
        },
        "image_ref": image_ref,
    }

    command = MODULE._docker_run_command(
        state=state,
        input_dir=tmp_path / "input",
        result_dir=tmp_path / "output",
        environment_names=["OPENAI_API_KEY"],
    )

    assert command[:3] == ["docker", "run", "--rm"]
    assert "-i" in command[: command.index(image_digest)]
    assert "-t" not in command
    assert command[-3:] == [image_digest, "python", "run.py"]
    assert image_ref not in command
    assert command.count("OPENAI_API_KEY") == 1
    assert f"E3_RUN_IDENTITY_SHA256={'d' * 64}" in command


def test_image_ref_verifies_immutable_id_but_preserves_qualified_identity() -> None:
    digest = "sha256:" + "a" * 64
    agreement = {"image_digest": digest}

    with patch.object(
        MODULE.subprocess,
        "run",
        return_value=MODULE.subprocess.CompletedProcess(
            args=["docker", "image", "inspect", digest],
            returncode=0,
            stdout=digest + "\n",
            stderr="",
        ),
    ) as run:
        reference = MODULE._image_ref(agreement)

    assert reference == f"{MODULE.IMAGE_REPOSITORY}@{digest}"
    assert run.call_args.args[0] == [
        "docker",
        "image",
        "inspect",
        digest,
        "--format",
        "{{.Id}}",
    ]


def test_image_ref_rejects_an_unexpected_image_id() -> None:
    digest = "sha256:" + "a" * 64
    with patch.object(
        MODULE.subprocess,
        "run",
        return_value=MODULE.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="sha256:" + "b" * 64 + "\n",
            stderr="",
        ),
    ):
        try:
            MODULE._image_ref({"image_digest": digest})
        except MODULE.LaunchError as exc:
            assert "image ID mismatch" in str(exc)
        else:
            raise AssertionError("expected mismatched immutable image ID to fail closed")


def test_image_ref_rejects_a_missing_immutable_image() -> None:
    digest = "sha256:" + "a" * 64
    with patch.object(
        MODULE.subprocess,
        "run",
        return_value=MODULE.subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="No such image",
        ),
    ):
        try:
            MODULE._image_ref({"image_digest": digest})
        except MODULE.LaunchError as exc:
            assert "unavailable locally" in str(exc)
        else:
            raise AssertionError("expected a missing immutable image to fail closed")


def test_credential_helper_username_is_not_promoted_to_asserted_login() -> None:
    environment = {"OPENAI_API_KEY": "sk-test-only"}
    with patch.dict(os.environ, environment, clear=True), patch.object(
        MODULE,
        "_credential_helper",
        return_value=("114008358", "github-test-token"),
    ):
        openai_key, github_token, github_username = MODULE._credentials()

    assert openai_key == "sk-test-only"
    assert github_token == "github-test-token"
    assert github_username is None


def test_explicit_github_username_remains_an_identity_assertion() -> None:
    environment = {
        "OPENAI_API_KEY": "sk-test-only",
        "GITHUB_TOKEN": "github-test-token",
        "GITHUB_USERNAME": "SiqiZhang77",
    }
    with patch.dict(os.environ, environment, clear=True):
        _, _, github_username = MODULE._credentials()

    assert github_username == "SiqiZhang77"
