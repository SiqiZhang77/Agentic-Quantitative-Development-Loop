"""Preflight or execute exactly one agreed Experiment 3 Terra observation."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
PROJECT_ROOT = REPO_ROOT.parents[1]
DEFAULT_AGREEMENT_DIR = HERE / "agreement-terra-v1.5"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "02_EXPERIMENT_CONTROL"
    / "status-and-logs"
    / "exp3-terra-observations"
)
IMAGE_REPOSITORY = "exp3-manager-star-formal"
REMOTE_NAME = "github"


class LaunchError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LaunchError(f"expected JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remote_head(ref: str) -> str | None:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "ls-remote",
            "--heads",
            REMOTE_NAME,
            f"refs/heads/{ref}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    if not completed.stdout.strip():
        return None
    return completed.stdout.split()[0]


def _credential_helper() -> tuple[str | None, str | None]:
    completed = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None, None
    values = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values.get("username"), values.get("password")


def _select_run(agreement: dict[str, Any], run_id: str | None) -> dict[str, Any]:
    selected_id = run_id or agreement["execution_order"][0]
    for item in agreement["runs"]:
        if item["run_id"] == selected_id:
            return item
    raise LaunchError(f"run is not in the agreed execution plan: {selected_id}")


def _image_ref(agreement: dict[str, Any]) -> str:
    digest = agreement["image_digest"]
    reference = f"{IMAGE_REPOSITORY}@{digest}"
    completed = subprocess.run(
        ["docker", "image", "inspect", reference],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise LaunchError(f"agreed Docker image is unavailable locally: {reference}")
    return reference


def preflight(agreement_dir: Path, run_id: str | None) -> dict[str, Any]:
    agreement_path = agreement_dir / "agreement.json"
    agreement = _load_json(agreement_path)
    digest_record = _load_json(agreement_dir / "agreement.sha256.json")
    if _sha256_file(agreement_path) != digest_record.get("agreement_sha256"):
        raise LaunchError("agreement hash mismatch")
    if agreement.get("status") != "user_agreed_preflight_ready":
        raise LaunchError("agreement status is not runnable")
    if agreement.get("rag_enabled") is not False:
        raise LaunchError("Experiment 3 agreement must keep RAG disabled")
    selected = _select_run(agreement, run_id)
    run_dir = agreement_dir / "runs" / selected["run_id"]
    manifest = _load_json(run_dir / "negative_ref_manifest.json")
    identity = _load_json(run_dir / "identity.json")
    request = _load_json(run_dir / "request.json")
    for name, filename in (
        ("manifest", "negative_ref_manifest.json"),
        ("identity", "identity.json"),
        ("request", "request.json"),
    ):
        if _sha256_file(run_dir / filename) != selected["file_sha256"][name]:
            raise LaunchError(f"{name} file hash mismatch")
    if request.get("run_id") != selected["run_id"]:
        raise LaunchError("request run identity mismatch")
    if identity.get("architecture_mode") != selected["architecture_mode"]:
        raise LaunchError("architecture identity mismatch")
    if identity.get("rag_enabled") is not False:
        raise LaunchError("run identity unexpectedly enables RAG")
    if manifest.get("run_id") != selected["run_id"]:
        raise LaunchError("negative-ref manifest run identity mismatch")
    if _remote_head(agreement["source_ref"]) != agreement["source_commit"]:
        raise LaunchError("remote source ref drifted from the agreed commit")
    if _remote_head(selected["target_ref"]) is not None:
        raise LaunchError("current target already exists; refusing to overwrite or reuse it")
    image_ref = _image_ref(agreement)
    return {
        "agreement": agreement,
        "selected": selected,
        "manifest": manifest,
        "identity": identity,
        "request": request,
        "run_dir": run_dir,
        "image_ref": image_ref,
        "agreement_sha256": digest_record["agreement_sha256"],
    }


def _credentials() -> tuple[str, str, str | None]:
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        openai_key = getpass.getpass("Paste OPENAI_API_KEY (hidden): ").strip()
    if not openai_key.startswith("sk-") or any(ch.isspace() for ch in openai_key):
        raise LaunchError("OPENAI_API_KEY shape is invalid")

    github_username = os.getenv("GITHUB_USERNAME")
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        _helper_username, helper_token = _credential_helper()
        # GitHub's macOS credential helper may return a numeric account ID in
        # the username field while the API reports the account login. Do not
        # reinterpret that transport hint as an asserted login identity.
        github_token = helper_token
    if not github_token:
        raise LaunchError(
            "GITHUB_TOKEN is unavailable and the Git credential helper returned no token"
        )
    return openai_key, github_token, github_username


def _docker_run_command(
    *,
    state: dict[str, Any],
    input_dir: Path,
    result_dir: Path,
    environment_names: list[str],
) -> list[str]:
    selected = state["selected"]
    command = [
        "docker",
        "run",
        "--rm",
        # Keep stdin attached so the JSON request supplied to subprocess.run
        # reaches run.py inside the container. A TTY is intentionally omitted.
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
    for name in environment_names:
        command.extend(["-e", name])
    command.extend(
        [
            "-e",
            "E3_NEGATIVE_REF_MANIFEST_PATH=/workspace/input/negative_ref_manifest.json",
            "-e",
            f"E3_NEGATIVE_REF_MANIFEST_SHA256={selected['manifest_sha256']}",
            "-e",
            f"E3_NEGATIVE_REF_SET_SHA256={selected['deny_ref_set_sha256']}",
            "-e",
            f"E3_RUN_IDENTITY_SHA256={selected['identity_sha256']}",
            "-e",
            "MAX_BRANCHES_PER_RUN=1",
            "-e",
            "MAX_COMMITS_PER_RUN=3",
            state["image_ref"],
            "python",
            "run.py",
        ]
    )
    return command


def execute(state: dict[str, Any], output_root: Path) -> dict[str, Any]:
    agreement = state["agreement"]
    selected = state["selected"]
    openai_key, github_token, github_username = _credentials()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (output_root / f"{selected['run_id']}-{stamp}").resolve()
    if output_dir.exists():
        raise LaunchError(f"output directory already exists: {output_dir}")
    input_dir = output_dir / "input"
    result_dir = output_dir / "output"
    input_dir.mkdir(parents=True)
    result_dir.mkdir()
    shutil.copy2(
        state["run_dir"] / "negative_ref_manifest.json",
        input_dir / "negative_ref_manifest.json",
    )
    (output_dir / "formal_start_record.json").write_text(
        json.dumps(
            {
                "schema_version": "exp3-formal-start-record-v1",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "run_id": selected["run_id"],
                "agreement_sha256": state["agreement_sha256"],
                "identity_sha256": selected["identity_sha256"],
                "manifest_sha256": selected["manifest_sha256"],
                "deny_ref_set_sha256": selected["deny_ref_set_sha256"],
                "source_ref": agreement["source_ref"],
                "source_commit": agreement["source_commit"],
                "target_ref": selected["target_ref"],
                "image_digest": agreement["image_digest"],
                "provider_identity_sha256": agreement["provider"]["identity_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
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
            "OPENAI_BASE_URL": agreement["provider"]["base_url"],
            "OPENAI_MODEL": agreement["provider"]["model_alias"],
            "USE_MCP_GITHUB": "true",
            "RAE_ENABLE_MANAGER_STAR": (
                "true" if selected["architecture_mode"] == "manager_star" else "false"
            ),
        }
    )
    if github_username:
        child_env["GITHUB_USERNAME"] = github_username

    environment_names = [
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "LLM_PROVIDER",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "USE_MCP_GITHUB",
        "RAE_ENABLE_MANAGER_STAR",
    ]
    if github_username:
        environment_names.append("GITHUB_USERNAME")
    command = _docker_run_command(
        state=state,
        input_dir=input_dir,
        result_dir=result_dir,
        environment_names=environment_names,
    )

    stdout_path = output_dir / "container.stdout.log"
    stderr_path = output_dir / "container.stderr.log"
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
            timeout=2_100,
        )

    result_path = result_dir / "result.json"
    result = _load_json(result_path) if result_path.is_file() else None
    status = {
        "schema_version": "exp3-formal-local-launch-result-v1",
        "run_id": selected["run_id"],
        "container_exit_code": completed.returncode,
        "result_present": result is not None,
        "result_status": (
            (result.get("execution_summary") or {}).get("status")
            if result is not None
            else None
        ),
        "target_ref": selected["target_ref"],
        "output_directory": str(output_dir),
    }
    (output_dir / "launch_result.json").write_text(
        json.dumps(status, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agreement-dir", type=Path, default=DEFAULT_AGREEMENT_DIR)
    parser.add_argument("--run-id")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        state = preflight(args.agreement_dir.resolve(), args.run_id)
        safe = {
            "status": "ready",
            "provider_call_count": 0,
            "run_id": state["selected"]["run_id"],
            "architecture_mode": state["selected"]["architecture_mode"],
            "target_ref": state["selected"]["target_ref"],
            "agreement_sha256": state["agreement_sha256"],
            "image_ref": state["image_ref"],
            "rag_enabled": False,
        }
        if not args.execute:
            print(json.dumps(safe, sort_keys=True))
            return
        print(json.dumps({**safe, "status": "starting"}, sort_keys=True), flush=True)
        print(json.dumps(execute(state, args.output_root.resolve()), sort_keys=True))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "failure_type": type(exc).__name__, "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
