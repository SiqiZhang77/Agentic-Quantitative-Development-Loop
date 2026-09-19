"""Run one Jira quant request in the sandbox and write the result to Jira."""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import uuid
import hashlib
import random
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse, urlunparse

import requests
from airflow.sdk import DAG, get_current_context, task

try:
    from .adf_utils.builder import PROGRESS_EVENT_STATUSES, build_workflow_progress_adf
    from .jira_command_validation import (
        JiraCommandValidationError,
        ValidatedJiraCommand,
        select_latest_quant_comment,
        validate_jira_command_payload,
    )
    from .jira_rag_retriever import retrieve_jira_memory
    from .jira_quant_common import (
        BOT_MARKER,
        extract_plain_text_from_adf,
        format_result_comment_adf,
        format_validation_failure_comment,
        format_result_comment,
        get_airflow_variable,
        get_litellm_config,
        get_openai_config,
        get_optional_ghcr_credentials,
        get_optional_github_credentials,
        jira_request,
        parse_final_json_stdout,
        post_jira_adf_comment,
        post_jira_attachments,
        post_jira_comment,
        resolve_user_scoped_github_credentials,
        verify_user_github_access,
    )
except ImportError:
    from adf_utils.builder import PROGRESS_EVENT_STATUSES, build_workflow_progress_adf
    from jira_command_validation import (
        JiraCommandValidationError,
        ValidatedJiraCommand,
        select_latest_quant_comment,
        validate_jira_command_payload,
    )
    from jira_rag_retriever import retrieve_jira_memory
    from jira_quant_common import (
        BOT_MARKER,
        extract_plain_text_from_adf,
        format_result_comment_adf,
        format_validation_failure_comment,
        format_result_comment,
        get_airflow_variable,
        get_litellm_config,
        get_openai_config,
        get_optional_ghcr_credentials,
        get_optional_github_credentials,
        jira_request,
        parse_final_json_stdout,
        post_jira_adf_comment,
        post_jira_attachments,
        post_jira_comment,
        resolve_user_scoped_github_credentials,
        verify_user_github_access,
    )

DEFAULT_SANDBOX_IMAGE = "ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:stable"
DEFAULT_CONTAINER_TIMEOUT_SECONDS = 30 * 60
WORKSPACE_OUTPUT_ROOT = PurePosixPath("/workspace/output")
WORKSPACE_INPUT_ROOT = PurePosixPath("/workspace/input")
WORKSPACE_DATASET_ROOT = PurePosixPath("/workspace/input/datasets")
DEFAULT_RESULT_PATH = "/workspace/output/result.json"
BACKTESTER_CONTAINER_REQUESTS_DIR = "/workspace/backtester/simulation-requests"
BACKTESTER_CONTAINER_RESULTS_DIR = "/workspace/backtester/simulation-results"
DEFAULT_DOCKER_REQUESTS_HOST_DIR = "~/simulation-requests"
DEFAULT_DOCKER_RESULTS_HOST_DIR = "~/simulation-results"
ARTIFACT_MAX_BYTES = 10 * 1024 * 1024
REQUEST_ATTACHMENT_MAX_BYTES = 2 * 1024 * 1024
STREAM_CAPTURE_MAX_CHARS = 2 * 1024 * 1024
BACKTESTER_ENV_VARS = (
    "USE_REAL_BACKTESTER",
    "SIMULATION_REQUESTS_DIR",
    "SIMULATION_RESULTS_DIR",
    "SIMULATION_REQUESTS_HOST_DIR",
    "SIMULATION_RESULTS_HOST_DIR",
    "BACKTEST_STORAGE_KIND",
    "SIMULATION_REQUESTS_HDFS_DIR",
    "SIMULATION_RESULTS_HDFS_DIR",
    "BACKTEST_RESULT_TIMEOUT_SECONDS",
)
PROVIDER_ENV_VARS = (
    "LLM_PROVIDER",
    "LITELLM_API_KEY",
    "LITELLM_PROXY_API_KEY",
    "LITELLM_BASE_URL",
    "LITELLM_MODEL",
    "API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_MODEL",
    "MODEL",
    "MODEL_NAME",
)
SOURCE_REVISION_MARKER = ".bslagenticquantdevloop_revision"
SOURCE_REVISION_ENV_VARS = (
    "BSLAGENTICQUANTDEVLOOP_COMMIT",
    "AIRFLOW_DAG_GIT_COMMIT",
    "DAG_SYNC_GIT_COMMIT",
    "GITHUB_SHA",
)
PROGRESS_EVENT_FIELDS = {
    "schema_version",
    "run_id",
    "ticket_id",
    "stage",
    "status",
    "timestamp",
    "iteration",
    "agent",
    "tool_call",
    "message",
}


def append_bounded_text_tail(current: str, chunk: str, limit: int) -> str:
    combined = current + chunk
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


def run_command_streaming(
    command: list[str],
    *,
    env: dict[str, str],
    input_text: str,
    timeout: int,
    capture_limit: int = STREAM_CAPTURE_MAX_CHARS,
) -> subprocess.CompletedProcess[str]:
    """Run a command while streaming combined stdout/stderr to Airflow logs."""

    started_at = time.monotonic()
    captured = ""
    process = subprocess.Popen(
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        if process.stdin is not None:
            try:
                process.stdin.write(input_text.encode("utf-8"))
                process.stdin.close()
            except BrokenPipeError:
                pass

        if process.stdout is not None:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            os.set_blocking(process.stdout.fileno(), False)
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started_at)
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            command,
                            timeout,
                            output=captured,
                            stderr="",
                        )
                    events = selector.select(timeout=min(1.0, remaining))
                    if not events:
                        if process.poll() is not None:
                            try:
                                tail_bytes = os.read(process.stdout.fileno(), 65536)
                            except BlockingIOError:
                                tail_bytes = b""
                            tail = tail_bytes.decode("utf-8", errors="replace")
                            if tail:
                                print(tail, end="", flush=True)
                                captured = append_bounded_text_tail(
                                    captured,
                                    tail,
                                    capture_limit,
                                )
                            break
                        continue
                    try:
                        chunk_bytes = os.read(process.stdout.fileno(), 65536)
                    except BlockingIOError:
                        chunk_bytes = b""
                    if chunk_bytes:
                        chunk = chunk_bytes.decode("utf-8", errors="replace")
                        print(chunk, end="", flush=True)
                        captured = append_bounded_text_tail(
                            captured,
                            chunk,
                            capture_limit,
                        )
                        continue
                    if process.poll() is not None:
                        break
            finally:
                selector.close()

        returncode = process.wait(
            timeout=max(0.1, timeout - (time.monotonic() - started_at))
        )
    except subprocess.TimeoutExpired:
        process.kill()
        tail_stdout, _ = process.communicate()
        if tail_stdout:
            tail = tail_stdout.decode("utf-8", errors="replace")
            print(tail, end="", flush=True)
            captured = append_bounded_text_tail(captured, tail, capture_limit)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=captured,
            stderr="",
        )

    return subprocess.CompletedProcess(
        args=command,
        returncode=returncode,
        stdout=captured,
        stderr="",
    )
PROGRESS_EVENT_STAGES = {"fetch", "modify", "commit", "backtest", "report"}
JIRA_TICKET_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")

RESULT_PATH = "/workspace/output/result.json"
REQUEST_PATH = "/workspace/input/request.json"

DEFAULT_HADOOP_HOME = "/opt/hadoop"
DEFAULT_HADOOP_CONF_DIR = "/opt/hadoop/etc/hadoop"
DEFAULT_YARN_MASTER_MEMORY_MB = 512
DEFAULT_YARN_CONTAINER_MEMORY_MB = 4096
DEFAULT_YARN_TIMEOUT_SECONDS = 35 * 60
DEFAULT_HDFS_SAFE_MODE_WAIT_SECONDS = 1 * 60
DEFAULT_INPUT_MAX_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_INPUT_MAX_TOTAL_BYTES = 250 * 1024 * 1024
DEFAULT_WORKFLOW_RECOVERY_STATE_DIR = "/tmp/jira_quant_workflow_recovery"
DEFAULT_CPU_VCPUS = 2.0
DEFAULT_MEMORY_MB = 4096
DEFAULT_GPU_COUNT = 0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 35 * 60
MAX_EXECUTION_TIMEOUT_SECONDS = 10800
DEFAULT_WORKFLOW_TASK_TIMEOUT_SECONDS = MAX_EXECUTION_TIMEOUT_SECONDS + 300
DEFAULT_PIDS_LIMIT = 256


class WorkflowRetryPolicy:
    def __init__(
        self,
        max_attempts: int,
        initial_delay_seconds: float,
        max_delay_seconds: float,
        backoff_multiplier: float,
        jitter_ratio: float,
    ) -> None:
        self.max_attempts = max_attempts
        self.initial_delay_seconds = initial_delay_seconds
        self.max_delay_seconds = max_delay_seconds
        self.backoff_multiplier = backoff_multiplier
        self.jitter_ratio = jitter_ratio

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkflowRetryPolicy):
            return False
        return self.__dict__ == other.__dict__


class WorkflowRetryExhausted(RuntimeError):
    """Raised when a retryable workflow stage exhausts its retry policy."""

    def __init__(self, stage_name: str, last_error: Exception):
        self.stage_name = stage_name
        self.last_error = last_error
        super().__init__(
            f"Workflow stage {stage_name} exhausted retries after "
            f"{type(last_error).__name__}: {last_error}"
        )


class HadoopCommandError(RuntimeError):
    """Raised when an HDFS/YARN command fails."""

    def __init__(
        self,
        command: list[str],
        stdout: str,
        stderr: str,
        *,
        returncode: int,
    ) -> None:
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        super().__init__(
            "Hadoop command failed.\n"
            f"Command: {' '.join(shlex.quote(part) for part in command)}\n"
            f"stdout:\n{stdout.strip()}\n"
            f"stderr:\n{stderr.strip()}"
        )


class SandboxStructuredResultUnavailable(RuntimeError):
    """Raised when sandbox progress exists but final structured output is unreadable."""

    def __init__(
        self,
        *,
        issue_key: str,
        runtime_request: dict[str, Any],
        progress_events: list[dict[str, Any]],
        returncode: int,
        stdout: str,
        stderr: str,
        result_path: str,
        execution_mode: str,
    ) -> None:
        self.issue_key = issue_key
        self.runtime_request = runtime_request
        self.progress_events = progress_events
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.result_path = result_path
        self.execution_mode = execution_mode
        super().__init__(
            f"{execution_mode} sandbox exited with code {returncode} for "
            f"{issue_key} and produced no readable structured result at "
            f"{result_path}.\n"
            f"stdout:\n{stdout.strip()}\n"
            f"stderr:\n{stderr.strip()}"
        )


def is_hdfs_safe_mode_error(stderr: str) -> bool:
    message = stderr.lower()
    return "safe mode" in message and "name node" in message



def extract_runner_input(payload: dict[str, Any]) -> dict[str, str]:
    """Extract the issue key and latest valid /quant request from a trigger conf."""

    validated = validate_jira_command_payload(payload)

    return {
        "issue_key": validated.issue_key,
        "request_text": validated.request_text,
    }


def workspace_output_relative_path(workspace_path: str) -> PurePosixPath:
    """Return a safe path relative to /workspace/output."""

    posix_path = PurePosixPath(workspace_path)
    try:
        relative_path = posix_path.relative_to(WORKSPACE_OUTPUT_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"Runtime output path must be under {WORKSPACE_OUTPUT_ROOT}: "
            f"{workspace_path}"
        ) from exc

    if (
        not relative_path.parts
        or ".." in posix_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError(f"Runtime output path is not safe: {workspace_path}")

    return relative_path


def workspace_input_relative_path(workspace_path: str) -> PurePosixPath:
    """Return a safe path relative to /workspace/input."""

    posix_path = PurePosixPath(workspace_path)
    try:
        relative_path = posix_path.relative_to(WORKSPACE_INPUT_ROOT)
    except ValueError as exc:
        raise ValueError(
            f"Runtime input path must be under {WORKSPACE_INPUT_ROOT}: "
            f"{workspace_path}"
        ) from exc

    if (
        not relative_path.parts
        or not posix_path.name
        or ".." in posix_path.parts
        or ".." in relative_path.parts
    ):
        raise ValueError(f"Runtime input path is not safe: {workspace_path}")

    input_kind = relative_path.parts[0]
    if input_kind == "attachments":
        if len(relative_path.parts) < 3:
            raise ValueError(
                f"Runtime attachment path is not safe: {workspace_path}"
            )
    elif input_kind == "datasets":
        try:
            posix_path.relative_to(WORKSPACE_DATASET_ROOT)
        except ValueError as exc:
            raise ValueError(
                "Runtime input dataset path must be under "
                f"{WORKSPACE_DATASET_ROOT}: {workspace_path}"
            ) from exc
    else:
        raise ValueError(
            "Runtime input path must be below /workspace/input/attachments/ "
            f"or /workspace/input/datasets/: {workspace_path}"
        )

    return relative_path


def request_template_workspace_path(
    runtime_request: dict[str, Any],
) -> str | None:
    input_paths = runtime_request.get("input_paths")
    if not isinstance(input_paths, dict):
        return None
    value = input_paths.get("request_template_path")
    if not isinstance(value, str) or not value.strip():
        return None
    workspace_input_relative_path(value)
    return value


def stage_jira_request_attachment(validated: Any, input_dir: Path) -> Path | None:
    """Download the selected Jira .request into the mounted input directory."""

    attachment = getattr(validated, "request_attachment", None)
    workspace_path = request_template_workspace_path(validated.runtime_request)
    if attachment is None:
        if workspace_path is not None:
            raise ValueError("Runtime request names an attachment without Jira metadata")
        return None
    if workspace_path is None:
        raise ValueError("Selected Jira attachment has no runtime input path")

    relative_path = workspace_input_relative_path(workspace_path)
    if relative_path.name != attachment["filename"]:
        raise ValueError("Selected Jira attachment filename does not match input path")

    expected_size = attachment.get("size")
    try:
        expected_size = int(expected_size) if expected_size is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Jira request attachment has an invalid size") from exc
    if expected_size is not None and (
        expected_size <= 0 or expected_size > REQUEST_ATTACHMENT_MAX_BYTES
    ):
        raise ValueError(
            "Jira request attachment size must be between 1 byte and "
            f"{REQUEST_ATTACHMENT_MAX_BYTES} bytes"
        )

    destination = input_dir.joinpath(*relative_path.parts)
    input_root = input_dir.resolve()
    if not destination.resolve(strict=False).is_relative_to(input_root):
        raise ValueError("Jira request attachment resolves outside input directory")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

    # Attachments are arbitrary user uploads served under their own content
    # type, so a narrow Accept is answered with 406 Not Acceptable. Accept
    # anything; do not narrow this back to application/octet-stream.
    response = jira_request(
        "GET",
        f"/rest/api/3/attachment/content/{attachment['id']}",
        headers={"Accept": "*/*"},
        stream=True,
        allow_redirects=True,
        timeout=60,
    )
    try:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise ValueError(
                    "Jira attachment response has an invalid Content-Length"
                ) from exc
            if declared_size > REQUEST_ATTACHMENT_MAX_BYTES:
                raise ValueError("Jira request attachment exceeds the size limit")

        written = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > REQUEST_ATTACHMENT_MAX_BYTES:
                    raise ValueError("Jira request attachment exceeds the size limit")
                handle.write(chunk)
        if written == 0:
            raise ValueError("Jira request attachment is empty")
        if expected_size is not None and written != expected_size:
            raise ValueError(
                "Jira request attachment size does not match Jira metadata "
                f"(expected {expected_size}, received {written})"
            )
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()

    destination.chmod(0o444)
    print(
        "Staged Jira request attachment: "
        f"id={attachment['id']}, filename={attachment['filename']}, bytes={written}, "
        f"container_path={workspace_path}"
    )
    return destination

def resolve_workspace_input_file(input_dir: Path, workspace_path: str) -> Path:
    """Map a safe /workspace/input dataset path to the mounted host input dir."""

    relative_path = workspace_input_relative_path(workspace_path)
    local_path = input_dir.joinpath(*relative_path.parts)
    input_root = input_dir.resolve()
    resolved_path = local_path.resolve(strict=False)
    if not resolved_path.is_relative_to(input_root):
        raise ValueError(
            f"Runtime input path resolves outside input dir: {workspace_path}"
        )
    return local_path


def resolve_workspace_output_file(output_dir: Path, workspace_path: str) -> Path:
    """Map a safe /workspace/output file path to the mounted host output dir."""

    relative_path = workspace_output_relative_path(workspace_path)
    local_path = output_dir.joinpath(*relative_path.parts)
    output_root = output_dir.resolve()
    resolved_path = local_path.resolve(strict=False)
    if not resolved_path.is_relative_to(output_root):
        raise ValueError(
            f"Runtime output path resolves outside output dir: {workspace_path}"
        )

    return local_path


def hdfs_workspace_output_file(hdfs_output_dir: str, workspace_path: str) -> str:
    """Map a configured /workspace/output path to its HDFS output location."""

    relative_path = workspace_output_relative_path(workspace_path)
    return f"{hdfs_output_dir.rstrip('/')}/{relative_path.as_posix()}"


def get_runtime_output_path(
    runtime_request: dict[str, Any],
    output_name: str,
) -> str | None:
    output_paths = runtime_request.get("output_paths")
    if not isinstance(output_paths, dict):
        return None

    value = output_paths.get(output_name)
    if not isinstance(value, str) or not value.strip():
        return None

    return value


def read_result_file(
    output_dir: Path,
    result_path: str | None = DEFAULT_RESULT_PATH,
) -> dict[str, Any]:
    result_file = resolve_workspace_output_file(
        output_dir,
        result_path or DEFAULT_RESULT_PATH,
    )
    if not result_file.exists():
        raise FileNotFoundError(f"Sandbox did not write {result_file}")

    result = json.loads(result_file.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Sandbox result.json must contain a JSON object")

    return result


def read_structured_result(
    output_dir: Path,
    stdout: str,
    result_path: str | None = DEFAULT_RESULT_PATH,
) -> dict[str, Any]:
    """Prefer result.json, then fall back to the final stdout JSON line."""

    try:
        return read_result_file(output_dir, result_path)
    except FileNotFoundError:
        return parse_final_json_stdout(stdout)


def read_progress_events_file(
    output_dir: Path,
    progress_events_path: str | None,
) -> list[dict[str, Any]]:
    """Read optional progress JSONL side-channel events without failing the run."""

    if not progress_events_path:
        return []

    try:
        progress_file = resolve_workspace_output_file(output_dir, progress_events_path)
    except ValueError as exc:
        print(f"Warning: ignoring progress events path: {exc}")
        return []

    events: list[dict[str, Any]] = []
    saw_non_empty_line = False

    try:
        with progress_file.open(encoding="utf-8") as file:
            for line_number, raw_line in enumerate(file, start=1):
                line = raw_line.strip()
                if not line:
                    continue

                saw_non_empty_line = True
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        "Warning: ignoring malformed progress event "
                        f"{progress_file}:{line_number}: {exc.msg}"
                    )
                    continue

                if not isinstance(payload, dict):
                    print(
                        "Warning: ignoring non-object progress event "
                        f"{progress_file}:{line_number}"
                    )
                    continue

                if not is_supported_progress_event(payload):
                    print(
                        "Warning: ignoring unsupported progress event "
                        f"{progress_file}:{line_number}"
                    )
                    continue

                events.append(
                    {
                        key: value
                        for key, value in payload.items()
                        if key in PROGRESS_EVENT_FIELDS
                    }
                )
    except FileNotFoundError:
        print(f"Warning: progress events file is missing: {progress_file}")
        return []
    except OSError as exc:
        print(f"Warning: could not read progress events file {progress_file}: {exc}")
        return []

    if not saw_non_empty_line:
        print(f"Warning: progress events file is empty: {progress_file}")

    return events


def is_supported_progress_event(payload: dict[str, Any]) -> bool:
    required_fields = {
        "schema_version",
        "run_id",
        "ticket_id",
        "stage",
        "status",
        "timestamp",
    }
    if any(field not in payload for field in required_fields):
        return False

    if payload.get("schema_version") != "1.0":
        return False

    if not isinstance(payload.get("run_id"), str) or not payload["run_id"]:
        return False

    ticket_id = payload.get("ticket_id")
    if (
        not isinstance(ticket_id, str)
        or not JIRA_TICKET_KEY_PATTERN.fullmatch(ticket_id)
    ):
        return False

    if payload.get("stage") not in PROGRESS_EVENT_STAGES:
        return False

    if payload.get("status") not in PROGRESS_EVENT_STATUSES:
        return False

    if not isinstance(payload.get("timestamp"), str) or not payload["timestamp"]:
        return False

    iteration = payload.get("iteration")
    if iteration is not None and (
        not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 1
    ):
        return False

    for field in ("agent", "tool_call"):
        value = payload.get(field)
        if value is not None and (not isinstance(value, str) or not value):
            return False

    message = payload.get("message")
    if message is not None and (
        not isinstance(message, str) or len(message) > 500
    ):
        return False

    return True


def is_successful_result(result: dict[str, Any]) -> bool:
    execution_summary = result.get("execution_summary") or {}
    status = execution_summary.get("status") or result.get("status")
    return isinstance(status, str) and status.lower() in {"succeeded", "success"}


def safe_filename_prefix(value: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return prefix or "run"


def attachment_upload_name(run_id: str, local_path: Path) -> str:
    return f"{safe_filename_prefix(run_id)}__{local_path.name}"


def collect_referenced_artifact_paths(
    result: dict[str, Any],
    extra_workspace_paths: list[str],
) -> list[str]:
    paths: list[str] = []
    metrics = result.get("performance_metrics") or {}
    artifacts = result.get("generated_artifacts") or {}
    diagnostics = result.get("diagnostics") or {}

    for value in (
        metrics.get("time_series_data_path"),
        artifacts.get("backtest_plots_path"),
        # The .request the engine actually ran. It lives in the temporary output
        # mount, so it has to be uploaded here or it is lost with the directory.
        artifacts.get("executed_request_path"),
        diagnostics.get("raw_log_reference"),
        *extra_workspace_paths,
    ):
        if isinstance(value, str) and value:
            paths.append(value)

    return list(dict.fromkeys(paths))


def resolve_artifact_candidate(
    workspace_path: str,
    output_dir: Path,
) -> tuple[Path | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return local file path, missing record, skipped record."""

    try:
        posix_path = PurePosixPath(workspace_path)
        relative_path = posix_path.relative_to(WORKSPACE_OUTPUT_ROOT)
    except ValueError:
        return None, None, {
            "path": workspace_path,
            "reason": "outside /workspace/output",
        }

    if ".." in posix_path.parts:
        return None, None, {
            "path": workspace_path,
            "reason": "parent traversal is not allowed",
        }

    local_path = output_dir.joinpath(*relative_path.parts)
    output_root = output_dir.resolve()
    resolved_path = local_path.resolve()
    if not resolved_path.is_relative_to(output_root):
        return None, None, {
            "path": workspace_path,
            "reason": "resolved outside mounted output directory",
        }

    if not resolved_path.exists():
        return None, {
            "path": workspace_path,
            "reason": "file does not exist",
        }, None

    if not resolved_path.is_file():
        return None, None, {
            "path": workspace_path,
            "reason": "not a regular file",
        }

    size_bytes = resolved_path.stat().st_size
    if size_bytes > ARTIFACT_MAX_BYTES:
        return None, None, {
            "path": workspace_path,
            "reason": f"file exceeds {ARTIFACT_MAX_BYTES} byte limit",
            "size_bytes": size_bytes,
        }

    return resolved_path, None, None


def ingest_mounted_artifacts(
    issue_key: str,
    output_dir: Path,
    result: dict[str, Any],
    extra_workspace_paths: list[str],
) -> dict[str, Any]:
    upload_candidates: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    run_id = str(result.get("run_id") or issue_key)

    for workspace_path in collect_referenced_artifact_paths(result, extra_workspace_paths):
        local_path, missing_record, skipped_record = resolve_artifact_candidate(
            workspace_path,
            output_dir,
        )
        if missing_record:
            missing.append(missing_record)
        if skipped_record:
            skipped.append(skipped_record)
        if local_path:
            upload_candidates.append(
                {
                    "path": workspace_path,
                    "local_path": local_path,
                    "filename": attachment_upload_name(run_id, local_path),
                    "size_bytes": local_path.stat().st_size,
                }
            )

    upload_paths = []
    staged_paths = []
    for candidate in upload_candidates:
        source_path = candidate["local_path"]
        upload_name = candidate["filename"]
        if upload_name == source_path.name:
            upload_paths.append(source_path)
            continue
        staged_path = source_path.with_name(upload_name)
        staged_path.write_bytes(source_path.read_bytes())
        staged_paths.append(staged_path)
        upload_paths.append(staged_path)

    attachments = post_jira_attachments(
        issue_key,
        upload_paths,
    )
    for staged_path in staged_paths:
        staged_path.unlink(missing_ok=True)

    uploaded: list[dict[str, Any]] = []
    for candidate, attachment in zip(upload_candidates, attachments):
        uploaded.append(
            {
                "source_path": candidate["path"],
                "filename": attachment.get("filename") or candidate["filename"],
                "size_bytes": attachment.get("size") or candidate["size_bytes"],
                "jira_attachment_id": attachment.get("id"),
                "jira_attachment_url": attachment.get("content"),
            }
        )

    return {
        "uploaded": uploaded,
        "missing": missing,
        "skipped": skipped,
    }


def current_airflow_log_path(
    context: dict[str, Any],
    task_id_override: str | None = None,
) -> Path | None:
    task_instance = context.get("task_instance") or context.get("ti")
    dag_run = context.get("dag_run")
    if task_instance is None or dag_run is None:
        return None

    explicit_path = getattr(task_instance, "log_filepath", None)
    if explicit_path:
        return Path(explicit_path)

    base_log_folder = (
        os.environ.get("AIRFLOW_LOG_BASE_PATH")
        or os.environ.get("AIRFLOW__LOGGING__BASE_LOG_FOLDER")
        or "/opt/airflow3/logs_masteruser"
    )
    dag_id = getattr(task_instance, "dag_id", None) or getattr(dag_run, "dag_id", None)
    task_id = task_id_override or getattr(task_instance, "task_id", None)
    run_id = getattr(task_instance, "run_id", None) or getattr(dag_run, "run_id", None)
    try_number = getattr(task_instance, "try_number", None) or 1
    map_index = getattr(task_instance, "map_index", -1)
    if not dag_id or not task_id or not run_id:
        return None

    path = (
        Path(base_log_folder)
        / f"dag_id={dag_id}"
        / f"run_id={run_id}"
        / f"task_id={task_id}"
    )
    if map_index not in (None, -1):
        path = path / f"map_index={map_index}"
    return path / f"attempt={try_number}.log"


def safe_airflow_attachment_component(value: Any) -> str:
    component = re.sub(r"[^A-Za-z0-9._:=+-]+", "_", str(value)).strip("_")
    return component or "unknown"


def airflow_log_attachment_name(
    context: dict[str, Any],
    task_id: str,
    log_path: Path,
) -> str:
    task_instance = context.get("task_instance") or context.get("ti")
    dag_run = context.get("dag_run")
    dag_id = getattr(task_instance, "dag_id", None) or getattr(dag_run, "dag_id", None)
    run_id = getattr(task_instance, "run_id", None) or getattr(dag_run, "run_id", None)
    run_label = str(run_id or "unknown")
    if "__" in run_label:
        run_label = run_label.split("__", 1)[1]
    run_label = re.sub(r"(?:[+-]\d{2}:\d{2}|Z)$", "", run_label)
    return (
        f"{safe_airflow_attachment_component(dag_id)}_"
        f"{safe_airflow_attachment_component(run_label)}_"
        f"{safe_airflow_attachment_component(task_id)}_"
        f"{safe_airflow_attachment_component(log_path.name)}"
    )


def attach_airflow_log(
    issue_key: str,
    context: dict[str, Any] | None,
    task_id: str = "run_sandbox_and_writeback",
) -> dict[str, Any]:
    if not context:
        return {
            "uploaded": [],
            "missing": [],
            "skipped": [{"path": "airflow task log", "reason": "Airflow context unavailable"}],
        }

    log_path = current_airflow_log_path(context, task_id_override=task_id)
    if log_path is None:
        return {
            "uploaded": [],
            "missing": [],
            "skipped": [{"path": "airflow task log", "reason": "log path unavailable"}],
        }
    if not log_path.exists():
        return {
            "uploaded": [],
            "missing": [
                {
                    "path": str(log_path),
                    "reason": "file does not exist",
                }
            ],
            "skipped": [],
        }
    if not log_path.is_file():
        return {
            "uploaded": [],
            "missing": [],
            "skipped": [{"path": str(log_path), "reason": "not a regular file"}],
        }
    size_bytes = log_path.stat().st_size
    if size_bytes > ARTIFACT_MAX_BYTES:
        return {
            "uploaded": [],
            "missing": [],
            "skipped": [
                {
                    "path": str(log_path),
                    "reason": f"file exceeds {ARTIFACT_MAX_BYTES} byte limit",
                    "size_bytes": size_bytes,
                }
            ],
        }

    upload_name = airflow_log_attachment_name(context, task_id, log_path)
    with tempfile.TemporaryDirectory(prefix="airflow-log-attachment-") as temp_dir:
        staged_path = Path(temp_dir) / upload_name
        staged_path.write_bytes(log_path.read_bytes())
        attachments = post_jira_attachments(issue_key, [staged_path])

    uploaded = []
    for attachment in attachments:
        uploaded.append(
            {
                "source_path": str(log_path),
                "filename": attachment.get("filename") or upload_name,
                "size_bytes": attachment.get("size") or size_bytes,
                "jira_attachment_id": attachment.get("id"),
                "jira_attachment_url": attachment.get("content"),
            }
        )
    return {"uploaded": uploaded, "missing": [], "skipped": []}



def safe_path_component(value: str) -> str:
    """Return a conservative string that is safe to place in an HDFS path."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-.") or "unknown"

def parse_int_config(name: str, value: str | None) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def parse_float_config(name: str, value: str | None) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def parse_bool_config(name: str, value: str | None) -> bool:
    normalized = str(value or "false").strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be a boolean: true/false")


def parse_hdfs_allowed_roots(value: str | None) -> list[str]:
    """Parse an Airflow Variable containing a JSON array or comma-separated roots."""

    raw = str(value or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and item.strip() for item in parsed
    ):
        raise ValueError(
            "SANDBOX_INPUT_HDFS_ALLOWED_ROOTS must be a JSON array or "
            "comma-separated list of hdfs:// roots"
        )
    return [item.strip() for item in parsed]


def canonical_hdfs_uri(uri: str, namenode_uri: str) -> str:
    """Return an absolute, normalised HDFS URI using the configured authority."""

    parsed = urlparse(str(uri))
    if parsed.scheme.lower() != "hdfs" or not parsed.path.startswith("/"):
        raise ValueError(f"Invalid HDFS input URI: {uri}")
    if parsed.query or parsed.fragment or parsed.params or ".." in PurePosixPath(parsed.path).parts:
        raise ValueError(f"Unsafe HDFS input URI: {uri}")

    configured = urlparse(str(namenode_uri))
    authority = parsed.netloc or configured.netloc
    if not authority:
        raise ValueError(
            "SANDBOX_HDFS_NAMENODE_URI must include an HDFS authority, "
            "for example hdfs://bialobog:8020"
        )
    normalized_path = PurePosixPath(parsed.path).as_posix()
    return urlunparse(("hdfs", authority, normalized_path, "", "", ""))


def hdfs_uri_is_under_root(source_uri: str, root_uri: str) -> bool:
    source = urlparse(source_uri)
    root = urlparse(root_uri)
    if source.scheme != "hdfs" or root.scheme != "hdfs":
        return False
    if source.netloc.lower() != root.netloc.lower():
        return False
    source_path = PurePosixPath(source.path).as_posix()
    root_path = PurePosixPath(root.path).as_posix().rstrip("/") or "/"
    return source_path == root_path or source_path.startswith(root_path + "/")


def resolve_input_dataset_size_config() -> dict[str, int]:
    """Resolve size limits shared by HDFS and Jira dataset inputs."""

    max_file_bytes = parse_int_config(
        "SANDBOX_INPUT_MAX_FILE_BYTES",
        get_airflow_variable(
            "SANDBOX_INPUT_MAX_FILE_BYTES",
            str(DEFAULT_INPUT_MAX_FILE_BYTES),
        ),
    )
    max_total_bytes = parse_int_config(
        "SANDBOX_INPUT_MAX_TOTAL_BYTES",
        get_airflow_variable(
            "SANDBOX_INPUT_MAX_TOTAL_BYTES",
            str(DEFAULT_INPUT_MAX_TOTAL_BYTES),
        ),
    )
    if max_file_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("Input dataset size limits must be positive integers")
    if max_total_bytes < max_file_bytes:
        raise ValueError(
            "SANDBOX_INPUT_MAX_TOTAL_BYTES must be at least "
            "SANDBOX_INPUT_MAX_FILE_BYTES"
        )
    return {
        "max_file_bytes": max_file_bytes,
        "max_total_bytes": max_total_bytes,
    }


def resolve_hdfs_input_config() -> dict[str, Any]:
    namenode_uri = require_yarn_config("SANDBOX_HDFS_NAMENODE_URI").rstrip("/")
    allowed_raw = get_airflow_variable("SANDBOX_INPUT_HDFS_ALLOWED_ROOTS", "")
    allowed_roots = [
        canonical_hdfs_uri(root, namenode_uri)
        for root in parse_hdfs_allowed_roots(allowed_raw)
    ]
    return {
        "hadoop_home": (
            get_airflow_variable("HADOOP_HOME", DEFAULT_HADOOP_HOME)
            or DEFAULT_HADOOP_HOME
        ),
        "hadoop_conf_dir": (
            get_airflow_variable("HADOOP_CONF_DIR", DEFAULT_HADOOP_CONF_DIR)
            or DEFAULT_HADOOP_CONF_DIR
        ),
        "hdfs_namenode_uri": namenode_uri,
        "hdfs_safe_mode_wait_seconds": parse_int_config(
            "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS",
            get_airflow_variable(
                "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS",
                str(DEFAULT_HDFS_SAFE_MODE_WAIT_SECONDS),
            ),
        ),
        "allowed_roots": allowed_roots,
        **resolve_input_dataset_size_config(),
    }


def resolve_input_dataset_materialization_config(
    runtime_request: dict[str, Any],
) -> dict[str, Any]:
    """Use HDFS settings only when the request actually has an HDFS source."""

    datasets = input_datasets_from_request(runtime_request)
    if any(str(item.get("source_kind") or "") == "hdfs" for item in datasets):
        return resolve_hdfs_input_config()
    return resolve_input_dataset_size_config()


def resolve_workflow_retry_policy() -> WorkflowRetryPolicy:
    policy = WorkflowRetryPolicy(
        max_attempts=parse_int_config(
            "WORKFLOW_RETRY_MAX_ATTEMPTS",
            get_airflow_variable("WORKFLOW_RETRY_MAX_ATTEMPTS", "3"),
        ),
        initial_delay_seconds=parse_float_config(
            "WORKFLOW_RETRY_INITIAL_DELAY_SECONDS",
            get_airflow_variable("WORKFLOW_RETRY_INITIAL_DELAY_SECONDS", "60"),
        ),
        max_delay_seconds=parse_float_config(
            "WORKFLOW_RETRY_MAX_DELAY_SECONDS",
            get_airflow_variable("WORKFLOW_RETRY_MAX_DELAY_SECONDS", "300"),
        ),
        backoff_multiplier=parse_float_config(
            "WORKFLOW_RETRY_BACKOFF_MULTIPLIER",
            get_airflow_variable("WORKFLOW_RETRY_BACKOFF_MULTIPLIER", "2"),
        ),
        jitter_ratio=parse_float_config(
            "WORKFLOW_RETRY_JITTER_RATIO",
            get_airflow_variable("WORKFLOW_RETRY_JITTER_RATIO", "0.2"),
        ),
    )
    if policy.max_attempts < 1:
        raise ValueError("WORKFLOW_RETRY_MAX_ATTEMPTS must be at least 1")
    if policy.initial_delay_seconds < 0:
        raise ValueError("WORKFLOW_RETRY_INITIAL_DELAY_SECONDS must be non-negative")
    if policy.max_delay_seconds < 0:
        raise ValueError("WORKFLOW_RETRY_MAX_DELAY_SECONDS must be non-negative")
    if policy.backoff_multiplier < 1:
        raise ValueError("WORKFLOW_RETRY_BACKOFF_MULTIPLIER must be at least 1")
    if policy.jitter_ratio < 0:
        raise ValueError("WORKFLOW_RETRY_JITTER_RATIO must be non-negative")
    return policy


def workflow_retry_delay(
    failed_attempt_number: int,
    policy: WorkflowRetryPolicy,
) -> float:
    delay = policy.initial_delay_seconds * (
        policy.backoff_multiplier ** max(0, failed_attempt_number - 1)
    )
    delay = min(delay, policy.max_delay_seconds)
    if policy.jitter_ratio:
        jitter = delay * policy.jitter_ratio
        delay += random.uniform(-jitter, jitter)
    return max(0.0, delay)


def workflow_state_dir() -> Path:
    configured = get_airflow_variable(
        "WORKFLOW_RECOVERY_STATE_DIR",
        DEFAULT_WORKFLOW_RECOVERY_STATE_DIR,
    )
    return Path(configured or DEFAULT_WORKFLOW_RECOVERY_STATE_DIR)


def build_workflow_id(issue_key: str, comment_id: str, request_text: str) -> str:
    request_hash = hashlib.sha256(request_text.encode("utf-8")).hexdigest()[:12]
    stable_comment_id = safe_filename_prefix(comment_id or "latest")
    return f"{safe_filename_prefix(issue_key)}_{stable_comment_id}_{request_hash}"


def build_workflow_id_from_payload(payload: dict[str, Any]) -> str | None:
    try:
        ticket = payload.get("ticket")
        request = payload.get("request")
        if not isinstance(ticket, dict) or not isinstance(request, dict):
            return None
        issue_key = ticket.get("key")
        comments = request.get("comments")
        if not isinstance(issue_key, str) or not isinstance(comments, list):
            return None
        latest_comment = select_latest_quant_comment(comments)
        if latest_comment is None:
            return None
        return build_workflow_id(
            issue_key.strip(),
            latest_comment.get("comment_id", ""),
            latest_comment["request_text"],
        )
    except Exception:
        return None


def workflow_state_path(workflow_id: str) -> Path:
    return workflow_state_dir() / f"{safe_filename_prefix(workflow_id)}.json"


def load_workflow_state(workflow_id: str) -> dict[str, Any]:
    path = workflow_state_path(workflow_id)
    if not path.exists():
        return {}
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError(f"Workflow state file {path} must contain a JSON object")
    return state


def save_workflow_state(workflow_id: str, state: dict[str, Any]) -> None:
    path = workflow_state_path(workflow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["workflow_id"] = workflow_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    temp_path = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def initialize_workflow_state(
    workflow_id: str,
    issue_key: str,
    comment_id: str,
    request_text: str,
    dag_run_id: str,
    existing_state: dict[str, Any],
) -> dict[str, Any]:
    state = dict(existing_state)
    state.setdefault("workflow_id", workflow_id)
    state.setdefault("issue_key", issue_key)
    state.setdefault("latest_comment_id", comment_id)
    state.setdefault(
        "request_hash",
        hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
    )
    state.setdefault("dag_run_id", dag_run_id)
    state.setdefault("status", "running")
    state.setdefault("stages", {})
    return state


def exception_message(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def structured_result_unavailable_error(
    exc: Exception,
) -> SandboxStructuredResultUnavailable | None:
    if isinstance(exc, SandboxStructuredResultUnavailable):
        return exc
    if (
        isinstance(exc, WorkflowRetryExhausted)
        and isinstance(exc.last_error, SandboxStructuredResultUnavailable)
    ):
        return exc.last_error
    return None


def is_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, JiraCommandValidationError):
        return False
    if isinstance(exc, SandboxStructuredResultUnavailable):
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return status_code == 429 or (
            isinstance(status_code, int) and 500 <= status_code <= 599
        )
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, HadoopCommandError)):
        return True

    message = str(exc).lower()
    retryable_markers = (
        "timed out",
        "timeout",
        "temporarily unavailable",
        "connection reset",
        "connection refused",
        "rate limit",
        "too many requests",
        "produced no readable structured result",
        "produced no readable hdfs result",
        "exited with code",
        "yarn sandbox timed out",
        "sandbox timed out",
    )
    terminal_markers = (
        "jira command validation failed",
        "payload is missing",
        "unapproved",
        "schema validation",
    )
    if any(marker in message for marker in terminal_markers):
        return False
    return any(marker in message for marker in retryable_markers)


def run_retryable_stage(
    workflow_id: str,
    state: dict[str, Any],
    stage_name: str,
    policy: WorkflowRetryPolicy,
    operation,
) -> Any:
    stages = state.setdefault("stages", {})
    stage = stages.setdefault(stage_name, {"status": "pending", "attempts": 0})
    last_error: Exception | None = None

    while int(stage.get("attempts") or 0) < policy.max_attempts:
        stage["attempts"] = int(stage.get("attempts") or 0) + 1
        stage["status"] = "running"
        stage.pop("next_retry_at", None)
        print(
            f"Workflow {workflow_id} stage {stage_name} attempt "
            f"{stage['attempts']}/{policy.max_attempts}"
        )
        save_workflow_state(workflow_id, state)
        try:
            result = operation()
        except Exception as exc:
            last_error = exc
            stage["last_error"] = exception_message(exc)
            stage["last_error_type"] = type(exc).__name__
            if not is_retryable_exception(exc):
                stage["status"] = "failed"
                save_workflow_state(workflow_id, state)
                raise

            attempts = int(stage.get("attempts") or 0)
            if attempts >= policy.max_attempts:
                stage["status"] = "failed"
                save_workflow_state(workflow_id, state)
                raise WorkflowRetryExhausted(stage_name, exc) from exc

            delay = workflow_retry_delay(attempts, policy)
            next_retry = datetime.now(timezone.utc) + timedelta(seconds=delay)
            stage["status"] = "retry_scheduled"
            stage["next_retry_at"] = next_retry.isoformat()
            print(
                f"Workflow {workflow_id} stage {stage_name} failed with "
                f"{exception_message(exc)}; retrying in {delay:.1f}s"
            )
            save_workflow_state(workflow_id, state)
            if delay:
                time.sleep(delay)
            continue

        stage["status"] = "succeeded"
        stage["last_error"] = None
        stage.pop("last_error_type", None)
        stage.pop("next_retry_at", None)
        save_workflow_state(workflow_id, state)
        return result

    if last_error is None:
        last_error = RuntimeError(f"Workflow stage {stage_name} had no attempts left")
    raise WorkflowRetryExhausted(stage_name, last_error)


def fetch_existing_jira_comments(issue_key: str) -> list[dict[str, Any]]:
    response = jira_request(
        "GET",
        f"/rest/api/3/issue/{issue_key}/comment",
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    comments = payload.get("comments") if isinstance(payload, dict) else None
    return comments if isinstance(comments, list) else []


def has_existing_workflow_comment(
    issue_key: str,
    workflow_id: str,
    *,
    include_partial: bool = True,
) -> bool:
    marker = f"Workflow ID: {workflow_id}"
    for comment in fetch_existing_jira_comments(issue_key):
        if not isinstance(comment, dict):
            continue
        plain_text = extract_plain_text_from_adf(comment.get("body"))
        if BOT_MARKER in plain_text and marker in plain_text:
            if include_partial or "Final Status: not available" not in plain_text:
                return True
    return False


def post_result_comment_once(
    issue_key: str,
    result: dict[str, Any],
    workflow_id: str,
) -> None:
    if has_existing_workflow_comment(issue_key, workflow_id, include_partial=False):
        print(f"Workflow {workflow_id} result comment already exists for {issue_key}")
        return
    post_jira_adf_comment(issue_key, format_result_comment_adf(result))


def post_failure_comment_once(
    issue_key: str,
    error: Exception,
    dag_run_id: str,
    workflow_id: str,
    runtime_request: dict[str, Any] | None = None,
) -> None:
    if has_existing_workflow_comment(issue_key, workflow_id, include_partial=False):
        print(f"Workflow {workflow_id} failure comment already exists for {issue_key}")
        return
    post_progress_milestone_comment(
        issue_key,
        runtime_request or {},
        dag_run_id,
        workflow_id,
        "failed",
        diagnostics={
            "dag_run_id": dag_run_id,
            "error_type": type(error).__name__,
            "failure_source": "docker_sandbox_runner",
        },
    )


def post_progress_milestone_comment(
    issue_key: str,
    runtime_request: dict[str, Any],
    dag_run_id: str,
    workflow_id: str,
    milestone: str,
    *,
    progress_events: list[dict[str, Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    request = dict(runtime_request)
    request.setdefault("run_id", dag_run_id)
    request["workflow_id"] = workflow_id
    post_jira_adf_comment(
        issue_key,
        build_workflow_progress_adf(
            runtime_request=request,
            progress_events=progress_events,
            diagnostics=diagnostics,
            state=milestone,
        ),
    )


def post_progress_milestone_if_needed(
    workflow_id: str,
    state: dict[str, Any],
    issue_key: str,
    runtime_request: dict[str, Any],
    dag_run_id: str,
    milestone: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    stage_name = f"jira_progress_{safe_filename_prefix(milestone)}"
    stage = state.setdefault("stages", {}).setdefault(
        stage_name,
        {"status": "pending", "attempts": 0},
    )
    if stage.get("status") == "succeeded":
        return

    stage["attempts"] = int(stage.get("attempts") or 0) + 1
    stage["status"] = "running"
    save_workflow_state(workflow_id, state)
    try:
        post_progress_milestone_comment(
            issue_key,
            runtime_request,
            dag_run_id,
            workflow_id,
            milestone,
            diagnostics=diagnostics,
        )
    except Exception as exc:
        stage["status"] = "failed"
        stage["last_error"] = exception_message(exc)
        stage["last_error_type"] = type(exc).__name__
        save_workflow_state(workflow_id, state)
        print(
            f"Warning: workflow {workflow_id} could not post {milestone} "
            f"progress milestone for {issue_key}: {exception_message(exc)}"
        )
        return

    stage["status"] = "succeeded"
    stage["last_error"] = None
    stage.pop("last_error_type", None)
    save_workflow_state(workflow_id, state)


def post_partial_progress_comment_once(
    issue_key: str,
    error: SandboxStructuredResultUnavailable,
    dag_run_id: str,
    workflow_id: str,
) -> None:
    if has_existing_workflow_comment(issue_key, workflow_id, include_partial=False):
        print(
            f"Workflow {workflow_id} partial progress comment already exists "
            f"for {issue_key}"
        )
        return

    runtime_request = dict(error.runtime_request)
    runtime_request.setdefault("run_id", dag_run_id)
    runtime_request["workflow_id"] = workflow_id
    post_jira_adf_comment(
        issue_key,
        build_workflow_progress_adf(
            runtime_request=runtime_request,
            progress_events=error.progress_events,
            diagnostics={
                "execution_mode": error.execution_mode,
                "exit_code": error.returncode,
                "result_path": error.result_path,
            },
            state="partial",
        ),
    )


def require_yarn_config(name: str) -> str:
    value = get_airflow_variable(name)
    if not value:
        raise ValueError(
            f"Missing {name}: configure Airflow Variable {name} for YARN mode"
        )
    return value


def resolve_yarn_config() -> dict[str, Any]:
    """Resolve YARN/Hadoop settings when YARN execution is selected."""

    return {
        "hadoop_home": (
            get_airflow_variable("HADOOP_HOME", DEFAULT_HADOOP_HOME)
            or DEFAULT_HADOOP_HOME
        ),
        "hadoop_conf_dir": (
            get_airflow_variable("HADOOP_CONF_DIR", DEFAULT_HADOOP_CONF_DIR)
            or DEFAULT_HADOOP_CONF_DIR
        ),
        "hdfs_namenode_uri": require_yarn_config("SANDBOX_HDFS_NAMENODE_URI").rstrip("/"),
        "hdfs_run_root": require_yarn_config("SANDBOX_HDFS_RUN_ROOT").rstrip("/"),
        "yarn_queue": require_yarn_config("SANDBOX_YARN_QUEUE"),
        "master_memory_mb": parse_int_config(
            "SANDBOX_YARN_MASTER_MEMORY_MB",
            get_airflow_variable(
                "SANDBOX_YARN_MASTER_MEMORY_MB",
                str(DEFAULT_YARN_MASTER_MEMORY_MB),
            ),
        ),
        "container_memory_mb": parse_int_config(
            "SANDBOX_YARN_CONTAINER_MEMORY_MB",
            get_airflow_variable(
                "SANDBOX_YARN_CONTAINER_MEMORY_MB",
                str(DEFAULT_YARN_CONTAINER_MEMORY_MB),
            ),
        ),
        "timeout_seconds": parse_int_config(
            "SANDBOX_YARN_TIMEOUT_SECONDS",
            get_airflow_variable(
                "SANDBOX_YARN_TIMEOUT_SECONDS",
                str(DEFAULT_YARN_TIMEOUT_SECONDS),
            ),
        ),
        "hdfs_safe_mode_wait_seconds": parse_int_config(
            "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS",
            get_airflow_variable(
                "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS",
                str(DEFAULT_HDFS_SAFE_MODE_WAIT_SECONDS),
            ),
        ),
        "cleanup_hdfs": parse_bool_config(
            "SANDBOX_YARN_CLEANUP_HDFS",
            get_airflow_variable("SANDBOX_YARN_CLEANUP_HDFS", "false"),
        ),
    }


def make_hdfs_uri(path: str, yarn_config: dict[str, Any]) -> str:
    """Convert an HDFS path into a full URI.

    Worker nodes may not share the scheduler's default HDFS configuration, so
    YARN mode uses explicit HDFS URIs for run input and output paths.
    """
    if path.startswith("hdfs://"):
        return path
    namenode_uri = str(yarn_config["hdfs_namenode_uri"]).rstrip("/")
    return f"{namenode_uri}/{path.lstrip('/')}"

def hadoop_environment(yarn_config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    hadoop_home = str(yarn_config["hadoop_home"])
    env.update(
        {
            "HADOOP_HOME": hadoop_home,
            "HADOOP_CONF_DIR": str(yarn_config["hadoop_conf_dir"]),
        }
    )
    env.pop("HADOOP_COMMON_HOME", None)
    env.pop("HADOOP_HDFS_HOME", None)
    env.pop("HADOOP_YARN_HOME", None)
    env.pop("HADOOP_MAPRED_HOME", None)
    env.pop("YARN_CONF_DIR", None)
    env["PATH"] = f"{hadoop_home}/bin:{hadoop_home}/sbin:{env.get('PATH', '')}"
    return env

def run_hadoop_command(
    command: list[str],
    yarn_config: dict[str, Any],
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    wait_seconds = int(yarn_config.get("hdfs_safe_mode_wait_seconds", 0) or 0)
    retry_delay_seconds = 10
    deadline = time.monotonic() + wait_seconds

    while True:
        completed = subprocess.run(
            command,
            env=hadoop_environment(yarn_config),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode == 0:
            return completed

        if (
            wait_seconds > 0
            and is_hdfs_safe_mode_error(completed.stderr)
            and time.monotonic() < deadline
        ):
            sleep_seconds = min(retry_delay_seconds, max(0.0, deadline - time.monotonic()))
            print(
                "HDFS NameNode is in safe mode; retrying Hadoop command "
                f"in {sleep_seconds:.0f}s."
            )
            time.sleep(sleep_seconds)
            continue

        raise HadoopCommandError(
            command,
            completed.stdout,
            completed.stderr,
            returncode=completed.returncode,
        )

def hdfs_exists(path: str, yarn_config: dict[str, Any]) -> bool:
    hadoop_home = str(yarn_config["hadoop_home"])
    completed = subprocess.run(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-test", "-e", path],
        env=hadoop_environment(yarn_config),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return completed.returncode == 0

def write_request_to_hdfs(
    payload: dict[str, Any],
    hdfs_request_path: str,
    yarn_config: dict[str, Any],
) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as tmp:
        json.dump(payload, tmp)
        tmp_path = tmp.name

    try:
        hadoop_home = str(yarn_config["hadoop_home"])
        run_hadoop_command(
            [f"{hadoop_home}/bin/hdfs", "dfs", "-put", "-f", tmp_path, hdfs_request_path],
            yarn_config,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def copy_input_file_to_hdfs(
    local_path: Path,
    hdfs_path: str,
    yarn_config: dict[str, Any],
) -> None:
    hadoop_home = str(yarn_config["hadoop_home"])
    hdfs_parent = hdfs_path.rsplit("/", 1)[0]
    run_hadoop_command(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-mkdir", "-p", hdfs_parent],
        yarn_config,
    )
    run_hadoop_command(
        [
            f"{hadoop_home}/bin/hdfs",
            "dfs",
            "-put",
            "-f",
            str(local_path),
            hdfs_path,
        ],
        yarn_config,
    )

def input_datasets_from_request(
    runtime_request: dict[str, Any],
) -> list[dict[str, Any]]:
    datasets = runtime_request.get("input_datasets") or []
    if not isinstance(datasets, list):
        raise ValueError("runtime_request.input_datasets must be an array")
    return [dict(item) for item in datasets]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hdfs_regular_file_size(
    source_uri: str,
    hadoop_home: str,
    hdfs_config: dict[str, Any],
) -> int:
    """Return the remote regular-file size before transferring its contents."""

    run_hadoop_command(
        [
            f"{hadoop_home}/bin/hdfs",
            "dfs",
            "-test",
            "-f",
            source_uri,
        ],
        hdfs_config,
    )
    completed = run_hadoop_command(
        [
            f"{hadoop_home}/bin/hdfs",
            "dfs",
            "-stat",
            "%b",
            source_uri,
        ],
        hdfs_config,
    )
    try:
        return int(completed.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"HDFS did not return a valid size for input: {source_uri}"
        ) from exc


def validate_approved_hdfs_input_uri(
    source_uri: str,
    hdfs_config: dict[str, Any],
) -> str:
    canonical = canonical_hdfs_uri(
        source_uri,
        str(hdfs_config["hdfs_namenode_uri"]),
    )
    allowed_roots = hdfs_config.get("allowed_roots") or []
    if not allowed_roots:
        raise ValueError(
            "HDFS input was requested but SANDBOX_INPUT_HDFS_ALLOWED_ROOTS "
            "is empty"
        )
    if not any(hdfs_uri_is_under_root(canonical, root) for root in allowed_roots):
        raise ValueError(
            "HDFS input URI is outside the approved roots: "
            f"{canonical}"
        )
    return canonical


def download_jira_dataset_attachment(
    dataset: dict[str, Any],
    destination: Path,
    *,
    max_bytes: int,
) -> int:
    """Download one selected Jira dataset attachment with bounded streaming."""

    attachment_id = str(dataset.get("jira_attachment_id") or "")
    if not attachment_id.isdigit():
        raise ValueError("Jira input dataset is missing a numeric attachment id")
    source_uri = str(dataset.get("source_uri") or "")
    if source_uri != f"jira-attachment://{attachment_id}":
        raise ValueError("Jira input dataset source URI does not match its attachment id")

    expected_size = dataset.get("size_bytes")
    try:
        expected_size = int(expected_size) if expected_size is not None else None
    except (TypeError, ValueError) as exc:
        raise ValueError("Jira input dataset has an invalid size") from exc
    if expected_size is not None and (expected_size <= 0 or expected_size > max_bytes):
        raise ValueError(
            "Jira input dataset size must be between 1 byte and "
            f"{max_bytes} bytes"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    # See stage_jira_request_attachment: a narrow Accept draws a 406.
    response = jira_request(
        "GET",
        f"/rest/api/3/attachment/content/{attachment_id}",
        headers={"Accept": "*/*"},
        stream=True,
        allow_redirects=True,
        timeout=60,
    )
    try:
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as exc:
                raise ValueError(
                    "Jira dataset response has an invalid Content-Length"
                ) from exc
            if declared_size <= 0 or declared_size > max_bytes:
                raise ValueError("Jira input dataset exceeds the size limit")

        written = 0
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("Jira input dataset exceeds the size limit")
                handle.write(chunk)
        if written == 0:
            raise ValueError("Jira input dataset is empty")
        if expected_size is not None and written != expected_size:
            raise ValueError(
                "Jira input dataset size does not match Jira metadata "
                f"(expected {expected_size}, received {written})"
            )
        return written
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def materialize_hdfs_input_datasets(
    runtime_request: dict[str, Any],
    input_dir: Path,
    hdfs_config: dict[str, Any],
) -> dict[str, Any]:
    """Materialise approved HDFS or Jira attachment files into the input mount."""

    datasets = input_datasets_from_request(runtime_request)
    if not datasets:
        return runtime_request

    updated_request = json.loads(json.dumps(runtime_request))
    updated_datasets: list[dict[str, Any]] = []
    total_bytes = 0
    for dataset in datasets:
        source_kind = str(dataset.get("source_kind") or "")
        container_path = str(dataset.get("container_path") or "")
        local_path = resolve_workspace_input_file(input_dir, container_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        if source_kind == "hdfs":
            hadoop_home = str(hdfs_config["hadoop_home"])
            source_uri = validate_approved_hdfs_input_uri(
                str(dataset.get("source_uri") or ""),
                hdfs_config,
            )

            # Check the remote HDFS size before downloading the file.
            remote_size_bytes = hdfs_regular_file_size(
                source_uri,
                hadoop_home,
                hdfs_config,
            )
            if remote_size_bytes > int(hdfs_config["max_file_bytes"]):
                raise ValueError(
                    f"HDFS input {dataset.get('original_filename')} is "
                    f"{remote_size_bytes} bytes, above "
                    "SANDBOX_INPUT_MAX_FILE_BYTES"
                )
            if total_bytes + remote_size_bytes > int(
                hdfs_config["max_total_bytes"]
            ):
                raise ValueError(
                    "Total HDFS input size exceeds "
                    "SANDBOX_INPUT_MAX_TOTAL_BYTES"
                )

            run_hadoop_command(
                [
                    f"{hadoop_home}/bin/hdfs",
                    "dfs",
                    "-get",
                    "-f",
                    source_uri,
                    str(local_path),
                ],
                hdfs_config,
            )
            if not local_path.is_file():
                raise ValueError(
                    f"HDFS input did not materialise as a regular file: "
                    f"{source_uri}"
                )
            dataset["source_uri"] = source_uri

        elif source_kind == "jira_attachment":
            download_jira_dataset_attachment(
                dataset,
                local_path,
                max_bytes=int(hdfs_config["max_file_bytes"]),
            )

        else:
            raise ValueError(
                "Input dataset source_kind must be hdfs or jira_attachment"
            )

        size_bytes = local_path.stat().st_size
        if size_bytes > int(hdfs_config["max_file_bytes"]):
            raise ValueError(
                f"HDFS input {dataset.get('original_filename')} is {size_bytes} "
                "bytes, above SANDBOX_INPUT_MAX_FILE_BYTES"
            )
        total_bytes += size_bytes
        if total_bytes > int(hdfs_config["max_total_bytes"]):
            raise ValueError(
                "Total HDFS input size exceeds SANDBOX_INPUT_MAX_TOTAL_BYTES"
            )

        checksum = sha256_file(local_path)
        local_path.chmod(0o444)
        dataset["size_bytes"] = size_bytes
        dataset["sha256"] = checksum
        updated_datasets.append(dataset)
        print(
            "Materialised HDFS input dataset: "
            f"dataset_id={dataset.get('dataset_id')}, "
            f"size_bytes={size_bytes}, sha256={checksum}"
        )

    updated_request["input_datasets"] = updated_datasets
    return updated_request


def stage_input_datasets_in_run_hdfs(
    runtime_request: dict[str, Any],
    local_input_dir: Path,
    hdfs_run_dir: str,
    yarn_config: dict[str, Any],
) -> dict[str, Any]:
    """Upload materialised files into the unique run HDFS directory."""

    datasets = input_datasets_from_request(runtime_request)
    if not datasets:
        return runtime_request

    updated_request = json.loads(json.dumps(runtime_request))
    updated_datasets: list[dict[str, Any]] = []
    hadoop_home = str(yarn_config["hadoop_home"])

    for dataset in datasets:
        relative_path = workspace_input_relative_path(
            str(dataset["container_path"])
        )
        local_path = local_input_dir.joinpath(*relative_path.parts)
        staged_uri = (
            f"{hdfs_run_dir.rstrip('/')}/input/{relative_path.as_posix()}"
        )
        staged_parent = staged_uri.rsplit("/", 1)[0]
        run_hadoop_command(
            [f"{hadoop_home}/bin/hdfs", "dfs", "-mkdir", "-p", staged_parent],
            yarn_config,
        )
        run_hadoop_command(
            [
                f"{hadoop_home}/bin/hdfs",
                "dfs",
                "-put",
                "-f",
                str(local_path),
                staged_uri,
            ],
            yarn_config,
        )
        dataset["run_staged_hdfs_uri"] = staged_uri
        updated_datasets.append(dataset)

    updated_request["input_datasets"] = updated_datasets
    return updated_request

def copy_hdfs_output_to_local(
    hdfs_output_dir: str,
    output_dir: Path,
    yarn_config: dict[str, Any],
) -> None:
    hadoop_home = str(yarn_config["hadoop_home"])
    run_hadoop_command(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-get", "-f", f"{hdfs_output_dir}/*", str(output_dir)],
        yarn_config,
    )

def remove_hdfs_file_if_exists(path: str, yarn_config: dict[str, Any]) -> None:
    hadoop_home = str(yarn_config["hadoop_home"])
    try:
        run_hadoop_command(
            [f"{hadoop_home}/bin/hdfs", "dfs", "-rm", "-f", path],
            yarn_config,
        )
    except Exception as exc:
        print(f"Warning: failed to remove temporary HDFS file {path}: {exc}")

def write_worker_env_file(worker_environment: dict[str, str | None]) -> str:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="yarn-worker-env-",
        suffix=".sh",
        delete=False,
    ) as handle:
        os.chmod(handle.name, 0o600)
        for key, value in worker_environment.items():
            if value is not None:
                handle.write(f"export {key}={shlex.quote(str(value))}\n")
        return handle.name

def find_distributed_shell_jar(yarn_config: dict[str, Any]) -> str:
    yarn_dir = Path(str(yarn_config["hadoop_home"])) / "share" / "hadoop" / "yarn"
    candidates = sorted(yarn_dir.glob("hadoop-yarn-applications-distributedshell-*.jar"))
    if not candidates:
        raise FileNotFoundError(f"DistributedShell jar not found under {yarn_dir}")
    return str(candidates[0])

def build_yarn_shell_command(
    hdfs_request_path: str,
    hdfs_output_dir: str,
    hdfs_debug_path: str,
    hdfs_env_path: str,
    sandbox_image: str,
    yarn_config: dict[str, Any],
    resource_requirements: dict[str, Any],
    request_attachment_hdfs_path: str | None = None,
    request_attachment_relative_path: str | None = None,
    input_datasets: list[dict[str, Any]] | None = None,
) -> str:
    """Build the command that YARN runs on a worker node.

    The worker cannot see Airflow's local temp directory, so request/result
    files move through HDFS.
    """
    hadoop_home = str(yarn_config["hadoop_home"])
    hadoop_conf_dir = str(yarn_config["hadoop_conf_dir"])
    if bool(request_attachment_hdfs_path) != bool(request_attachment_relative_path):
        raise ValueError(
            "YARN attachment HDFS path and relative input path must be supplied together"
        )
    attachment_lines: list[str] = []
    if request_attachment_hdfs_path and request_attachment_relative_path:
        relative_path = workspace_input_relative_path(
            f"/workspace/input/{request_attachment_relative_path}"
        )
        relative_text = relative_path.as_posix()
        parent_text = relative_path.parent.as_posix()
        attachment_lines = [
            f'mkdir -p "$WORKDIR/input/{parent_text}"',
            (
                f'"$HADOOP_HOME/bin/hdfs" dfs -get -f '
                f'{shlex.quote(request_attachment_hdfs_path)} '
                f'"$WORKDIR/input/{relative_text}" || exit 24'
            ),
            f'chmod 444 "$WORKDIR/input/{relative_text}"',
            f"echo REQUEST_ATTACHMENT={shlex.quote(relative_text)} >> \"$DEBUG_FILE\"",
        ]

    dataset_lines: list[str] = []
    for dataset in input_datasets or []:
        relative_path = workspace_input_relative_path(
            str(dataset.get("container_path") or "")
        ).as_posix()
        staged_uri = str(dataset.get("run_staged_hdfs_uri") or "")
        expected_sha = str(dataset.get("sha256") or "")
        expected_size = int(dataset.get("size_bytes") or 0)
        if not staged_uri or not expected_sha:
            raise ValueError(
                "YARN input dataset is missing run_staged_hdfs_uri or sha256"
            )
        parent = PurePosixPath(relative_path).parent.as_posix()
        target = f"$WORKDIR/input/{relative_path}"
        dataset_id = safe_path_component(
            str(dataset.get("dataset_id") or "dataset")
        )
        dataset_lines.extend(
            [
                f'mkdir -p "$WORKDIR/input/{parent}"',
                (
                    f'"$HADOOP_HOME/bin/hdfs" dfs -get -f '
                    f'{shlex.quote(staged_uri)} "{target}" || exit 24'
                ),
                f'chmod 444 "{target}"',
                f'ACTUAL_SIZE=$(wc -c < "{target}" | tr -d " ")',
                f'[ "$ACTUAL_SIZE" = "{expected_size}" ] || exit 25',
                f'ACTUAL_SHA=$(sha256sum "{target}" | awk \'{{print $1}}\')',
                f'[ "$ACTUAL_SHA" = "{expected_sha}" ] || exit 26',
                f'echo INPUT_DATASET_VERIFIED={dataset_id} >> "$DEBUG_FILE"',
            ]
        )
    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        f"export HADOOP_HOME={shlex.quote(hadoop_home)}",
        f"export HADOOP_CONF_DIR={shlex.quote(hadoop_conf_dir)}",
        "export PATH=\"$HADOOP_HOME/bin:$HADOOP_HOME/sbin:$PATH\"",
        "WORKDIR=$(mktemp -d)",
        "mkdir -p \"$WORKDIR/input\" \"$WORKDIR/output\"",
        "if [ -n \"${SANDBOX_ENV_HDFS_PATH:-}\" ]; then",
        "  \"$HADOOP_HOME/bin/hdfs\" dfs -get -f \"$SANDBOX_ENV_HDFS_PATH\" \"$WORKDIR/sandbox-env.sh\" || exit 23",
        "  chmod 600 \"$WORKDIR/sandbox-env.sh\"",
        "  set -a",
        "  . \"$WORKDIR/sandbox-env.sh\"",
        "  set +a",
        "  rm -f \"$WORKDIR/sandbox-env.sh\"",
        "fi",
        "DEBUG_FILE=\"$WORKDIR/worker-debug.txt\"",
        "export DOCKER_CONFIG=\"$WORKDIR/docker-config\"",
        "mkdir -p \"$DOCKER_CONFIG\"",
        "chmod 700 \"$DOCKER_CONFIG\"",
        "echo STEP=start > \"$DEBUG_FILE\"",
        "echo HOST=$(hostname) >> \"$DEBUG_FILE\"",
        "echo USER=$(whoami) >> \"$DEBUG_FILE\"",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -get -f {shlex.quote(hdfs_request_path)} \"$WORKDIR/input/request.json\"",
        *attachment_lines,
        "REQ_JSON=\"$WORKDIR/input/request.json\"",
        "REQUEST_FROM_FILE=$(REQ_JSON=\"$REQ_JSON\" python3 -c 'import json, os; print(json.load(open(os.environ[\"REQ_JSON\"])).get(\"request_text\", \"\"))' 2>/dev/null || true)",
        "export REQUEST=\"$REQUEST_FROM_FILE\"",
        "echo STEP=after-hdfs-get >> \"$DEBUG_FILE\"",
        "echo REQUEST_LEN=${#REQUEST} >> \"$DEBUG_FILE\"",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "chmod 755 \"$WORKDIR/input\"",
        "chmod 644 \"$WORKDIR/input/request.json\"",
        *dataset_lines,
        "chmod 777 \"$WORKDIR/output\"",
        "DOCKER_LOGIN_USER=\"${GHCR_USERNAME:-${GITHUB_USERNAME:-}}\"",
        "DOCKER_LOGIN_TOKEN=\"${GHCR_TOKEN:-${GITHUB_TOKEN:-}}\"",
        "if [ -n \"$DOCKER_LOGIN_USER\" ] && [ -n \"$DOCKER_LOGIN_TOKEN\" ]; then",
        "  echo STEP=before-ghcr-login >> \"$DEBUG_FILE\"",
        "  printf '%s' \"$DOCKER_LOGIN_TOKEN\" | docker login ghcr.io -u \"$DOCKER_LOGIN_USER\" --password-stdin >/tmp/ghcr-login.out 2>&1",
        "  GHCR_LOGIN_RC=$?",
        "  echo STEP=after-ghcr-login >> \"$DEBUG_FILE\"",
        "  echo GHCR_LOGIN_RC=$GHCR_LOGIN_RC >> \"$DEBUG_FILE\"",
        "  if [ \"$GHCR_LOGIN_RC\" -ne 0 ]; then",
        "    sed -E 's/(ghp_|github_pat_)[A-Za-z0-9_]+/[REDACTED_TOKEN]/g' /tmp/ghcr-login.out >> \"$DEBUG_FILE\" || true",
        f"    \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "    exit $GHCR_LOGIN_RC",
        "  fi",
        "else",
        "  echo GHCR_LOGIN=skipped_missing_credentials >> \"$DEBUG_FILE\"",
        "fi",
        "resolve_backtester_worker_home() {",
        "  WORKER_USER=$(id -un 2>/dev/null || whoami 2>/dev/null || true)",
        "  PASSWD_HOME=\"\"",
        "  if [ -n \"$WORKER_USER\" ]; then",
        "    PASSWD_HOME=$(getent passwd \"$WORKER_USER\" 2>/dev/null | cut -d: -f6 || true)",
        "  fi",
        "  if [ -n \"$PASSWD_HOME\" ] && [ \"$PASSWD_HOME\" != \"/\" ]; then",
        "    printf '%s\\n' \"$PASSWD_HOME\"",
        "  elif [ -n \"${HOME:-}\" ] && [ \"$HOME\" != \"/\" ] && [ \"$HOME\" != \"/home/\" ]; then",
        "    printf '%s\\n' \"${HOME%/}\"",
        "  elif [ -n \"$WORKER_USER\" ]; then",
        "    printf '/home/%s\\n' \"$WORKER_USER\"",
        "  else",
        "    return 2",
        "  fi",
        "}",
        "if ! BACKTESTER_WORKER_HOME=$(resolve_backtester_worker_home); then",
        "  echo BACKTESTER_WORKER_HOME_UNRESOLVED >> \"$DEBUG_FILE\"",
        f"  \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "  exit 32",
        "fi",
        "echo HOME=${HOME:-} >> \"$DEBUG_FILE\"",
        "echo BACKTESTER_WORKER_HOME=$BACKTESTER_WORKER_HOME >> \"$DEBUG_FILE\"",
        "expand_backtester_host_dir() {",
        "  case \"$1\" in",
        "    '~') printf '%s\\n' \"$BACKTESTER_WORKER_HOME\" ;;",
        "    '~/'*) printf '%s/%s\\n' \"$BACKTESTER_WORKER_HOME\" \"${1#~/}\" ;;",
        "    */~/*) printf '%s/%s\\n' \"$BACKTESTER_WORKER_HOME\" \"${1#*~/}\" ;;",
        "    /*) printf '%s\\n' \"$1\" ;;",
        "    *) return 2 ;;",
        "  esac",
        "}",
        "echo STEP=before-docker >> \"$DEBUG_FILE\"",
        "BACKTEST_MOUNTS=\"\"",
        "HDFS_BRIDGE_PID=\"\"",
        "start_hdfs_backtester_bridge() {",
        "  (",
        "    while true; do",
        "      for REQUEST_FILE in \"$SIMULATION_REQUESTS_HOST_DIR\"/*.request; do",
        "        [ -f \"$REQUEST_FILE\" ] || continue",
        "        UPLOAD_MARKER=\"$REQUEST_FILE.hdfs_uploaded\"",
        "        [ -e \"$UPLOAD_MARKER\" ] && continue",
        "        sleep 1",
        "        REQUEST_NAME=$(basename \"$REQUEST_FILE\")",
        "        if \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$REQUEST_FILE\" \"$SIMULATION_REQUESTS_HDFS_DIR/$REQUEST_NAME\" 2>> \"$DEBUG_FILE\"; then",
        "          touch \"$UPLOAD_MARKER\"",
        "          echo HDFS_BRIDGE_UPLOADED_REQUEST=$REQUEST_NAME >> \"$DEBUG_FILE\"",
        "        fi",
        "      done",
        "      \"$HADOOP_HOME/bin/hdfs\" dfs -ls \"$SIMULATION_RESULTS_HDFS_DIR\" 2>/dev/null | awk 'NF >= 8 {print $8}' | while read -r REMOTE_RESULT_PATH; do",
        "        [ -n \"$REMOTE_RESULT_PATH\" ] || continue",
        "        RESULT_NAME=$(basename \"$REMOTE_RESULT_PATH\")",
        "        LOCAL_RESULT_PATH=\"$SIMULATION_RESULTS_HOST_DIR/$RESULT_NAME\"",
        "        TMP_RESULT_PATH=\"$SIMULATION_RESULTS_HOST_DIR/.${RESULT_NAME}.hdfs_tmp\"",
        "        if [ ! -e \"$LOCAL_RESULT_PATH\" ]; then",
        "          rm -rf \"$TMP_RESULT_PATH\" 2>/dev/null || true",
        "          if \"$HADOOP_HOME/bin/hdfs\" dfs -get \"$REMOTE_RESULT_PATH\" \"$TMP_RESULT_PATH\" 2>> \"$DEBUG_FILE\"; then",
        "            mv \"$TMP_RESULT_PATH\" \"$LOCAL_RESULT_PATH\" 2>> \"$DEBUG_FILE\" || true",
        "            echo HDFS_BRIDGE_DOWNLOADED_RESULT=$RESULT_NAME >> \"$DEBUG_FILE\"",
        "          fi",
        "        fi",
        "      done",
        "      sleep 2",
        "    done",
        "  ) &",
        "  HDFS_BRIDGE_PID=$!",
        "  echo HDFS_BRIDGE_PID=$HDFS_BRIDGE_PID >> \"$DEBUG_FILE\"",
        "}",
        "stop_hdfs_backtester_bridge() {",
        "  if [ -n \"${HDFS_BRIDGE_PID:-}\" ]; then",
        "    kill \"$HDFS_BRIDGE_PID\" 2>/dev/null || true",
        "    wait \"$HDFS_BRIDGE_PID\" 2>/dev/null || true",
        "  fi",
        "}",
        "trap stop_hdfs_backtester_bridge EXIT",
        "if [ \"${USE_REAL_BACKTESTER:-false}\" = \"true\" ]; then",
        "  if [ \"${BACKTEST_STORAGE_KIND:-}\" = \"shared_fs\" ]; then",
        "    RAW_SIMULATION_REQUESTS_HOST_DIR=\"${SIMULATION_REQUESTS_HOST_DIR:-}\"",
        "    RAW_SIMULATION_RESULTS_HOST_DIR=\"${SIMULATION_RESULTS_HOST_DIR:-}\"",
        "    if ! SIMULATION_REQUESTS_HOST_DIR=$(expand_backtester_host_dir \"$RAW_SIMULATION_REQUESTS_HOST_DIR\"); then",
        "      echo SIMULATION_REQUESTS_HOST_DIR_INVALID=$RAW_SIMULATION_REQUESTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "      echo SIMULATION_REQUESTS_HOST_DIR_INVALID=$RAW_SIMULATION_REQUESTS_HOST_DIR >&2",
        f"      \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "      exit 32",
        "    fi",
        "    if ! SIMULATION_RESULTS_HOST_DIR=$(expand_backtester_host_dir \"$RAW_SIMULATION_RESULTS_HOST_DIR\"); then",
        "      echo SIMULATION_RESULTS_HOST_DIR_INVALID=$RAW_SIMULATION_RESULTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "      echo SIMULATION_RESULTS_HOST_DIR_INVALID=$RAW_SIMULATION_RESULTS_HOST_DIR >&2",
        f"      \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "      exit 32",
        "    fi",
        "    export SIMULATION_REQUESTS_HOST_DIR SIMULATION_RESULTS_HOST_DIR",
        "    echo SIMULATION_REQUESTS_HOST_DIR_RESOLVED=$SIMULATION_REQUESTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "    echo SIMULATION_RESULTS_HOST_DIR_RESOLVED=$SIMULATION_RESULTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "    if mkdir -p \"$SIMULATION_REQUESTS_HOST_DIR\" \"$SIMULATION_RESULTS_HOST_DIR\" 2>> \"$DEBUG_FILE\"; then",
        "      echo SIMULATION_HOST_DIR_MKDIR_RC=0 >> \"$DEBUG_FILE\"",
        "    else",
        "      SIMULATION_HOST_DIR_MKDIR_RC=$?",
        "      echo SIMULATION_HOST_DIR_MKDIR_RC=$SIMULATION_HOST_DIR_MKDIR_RC >> \"$DEBUG_FILE\"",
        "      echo SIMULATION_HOST_DIR_MKDIR_RC=$SIMULATION_HOST_DIR_MKDIR_RC >&2",
        "    fi",
        "    chmod 777 \"$SIMULATION_REQUESTS_HOST_DIR\" \"$SIMULATION_RESULTS_HOST_DIR\" 2>> \"$DEBUG_FILE\" || true",
        "    for DIR_VAR in SIMULATION_REQUESTS_HOST_DIR SIMULATION_RESULTS_HOST_DIR; do",
        "      DIR_VALUE=\"${!DIR_VAR:-}\"",
        "      if [ -z \"$DIR_VALUE\" ] || [ ! -d \"$DIR_VALUE\" ]; then",
        "        echo ${DIR_VAR}_UNUSABLE=$DIR_VALUE >> \"$DEBUG_FILE\"",
        "        echo ${DIR_VAR}_UNUSABLE=$DIR_VALUE >&2",
        f"        \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "        exit 32",
        "      fi",
        "    done",
        "    if [ ! -w \"${SIMULATION_REQUESTS_HOST_DIR:-}\" ]; then",
        "      echo SIMULATION_REQUESTS_HOST_DIR_UNWRITABLE=${SIMULATION_REQUESTS_HOST_DIR:-} >> \"$DEBUG_FILE\"",
        "      echo SIMULATION_REQUESTS_HOST_DIR_UNWRITABLE=${SIMULATION_REQUESTS_HOST_DIR:-} >&2",
        f"      \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "      exit 32",
        "    fi",
        "    if [ ! -r \"${SIMULATION_RESULTS_HOST_DIR:-}\" ]; then",
        "      echo SIMULATION_RESULTS_HOST_DIR_UNREADABLE=${SIMULATION_RESULTS_HOST_DIR:-} >> \"$DEBUG_FILE\"",
        "      echo SIMULATION_RESULTS_HOST_DIR_UNREADABLE=${SIMULATION_RESULTS_HOST_DIR:-} >&2",
        f"      \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "      exit 32",
        "    fi",
        "    BACKTEST_MOUNTS=\"--mount type=bind,source=$SIMULATION_REQUESTS_HOST_DIR,target=$SIMULATION_REQUESTS_DIR --mount type=bind,source=$SIMULATION_RESULTS_HOST_DIR,target=$SIMULATION_RESULTS_DIR\"",
        "  elif [ \"${BACKTEST_STORAGE_KIND:-}\" = \"hdfs\" ]; then",
        "    SIMULATION_REQUESTS_HDFS_DIR=\"${SIMULATION_REQUESTS_HDFS_DIR:-${SIMULATION_REQUESTS_HOST_DIR:-}}\"",
        "    SIMULATION_RESULTS_HDFS_DIR=\"${SIMULATION_RESULTS_HDFS_DIR:-${SIMULATION_RESULTS_HOST_DIR:-}}\"",
        "    if [ -z \"$SIMULATION_REQUESTS_HDFS_DIR\" ] || [ -z \"$SIMULATION_RESULTS_HDFS_DIR\" ]; then",
        "      echo BACKTEST_HDFS_DIRS_MISSING >> \"$DEBUG_FILE\"",
        f"      \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "      exit 31",
        "    fi",
        "    SIMULATION_REQUESTS_HOST_DIR=\"$WORKDIR/backtester/simulation-requests\"",
        "    SIMULATION_RESULTS_HOST_DIR=\"$WORKDIR/backtester/simulation-results\"",
        "    mkdir -p \"$SIMULATION_REQUESTS_HOST_DIR\" \"$SIMULATION_RESULTS_HOST_DIR\" 2>> \"$DEBUG_FILE\" || exit 32",
        "    chmod 777 \"$SIMULATION_REQUESTS_HOST_DIR\" \"$SIMULATION_RESULTS_HOST_DIR\" 2>> \"$DEBUG_FILE\" || true",
        "    \"$HADOOP_HOME/bin/hdfs\" dfs -mkdir -p \"$SIMULATION_REQUESTS_HDFS_DIR\" \"$SIMULATION_RESULTS_HDFS_DIR\" 2>> \"$DEBUG_FILE\" || exit 33",
        "    echo BACKTEST_STORAGE_KIND=hdfs >> \"$DEBUG_FILE\"",
        "    echo SIMULATION_REQUESTS_HDFS_DIR=$SIMULATION_REQUESTS_HDFS_DIR >> \"$DEBUG_FILE\"",
        "    echo SIMULATION_RESULTS_HDFS_DIR=$SIMULATION_RESULTS_HDFS_DIR >> \"$DEBUG_FILE\"",
        "    echo SIMULATION_REQUESTS_HOST_DIR_RESOLVED=$SIMULATION_REQUESTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "    echo SIMULATION_RESULTS_HOST_DIR_RESOLVED=$SIMULATION_RESULTS_HOST_DIR >> \"$DEBUG_FILE\"",
        "    export SIMULATION_REQUESTS_HOST_DIR SIMULATION_RESULTS_HOST_DIR SIMULATION_REQUESTS_HDFS_DIR SIMULATION_RESULTS_HDFS_DIR",
        "    BACKTEST_MOUNTS=\"--mount type=bind,source=$SIMULATION_REQUESTS_HOST_DIR,target=$SIMULATION_REQUESTS_DIR --mount type=bind,source=$SIMULATION_RESULTS_HOST_DIR,target=$SIMULATION_RESULTS_DIR\"",
        "    start_hdfs_backtester_bridge",
        "  else",
        "    echo BACKTEST_STORAGE_KIND_UNSUPPORTED=${BACKTEST_STORAGE_KIND:-missing} >> \"$DEBUG_FILE\"",
        f"    \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "    exit 31",
        "  fi",
        "fi",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "docker run -i --pull always --rm "
        f"--cpus {shlex.quote(str(resource_requirements['cpu_vcpus']))} "
        f"--memory {shlex.quote(str(resource_requirements['memory_mb']))}m "
        f"--pids-limit {shlex.quote(str(resource_requirements['pids_limit']))} "
        "--read-only "
        "--user 1000:1000 "
        "--cap-drop ALL "
        "--security-opt no-new-privileges "
        "--tmpfs /tmp:rw,noexec,nosuid,size=1g,mode=1777 "
        "--tmpfs /workspace:rw,nosuid,size=4g,uid=1000,gid=1000,mode=0770 "
        "--mount type=bind,source=\"$WORKDIR/input\",target=/workspace/input,readonly "
        "--mount type=bind,source=\"$WORKDIR/output\",target=/workspace/output "
        "$BACKTEST_MOUNTS "
        "-e LITELLM_API_KEY "
        "-e LITELLM_PROXY_API_KEY "
        "-e LITELLM_BASE_URL "
        "-e LITELLM_MODEL "
        "-e LLM_PROVIDER "
        "-e API_KEY "
        "-e OPENAI_API_KEY "
        "-e OPENAI_BASE_URL "
        "-e OPENAI_API_BASE "
        "-e OPENAI_MODEL "
        "-e MODEL "
        "-e MODEL_NAME "
        "-e ALLOWED_REPOS "
        "-e USE_REAL_BACKTESTER "
        "-e SIMULATION_REQUESTS_DIR "
        "-e SIMULATION_RESULTS_DIR "
        "-e BACKTEST_RESULT_TIMEOUT_SECONDS "
        "-e GITHUB_USERNAME "
        "-e GITHUB_TOKEN "
        f"{shlex.quote(sandbox_image)} < \"$WORKDIR/input/request.json\"",
        "DOCKER_RC=$?",
        "echo STEP=after-docker >> \"$DEBUG_FILE\"",
        "echo DOCKER_RC=$DOCKER_RC >> \"$DEBUG_FILE\"",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "echo STEP=before-output-inspect >> \"$DEBUG_FILE\"",
        "echo OUTPUT_TREE_START >> \"$DEBUG_FILE\"",
        "find \"$WORKDIR/output\" -maxdepth 5 -type f -print >> \"$DEBUG_FILE\" 2>&1 || true",
        "echo OUTPUT_TREE_END >> \"$DEBUG_FILE\"",
        f"find \"$WORKDIR/output\" -type f -print0 | while IFS= read -r -d '' FILE_PATH; do",
        "  REL_PATH=\"${FILE_PATH#$WORKDIR/output/}\"",
        "  REL_DIR=$(dirname \"$REL_PATH\")",
        f"  \"$HADOOP_HOME/bin/hdfs\" dfs -mkdir -p {shlex.quote(hdfs_output_dir)}/\"$REL_DIR\" || true",
        f"  \"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$FILE_PATH\" {shlex.quote(hdfs_output_dir)}/\"$REL_PATH\" || true",
        "done",
        f"\"$HADOOP_HOME/bin/hdfs\" dfs -put -f \"$DEBUG_FILE\" {shlex.quote(hdfs_debug_path)} || true",
        "rm -rf \"$WORKDIR\"",
        "exit $DOCKER_RC",
    ]
    return "\n".join(lines) + "\n"

def resolve_runtime_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve non-secret runtime config from validated request runtime config."""

    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}

    github_connection_id = runtime.get("github_connection_id") or get_airflow_variable(
        "GITHUB_CONNECTION_ID"
    )
    if github_connection_id is not None and not isinstance(github_connection_id, str):
        raise ValueError("runtime.github_connection_id must be a string when present")

    container_timeout_value = get_airflow_variable("SANDBOX_CONTAINER_TIMEOUT_SECONDS")
    container_timeout_configured = (
        container_timeout_value is not None
        and bool(str(container_timeout_value).strip())
    )
    if not container_timeout_configured:
        container_timeout_value = str(DEFAULT_CONTAINER_TIMEOUT_SECONDS)

    return {
        "sandbox_image": get_airflow_variable("SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE),
        "docker_url": get_airflow_variable("DOCKER_URL"),
        "github_connection_id": github_connection_id,
        "container_timeout_seconds": parse_int_config(
            "SANDBOX_CONTAINER_TIMEOUT_SECONDS",
            container_timeout_value,
        ),
        "container_timeout_seconds_configured": container_timeout_configured,
    }


def resolve_backtester_environment(*, execution_mode: str | None = None) -> dict[str, str]:
    """Resolve non-secret backtester settings for YARN or local Docker.

    HDFS paths are meaningful only to the YARN worker. If YARN falls back to
    local Docker (or Docker is selected directly while HDFS is configured),
    use the watcher's conventional local directories instead of trying to bind
    mount ``hdfs://`` URIs.
    """

    environment: dict[str, str] = {}
    use_real = parse_bool_config(
        "USE_REAL_BACKTESTER",
        get_airflow_variable("USE_REAL_BACKTESTER", "false"),
    )
    environment["USE_REAL_BACKTESTER"] = "true" if use_real else "false"

    if not use_real:
        return environment

    configured_storage_kind = str(
        get_airflow_variable("BACKTEST_STORAGE_KIND", "shared_fs") or "shared_fs"
    ).strip().lower()
    if configured_storage_kind not in {"shared_fs", "hdfs"}:
        raise ValueError(
            "BACKTEST_STORAGE_KIND must be either 'shared_fs' or 'hdfs' for real backtester mode"
        )
    if execution_mode not in {None, "docker", "yarn"}:
        raise ValueError("execution_mode must be either 'docker' or 'yarn'")

    if execution_mode == "docker" and configured_storage_kind == "hdfs":
        environment.update(
            {
                "BACKTEST_STORAGE_KIND": "shared_fs",
                "SIMULATION_REQUESTS_HOST_DIR": DEFAULT_DOCKER_REQUESTS_HOST_DIR,
                "SIMULATION_RESULTS_HOST_DIR": DEFAULT_DOCKER_RESULTS_HOST_DIR,
                "SIMULATION_REQUESTS_DIR": BACKTESTER_CONTAINER_REQUESTS_DIR,
                "SIMULATION_RESULTS_DIR": BACKTESTER_CONTAINER_RESULTS_DIR,
            }
        )
    elif configured_storage_kind == "shared_fs":
        requests_host_dir = resolve_required_backtester_path_variable("SIMULATION_REQUESTS_DIR")
        results_host_dir = resolve_required_backtester_path_variable("SIMULATION_RESULTS_DIR")
        environment.update(
            {
                "BACKTEST_STORAGE_KIND": configured_storage_kind,
                "SIMULATION_REQUESTS_HOST_DIR": requests_host_dir,
                "SIMULATION_RESULTS_HOST_DIR": results_host_dir,
                "SIMULATION_REQUESTS_DIR": BACKTESTER_CONTAINER_REQUESTS_DIR,
                "SIMULATION_RESULTS_DIR": BACKTESTER_CONTAINER_RESULTS_DIR,
            }
        )
    else:
        requests_hdfs_dir = resolve_required_backtester_path_variable(
            "SIMULATION_REQUESTS_DIR",
            allow_hdfs=True,
        )
        results_hdfs_dir = resolve_required_backtester_path_variable(
            "SIMULATION_RESULTS_DIR",
            allow_hdfs=True,
        )
        environment.update(
            {
                "BACKTEST_STORAGE_KIND": configured_storage_kind,
                "SIMULATION_REQUESTS_HDFS_DIR": requests_hdfs_dir.rstrip("/"),
                "SIMULATION_RESULTS_HDFS_DIR": results_hdfs_dir.rstrip("/"),
                "SIMULATION_REQUESTS_DIR": BACKTESTER_CONTAINER_REQUESTS_DIR,
                "SIMULATION_RESULTS_DIR": BACKTESTER_CONTAINER_RESULTS_DIR,
            }
        )
    result_timeout = get_airflow_variable("BACKTEST_RESULT_TIMEOUT_SECONDS")
    if result_timeout is not None and str(result_timeout).strip():
        try:
            parsed_timeout = int(str(result_timeout).strip())
        except ValueError as exc:
            raise ValueError("BACKTEST_RESULT_TIMEOUT_SECONDS must be an integer") from exc
        if parsed_timeout < 1:
            raise ValueError("BACKTEST_RESULT_TIMEOUT_SECONDS must be at least 1")
        environment["BACKTEST_RESULT_TIMEOUT_SECONDS"] = str(parsed_timeout)

    return environment


def resolve_required_backtester_path_variable(name: str, *, allow_hdfs: bool = False) -> str:
    value = get_airflow_variable(name)
    if not value or not str(value).strip():
        allowed = "an absolute path, ~/ path, or hdfs:// URI" if allow_hdfs else "an absolute path or ~/ path"
        raise ValueError(f"{name} must be configured as {allowed}")
    raw = str(value).strip()
    if allow_hdfs and raw.startswith("hdfs://"):
        return raw
    if raw == "~" or raw.startswith("~/"):
        return raw
    if raw.startswith("~"):
        raise ValueError(f"{name} must use the current user's home only; '~user' paths are not allowed")
    if not Path(raw).is_absolute():
        allowed = "an absolute path, ~/ path, or hdfs:// URI" if allow_hdfs else "an absolute path or ~/ path"
        raise ValueError(f"{name} must be {allowed}, got {raw!r}")
    return raw

def expand_local_backtester_host_path(raw: str) -> Path:
    if raw == "~" or raw.startswith("~/"):
        return Path(raw).expanduser()
    return Path(raw)


def validate_local_backtester_host_dirs(environment: dict[str, str]) -> None:
    if environment.get("USE_REAL_BACKTESTER") != "true":
        return
    if environment.get("BACKTEST_STORAGE_KIND") == "hdfs":
        return
    for key in (
        "SIMULATION_REQUESTS_HOST_DIR",
        "SIMULATION_RESULTS_HOST_DIR",
    ):
        path = expand_local_backtester_host_path(environment[key])
        if not path.exists():
            raise ValueError(f"{key} does not exist: {path}")
        if not path.is_dir():
            raise ValueError(f"{key} is not a directory: {path}")
    requests_path = expand_local_backtester_host_path(environment["SIMULATION_REQUESTS_HOST_DIR"])
    probe = requests_path / f".write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise ValueError(f"SIMULATION_REQUESTS_HOST_DIR is not writable: {requests_path}: {exc}") from exc

    results_path = expand_local_backtester_host_path(environment["SIMULATION_RESULTS_HOST_DIR"])
    try:
        list(results_path.iterdir())
    except OSError as exc:
        raise ValueError(f"SIMULATION_RESULTS_HOST_DIR is not readable: {results_path}: {exc}") from exc


def describe_backtester_environment(environment: dict[str, str]) -> str:
    mode = "real" if environment.get("USE_REAL_BACKTESTER") == "true" else "mock"
    requests_dir = environment.get("SIMULATION_REQUESTS_DIR") or "<runtime default>"
    results_dir = environment.get("SIMULATION_RESULTS_DIR") or "<runtime default>"
    requests_host_dir = environment.get("SIMULATION_REQUESTS_HOST_DIR") or "<not mounted>"
    results_host_dir = environment.get("SIMULATION_RESULTS_HOST_DIR") or "<not mounted>"
    return (
        f"mode={mode}, "
        f"USE_REAL_BACKTESTER={environment.get('USE_REAL_BACKTESTER', '<missing>')}, "
        f"simulation_requests_dir={requests_dir}, "
        f"simulation_results_dir={results_dir}, "
        f"simulation_requests_host_dir={requests_host_dir}, "
        f"simulation_results_host_dir={results_host_dir}, "
        f"storage_kind={environment.get('BACKTEST_STORAGE_KIND', '<none>')}"
    )


def resolve_source_revision() -> str:
    """Best-effort source revision for Airflow logs.

    Production DAG sync copies Python files out of a git worktree, so the live
    DAG folder is usually not itself a git checkout. The sync script writes a
    marker file for that case; local/dev runs can still fall back to git.
    """

    for name in SOURCE_REVISION_ENV_VARS:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()

    marker_path = Path(__file__).resolve().parent / SOURCE_REVISION_MARKER
    try:
        marker = marker_path.read_text(encoding="utf-8").strip()
    except OSError:
        marker = ""
    if marker:
        return marker

    try:
        completed = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return "<unknown>"

    if completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    return "<unknown>"


def resolve_resource_requirements(
    runtime_request: dict[str, Any],
) -> dict[str, Any]:
    """Return validated resources, including defaults for legacy requests."""

    requested = runtime_request.get("resource_requirements")
    if not isinstance(requested, dict):
        requested = {}
    return {
        "cpu_vcpus": requested.get("cpu_vcpus", DEFAULT_CPU_VCPUS),
        "memory_mb": requested.get("memory_mb", DEFAULT_MEMORY_MB),
        "gpu_count": requested.get("gpu_count", DEFAULT_GPU_COUNT),
        "execution_timeout_seconds": requested.get(
            "execution_timeout_seconds",
            DEFAULT_EXECUTION_TIMEOUT_SECONDS,
        ),
        "pids_limit": requested.get("pids_limit", DEFAULT_PIDS_LIMIT),
        "yarn_queue": requested.get("yarn_queue"),
    }


def request_has_explicit_workflow_timeout(runtime_request: dict[str, Any]) -> bool:
    objectives = runtime_request.get("execution_objectives")
    if not isinstance(objectives, dict):
        return False
    parameters = objectives.get("parsed_task_parameters")
    return isinstance(parameters, dict) and "timeout_seconds" in parameters


def describe_github_credentials(credentials: dict[str, str]) -> str:
    username = credentials.get("GITHUB_USERNAME") or ""
    token = credentials.get("GITHUB_TOKEN") or ""
    return (
        f"username={'present' if username else 'missing'}"
        f"{f' length={len(username)}' if username else ''}, "
        f"token={'present' if token else 'missing'}"
        f"{f' length={len(token)}' if token else ''}"
    )


def resolve_sandbox_github_credentials(
    validated: Any,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Resolve user-scoped GitHub credentials for a validated Jira workflow."""

    runtime_request = validated.runtime_request
    objectives = runtime_request.get("execution_objectives") or {}
    parameters = objectives.get("parsed_task_parameters") or {}
    explicitly_read_only = bool(objectives.get("zero_code_modifications")) and (
        "zero_code_modifications" in parameters
    )
    repositories = runtime_request.get("repository_details") or []
    credentials, metadata = resolve_user_scoped_github_credentials(
        getattr(validated, "requester", {}) or {},
        repositories,
        explicitly_read_only=explicitly_read_only,
    )
    verify_user_github_access(
        credentials,
        str(metadata.get("github_username") or ""),
        repositories,
    )
    return credentials, metadata


def describe_user_scoped_github_credentials(metadata: dict[str, Any]) -> str:
    github_username = metadata.get("github_username") or ""
    connection_id = metadata.get("github_connection_id") or ""
    repositories = metadata.get("requested_repositories") or []
    return (
        f"github_username={github_username or '<missing>'}, "
        f"connection_id={connection_id or '<missing>'}, "
        f"repos={','.join(str(repo) for repo in repositories) or '<none>'}, "
        f"token={'present' if metadata.get('token_present') else 'missing'}"
        f"{' length=' + str(metadata.get('token_length')) if metadata.get('token_present') else ''}"
    )


def describe_ghcr_credentials(credentials: dict[str, str]) -> str:
    username = credentials.get("GHCR_USERNAME") or ""
    token = credentials.get("GHCR_TOKEN") or ""
    return (
        f"username={'present' if username else 'missing'}"
        f"{f' length={len(username)}' if username else ''}, "
        f"token={'present' if token else 'missing'}"
        f"{f' length={len(token)}' if token else ''}"
    )


def resolve_image_pull_credentials(
    github_connection_id: str | None,
) -> dict[str, str]:
    """Resolve GHCR pull credentials without using user-scoped GitHub PATs."""

    ghcr_credentials = get_optional_ghcr_credentials()
    if ghcr_credentials:
        return ghcr_credentials

    global_github_credentials = get_optional_github_credentials(github_connection_id)
    ghcr_credentials = {}
    if global_github_credentials.get("GITHUB_USERNAME"):
        ghcr_credentials["GHCR_USERNAME"] = global_github_credentials["GITHUB_USERNAME"]
    if global_github_credentials.get("GITHUB_TOKEN"):
        ghcr_credentials["GHCR_TOKEN"] = global_github_credentials["GITHUB_TOKEN"]
    return ghcr_credentials


def docker_login_ghcr(
    docker_environment: dict[str, str],
    ghcr_credentials: dict[str, str],
) -> None:
    username = ghcr_credentials.get("GHCR_USERNAME")
    token = ghcr_credentials.get("GHCR_TOKEN")
    if not username or not token:
        print("GHCR docker login skipped: credentials missing")
        return

    completed = subprocess.run(
        ["docker", "login", "ghcr.io", "-u", username, "--password-stdin"],
        env=docker_environment,
        input=token,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        stderr = re.sub(
            r"(ghp_|github_pat_)[A-Za-z0-9_]+",
            "[REDACTED_TOKEN]",
            completed.stderr or "",
        )
        raise RuntimeError(
            "GHCR docker login failed before sandbox run. "
            f"stdout={completed.stdout.strip()} stderr={stderr.strip()}"
        )

    print("GHCR docker login succeeded")


def current_docker_user_spec() -> tuple[str, str, str]:
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    user_spec = f"{uid}:{gid}"
    workspace_tmpfs = (
        f"/workspace:rw,nosuid,size=4g,uid={uid},gid={gid},mode=0770"
    )
    return str(uid), user_spec, workspace_tmpfs


def resolve_litellm_config_for_request(
    runtime_request: dict[str, Any],
) -> dict[str, str]:
    """Resolve LiteLLM config, letting a validated Jira model option override it."""

    litellm_config = get_litellm_config()
    parsed_parameters = (
        runtime_request.get("execution_objectives", {}).get("parsed_task_parameters")
        or {}
    )
    requested_model = parsed_parameters.get("model")
    if isinstance(requested_model, str) and requested_model.strip():
        selected_model = requested_model.strip()
        reason = "requested in Jira command"
    else:
        selected_model = litellm_config["model"]
        reason = "Airflow default"

    resolved_config = {**litellm_config, "model": selected_model}
    print(f"LiteLLM model selection: model={selected_model}, reason={reason}")
    return resolved_config


def resolve_provider_config_for_request(
    runtime_request: dict[str, Any],
) -> dict[str, str]:
    """Resolve one provider and bind explicit E3 identity before container start."""

    parsed_parameters = (
        runtime_request.get("execution_objectives", {}).get("parsed_task_parameters")
        or {}
    )
    provider_mode = parsed_parameters.get("provider_mode")
    if provider_mode is None:
        legacy = resolve_litellm_config_for_request(runtime_request)
        return {**legacy, "provider_mode": "legacy_company_litellm"}
    if provider_mode not in {"company_litellm", "openai"}:
        raise ValueError("provider_mode must be company_litellm or openai")

    configured = (
        get_openai_config()
        if provider_mode == "openai"
        else get_litellm_config()
    )
    requested_base_url = parsed_parameters.get("provider_base_url")
    requested_model = parsed_parameters.get("model")
    if not isinstance(requested_base_url, str) or not requested_base_url.strip():
        raise ValueError("explicit provider request requires provider_base_url")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise ValueError("explicit provider request requires model")
    if requested_base_url.rstrip("/") != configured["base_url"].rstrip("/"):
        raise ValueError("requested provider_base_url does not match secret configuration")
    if requested_model.strip() != configured["model"].strip():
        raise ValueError("requested model does not match secret configuration")
    adapter = (
        "openai_chat_completions"
        if provider_mode == "openai"
        else "litellm_chat_completions"
    )
    transport_model = (
        configured["model"].strip()
        if provider_mode == "openai"
        else f"litellm_proxy/{configured['model'].strip()}"
    )
    identity = {
        "schema_version": "llm-provider-identity-v1",
        "provider_mode": provider_mode,
        "base_url": configured["base_url"].rstrip("/"),
        "model_alias": configured["model"].strip(),
        "transport_model": transport_model,
        "adapter": adapter,
    }
    identity_hash = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    if parsed_parameters.get("provider_identity_sha256") != identity_hash:
        raise ValueError("requested provider identity hash does not match configuration")
    return {
        "provider_mode": provider_mode,
        "api_key": configured["api_key"],
        "base_url": configured["base_url"].rstrip("/"),
        "model": configured["model"].strip(),
    }


def provider_environment(config: dict[str, str]) -> dict[str, str]:
    """Build a provider-scoped container environment with no cross-secret aliases."""

    provider_mode = config["provider_mode"]
    if provider_mode == "openai":
        return {
            "LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": config["api_key"],
            "OPENAI_BASE_URL": config["base_url"],
            "OPENAI_MODEL": config["model"],
        }
    if provider_mode == "company_litellm":
        return {
            "LLM_PROVIDER": "company_litellm",
            "LITELLM_API_KEY": config["api_key"],
            "LITELLM_BASE_URL": config["base_url"],
            "LITELLM_MODEL": config["model"],
        }
    if provider_mode != "legacy_company_litellm":
        raise ValueError("unsupported resolved provider mode")
    return {
        "LITELLM_API_KEY": config["api_key"],
        "LITELLM_BASE_URL": config["base_url"],
        "LITELLM_MODEL": config["model"],
        "API_KEY": config["api_key"],
        "OPENAI_API_KEY": config["api_key"],
        "OPENAI_BASE_URL": config["base_url"],
        "OPENAI_API_BASE": config["base_url"],
        "MODEL": config["model"],
        "MODEL_NAME": config["model"],
    }


def allowed_repos_env(runtime_request: dict[str, Any]) -> str:
    repos = runtime_request.get("repository_details") or []
    names = []
    for repo in repos:
        if isinstance(repo, dict) and isinstance(repo.get("repo_full_name"), str):
            names.append(repo["repo_full_name"])
        elif isinstance(repo, dict) and isinstance(repo.get("clone_url"), str):
            match = re.match(r"^https://github\.com/([^/]+/[^/.]+)", repo["clone_url"])
            if match:
                names.append(match.group(1))
    return ",".join(dict.fromkeys(names))

def selected_execution_mode(payload: dict[str, Any]) -> str:
    """Resolve docker/yarn mode from Airflow Variables."""

    mode = get_airflow_variable("SANDBOX_EXECUTION_MODE", "docker") or "docker"
    mode = str(mode).strip().lower()

    if mode not in {"docker", "yarn"}:
        raise ValueError(
            f"Invalid sandbox_execution_mode={mode!r}; expected 'docker' or 'yarn'"
        )

    return mode


def docker_fallback_enabled() -> bool:
    return parse_bool_config(
        "WORKFLOW_ENABLE_DOCKER_FALLBACK",
        get_airflow_variable("WORKFLOW_ENABLE_DOCKER_FALLBACK", "false"),
    )


def is_yarn_docker_fallback_exception(exc: Exception) -> bool:
    if isinstance(exc, HadoopCommandError):
        return True
    if isinstance(exc, FileNotFoundError) and "HDFS" in str(exc):
        return True

    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "hdfs",
            "hadoop command failed",
            "yarn sandbox timed out",
            "yarn sandbox exited",
            "yarn sandbox completed",
            "distributedshell jar not found",
        )
    )


def run_container_via_yarn(payload: dict[str, Any], dag_run_id: str) -> dict[str, Any]:
    """Submit the sandbox Docker launch to YARN using DistributedShell."""
    validated = validate_jira_command_payload(payload)
    issue_key = validated.issue_key
    runtime_request = validated.runtime_request
    result_path = (
        get_runtime_output_path(runtime_request, "result_path")
        or DEFAULT_RESULT_PATH
    )
    progress_events_path = get_runtime_output_path(
        runtime_request,
        "progress_events_path",
    )

    runtime_config = resolve_runtime_config(payload)
    yarn_config = resolve_yarn_config()
    resource_requirements = resolve_resource_requirements(runtime_request)
    provider_config = resolve_provider_config_for_request(runtime_request)
    backtester_environment = resolve_backtester_environment(execution_mode="yarn")
    source_revision = resolve_source_revision()
    github_credentials, github_metadata = resolve_sandbox_github_credentials(validated)
    ghcr_credentials = get_optional_ghcr_credentials()
    print(
        "GitHub user-scoped sandbox credentials: "
        f"{describe_user_scoped_github_credentials(github_metadata)}"
    )
    print(f"GHCR pull credentials: {describe_ghcr_credentials(ghcr_credentials)}")

    safe_issue = safe_path_component(issue_key)
    safe_run = safe_path_component(dag_run_id)[-80:]
    run_token = uuid.uuid4().hex[:12]

    hdfs_root_path = str(yarn_config["hdfs_run_root"]).rstrip("/")
    hdfs_run_dir_path = f"{hdfs_root_path}/{safe_issue}/{safe_run}-{run_token}"
    hdfs_run_dir = make_hdfs_uri(hdfs_run_dir_path, yarn_config)
    hdfs_request_path = f"{hdfs_run_dir}/request.json"
    hdfs_output_dir = f"{hdfs_run_dir}/output"
    hdfs_result_path = hdfs_workspace_output_file(hdfs_output_dir, result_path)
    hdfs_debug_path = f"{hdfs_run_dir}/worker-debug.txt"
    hdfs_env_path = f"{hdfs_run_dir}/worker-env.sh"

    hadoop_home = str(yarn_config["hadoop_home"])
    run_hadoop_command(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-mkdir", "-p", hdfs_run_dir],
        yarn_config,
    )
    if input_datasets_from_request(runtime_request):
        hdfs_input_config = resolve_input_dataset_materialization_config(
            runtime_request
        )
        with tempfile.TemporaryDirectory(
            prefix=f"sandbox-yarn-input-{issue_key}-"
        ) as input_temp_dir:
            local_input_dir = Path(input_temp_dir) / "input"
            local_input_dir.mkdir()
            runtime_request = materialize_hdfs_input_datasets(
                runtime_request,
                local_input_dir,
                hdfs_input_config,
            )
            runtime_request = stage_input_datasets_in_run_hdfs(
                runtime_request,
                local_input_dir,
                hdfs_run_dir,
                yarn_config,
            )
    write_request_to_hdfs(runtime_request, hdfs_request_path, yarn_config)

    request_attachment_hdfs_path = None
    request_attachment_relative_path = None
    if getattr(validated, "request_attachment", None) is not None:
        workspace_path = request_template_workspace_path(runtime_request)
        if workspace_path is None:
            raise ValueError("Selected Jira attachment has no runtime input path")
        relative_path = workspace_input_relative_path(workspace_path)
        request_attachment_relative_path = relative_path.as_posix()
        request_attachment_hdfs_path = (
            f"{hdfs_run_dir}/input/{request_attachment_relative_path}"
        )
        with tempfile.TemporaryDirectory(
            prefix=f"jira-request-attachment-{safe_issue}-"
        ) as attachment_temp_dir:
            attachment_input_dir = Path(attachment_temp_dir) / "input"
            attachment_input_dir.mkdir()
            staged_attachment = stage_jira_request_attachment(
                validated,
                attachment_input_dir,
            )
            if staged_attachment is None:
                raise ValueError("Selected Jira attachment was not staged")
            copy_input_file_to_hdfs(
                staged_attachment,
                request_attachment_hdfs_path,
                yarn_config,
            )

    print(
        "YARN sandbox config: "
        f"queue={yarn_config['yarn_queue']}, "
        f"hdfs_run_dir={hdfs_run_dir}, "
        f"result_path={result_path}, "
        f"progress_events_path={progress_events_path}, "
        f"sandbox_image={runtime_config['sandbox_image']}, "
        f"source_revision={source_revision}, "
        f"backtester={describe_backtester_environment(backtester_environment)}"
    )

    shell_command = build_yarn_shell_command(
        hdfs_request_path,
        hdfs_output_dir,
        hdfs_debug_path,
        hdfs_env_path,
        str(runtime_config["sandbox_image"]),
        yarn_config,
        resource_requirements,
        request_attachment_hdfs_path,
        request_attachment_relative_path,
        input_datasets=input_datasets_from_request(runtime_request),
    )
    ds_jar = find_distributed_shell_jar(yarn_config)

    worker_environment = provider_environment(provider_config)
    worker_environment["ALLOWED_REPOS"] = allowed_repos_env(runtime_request)
    worker_environment.update(backtester_environment)
    worker_environment.update(github_credentials)
    worker_environment.update(ghcr_credentials)

    shell_script_path = os.path.join(
        tempfile.gettempdir(),
        f"yarn_worker_{issue_key}_{uuid.uuid4().hex}.sh",
    )
    with open(shell_script_path, "w", encoding="utf-8") as handle:
        handle.write(shell_command)
    os.chmod(shell_script_path, 0o755)
    worker_env_path = write_worker_env_file(worker_environment)
    run_hadoop_command(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-put", "-f", worker_env_path, hdfs_env_path],
        yarn_config,
    )
    run_hadoop_command(
        [f"{hadoop_home}/bin/hdfs", "dfs", "-chmod", "600", hdfs_env_path],
        yarn_config,
    )

    yarn_command = [
        f"{hadoop_home}/bin/yarn",
        "jar",
        ds_jar,
        "org.apache.hadoop.yarn.applications.distributedshell.Client",
        "-jar",
        ds_jar,
        "-shell_script",
        shell_script_path,
        "-shell_env",
        f"SANDBOX_ENV_HDFS_PATH={hdfs_env_path}",
        "-timeout",
        str(int(yarn_config["timeout_seconds"]) * 1000),
    ]

    yarn_command.extend(
        [
            "-num_containers",
            "1",
            "-master_memory",
            str(yarn_config["master_memory_mb"]),
            "-container_memory",
            str(resource_requirements["memory_mb"]),
            "-container_vcores",
            str(max(1, math.ceil(float(resource_requirements["cpu_vcpus"])))),
            "-queue",
            str(resource_requirements["yarn_queue"] or yarn_config["yarn_queue"]),
        ]
    )

    try:
        try:
            completed = subprocess.run(
                yarn_command,
                env=hadoop_environment(yarn_config),
                capture_output=True,
                text=True,
                timeout=int(resource_requirements["execution_timeout_seconds"]),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            print(f"YARN stdout for {issue_key}:\n{stdout}")
            print(f"YARN stderr for {issue_key}:\n{stderr}")
            raise RuntimeError(
                "YARN sandbox timed out after "
                f"{resource_requirements['execution_timeout_seconds']}s for {issue_key}"
            ) from exc
    finally:
        Path(shell_script_path).unlink(missing_ok=True)
        Path(worker_env_path).unlink(missing_ok=True)
        remove_hdfs_file_if_exists(hdfs_env_path, yarn_config)

    print(f"YARN stdout for {issue_key}:\n{completed.stdout}")
    print(f"YARN stderr for {issue_key}:\n{completed.stderr}")
    print(f"YARN HDFS run dir for {issue_key}: {hdfs_run_dir}")

    result_exists = hdfs_exists(hdfs_result_path, yarn_config)
    output_exists = result_exists or hdfs_exists(hdfs_output_dir, yarn_config)

    with tempfile.TemporaryDirectory(prefix=f"sandbox-yarn-{issue_key}-") as temp_dir:
        output_dir = Path(temp_dir) / "output"
        output_dir.mkdir()
        if output_exists:
            copy_hdfs_output_to_local(hdfs_output_dir, output_dir, yarn_config)

        progress_events = read_progress_events_file(output_dir, progress_events_path)
        try:
            result = read_structured_result(
                output_dir,
                completed.stdout,
                result_path,
            )
        except Exception as exc:
            raise SandboxStructuredResultUnavailable(
                issue_key=issue_key,
                runtime_request=runtime_request,
                progress_events=progress_events,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                result_path=result_path,
                execution_mode="YARN",
            ) from exc

        result.setdefault("issue_key", issue_key)
        if completed.returncode != 0:
            result.setdefault("status", "failed")
        result["artifact_ingestion"] = ingest_mounted_artifacts(
            issue_key,
            output_dir,
            result,
            [],
        )
        if not is_successful_result(result):
            result.setdefault("status", "failed")
        if progress_events:
            result["_progress_events"] = progress_events

        if yarn_config["cleanup_hdfs"]:
            run_hadoop_command(
                [f"{hadoop_home}/bin/hdfs", "dfs", "-rm", "-r", "-f", hdfs_run_dir],
                yarn_config,
            )

    return result

def run_container_local_docker(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one Jira request in the sandbox using the cluster's Docker CLI."""

    validated = validate_jira_command_payload(payload)
    issue_key = validated.issue_key
    runtime_request = validated.runtime_request
    resource_requirements = resolve_resource_requirements(runtime_request)
    result_path = (
        get_runtime_output_path(runtime_request, "result_path")
        or DEFAULT_RESULT_PATH
    )
    progress_events_path = get_runtime_output_path(
        runtime_request,
        "progress_events_path",
    )

    with tempfile.TemporaryDirectory(
        prefix=f"sandbox-{issue_key}-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        temp_path = Path(temp_dir)
        input_dir = temp_path / "input"
        output_dir = temp_path / "output"
        input_dir.mkdir()
        output_dir.mkdir()

        stage_jira_request_attachment(validated, input_dir)
        if input_datasets_from_request(runtime_request):
            runtime_request = materialize_hdfs_input_datasets(
                runtime_request,
                input_dir,
                resolve_input_dataset_materialization_config(runtime_request),
            )
        request_file_payload = runtime_request
        request_json = json.dumps(request_file_payload)
        (input_dir / "request.json").write_text(
            request_json,
            encoding="utf-8",
        )
        input_dir.chmod(0o755)
        (input_dir / "request.json").chmod(0o644)
        output_dir.chmod(0o777)

        runtime_config = resolve_runtime_config(payload)
        provider_config = resolve_provider_config_for_request(runtime_request)
        backtester_environment = resolve_backtester_environment(execution_mode="docker")
        validate_local_backtester_host_dirs(backtester_environment)
        source_revision = resolve_source_revision()
        print(
            "Provider container config: "
            f"provider={provider_config['provider_mode']}, "
            f"base_url={provider_config['base_url']}, "
            f"model={provider_config['model']}, "
            f"sandbox_image={runtime_config['sandbox_image']}, "
            f"source_revision={source_revision}, "
            f"backtester={describe_backtester_environment(backtester_environment)}"
        )
        docker_environment = os.environ.copy()
        for legacy_name in (
            "GITHUB_USERNAME",
            "GITHUB_TOKEN",
            "RAE_LEGACY_INPUT",
            "REQUEST",
            "REQUEST_PATH",
            "RESULT_PATH",
            "TICKET",
        ):
            docker_environment.pop(legacy_name, None)
        for name in PROVIDER_ENV_VARS:
            docker_environment.pop(name, None)
        docker_environment.update(provider_environment(provider_config))
        docker_environment["ALLOWED_REPOS"] = allowed_repos_env(runtime_request)
        docker_environment.update(backtester_environment)
        if backtester_environment.get("USE_REAL_BACKTESTER") == "true":
            for name in BACKTESTER_ENV_VARS:
                docker_environment.setdefault(name, "")
        github_credentials, github_metadata = resolve_sandbox_github_credentials(validated)
        docker_environment.update(github_credentials)
        ghcr_credentials = resolve_image_pull_credentials(runtime_config["github_connection_id"])
        docker_environment.update(ghcr_credentials)
        print(
            "GitHub user-scoped sandbox credentials: "
            f"{describe_user_scoped_github_credentials(github_metadata)}"
        )
        print(f"GHCR pull credentials: {describe_ghcr_credentials(ghcr_credentials)}")
        if runtime_config["docker_url"]:
            docker_environment.setdefault("DOCKER_HOST", runtime_config["docker_url"])
        docker_config_dir = output_dir / ".docker"
        if ghcr_credentials:
            docker_config_dir.mkdir(mode=0o700)
            docker_environment["DOCKER_CONFIG"] = str(docker_config_dir)
            docker_login_ghcr(docker_environment, ghcr_credentials)
        _, docker_user, workspace_tmpfs = current_docker_user_spec()

        command = [
            "docker",
            "run",
            "-i",
            "--pull",
            "always",
            "--rm",
            "--cpus",
            str(resource_requirements["cpu_vcpus"]),
            "--memory",
            f"{resource_requirements['memory_mb']}m",
            "--pids-limit",
            str(resource_requirements["pids_limit"]),
            "--read-only",
            "--user",
            docker_user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=1g,mode=1777",
            "--tmpfs",
            workspace_tmpfs,
            "--mount",
            f"type=bind,source={input_dir},target=/workspace/input,readonly",
            "--mount",
            f"type=bind,source={output_dir},target=/workspace/output",
        ]
        for name in (*PROVIDER_ENV_VARS, "ALLOWED_REPOS"):
            if name in docker_environment:
                command.extend(["-e", name])
        if backtester_environment.get("USE_REAL_BACKTESTER") == "true":
            if backtester_environment.get("BACKTEST_STORAGE_KIND") == "hdfs":
                raise RuntimeError(
                    "BACKTEST_STORAGE_KIND=hdfs is supported only in YARN mode. "
                    "Use shared_fs for local Docker mode or run the HDFS backtester path through YARN."
                )
            requests_host_dir = expand_local_backtester_host_path(
                backtester_environment["SIMULATION_REQUESTS_HOST_DIR"]
            )
            results_host_dir = expand_local_backtester_host_path(
                backtester_environment["SIMULATION_RESULTS_HOST_DIR"]
            )
            command.extend(
                [
                    "--mount",
                    (
                        "type=bind,"
                        f"source={requests_host_dir},"
                        f"target={backtester_environment['SIMULATION_REQUESTS_DIR']}"
                    ),
                    "--mount",
                    (
                        "type=bind,"
                        f"source={results_host_dir},"
                        f"target={backtester_environment['SIMULATION_RESULTS_DIR']}"
                    ),
                ]
            )
        for name in BACKTESTER_ENV_VARS:
            if name in docker_environment:
                command.extend(["-e", name])
        if "GITHUB_USERNAME" in docker_environment:
            command.extend(["-e", "GITHUB_USERNAME"])
        if "GITHUB_TOKEN" in docker_environment:
            command.extend(["-e", "GITHUB_TOKEN"])
        command.append(runtime_config["sandbox_image"])
        requested_execution_timeout_seconds = int(
            resource_requirements["execution_timeout_seconds"]
        )
        container_timeout_cap_seconds = int(runtime_config["container_timeout_seconds"])
        if (
            not runtime_config.get("container_timeout_seconds_configured")
            and request_has_explicit_workflow_timeout(runtime_request)
        ):
            container_timeout_cap_seconds = max(
                container_timeout_cap_seconds,
                requested_execution_timeout_seconds,
            )
        container_timeout_seconds = min(
            requested_execution_timeout_seconds,
            container_timeout_cap_seconds,
        )

        try:
            completed = run_command_streaming(
                command,
                env=docker_environment,
                input_text=f"{request_json}\n",
                timeout=container_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            print(f"Sandbox captured output tail for {issue_key}:\n{stdout}")
            if stderr:
                print(f"Sandbox stderr for {issue_key}:\n{stderr}")
            raise RuntimeError(
                "Sandbox timed out after "
                f"{container_timeout_seconds}s for {issue_key}"
            ) from exc

        print(
            f"Sandbox process for {issue_key} exited with code "
            f"{completed.returncode}"
        )

        if completed.returncode != 0:
            progress_events = read_progress_events_file(
                output_dir,
                progress_events_path,
            )
            try:
                result = read_structured_result(
                    output_dir,
                    completed.stdout,
                    result_path,
                )
            except Exception as exc:
                raise SandboxStructuredResultUnavailable(
                    issue_key=issue_key,
                    runtime_request=runtime_request,
                    progress_events=progress_events,
                    returncode=completed.returncode,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    result_path=result_path,
                    execution_mode="Docker",
                ) from exc

            result.setdefault("issue_key", issue_key)
            result.setdefault("status", "failed")
            result["artifact_ingestion"] = ingest_mounted_artifacts(
                issue_key,
                output_dir,
                result,
                [],
            )
            if progress_events:
                result["_progress_events"] = progress_events
            return result

        progress_events = read_progress_events_file(output_dir, progress_events_path)
        try:
            result = read_structured_result(output_dir, completed.stdout, result_path)
        except Exception as exc:
            raise SandboxStructuredResultUnavailable(
                issue_key=issue_key,
                runtime_request=runtime_request,
                progress_events=progress_events,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                result_path=result_path,
                execution_mode="Docker",
            ) from exc
        result.setdefault("issue_key", issue_key)
        result["artifact_ingestion"] = ingest_mounted_artifacts(
            issue_key,
            output_dir,
            result,
            [],
        )
        if not is_successful_result(result):
            result.setdefault("status", "failed")
        if progress_events:
            result["_progress_events"] = progress_events
        return result




def run_container(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one Jira request using either local Docker or YARN execution mode."""

    mode = selected_execution_mode(payload)
    context = get_current_context()
    dag_run = context.get("dag_run")
    dag_run_id = getattr(dag_run, "run_id", "manual__unknown")

    if mode == "yarn":
        try:
            return run_container_via_yarn(payload, dag_run_id)
        except Exception as exc:
            if not docker_fallback_enabled() or not is_yarn_docker_fallback_exception(exc):
                raise
            print(
                "YARN/HDFS infrastructure failure encountered; "
                "falling back to local Docker for this workflow only. "
                f"Reason: {type(exc).__name__}: {exc}"
            )
            result = run_container_local_docker(payload)
            result["execution_fallback"] = {
                "enabled": True,
                "primary_execution_mode": "yarn",
                "fallback_execution_mode": "docker",
                "fallback_reason": exception_message(exc),
            }
            return result

    return run_container_local_docker(payload)


def prepare_rag_execution_payload(
    payload: dict[str, Any],
    validated: ValidatedJiraCommand,
) -> tuple[dict[str, Any], ValidatedJiraCommand]:
    """Retrieve C1 evidence once and return a newly validated execution payload.

    The orchestrator payload remains small and contains no retrieved Jira text.
    C0 returns without reading index configuration; C1 fails closed if the frozen
    index path or independently configured digest is unavailable or invalid.
    """

    parameters = validated.runtime_request.get("execution_objectives", {}).get(
        "parsed_task_parameters", {}
    )
    if parameters.get("rag_enabled") is not True:
        return payload, validated

    # Prompt-enabled RAG evidence must reach only the coding-agent container.
    # YARN mode persists the complete request as HDFS request.json, which would
    # create a second raw-memory record outside that boundary.
    if selected_execution_mode(payload) != "docker":
        raise ValueError(
            "rag_enabled=true requires Docker execution because YARN persists "
            "the runtime request (including Jira memory) to HDFS"
        )

    index_path = str(get_airflow_variable("JIRA_RAG_INDEX_PATH") or "").strip()
    expected_sha256 = str(
        get_airflow_variable("JIRA_RAG_INDEX_SHA256") or ""
    ).strip()
    if not index_path:
        raise ValueError(
            "JIRA_RAG_INDEX_PATH must point to the approved frozen Jira RAG index"
        )
    if not expected_sha256:
        raise ValueError(
            "JIRA_RAG_INDEX_SHA256 must contain the approved frozen index digest"
        )

    retrieval_context = retrieve_jira_memory(
        validated.runtime_request,
        index_path=index_path,
        expected_index_sha256=expected_sha256,
        top_k=parameters["rag_top_k"],
    )
    execution_payload = json.loads(json.dumps(payload))
    execution_payload["retrieval_context"] = retrieval_context
    enriched = validate_jira_command_payload(execution_payload)
    print(
        "Prepared frozen Jira memory evidence "
        f"index_id={retrieval_context['index_id']} "
        f"retriever={retrieval_context['retriever_version']} "
        f"candidates={retrieval_context['candidate_count']} "
        f"retrieved={len(retrieval_context['memories'])}"
    )
    return execution_payload, enriched


def run_and_writeback(
    payload: dict[str, Any],
    dag_run_id: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        validated = validate_jira_command_payload(payload)
    except JiraCommandValidationError as exc:
        workflow_id = build_workflow_id_from_payload(payload)
        if exc.issue_key:
            print(f"Jira command validation failed for {exc.issue_key}:")
            for error in exc.errors:
                print(f"- {error}")
            post_jira_comment(
                exc.issue_key,
                format_validation_failure_comment(
                    exc.issue_key,
                    exc.errors,
                    dag_run_id,
                    workflow_id,
                ),
            )
        result = {
            "status": "validation_failed",
            "issue_key": exc.issue_key,
            "command": "/quant",
            "summary": "Jira command validation failed.",
            "validation_errors": exc.errors,
        }
        if workflow_id:
            result["workflow_id"] = workflow_id
        return result

    issue_key = validated.issue_key
    workflow_id = build_workflow_id(
        issue_key,
        validated.comment_id,
        validated.request_text,
    )
    try:
        resolve_sandbox_github_credentials(validated)
    except ValueError as exc:
        errors = [str(exc)]
        print(f"Jira command validation failed for {issue_key}:")
        for error in errors:
            print(f"- {error}")
        post_jira_comment(
            issue_key,
            format_validation_failure_comment(
                issue_key,
                errors,
                dag_run_id,
                workflow_id,
            ),
        )
        return {
            "status": "validation_failed",
            "issue_key": issue_key,
            "command": "/quant",
            "summary": "Jira command validation failed.",
            "validation_errors": errors,
            "workflow_id": workflow_id,
        }
    policy = resolve_workflow_retry_policy()
    state = initialize_workflow_state(
        workflow_id,
        issue_key,
        validated.comment_id,
        validated.request_text,
        dag_run_id,
        load_workflow_state(workflow_id),
    )

    final_result = state.get("final_result")
    jira_writeback_stage = state.get("stages", {}).get("jira_writeback", {})
    if (
        state.get("status") in {"succeeded", "failed"}
        and isinstance(final_result, dict)
        and jira_writeback_stage.get("status") == "succeeded"
    ):
        print(f"Workflow {workflow_id} already terminal; returning saved result")
        return final_result

    sandbox_stage = state.setdefault("stages", {}).setdefault(
        "sandbox",
        {"status": "pending", "attempts": 0},
    )
    if sandbox_stage.get("status") == "succeeded" and isinstance(
        sandbox_stage.get("result"),
        dict,
    ):
        result = sandbox_stage["result"]
        print(f"Workflow {workflow_id} reusing completed sandbox result")
    else:
        try:
            execution_payload, validated = prepare_rag_execution_payload(
                payload,
                validated,
            )
            progress_diagnostics: dict[str, Any] = {"dag_run_id": dag_run_id}
            try:
                progress_diagnostics["execution_mode"] = selected_execution_mode(
                    execution_payload
                )
            except Exception as exc:
                progress_diagnostics["execution_mode"] = "unknown"
                progress_diagnostics["execution_mode_error"] = exception_message(exc)
            post_progress_milestone_if_needed(
                workflow_id,
                state,
                issue_key,
                validated.runtime_request,
                dag_run_id,
                "running",
                diagnostics=progress_diagnostics,
            )
            result = run_retryable_stage(
                workflow_id,
                state,
                "sandbox",
                policy,
                lambda: run_container(execution_payload),
            )
        except Exception as exc:
            state["status"] = "failed"
            save_workflow_state(workflow_id, state)
            partial_error = structured_result_unavailable_error(exc)
            if partial_error:
                failure_writeback_operation = lambda: post_partial_progress_comment_once(
                    issue_key,
                    partial_error,
                    dag_run_id,
                    workflow_id,
                )
            else:
                failure_writeback_operation = lambda: post_failure_comment_once(
                    issue_key,
                    exc,
                    dag_run_id,
                    workflow_id,
                    validated.runtime_request,
                )
            try:
                run_retryable_stage(
                    workflow_id,
                    state,
                    "failure_writeback",
                    policy,
                    failure_writeback_operation,
                )
            finally:
                save_workflow_state(workflow_id, state)
            raise

        result["workflow_id"] = workflow_id
        sandbox_stage = state.setdefault("stages", {}).setdefault("sandbox", {})
        sandbox_stage["result"] = result
        state["final_result"] = result
        save_workflow_state(workflow_id, state)

    result["workflow_id"] = workflow_id

    print(f"\nSandbox result for {issue_key}:")
    print(json.dumps(result, indent=2))

    state["final_result"] = result
    try:
        run_retryable_stage(
            workflow_id,
            state,
            "jira_writeback",
            policy,
            lambda: post_result_comment_once(issue_key, result, workflow_id),
        )
    except Exception:
        state["status"] = "failed"
        save_workflow_state(workflow_id, state)
        raise

    state["status"] = "succeeded" if is_successful_result(result) else "failed"
    save_workflow_state(workflow_id, state)
    return result

def attach_failure_log_if_needed(
    payload: dict[str, Any],
    context: dict[str, Any],
    run_result: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        validated = validate_jira_command_payload(payload)
    except JiraCommandValidationError as exc:
        return {
            "uploaded": [],
            "missing": [],
            "skipped": [
                {
                    "path": "airflow task log",
                    "reason": f"issue key unavailable: {exc}",
                }
            ],
        }

    if isinstance(run_result, dict) and is_successful_result(run_result):
        return {"uploaded": [], "missing": [], "skipped": []}

    ingestion = attach_airflow_log(validated.issue_key, context)
    print("Failure Airflow log ingestion:")
    print(json.dumps(ingestion, indent=2))
    return ingestion


@task(
    task_id="run_sandbox_and_writeback",
    execution_timeout=timedelta(
        seconds=parse_int_config(
            "WORKFLOW_TASK_TIMEOUT_SECONDS",
            get_airflow_variable(
                "WORKFLOW_TASK_TIMEOUT_SECONDS",
                str(DEFAULT_WORKFLOW_TASK_TIMEOUT_SECONDS),
            ),
        )
    ),
    retries=0,
)
def run_sandbox_and_writeback() -> dict[str, Any]:
    context = get_current_context()
    dag_run = context["dag_run"]
    payload = dag_run.conf or {}
    if not isinstance(payload, dict):
        raise ValueError("docker_sandbox_runner dag_run.conf must be a JSON object")

    return run_and_writeback(payload, dag_run.run_id, context)


@task(
    task_id="attach_failure_airflow_log",
    trigger_rule="all_done",
    retries=0,
)
def attach_failure_airflow_log() -> dict[str, Any]:
    context = get_current_context()
    dag_run = context["dag_run"]
    payload = dag_run.conf or {}
    if not isinstance(payload, dict):
        raise ValueError("docker_sandbox_runner dag_run.conf must be a JSON object")

    task_instance = context["task_instance"]
    run_result = task_instance.xcom_pull(task_ids="run_sandbox_and_writeback")
    return attach_failure_log_if_needed(payload, context, run_result)


default_args = {
    "owner": "rshah",
    "depends_on_past": False,
    "retries": 0,
}


with DAG(
    dag_id="docker_sandbox_runner",
    default_args=default_args,
    description="Run one quant sandbox request and write the result back to Jira",
    start_date=datetime(2026, 6, 1),
    schedule=None,
    catchup=False,
    max_active_runs=8,
    tags=["jira", "docker", "quant-loop", "sandbox"],
) as dag:
    run_task = run_sandbox_and_writeback()
    attach_task = attach_failure_airflow_log()
    run_task >> attach_task
