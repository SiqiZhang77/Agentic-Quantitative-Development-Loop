#!/usr/bin/env python3
"""Preflight or execute exactly one E3V17 observation."""

from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

try:
    from . import controls
except ImportError:  # pragma: no cover - direct script execution
    import controls  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[2]
PROJECT_ROOT = CONTROL_ROOT.parents[1]
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
    / "exp3-terra-observations"
)
REMOTE_NAME = "github"


class ObservationLaunchError(RuntimeError):
    """One formal identity or execution boundary is invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationLaunchError(f"cannot read launch file: {path.name}") from exc


def _git(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=CONTROL_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise ObservationLaunchError(f"launch control Git check failed: {detail}")
    return completed.stdout.strip()


def _remote_head(ref: str) -> str | None:
    completed = subprocess.run(
        [
            "git",
            "ls-remote",
            "--heads",
            REMOTE_NAME,
            f"refs/heads/{ref}",
        ],
        cwd=CONTROL_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise ObservationLaunchError("remote source/target identity check failed")
    if not completed.stdout.strip():
        return None
    return completed.stdout.split()[0]


def _credential_helper() -> tuple[str | None, str | None]:
    completed = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        return None, None
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values.get("username"), values.get("password")


def _validate_package(package: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = _load_json(package / "launch_manifest.json")
    digest = _load_json(package / "launch_manifest.sha256.json")
    if not isinstance(manifest, dict) or not isinstance(digest, dict):
        raise ObservationLaunchError("launch manifest root type is invalid")
    claimed = manifest.get("launch_package_sha256")
    unsigned = dict(manifest)
    unsigned.pop("launch_package_sha256", None)
    if claimed != _sha256_bytes(_canonical(unsigned)):
        raise ObservationLaunchError("launch package logical SHA-256 mismatch")
    if digest.get("launch_package_sha256") != claimed:
        raise ObservationLaunchError("launch package digest record mismatch")
    if (
        manifest.get("schema_version") != "exp3-e3v17-launch-package-v1"
        or manifest.get("experiment_id") != "E3V17"
        or manifest.get("status") != "execution_authorized_not_started"
        or manifest.get("formal_model_execution_enabled") is not True
        or manifest.get("serial") is not True
        or manifest.get("observations_per_authorization") != 2
    ):
        raise ObservationLaunchError("launch package is not the approved E3V17 profile")
    agreement = _load_json(package / "agreement" / "agreement.json")
    schedule = _load_json(package / "agreement" / "schedule.json")
    agreement_digest = _load_json(
        package / "agreement" / "agreement.sha256.json"
    )
    if (
        not isinstance(agreement, dict)
        or not isinstance(schedule, list)
        or not isinstance(agreement_digest, dict)
    ):
        raise ObservationLaunchError("copied agreement is invalid")
    unsigned_agreement = copy.deepcopy(agreement)
    unsigned_agreement.pop("agreement_sha256", None)
    if (
        agreement.get("experiment_id") != manifest.get("experiment_id")
        or agreement.get("agreement_sha256") != manifest.get("agreement_sha256")
        or agreement_digest.get("agreement_sha256") != manifest.get("agreement_sha256")
        or controls.sha256_value(
            {"manifest": unsigned_agreement, "schedule": schedule}
        )
        != manifest.get("agreement_sha256")
    ):
        raise ObservationLaunchError("launch package does not bind the frozen agreement")
    return manifest, agreement


def _validate_control(manifest: dict[str, Any]) -> None:
    launch_control = manifest.get("launch_control")
    if not isinstance(launch_control, dict):
        raise ObservationLaunchError("launch control binding is missing")
    if _git(["rev-parse", "HEAD"]) != launch_control.get("control_commit"):
        raise ObservationLaunchError("launch checkout commit changed")
    if _git(["status", "--porcelain"]):
        raise ObservationLaunchError("launch checkout is dirty")
    files = launch_control.get("files_sha256")
    if not isinstance(files, dict) or not files:
        raise ObservationLaunchError("launch control file inventory is missing")
    for relative, expected in files.items():
        path = CONTROL_ROOT / relative
        if not path.is_file() or _sha256_file(path) != expected:
            raise ObservationLaunchError(f"launch control file changed: {relative}")
    if _sha256_bytes(_canonical(files)) != launch_control.get(
        "files_fingerprint_sha256"
    ):
        raise ObservationLaunchError("launch control file fingerprint mismatch")


def _validate_evidence(package: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    expected = manifest.get("evidence_sha256")
    paths = {
        "freeze_config": package / "evidence" / "freeze_config.json",
        "public_bindings": package / "evidence" / "public_bindings.json",
        "private_evaluator_hashes": package
        / "evidence"
        / "private_evaluator_hashes.json",
    }
    if not isinstance(expected, dict) or set(expected) != set(paths):
        raise ObservationLaunchError("launch evidence inventory is invalid")
    for name, path in paths.items():
        if not path.is_file() or _sha256_file(path) != expected[name]:
            raise ObservationLaunchError(f"launch evidence changed: {name}")
    config = controls.load_config(paths["freeze_config"])
    return config


def _validate_request(
    request: dict[str, Any], run: dict[str, Any], config: dict[str, Any]
) -> None:
    schema_path = CONTROL_ROOT / "rae_runtime/sandbox/schemas/runtime_request.schema.json"
    if _sha256_file(schema_path) != config["runtime"][
        "runtime_request_schema_sha256"
    ]:
        raise ObservationLaunchError("runtime request schema changed")
    schema = _load_json(schema_path)
    errors = sorted(
        Draft202012Validator(
            schema, format_checker=FormatChecker()
        ).iter_errors(request),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        raise ObservationLaunchError("frozen runtime request violates its schema")
    repositories = request.get("repository_details")
    parameters = (
        (request.get("execution_objectives") or {}).get("parsed_task_parameters")
        or {}
    )
    if (
        request.get("run_id") != run["run_id"]
        or request.get("architecture_mode") != run["architecture_mode"]
        or not isinstance(repositories, list)
        or len(repositories) != 1
        or repositories[0].get("source_branch") != config["source"]["source_ref"]
        or repositories[0].get("target_branch") != run["target_ref"]
        or repositories[0].get("allowed_directories")
        != [config["task_suite"]["root"]]
        or parameters.get("rag_enabled") is not False
        or parameters.get("answer_capture_profile") != "t3_item_results_v2"
        or parameters.get("t3_item_submission_schema_path")
        != config["task_suite"]["item_submission_schema_path"]
        or "retrieval_context" in request
    ):
        raise ObservationLaunchError("runtime request differs from the frozen boundary")


def _validate_negative(
    negative: dict[str, Any], run: dict[str, Any]
) -> None:
    sys_path = CONTROL_ROOT / "rae_runtime" / "proxy"
    import sys

    sys.path.insert(0, str(sys_path))
    try:
        from exp3.isolation import NegativeRefManifest
    finally:
        sys.path.pop(0)
    parsed = NegativeRefManifest.from_dict(negative)
    if (
        parsed.sha256 != run["negative_ref_manifest_sha256"]
        or parsed.deny_ref_set_sha256 != run["negative_ref_set_sha256"]
        or negative.get("run_id") != run["run_id"]
    ):
        raise ObservationLaunchError("negative-ref isolation binding mismatch")


def _image_id(digest: str) -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", digest, "--format", "{{.Id}}"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode or completed.stdout.strip() != digest:
        raise ObservationLaunchError("agreed immutable Docker image is unavailable")


def preflight(package: Path, run_id: str) -> dict[str, Any]:
    package = package.resolve()
    manifest, agreement = _validate_package(package)
    _validate_control(manifest)
    config = _validate_evidence(package, manifest)
    runs = manifest.get("runs")
    if not isinstance(runs, list):
        raise ObservationLaunchError("launch run inventory is invalid")
    selected = next(
        (item for item in runs if isinstance(item, dict) and item.get("run_id") == run_id),
        None,
    )
    if selected is None:
        raise ObservationLaunchError("run ID is not in the launch package")
    run_dir = package / "runs" / run_id
    files = {
        "identity": run_dir / "identity.json",
        "request": run_dir / "request.json",
        "negative_ref_manifest": run_dir / "negative_ref_manifest.json",
    }
    for name, path in files.items():
        if not path.is_file() or _sha256_file(path) != selected["file_sha256"][name]:
            raise ObservationLaunchError(f"frozen run file changed: {name}")
    identity = _load_json(files["identity"])
    request = _load_json(files["request"])
    negative = _load_json(files["negative_ref_manifest"])
    if not all(isinstance(value, dict) for value in (identity, request, negative)):
        raise ObservationLaunchError("frozen run file root type is invalid")
    if controls.sha256_value({key: value for key, value in identity.items() if key != "identity_sha256"}) != identity.get("identity_sha256"):
        # E3V17 identities hash the complete object before the identity field is added.
        raise ObservationLaunchError("run identity logical SHA-256 mismatch")
    if identity.get("identity_sha256") != selected["identity_sha256"]:
        raise ObservationLaunchError("run identity differs from launch manifest")
    _validate_request(request, selected, config)
    _validate_negative(negative, selected)
    if _remote_head(config["source"]["source_ref"]) != config["source"]["source_commit"]:
        raise ObservationLaunchError("remote source ref drifted from the agreement")
    if _remote_head(selected["target_ref"]) is not None:
        raise ObservationLaunchError("formal target ref already exists")
    _image_id(config["runtime"]["image_digest"])
    return {
        "schema_version": (
            f"exp3-{manifest['experiment_id'].lower()}-observation-preflight-v1"
        ),
        "status": "ready",
        "formal_observation": False,
        "provider_call_count": 0,
        "external_model_calls_made": False,
        "agreement_sha256": agreement["agreement_sha256"],
        "launch_package_sha256": manifest["launch_package_sha256"],
        "run_id": run_id,
        "target_ref": selected["target_ref"],
        "architecture_mode": selected["architecture_mode"],
        "image_digest": config["runtime"]["image_digest"],
        "package": package,
        "manifest": manifest,
        "agreement": agreement,
        "config": config,
        "selected": selected,
        "request": request,
        "negative_path": files["negative_ref_manifest"],
    }


def _credentials() -> tuple[str, str, str | None]:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not openai_key:
        openai_key = getpass.getpass("Paste OPENAI_API_KEY (hidden): ").strip()
    if not openai_key.startswith("sk-") or any(char.isspace() for char in openai_key):
        raise ObservationLaunchError("OPENAI_API_KEY shape is invalid")
    github_username = os.getenv("GITHUB_USERNAME")
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        # A Git credential helper may return a numeric account identifier as
        # its `username`, while the GitHub API reports the account login.  Git
        # accepts either alongside the PAT, but github_client deliberately
        # compares an explicitly supplied GITHUB_USERNAME with the API login.
        # Therefore use the helper only for the token; propagate a username
        # only when the operator explicitly configured one.
        _helper_user, github_token = _credential_helper()
    if not github_token:
        raise ObservationLaunchError("GitHub credential token is unavailable")
    return openai_key, github_token, github_username


def _docker_command(
    state: dict[str, Any], input_dir: Path, result_dir: Path, env_names: list[str]
) -> list[str]:
    config = state["config"]
    selected = state["selected"]
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--pull",
        "never",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--cpus",
        "2",
        "--memory",
        "4096m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{input_dir}:/workspace/input:ro",
        "-v",
        f"{result_dir}:/workspace/output:rw",
    ]
    for name in env_names:
        command.extend(["-e", name])
    command.extend(
        [
            "-e",
            "E3_NEGATIVE_REF_MANIFEST_PATH=/workspace/input/negative_ref_manifest.json",
            "-e",
            f"E3_NEGATIVE_REF_MANIFEST_SHA256={selected['negative_ref_manifest_sha256']}",
            "-e",
            f"E3_NEGATIVE_REF_SET_SHA256={selected['negative_ref_set_sha256']}",
            "-e",
            f"E3_RUN_IDENTITY_SHA256={selected['identity_sha256']}",
            "-e",
            "MAX_BRANCHES_PER_RUN=1",
            "-e",
            "MAX_COMMITS_PER_RUN=3",
            config["runtime"]["image_digest"],
            "python",
            "run.py",
        ]
    )
    return command


def execute(state: dict[str, Any], output_root: Path) -> dict[str, Any]:
    experiment_id = state["manifest"]["experiment_id"]
    authorization_name = f"{experiment_id}_RUN_AUTHORIZATION"
    expected = f"AUTHORIZE_{state['run_id']}"
    if os.getenv(authorization_name) != expected:
        raise ObservationLaunchError(
            f"set {authorization_name}={expected} for this observation"
        )
    openai_key, github_token, github_username = _credentials()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (output_root / f"{state['run_id']}-{stamp}").resolve()
    if output_dir.exists():
        raise ObservationLaunchError(f"output directory already exists: {output_dir}")
    input_dir = output_dir / "input"
    result_dir = output_dir / "output"
    input_dir.mkdir(parents=True)
    result_dir.mkdir()
    # The T3 completion checker may inspect the item store while handling a
    # failure that occurs before the first tool call.  Create its declared
    # parent now so that this secondary check cannot mask the original error.
    (result_dir / "artifacts").mkdir()
    shutil.copy2(
        state["negative_path"], input_dir / "negative_ref_manifest.json"
    )
    start = {
        "schema_version": f"exp3-{experiment_id.lower()}-formal-start-v1",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "run_id": state["run_id"],
        "agreement_sha256": state["agreement_sha256"],
        "launch_package_sha256": state["launch_package_sha256"],
        "identity_sha256": state["selected"]["identity_sha256"],
        "source_commit": state["config"]["source"]["source_commit"],
        "target_ref": state["target_ref"],
        "image_digest": state["image_digest"],
    }
    (output_dir / "formal_start_record.json").write_text(
        json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    child_env = os.environ.copy()
    for name in (
        "API_KEY",
        "MODEL",
        "LITELLM_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "LITELLM_PROXY_API_KEY",
        "RAE_OFFLINE",
    ):
        child_env.pop(name, None)
    child_env.update(
        {
            "OPENAI_API_KEY": openai_key,
            "GITHUB_TOKEN": github_token,
            "LLM_PROVIDER": "openai",
            "OPENAI_BASE_URL": state["config"]["runtime"]["provider_base_url"],
            "OPENAI_MODEL": state["config"]["runtime"]["model_alias"],
            "USE_MCP_GITHUB": "true",
            "RAE_ENABLE_MANAGER_STAR": (
                "true"
                if state["architecture_mode"] == "manager_star"
                else "false"
            ),
        }
    )
    if github_username:
        child_env["GITHUB_USERNAME"] = github_username
    env_names = [
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "LLM_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "USE_MCP_GITHUB",
        "RAE_ENABLE_MANAGER_STAR",
    ]
    if github_username:
        env_names.append("GITHUB_USERNAME")
    command = _docker_command(state, input_dir, result_dir, env_names)
    stdout_path = output_dir / "container.stdout.log"
    stderr_path = output_dir / "container.stderr.log"
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            completed = subprocess.run(
                command,
                input=json.dumps(state["request"], ensure_ascii=False),
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                env=child_env,
                timeout=3900,
                check=False,
            )
        container_exit_code: int | None = completed.returncode
        timeout = False
    except subprocess.TimeoutExpired:
        container_exit_code = None
        timeout = True
    result_path = result_dir / "result.json"
    result_present = result_path.is_file()
    result_status: str | None = None
    if result_present:
        try:
            value = _load_json(result_path)
            execution_summary = (
                value.get("execution_summary") if isinstance(value, dict) else None
            )
            if isinstance(execution_summary, dict):
                nested_status = execution_summary.get("status")
                if nested_status in {"succeeded", "failed"}:
                    result_status = nested_status
        except ObservationLaunchError:
            result_status = None
    launch_result = {
        "schema_version": f"exp3-{experiment_id.lower()}-local-launch-result-v1",
        "run_id": state["run_id"],
        "target_ref": state["target_ref"],
        "agreement_sha256": state["agreement_sha256"],
        "launch_package_sha256": state["launch_package_sha256"],
        "output_directory": str(output_dir),
        "container_exit_code": container_exit_code,
        "timed_out": timeout,
        "result_present": result_present,
        "result_status": result_status or "failed",
    }
    (output_dir / "launch_result.json").write_text(
        json.dumps(launch_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return launch_result


def _public_result(state: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in state.items()
        if key
        in {
            "schema_version",
            "status",
            "formal_observation",
            "provider_call_count",
            "external_model_calls_made",
            "agreement_sha256",
            "launch_package_sha256",
            "run_id",
            "target_ref",
            "architecture_mode",
            "image_digest",
        }
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        state = preflight(args.launch_package, args.run_id)
        print(json.dumps(_public_result(state), sort_keys=True), flush=True)
        if not args.execute:
            return 0
        result = execute(state, args.output_root)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "exp3-e3v17-observation-launch-error-v1",
                    "status": "failed",
                    # Once --execute is present, a late failure could occur
                    # after a provider call.  Report unknown rather than
                    # incorrectly claiming a zero-call failure.
                    "provider_call_count": 0 if not args.execute else None,
                    "formal_observation": bool(args.execute),
                    "failure_type": type(exc).__name__,
                    "message": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
