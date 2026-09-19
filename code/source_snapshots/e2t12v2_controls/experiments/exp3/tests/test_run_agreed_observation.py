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


def test_docker_command_attaches_stdin_without_allocating_tty(tmp_path: Path) -> None:
    image_ref = "exp3-manager-star-formal@sha256:" + "a" * 64
    state = {
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
    assert "-i" in command[: command.index(image_ref)]
    assert "-t" not in command
    assert command[-3:] == [image_ref, "python", "run.py"]
    assert command.count("OPENAI_API_KEY") == 1
    assert f"E3_RUN_IDENTITY_SHA256={'d' * 64}" in command


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
