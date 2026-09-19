#!/usr/bin/env python3
"""Preflight or execute exactly one frozen E4V1 observation."""

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
    from . import identity
except ImportError:  # pragma: no cover
    import identity  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
CONTROL_ROOT = HERE.parents[1]
PROJECT_ROOT = CONTROL_ROOT.parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "02_EXPERIMENT_CONTROL/status-and-logs/exp4-terra-observations"
REMOTE_NAME = "github"


class ObservationLaunchError(RuntimeError):
    """One E4 identity or execution boundary is invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationLaunchError(f"cannot read launch file: {path}") from exc


def _git(args: list[str]) -> str:
    completed = subprocess.run(["git", *args], cwd=CONTROL_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
        raise ObservationLaunchError(f"launch control Git check failed: {detail}")
    return completed.stdout.strip()


def _remote_head(ref: str) -> str | None:
    completed = subprocess.run(["git", "ls-remote", "--heads", REMOTE_NAME, f"refs/heads/{ref}"], cwd=CONTROL_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        raise ObservationLaunchError("remote source/target identity check failed")
    return completed.stdout.split()[0] if completed.stdout.strip() else None


def _credential_helper() -> tuple[str | None, str | None]:
    completed = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n", text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode:
        return None, None
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values.get("username"), values.get("password")


def _validate_package(package: Path) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    manifest = _load_json(package / "launch_manifest.json")
    digest = _load_json(package / "launch_manifest.sha256.json")
    if not isinstance(manifest, dict) or not isinstance(digest, dict):
        raise ObservationLaunchError("launch manifest root type is invalid")
    claimed = manifest.get("launch_package_sha256")
    unsigned = dict(manifest)
    unsigned.pop("launch_package_sha256", None)
    if claimed != _sha256_bytes(_canonical(unsigned)) or digest.get("launch_package_sha256") != claimed:
        raise ObservationLaunchError("launch package logical SHA-256 mismatch")
    if (
        manifest.get("schema_version") != "exp4-e4v1-launch-package-v1"
        or manifest.get("experiment_id") != "E4V1"
        or manifest.get("status") != "execution_authorized_not_started"
        or manifest.get("formal_model_execution_enabled") is not True
        or manifest.get("serial") is not True
        or manifest.get("observations_per_authorization") != 5
        or manifest.get("continue_after_observation_failure") is not True
    ):
        raise ObservationLaunchError("launch package is not the approved E4V1 profile")
    agreement = _load_json(package / "agreement/agreement.json")
    schedule = _load_json(package / "agreement/schedule.json")
    agreement_digest = _load_json(package / "agreement/agreement.sha256.json")
    if not isinstance(agreement, dict) or not isinstance(schedule, list) or not isinstance(agreement_digest, dict):
        raise ObservationLaunchError("copied agreement root type is invalid")
    unsigned_agreement = copy.deepcopy(agreement)
    unsigned_agreement.pop("agreement_sha256", None)
    calculated = identity.sha256_value({"manifest": unsigned_agreement, "schedule": schedule})
    if calculated != manifest.get("agreement_sha256") or agreement.get("agreement_sha256") != calculated or agreement_digest.get("agreement_sha256") != calculated:
        raise ObservationLaunchError("launch package does not bind the frozen agreement")
    return manifest, agreement, schedule


def _validate_control(manifest: dict[str, Any]) -> None:
    control = manifest.get("launch_control")
    if not isinstance(control, dict):
        raise ObservationLaunchError("launch control binding is missing")
    if _git(["rev-parse", "HEAD"]) != control.get("control_commit"):
        raise ObservationLaunchError("launch checkout commit changed")
    if _git(["status", "--porcelain"]):
        raise ObservationLaunchError("launch checkout is dirty")
    files = control.get("files_sha256")
    if not isinstance(files, dict) or not files:
        raise ObservationLaunchError("launch control file inventory is missing")
    for relative, expected in files.items():
        path = CONTROL_ROOT / relative
        if not path.is_file() or _sha256_file(path) != expected:
            raise ObservationLaunchError(f"launch control file changed: {relative}")
    if _sha256_bytes(_canonical(files)) != control.get("files_fingerprint_sha256"):
        raise ObservationLaunchError("launch control file fingerprint mismatch")


def _validate_evidence(package: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    evidence = package / "evidence"
    expected = manifest.get("evidence_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ObservationLaunchError("launch evidence inventory is missing")
    actual = {str(path.relative_to(evidence)): _sha256_file(path) for path in sorted(evidence.rglob("*")) if path.is_file()}
    if actual != expected:
        raise ObservationLaunchError("launch evidence files changed")
    return identity.load_config(evidence / "freeze_config.json")


def _validate_request(request: dict[str, Any], run: dict[str, Any], config: dict[str, Any], package: Path) -> None:
    schema_path = CONTROL_ROOT / "rae_runtime/sandbox/schemas/runtime_request.schema.json"
    if _sha256_file(schema_path) != config["runtime"]["runtime_request_schema_sha256"]:
        raise ObservationLaunchError("runtime request schema changed")
    schema = _load_json(schema_path)
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(request))
    if errors:
        raise ObservationLaunchError("frozen runtime request violates its schema")
    repositories = request.get("repository_details")
    parameters = ((request.get("execution_objectives") or {}).get("parsed_task_parameters") or {})
    rag_enabled = run["rag_enabled"]
    if (
        request.get("run_id") != run["run_id"]
        or request.get("architecture_mode") != run["architecture_mode"]
        or not isinstance(repositories, list) or len(repositories) != 1
        or repositories[0].get("source_branch") != config["source"]["source_ref"]
        or repositories[0].get("target_branch") != run["target_ref"]
        or repositories[0].get("allowed_directories") != [config["task_bindings"]["T3"]["suite_root"]]
        or parameters.get("rag_enabled") is not rag_enabled
        or parameters.get("formal_execution_contract") != "factorial_rag_architecture_v1"
        or parameters.get("rag_delivery_policy") != "all_model_stages_v1"
        or parameters.get("answer_capture_profile") != "t3_item_results_v2"
        or parameters.get("t3_item_submission_schema_path") != config["task_bindings"]["T3"]["item_submission_schema_path"]
    ):
        raise ObservationLaunchError("runtime request differs from the frozen E4 boundary")
    retrieval = request.get("retrieval_context")
    if rag_enabled:
        expected = config["retrieval_binding"]["task_query_context"]["T3"]["retrieval_context_sha256"]
        if not isinstance(retrieval, dict) or _sha256_bytes(_canonical(retrieval)) != expected:
            raise ObservationLaunchError("RAG-on request lacks the frozen retrieval context")
    elif retrieval is not None:
        raise ObservationLaunchError("RAG-off request contains retrieval context")


def _validate_negative(value: dict[str, Any], run: dict[str, Any]) -> None:
    import sys
    sys.path.insert(0, str(CONTROL_ROOT / "rae_runtime/proxy"))
    try:
        from exp3.isolation import NegativeRefManifest
    finally:
        sys.path.pop(0)
    parsed = NegativeRefManifest.from_dict(value)
    if parsed.sha256 != run["negative_ref_manifest_sha256"] or parsed.deny_ref_set_sha256 != run["negative_ref_set_sha256"] or value.get("run_id") != run["run_id"]:
        raise ObservationLaunchError("negative-ref isolation binding mismatch")


def _image_available(digest: str) -> None:
    completed = subprocess.run(["docker", "image", "inspect", digest, "--format", "{{.Id}}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if completed.returncode or completed.stdout.strip() != digest:
        raise ObservationLaunchError("agreed immutable Docker image is unavailable")


def preflight(package: Path, run_id: str) -> dict[str, Any]:
    package = package.resolve()
    manifest, agreement, _schedule = _validate_package(package)
    _validate_control(manifest)
    config = _validate_evidence(package, manifest)
    run = next((row for row in manifest.get("runs", []) if isinstance(row, dict) and row.get("run_id") == run_id), None)
    if run is None:
        raise ObservationLaunchError("run ID is not in the launch package")
    run_dir = package / "runs" / run_id
    paths = {name: run_dir / f"{name}.json" for name in ("identity", "request", "negative_ref_manifest")}
    for name, path in paths.items():
        if not path.is_file() or _sha256_file(path) != run["file_sha256"][name]:
            raise ObservationLaunchError(f"frozen run file changed: {name}")
    run_identity, request, negative = (_load_json(paths[name]) for name in ("identity", "request", "negative_ref_manifest"))
    if not all(isinstance(value, dict) for value in (run_identity, request, negative)):
        raise ObservationLaunchError("frozen run file root type is invalid")
    unsigned_identity = dict(run_identity)
    unsigned_identity.pop("identity_sha256", None)
    if identity.sha256_value(unsigned_identity) != run.get("identity_sha256") or run_identity.get("identity_sha256") != run.get("identity_sha256"):
        raise ObservationLaunchError("run identity logical SHA-256 mismatch")
    _validate_request(request, run, config, package)
    _validate_negative(negative, run)
    if _remote_head(config["source"]["source_ref"]) != config["source"]["source_commit"]:
        raise ObservationLaunchError("remote shared source ref drifted")
    if _remote_head(run["target_ref"]) is not None:
        raise ObservationLaunchError("formal target ref already exists")
    _image_available(config["runtime"]["image_digest"])
    return {
        "schema_version": "exp4-e4v1-observation-preflight-v1", "status": "ready",
        "formal_observation": False, "provider_call_count": 0, "external_model_calls_made": False,
        "agreement_sha256": agreement["agreement_sha256"], "launch_package_sha256": manifest["launch_package_sha256"],
        "run_id": run_id, "target_ref": run["target_ref"], "condition_code": run["condition_code"],
        "architecture_mode": run["architecture_mode"], "rag_enabled": run["rag_enabled"],
        "image_digest": config["runtime"]["image_digest"], "package": package, "manifest": manifest,
        "agreement": agreement, "config": config, "selected": run, "request": request,
        "negative_path": paths["negative_ref_manifest"],
    }


def _credentials() -> tuple[str, str, str | None]:
    openai_key = os.getenv("OPENAI_API_KEY", "").strip() or getpass.getpass("Paste OPENAI_API_KEY (hidden): ").strip()
    if not openai_key.startswith("sk-") or any(char.isspace() for char in openai_key):
        raise ObservationLaunchError("OPENAI_API_KEY shape is invalid")
    username = os.getenv("GITHUB_USERNAME")
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        _helper_user, token = _credential_helper()
    if not token:
        raise ObservationLaunchError("GitHub credential token is unavailable")
    return openai_key, token, username


def _docker_command(state: dict[str, Any], input_dir: Path, result_dir: Path, env_names: list[str]) -> list[str]:
    command = ["docker", "run", "--rm", "-i", "--pull", "never", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--cpus", "2", "--memory", "4096m", "--pids-limit", "256", "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "-v", f"{input_dir}:/workspace/input:ro", "-v", f"{result_dir}:/workspace/output:rw"]
    for name in env_names:
        command.extend(["-e", name])
    selected = state["selected"]
    command.extend(["-e", "E3_NEGATIVE_REF_MANIFEST_PATH=/workspace/input/negative_ref_manifest.json", "-e", f"E3_NEGATIVE_REF_MANIFEST_SHA256={selected['negative_ref_manifest_sha256']}", "-e", f"E3_NEGATIVE_REF_SET_SHA256={selected['negative_ref_set_sha256']}", "-e", f"E3_RUN_IDENTITY_SHA256={selected['identity_sha256']}", "-e", "MAX_BRANCHES_PER_RUN=1", "-e", "MAX_COMMITS_PER_RUN=3", state["image_digest"], "python", "run.py"])
    return command


def execute(state: dict[str, Any], output_root: Path = DEFAULT_OUTPUT_ROOT) -> dict[str, Any]:
    if os.getenv("E4V1_RUN_AUTHORIZATION") != f"AUTHORIZE_{state['run_id']}":
        raise ObservationLaunchError(f"set E4V1_RUN_AUTHORIZATION=AUTHORIZE_{state['run_id']}")
    openai_key, github_token, github_username = _credentials()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = (output_root / f"{state['run_id']}-{stamp}").resolve()
    if output_dir.exists():
        raise ObservationLaunchError(f"output directory already exists: {output_dir}")
    input_dir, result_dir = output_dir / "input", output_dir / "output"
    input_dir.mkdir(parents=True)
    (result_dir / "artifacts").mkdir(parents=True)
    shutil.copy2(state["negative_path"], input_dir / "negative_ref_manifest.json")
    start = {
        "schema_version": "exp4-e4v1-formal-start-v1", "started_at": datetime.now(timezone.utc).isoformat(),
        "run_id": state["run_id"], "condition_code": state["condition_code"],
        "agreement_sha256": state["agreement_sha256"], "launch_package_sha256": state["launch_package_sha256"],
        "identity_sha256": state["selected"]["identity_sha256"], "source_commit": state["config"]["source"]["source_commit"],
        "target_ref": state["target_ref"], "image_digest": state["image_digest"],
    }
    (output_dir / "formal_start_record.json").write_text(json.dumps(start, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    child_env = os.environ.copy()
    for name in ("API_KEY", "MODEL", "LITELLM_API_KEY", "LITELLM_BASE_URL", "LITELLM_MODEL", "LITELLM_PROXY_API_KEY", "RAE_OFFLINE"):
        child_env.pop(name, None)
    child_env.update({
        "OPENAI_API_KEY": openai_key, "GITHUB_TOKEN": github_token, "LLM_PROVIDER": "openai",
        "OPENAI_BASE_URL": state["config"]["runtime"]["provider_base_url"],
        "OPENAI_MODEL": state["config"]["runtime"]["model_alias"], "USE_MCP_GITHUB": "true",
        "RAE_ENABLE_MANAGER_STAR": "true" if state["architecture_mode"] == "manager_star" else "false",
    })
    if github_username:
        child_env["GITHUB_USERNAME"] = github_username
    env_names = ["OPENAI_API_KEY", "GITHUB_TOKEN", "LLM_PROVIDER", "OPENAI_BASE_URL", "OPENAI_MODEL", "USE_MCP_GITHUB", "RAE_ENABLE_MANAGER_STAR"]
    if github_username:
        env_names.append("GITHUB_USERNAME")
    try:
        with (output_dir / "container.stdout.log").open("w", encoding="utf-8") as stdout_handle, (output_dir / "container.stderr.log").open("w", encoding="utf-8") as stderr_handle:
            completed = subprocess.run(_docker_command(state, input_dir, result_dir, env_names), input=json.dumps(state["request"], ensure_ascii=False), text=True, stdout=stdout_handle, stderr=stderr_handle, env=child_env, timeout=3900, check=False)
        exit_code, timed_out = completed.returncode, False
    except subprocess.TimeoutExpired:
        exit_code, timed_out = None, True
    result_path = result_dir / "result.json"
    result_status = "failed"
    if result_path.is_file():
        value = _load_json(result_path)
        summary = value.get("execution_summary") if isinstance(value, dict) else None
        if isinstance(summary, dict) and summary.get("status") in {"succeeded", "failed"}:
            result_status = summary["status"]
    launch_result = {
        "schema_version": "exp4-e4v1-local-launch-result-v1", "run_id": state["run_id"],
        "condition_code": state["condition_code"], "target_ref": state["target_ref"],
        "agreement_sha256": state["agreement_sha256"], "launch_package_sha256": state["launch_package_sha256"],
        "output_directory": str(output_dir), "container_exit_code": exit_code, "timed_out": timed_out,
        "result_present": result_path.is_file(), "result_status": result_status,
    }
    (output_dir / "launch_result.json").write_text(json.dumps(launch_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return launch_result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-package", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        state = preflight(args.launch_package, args.run_id)
        public = {key: state[key] for key in ("status", "provider_call_count", "agreement_sha256", "launch_package_sha256", "run_id", "target_ref", "condition_code", "architecture_mode", "rag_enabled", "image_digest")}
        print(json.dumps(public, sort_keys=True), flush=True)
        if args.execute:
            print(json.dumps(execute(state, args.output_root), sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"schema_version": "exp4-e4v1-observation-error-v1", "status": "failed", "provider_call_count": 0 if not args.execute else None, "formal_observation": bool(args.execute), "failure_type": type(exc).__name__, "message": str(exc)}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
