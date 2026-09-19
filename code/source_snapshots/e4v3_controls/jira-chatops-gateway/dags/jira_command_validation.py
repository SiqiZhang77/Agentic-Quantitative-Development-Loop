"""Validation helpers for Jira /quant workflow commands."""

from __future__ import annotations

import json
import math
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

try:
    from .repository_catalog import (
        accepted_aliases,
        default_repository,
        prose_aliases_in,
        resolve_repository,
    )
    from .jira_quant_common import extract_quant_request, sanitize_ticket_text
except ImportError:
    from repository_catalog import (
        accepted_aliases,
        default_repository,
        prose_aliases_in,
        resolve_repository,
    )
    from jira_quant_common import extract_quant_request, sanitize_ticket_text


DEFAULT_MAX_ITERATIONS = 5
DEFAULT_MAX_FAILED_ITERATIONS = 1
DEFAULT_TIMEOUT_SECONDS = 5400
DEFAULT_MAX_TOKEN_BUDGET = 100000
DEFAULT_MAX_AGENT_TURNS = None
DEFAULT_CPU_VCPUS = 2.0
DEFAULT_MEMORY_MB = 4096
DEFAULT_GPU_COUNT = 0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 2100
DEFAULT_PIDS_LIMIT = 256
RUNTIME_REQUEST_SCHEMA_VERSION = "1.0"
MAX_SUMMARY_CHARS = 500
MAX_DESCRIPTION_CHARS = 12_000
MAX_COMMENT_CHARS = 4_000
MAX_HISTORY_COMMENTS = 20

WORKFLOW_TYPES = {"backtest", "ingestion", "refactor", "analysis", "other"}
# Mirrors repository-relative path fields in runtime_request.schema.json:
# repo-relative, no leading slash, no parent traversal.
RESOURCE_PATH_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+$")
REQUEST_ATTACHMENT_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.request$"
)
DATASET_ATTACHMENT_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}\.(?:csv|json|xlsx|parquet)$",
    re.IGNORECASE,
)
INPUT_DATASET_ROOT = PurePosixPath("/workspace/input/datasets")
INPUT_DATASET_FORMATS = {"csv", "json", "xlsx", "parquet"}
INPUT_DATASET_EXTENSIONS = {
    ".csv": "csv",
    ".json": "json",
    ".xlsx": "xlsx",
    ".parquet": "parquet",
}
OPTION_ALIASES = {
    "action": "strategy_type",
    "attachment": "request_attachment",
    "allowed_dir": "allowed_directories",
    "allowed_dirs": "allowed_directories",
    "allowed_dirs_map": "allowed_directories_map",
    "allowed_paths_map": "allowed_directories_map",
    "branch": "target_branch",
    "iterate": "allow_iteration",
    "branch_map": "branch_map",
    "cpu": "cpu_vcpus",
    "end": "end_date",
    "file": "resource_path",
    "iterations": "max_iterations",
    "gpu": "gpu_count",
    "max_failures": "max_failed_iterations",
    "agent_turns": "max_agent_turns",
    "max_commits": "max_commits_per_run",
    "max_tokens": "max_token_budget_per_run",
    "llm_model": "model",
    "memory": "memory_mb",
    "path": "resource_path",
    "read_only": "zero_code_modifications",
    "repo": "clone_url",
    "repos": "repositories",
    "repository": "clone_url",
    "request_template": "request_attachment",
    "repositories": "repositories",
    "queue": "yarn_queue",
    "rag": "rag_enabled",
    "rag_topk": "rag_top_k",
    "runtime_timeout": "execution_timeout_seconds",
    "start": "start_date",
    "strategy": "strategy",
    "target_file": "resource_path",
    "ticker": "stock_type",
    "timeout": "timeout_seconds",
    "target_branch_map": "target_branch_map",
    "destination": "target_path",
    "output_file": "target_path",
    "dataset_hdfs_uri": "input_hdfs_uri",
    "dataset_mount_path": "input_mount_path",
    "dataset_format": "input_format",
    "dataset_attachment": "input_attachment",
    "jira_attachment": "input_attachment",
    "attached_file": "input_attachment",
    "hdfs_input": "input_hdfs_uri",
    "input_file": "input_hdfs_uri",
    "workflow": "strategy_type",
}
SUPPORTED_OPTIONS = {
    "allow_iteration",
    "allowed_directories",
    "allowed_directories_map",
    "branch_map",
    "clone_url",
    "cpu_vcpus",
    "end_date",
    "execution_timeout_seconds",
    "gpu_count",
    "input_attachment",
    "input_format",
    "input_hdfs_uri",
    "input_mount_path",
    "max_failed_iterations",
    "max_agent_turns",
    "max_commits_per_run",
    "max_iterations",
    "max_token_budget_per_run",
    "memory_mb",
    "model",
    "params",
    "resource_path",
    "request_attachment",
    "start_date",
    "stock_type",
    "strategy",
    "strategy_type",
    "repositories",
    "target_branch",
    "target_branch_map",
    "target_path",
    "timeout_seconds",
    "pids_limit",
    "provider_base_url",
    "provider_identity_sha256",
    "provider_mode",
    "rag_enabled",
    "rag_top_k",
    "yarn_queue",
    "zero_code_modifications",
}


@dataclass
class ValidatedJiraCommand:
    issue_key: str
    request_text: str
    runtime_request: dict[str, Any]
    comment_id: str
    requester: dict[str, Any]
    request_attachment: dict[str, Any] | None
    dataset_attachment: dict[str, Any] | None


class JiraCommandValidationError(ValueError):
    def __init__(self, message: str, errors: list[str], issue_key: str | None = None):
        super().__init__(f"{message}: {'; '.join(errors)}")
        self.errors = errors
        self.issue_key = issue_key


def validate_jira_command_payload(payload: dict[str, Any]) -> ValidatedJiraCommand:
    errors: list[str] = []
    ticket = payload.get("ticket")
    request = payload.get("request")

    if not isinstance(ticket, dict):
        raise JiraCommandValidationError(
            "Jira command validation failed",
            ["Payload is missing ticket metadata."],
        )

    issue_key = ticket.get("key")
    if not isinstance(issue_key, str) or not issue_key.strip():
        raise JiraCommandValidationError(
            "Jira command validation failed",
            ["Payload is missing ticket.key."],
        )
    issue_key = issue_key.strip()

    if not isinstance(request, dict):
        raise JiraCommandValidationError(
            "Jira command validation failed",
            ["Payload is missing request metadata."],
            issue_key=issue_key,
        )

    comments = request.get("comments")
    if not isinstance(comments, list) or not comments:
        raise JiraCommandValidationError(
            "Jira command validation failed",
            ["Payload request.comments must contain at least one comment."],
            issue_key=issue_key,
        )

    triggering_comments = request.get("triggering_comments", comments)
    if not isinstance(triggering_comments, list):
        triggering_comments = []
    latest_comment = select_latest_quant_comment(triggering_comments)
    if latest_comment is None:
        raise JiraCommandValidationError(
            "Jira command validation failed",
            ["no valid /quant request was found in the recent Jira comments."],
            issue_key=issue_key,
        )

    command_options, command_errors = parse_quant_command_options(
        latest_comment["request_text"]
    )
    errors.extend(command_errors)

    request_attachment = select_request_attachment(
        payload,
        command_options,
        errors,
    )
    dataset_attachment = select_dataset_attachment(
        payload,
        command_options,
        errors,
    )

    runtime_request = build_runtime_request(
        payload,
        latest_comment,
        command_options,
        errors,
        request_attachment=request_attachment,
        dataset_attachment=dataset_attachment,
    )
    errors.extend(validate_runtime_request_schema(runtime_request))

    if errors:
        raise JiraCommandValidationError(
            "Jira command validation failed",
            errors,
            issue_key=issue_key,
        )

    return ValidatedJiraCommand(
        issue_key=issue_key,
        request_text=latest_comment["request_text"],
        runtime_request=runtime_request,
        comment_id=latest_comment["comment_id"],
        requester=latest_comment.get("requester") or {},
        request_attachment=request_attachment,
        dataset_attachment=dataset_attachment,
    )


def select_latest_quant_comment(
    comments: list[Any],
) -> dict[str, Any] | None:
    valid_requests: list[dict[str, Any]] = []

    for comment in comments:
        if not isinstance(comment, dict):
            continue

        updated = comment.get("updated") or comment.get("created")
        text = comment.get("text")
        if not isinstance(updated, str) or not isinstance(text, str):
            continue

        request_text = extract_quant_request(text)
        if request_text is None:
            continue

        requester = {
            "displayName": str(
                comment.get("author_display_name")
                or comment.get("author")
                or ""
            ),
            "emailAddress": str(comment.get("author_email") or ""),
            "accountId": str(comment.get("author_account_id") or ""),
        }
        valid_requests.append(
            {
                "comment_id": str(comment.get("comment_id") or ""),
                "updated": updated,
                "created": str(comment.get("created") or updated),
                "author": requester["displayName"],
                "requester": requester,
                "text": text,
                "request_text": request_text,
            }
        )

    if not valid_requests:
        return None

    return max(
        valid_requests,
        key=lambda item: parse_jira_datetime(item["updated"]),
    )


def parse_quant_command_options(request_text: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    options: dict[str, Any] = {}
    objective_parts: list[str] = []

    for raw_line in request_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        key_value = parse_key_value_line(line)
        if key_value:
            key, value = key_value
            if not value and key == "options":
                continue
            add_command_option(options, key, value, errors)
            continue

        line_tokens, token_options, token_errors = parse_inline_options(line)
        errors.extend(token_errors)
        objective_parts.extend(line_tokens)
        options.update(token_options)

    objective = " ".join(part for part in objective_parts if part).strip()
    options["objective"] = objective

    if not objective:
        errors.append(
            "Add a request body after /quant, for example: "
            "/quant Backtest the momentum strategy strategy_type=backtest."
        )

    # start_date / end_date are optional: when omitted, the run inherits the
    # strategy's own backtest window (strategy.request). A supplied window is still
    # honoured (and a too-short one is rejected downstream by the engine guard).

    return options, errors


def parse_key_value_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", line)
    if not match:
        return None

    return match.group(1).strip(), match.group(2).strip()


def parse_inline_options(line: str) -> tuple[list[str], dict[str, Any], list[str]]:
    errors: list[str] = []
    options: dict[str, Any] = {}
    objective_tokens: list[str] = []

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return [], {}, [f"Could not parse command line '{line}': {exc}."]

    for token in tokens:
        separator = parse_inline_option_separator(token)
        if separator is None:
            objective_tokens.append(token)
            continue

        key, value = token.split(separator, 1)
        add_command_option(options, key, value, errors)

    return objective_tokens, options, errors


def parse_inline_option_separator(token: str) -> str | None:
    if "=" in token:
        return "="
    if ":" in token and re.match(r"^[A-Za-z_][A-Za-z0-9_-]*:", token):
        raw_key = token.split(":", 1)[0]
        key = OPTION_ALIASES.get(raw_key.strip().lower(), raw_key.strip().lower())
        if key not in SUPPORTED_OPTIONS:
            return None
        return ":"
    return None


def add_command_option(
    options: dict[str, Any],
    raw_key: str,
    value: str,
    errors: list[str],
) -> None:
    key = OPTION_ALIASES.get(raw_key.strip().lower(), raw_key.strip().lower())
    if key not in SUPPORTED_OPTIONS:
        errors.append(
            f"Unsupported /quant option '{raw_key}'. Supported options are: "
            f"{', '.join(sorted(SUPPORTED_OPTIONS))}."
        )
        return

    if key == "allowed_directories":
        options[key] = [
            directory.strip()
            for directory in re.split(r"[, ]+", value)
            if directory.strip()
        ]
        return

    if key in {
        "repositories",
        "branch_map",
        "target_branch_map",
        "allowed_directories_map",
    }:
        options[key] = value.strip()
        return

    if key == "model":
        model = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}", model):
            errors.append(
                "model must be a non-empty model id using letters, numbers, "
                "'.', '_', '-', '+', ':', or '/'."
            )
            return
        options[key] = model
        return

    if key == "params":
        options[key] = value.strip()
        return

    if key in {"resource_path", "target_path"}:
        path = value.strip()
        if not RESOURCE_PATH_RE.fullmatch(path):
            errors.append(
                f"{key} must be a repository-relative path without a leading "
                f"'/' or '..', for example {key}=strategies/momentum.py."
            )
            return
        options[key] = path
        return

    if key == "input_hdfs_uri":
        uri = value.strip()
        if not is_safe_hdfs_input_uri(uri):
            errors.append(
                "input_hdfs_uri must be an absolute hdfs:// URI with a safe "
                "file path and no '..', query string, or fragment."
            )
            return
        options[key] = uri
        return

    if key == "input_mount_path":
        mount_path = value.strip()
        if not is_safe_input_mount_path(mount_path):
            errors.append(
                "input_mount_path must name a file below "
                "/workspace/input/datasets/ and must not contain '..'."
            )
            return
        options[key] = mount_path
        return

    if key == "input_format":
        input_format = value.strip().lower().lstrip(".")
        if input_format not in INPUT_DATASET_FORMATS:
            errors.append(
                "input_format must be one of: "
                f"{', '.join(sorted(INPUT_DATASET_FORMATS))}."
            )
            return
        options[key] = input_format
        return

    options[key] = value.strip()


def build_runtime_request(
    payload: dict[str, Any],
    latest_comment: dict[str, str],
    options: dict[str, Any],
    errors: list[str],
    *,
    request_attachment: dict[str, Any] | None = None,
    dataset_attachment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ticket = payload["ticket"]
    issue_key = ticket["key"]
    strategy_type = resolve_strategy_type(options.get("strategy_type"), errors)
    repository_details = build_repository_details(options, issue_key, errors)
    if strategy_type == "backtest" and len(repository_details or []) > 1:
        errors.append(
            "Multi-repository backtests are not supported for the current ATP "
            "repository catalog because those repositories are not ATRADE engine "
            "components. Select one repository, or use a non-backtest workflow "
            "for coordinated repository edits."
        )
    iteration_controls = build_iteration_controls(
        options, errors, strategy_type=strategy_type
    )
    default_execution_timeout_seconds = (
        iteration_controls["timeout_seconds"]
        if "timeout_seconds" in options
        else DEFAULT_EXECUTION_TIMEOUT_SECONDS
    )
    resource_requirements = build_resource_requirements(
        options,
        errors,
        default_execution_timeout_seconds=default_execution_timeout_seconds,
    )
    input_datasets = build_input_datasets(
        options,
        errors,
        dataset_attachment=dataset_attachment,
    )
    rag_enabled, rag_top_k = resolve_rag_options(options, errors)
    parsed_task_parameters = {
        key: value for key, value in options.items() if key != "objective"
    } | {
        "objective": options["objective"],
        "rag_enabled": rag_enabled,
        "rag_top_k": rag_top_k,
    }

    runtime_request = {
        "schema_version": RUNTIME_REQUEST_SCHEMA_VERSION,
        "run_id": build_run_id(issue_key, latest_comment["updated"]),
        "repository_details": repository_details,
        "jira_metadata": {
            "ticket_id": issue_key,
            "current_status": str(ticket.get("status") or "Unknown"),
            "summary": sanitize_ticket_text(
                ticket.get("summary") or issue_key, MAX_SUMMARY_CHARS
            ),
            "description": sanitize_ticket_text(
                ticket.get("description"), MAX_DESCRIPTION_CHARS
            ),
            "triggering_comment": {
                "comment_id": latest_comment["comment_id"],
                "timestamp": normalize_jira_timestamp(latest_comment["updated"]),
                "author": latest_comment["author"],
                "text": sanitize_ticket_text(
                    latest_comment["text"], MAX_COMMENT_CHARS
                ),
            },
            "events_history": build_events_history(payload["request"].get("comments", [])),
        },
        "execution_objectives": {
            "strategy_type": strategy_type,
            "stock_type": options.get("stock_type"),
            "target_date_range": {
                key: str(options[key])
                for key in ("start_date", "end_date")
                if options.get(key)
            },
            "parsed_task_parameters": {
                **parsed_task_parameters,
            },
            "system_instruction_override": None,
            "zero_code_modifications": parse_bool_option(
                options, "zero_code_modifications", False, errors
            ),
            **(
                {"resource_path": options["resource_path"]}
                if options.get("resource_path")
                else {}
            ),
            **(
                {
                    "target_path": options.get("target_path")
                    or options["resource_path"]
                }
                if options.get("target_path") or options.get("resource_path")
                else {}
            ),
        },
        "iteration_controls": iteration_controls,
        "resource_requirements": resource_requirements,
        **({"input_datasets": input_datasets} if input_datasets else {}),
        "output_paths": {
            "result_path": "/workspace/output/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    if request_attachment is not None:
        runtime_request["input_paths"] = {
            "request_template_path": (
                f"/workspace/input/attachments/{issue_key}/"
                f"{request_attachment['filename']}"
            )
        }
    retrieval_context = payload.get("retrieval_context")
    if retrieval_context is not None:
        if not rag_enabled:
            errors.append(
                "retrieval_context is forbidden when rag_enabled is false."
            )
        elif not isinstance(retrieval_context, dict):
            errors.append("retrieval_context must be an object.")
        else:
            context_top_k = retrieval_context.get("requested_top_k")
            if context_top_k != rag_top_k:
                errors.append(
                    "retrieval_context.requested_top_k must match rag_top_k."
                )
            runtime_request["retrieval_context"] = json.loads(
                json.dumps(retrieval_context)
            )
    return runtime_request


def resolve_rag_options(
    options: dict[str, Any],
    errors: list[str],
) -> tuple[bool, int]:
    """Normalize the experiment treatment controls used by gateway and runner."""

    return (
        parse_bool_option(options, "rag_enabled", False, errors),
        parse_int_option(options, "rag_top_k", 5, 1, 10, errors),
    )


def select_request_attachment(
    payload: dict[str, Any],
    options: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    """Resolve one safe Jira .request attachment without guessing."""

    raw_attachments = (payload.get("request") or {}).get("attachments") or []
    eligible: list[dict[str, Any]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "")
        filename = item.get("filename")
        if not attachment_id.isdigit() or not isinstance(filename, str):
            continue
        if not REQUEST_ATTACHMENT_FILENAME_RE.fullmatch(filename):
            continue
        eligible.append(
            {
                "id": attachment_id,
                "filename": filename,
                "size": item.get("size"),
                "mime_type": item.get("mime_type"),
                "created": item.get("created"),
            }
        )

    requested = str(options.get("request_attachment") or "").strip()
    if requested and not REQUEST_ATTACHMENT_FILENAME_RE.fullmatch(requested):
        errors.append(
            "request_attachment must be a plain .request filename without path "
            "components, for example request_attachment=template.request."
        )
        return None

    matches: list[dict[str, Any]] = []
    if requested:
        matches = [item for item in eligible if item["filename"] == requested]
        if not matches:
            errors.append(
                f"Jira attachment '{requested}' was not found on this issue. "
                "Upload it before posting the /quant command."
            )
            return None
    else:
        objective = str(options.get("objective") or "").lower()
        mentioned_names = {
            item["filename"]
            for item in eligible
            if item["filename"].lower() in objective
        }
        if len(mentioned_names) == 1:
            selected_name = next(iter(mentioned_names))
            matches = [
                item for item in eligible if item["filename"] == selected_name
            ]
        elif len(mentioned_names) > 1:
            errors.append(
                "The /quant request names multiple .request attachments. Add "
                "request_attachment: <filename.request> to select exactly one."
            )
            return None
        elif eligible and any(
            marker in objective
            for marker in ("attachment", "attached", "附件")
        ):
            if len({item["filename"] for item in eligible}) == 1:
                matches = eligible
            else:
                errors.append(
                    "Multiple .request attachments are available. Add "
                    "request_attachment: <filename.request> to select exactly one."
                )
                return None

    if not matches:
        return None

    strategy_type = str(options.get("strategy_type") or "").strip().lower()
    if strategy_type and strategy_type != "backtest":
        errors.append("request_attachment is supported only for strategy_type=backtest.")
        return None

    # Re-uploading the same filename creates multiple Jira attachment ids. The
    # highest numeric id is the latest upload and is deterministic.
    return max(matches, key=lambda item: int(item["id"]))


def select_dataset_attachment(
    payload: dict[str, Any],
    options: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    """Resolve one explicitly selected Jira dataset attachment."""

    if "input_attachment" not in options:
        return None

    # An inline "input_attachment: <file>" token binds an empty value and leaves
    # the filename in the objective text. Fail loudly instead of dropping the
    # dataset and running the request with no input mounted.
    requested = str(options.get("input_attachment") or "").strip()
    if not requested:
        errors.append(
            "input_attachment requires a filename, for example "
            "input_attachment=experiment_2_input.csv. Put the option on its own "
            "line to use the 'input_attachment: <filename>' form."
        )
        return None
    if not DATASET_ATTACHMENT_FILENAME_RE.fullmatch(requested):
        errors.append(
            "input_attachment must be a plain .csv, .json, .xlsx, or .parquet "
            "filename without spaces or path components."
        )
        return None

    raw_attachments = (payload.get("request") or {}).get("attachments") or []
    matches: list[dict[str, Any]] = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "")
        filename = item.get("filename")
        if not attachment_id.isdigit() or not isinstance(filename, str):
            continue
        if filename != requested:
            continue
        if not DATASET_ATTACHMENT_FILENAME_RE.fullmatch(filename):
            continue
        size = item.get("size")
        if size is not None:
            try:
                size = int(size)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
        matches.append(
            {
                "id": attachment_id,
                "filename": filename,
                "size": size,
                "mime_type": item.get("mime_type"),
                "created": item.get("created"),
            }
        )

    if not matches:
        errors.append(
            f"Jira dataset attachment {requested!r} was not found on this issue. "
            "Upload it before posting the /quant command."
        )
        return None

    # Re-uploading the same filename creates multiple Jira attachment ids. The
    # highest numeric id is the latest upload and is deterministic.
    return max(matches, key=lambda item: int(item["id"]))


def is_safe_hdfs_input_uri(uri: str) -> bool:
    if not uri or "\\" in uri:
        return False
    parsed = urlparse(uri)
    if parsed.scheme.lower() != "hdfs" or not parsed.path.startswith("/"):
        return False
    if parsed.query or parsed.fragment or parsed.params:
        return False
    path = PurePosixPath(parsed.path)
    return bool(path.name) and ".." not in path.parts


def is_safe_input_mount_path(value: str) -> bool:
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    try:
        relative = path.relative_to(INPUT_DATASET_ROOT)
    except ValueError:
        return False
    relative_text = relative.as_posix()
    return (
        bool(relative.parts)
        and ".." not in path.parts
        and bool(path.name)
        and RESOURCE_PATH_RE.fullmatch(relative_text) is not None
    )


def infer_input_format(filename: str) -> str | None:
    return INPUT_DATASET_EXTENSIONS.get(PurePosixPath(filename).suffix.lower())


def dataset_id_from_filename(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    dataset_id = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return dataset_id[:80] or "input_dataset"


def build_input_datasets(
    options: dict[str, Any],
    errors: list[str],
    *,
    dataset_attachment: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    source_uri = options.get("input_hdfs_uri")
    requested_attachment = options.get("input_attachment")
    mount_path_option = options.get("input_mount_path")
    format_option = options.get("input_format")

    if source_uri and (requested_attachment or dataset_attachment is not None):
        errors.append(
            "Use either input_hdfs_uri or input_attachment for one dataset, not both."
        )
        return []

    if not source_uri and dataset_attachment is None:
        if mount_path_option:
            errors.append(
                "input_mount_path requires input_hdfs_uri or input_attachment."
            )
        if format_option:
            errors.append("input_format requires input_hdfs_uri or input_attachment.")
        return []

    if dataset_attachment is not None:
        original_filename = str(dataset_attachment["filename"])
        source_kind = "jira_attachment"
        attachment_id = str(dataset_attachment["id"])
        source_value = f"jira-attachment://{attachment_id}"
    else:
        parsed = urlparse(str(source_uri))
        original_filename = PurePosixPath(parsed.path).name
        source_kind = "hdfs"
        source_value = str(source_uri)

    inferred_format = infer_input_format(original_filename)
    declared_format = str(format_option or inferred_format or "").lower()
    if not declared_format:
        errors.append(
            "Could not infer the dataset format from the selected input. "
            "Set input_format explicitly."
        )
        return []
    if declared_format not in INPUT_DATASET_FORMATS:
        errors.append(
            "input_format must be one of: "
            f"{', '.join(sorted(INPUT_DATASET_FORMATS))}."
        )
        return []
    if inferred_format and inferred_format != declared_format:
        errors.append(
            f"input_format={declared_format} does not match the file extension "
            f"for {original_filename!r} ({inferred_format})."
        )

    mount_path = str(
        mount_path_option
        or (INPUT_DATASET_ROOT / original_filename).as_posix()
    )
    if not is_safe_input_mount_path(mount_path):
        errors.append(
            "input_mount_path must name a file below "
            "/workspace/input/datasets/ and must not contain '..'."
        )
        return []
    if PurePosixPath(mount_path).name != original_filename:
        mount_format = infer_input_format(PurePosixPath(mount_path).name)
        if mount_format and mount_format != declared_format:
            errors.append("input_mount_path extension must agree with input_format.")

    record: dict[str, Any] = {
        "dataset_id": dataset_id_from_filename(original_filename),
        "source_kind": source_kind,
        "original_filename": original_filename,
        "source_uri": source_value,
        "container_path": mount_path,
        "format": declared_format,
        "read_only": True,
    }
    if dataset_attachment is not None:
        record["jira_attachment_id"] = str(dataset_attachment["id"])
        if dataset_attachment.get("mime_type"):
            record["jira_mime_type"] = str(dataset_attachment["mime_type"])
        if dataset_attachment.get("created"):
            record["jira_created"] = str(dataset_attachment["created"])
        if dataset_attachment.get("size") is not None:
            record["size_bytes"] = int(dataset_attachment["size"])
    return [record]


def build_repository_details(
    options: dict[str, Any],
    issue_key: str,
    errors: list[str],
) -> list[dict[str, Any]] | None:
    selections: dict[str, dict[str, Any]] = {}

    def select(raw_alias: Any, *, source_branch: str | None = None) -> None:
        entry = resolve_repository(raw_alias)
        if entry is None:
            raw_text = str(raw_alias)
            if "github.com/" in raw_text or "/" in raw_text:
                errors.append(
                    "Repository must be in the approved bankingscience GitHub "
                    "organisation and known repository catalog, for example "
                    "repo=bankingscience/BSLAgenticQuantDevLoop."
                )
            else:
                errors.append(
                    f"Unknown repository alias '{raw_alias}'. Accepted aliases include: "
                    f"{', '.join(accepted_aliases())}."
                )
            return
        record = selections.setdefault(
            entry.alias,
            {
                "entry": entry,
                "source_branch": entry.default_source_branch,
                "source_explicit": False,
                "target_branch": None,
            },
        )
        if source_branch:
            existing = record.get("source_branch")
            if record.get("source_explicit") and existing and existing != source_branch:
                errors.append(
                    f"Repository alias '{entry.alias}' has conflicting source "
                    f"branches '{existing}' and '{source_branch}'."
                )
            record["source_branch"] = source_branch
            record["source_explicit"] = True

    for raw in split_repository_list(options.get("repositories")):
        select(raw)

    clone_url = options.get("clone_url")
    if clone_url:
        select(clone_url)

    branch_map = parse_branch_map(options.get("branch_map"), errors, "branch_map")
    for raw_alias, source_branch in branch_map.items():
        select(raw_alias, source_branch=source_branch)

    if not selections:
        for entry in prose_aliases_in(options.get("objective") or ""):
            select(entry.alias)

    if not selections:
        select(default_repository().alias)

    target_map = parse_branch_map(
        options.get("target_branch_map"),
        errors,
        "target_branch_map",
    )
    for raw_alias, target_branch in target_map.items():
        entry = resolve_repository(raw_alias)
        if entry is None:
            errors.append(
                f"Unknown repository alias '{raw_alias}' in target_branch_map. "
                f"Accepted aliases include: {', '.join(accepted_aliases())}."
            )
            continue
        if entry.alias not in selections:
            errors.append(
                f"target_branch_map references '{entry.alias}', but that repository "
                "was not selected by repo/repos or branch_map."
            )
            continue
        existing_target = selections[entry.alias].get("target_branch")
        if existing_target and existing_target != target_branch:
            errors.append(
                f"Repository '{entry.repo_full_name}' has conflicting target "
                f"branches '{existing_target}' and '{target_branch}'."
            )
        selections[entry.alias]["target_branch"] = target_branch

    allowed_map = parse_allowed_directories_map(
        options.get("allowed_directories_map"), errors
    )
    for alias in allowed_map:
        if alias not in selections:
            errors.append(
                f"allowed_directories_map references '{alias}', but that repository "
                "was not selected by repo/repos or branch_map."
            )

    single_repo = len(selections) == 1
    repositories: list[dict[str, Any]] = []
    for alias, record in selections.items():
        entry = record["entry"]
        source_branch = str(
            record.get("source_branch") or entry.default_source_branch
        ).strip()
        target_branch = record.get("target_branch")
        if not target_branch and options.get("target_branch") and single_repo:
            target_branch = str(options["target_branch"]).strip()
        if not target_branch:
            target_branch = f"quant/{issue_key}"

        allowed_branch = f"quant/{issue_key}"
        if target_branch != allowed_branch and not target_branch.startswith(
            f"{allowed_branch}/"
        ):
            errors.append(
                f"target branch for '{alias}' must be the current ticket quant "
                f"branch '{allowed_branch}' or one of its descendants."
            )

        validate_branch_name(source_branch, "source branch", errors)
        validate_branch_name(target_branch, "target branch", errors)

        repository = {
            "alias": alias,
            "repo_full_name": entry.repo_full_name,
            "clone_url": entry.clone_url,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "runtime_role": entry.runtime_role,
            "allowed_directories": allowed_map.get(alias)
            or options.get("allowed_directories")
            or ["."],
        }
        repositories.append(repository)

    if repositories:
        check_repository_paths_in_scope(
            options,
            errors,
            repositories[0].get("allowed_directories") or ["."],
        )
    return repositories


def split_repository_list(value: Any) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"[,;]", str(value)) if part.strip()]


def parse_branch_map(
    value: Any,
    errors: list[str],
    option_name: str,
) -> dict[str, str]:
    if not value:
        return {}
    parsed: dict[str, str] = {}
    for raw_part in re.split(r"[,;]", str(value)):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            errors.append(
                f"{option_name} entry '{part}' must use alias=branch syntax."
            )
            continue
        alias, branch = part.split("=", 1)
        alias = alias.strip()
        branch = branch.strip()
        if not alias or not branch:
            errors.append(
                f"{option_name} entry '{part}' must include both alias and branch."
            )
            continue
        if alias in parsed and parsed[alias] != branch:
            errors.append(
                f"{option_name} has conflicting values for '{alias}': "
                f"'{parsed[alias]}' and '{branch}'."
            )
        parsed[alias] = branch
    return parsed


def parse_allowed_directories_map(
    value: Any,
    errors: list[str],
) -> dict[str, list[str]]:
    """Parse ``repo=path|path; repo=.`` into canonical catalog aliases."""

    if not value:
        return {}
    parsed: dict[str, list[str]] = {}
    for raw_part in str(value).split(";"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" not in part:
            errors.append(
                f"allowed_directories_map entry '{part}' must use alias=path|path syntax."
            )
            continue
        raw_alias, raw_paths = part.split("=", 1)
        raw_alias = raw_alias.strip()
        entry = resolve_repository(raw_alias)
        if entry is None:
            errors.append(
                f"Unknown repository alias '{raw_alias}' in allowed_directories_map. "
                f"Accepted aliases include: {', '.join(accepted_aliases())}."
            )
            continue
        paths = [path.strip() for path in raw_paths.split("|") if path.strip()]
        if not paths:
            errors.append(
                f"allowed_directories_map entry '{part}' must include at least one path."
            )
            continue
        invalid = [path for path in paths if not RESOURCE_PATH_RE.fullmatch(path)]
        if invalid:
            errors.append(
                "allowed_directories_map paths must be repository-relative without "
                f"a leading '/' or '..': {', '.join(invalid)}."
            )
            continue
        normalized_paths = list(dict.fromkeys(paths))
        existing = parsed.get(entry.alias)
        if existing is not None and existing != normalized_paths:
            errors.append(
                f"allowed_directories_map has conflicting values for "
                f"'{entry.alias}': '{'|'.join(existing)}' and "
                f"'{'|'.join(normalized_paths)}'."
            )
            continue
        parsed[entry.alias] = normalized_paths
    return parsed


def validate_branch_name(value: str, label: str, errors: list[str]) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value or ""):
        errors.append(f"{label} '{value}' must use letters, numbers, '.', '_', '-' or '/'.")


def check_repository_paths_in_scope(
    options: dict[str, Any],
    errors: list[str],
    allowed_directories: list[str],
) -> None:
    """A target outside the allowlist is a contradiction in the ticket itself.

    The MCP server enforces allowed_directories at runtime, so this would otherwise
    surface as the agent being blocked from the very file it was told to edit. Fail
    here instead, where the message can name both halves of the conflict.
    """
    allowed = [str(d).strip().strip("/") for d in allowed_directories]
    for field in ("resource_path", "target_path"):
        path = options.get(field)
        if not path:
            continue
        if any(
            directory == "."
            or path == directory
            or path.startswith(directory + "/")
            for directory in allowed
            if directory
        ):
            continue
        errors.append(
            f"{field} '{path}' is outside allowed_directories "
            f"({', '.join(allowed)}). Add its directory to allowed_directories, or "
            "drop allowed_directories to leave the run unrestricted."
        )


def build_iteration_controls(
    options: dict[str, Any],
    errors: list[str],
    *,
    strategy_type: str,
) -> dict[str, Any]:
    controls = {
        "allow_iteration": parse_bool_option(
            options,
            "allow_iteration",
            # Backtest runs iterate by default: the engine returns measured metrics
            # to improve against. General runs are scored by an advisory prose
            # review, so they run once unless the ticket opts in.
            strategy_type == "backtest",
            errors,
        ),
        "max_iterations": parse_int_option(
            options,
            "max_iterations",
            DEFAULT_MAX_ITERATIONS,
            1,
            10,
            errors,
        ),
        "max_failed_iterations": parse_int_option(
            options,
            "max_failed_iterations",
            DEFAULT_MAX_FAILED_ITERATIONS,
            1,
            10,
            errors,
        ),
        "max_agent_turns": (
            parse_int_option(options, "max_agent_turns", 30, 10, 60, errors)
            if "max_agent_turns" in options
            else DEFAULT_MAX_AGENT_TURNS
        ),
        "timeout_seconds": parse_int_option(
            options,
            "timeout_seconds",
            DEFAULT_TIMEOUT_SECONDS,
            60,
            10800,
            errors,
        ),
        "max_token_budget_per_run": parse_int_option(
            options,
            "max_token_budget_per_run",
            DEFAULT_MAX_TOKEN_BUDGET,
            1000,
            1000000,
            errors,
        ),
    }
    # Preserve the deployment-level MAX_COMMITS_PER_RUN setting when a Jira
    # request does not explicitly opt into a different bounded per-pass cap.
    if "max_commits_per_run" in options:
        controls["max_commits_per_run"] = parse_int_option(
            options,
            "max_commits_per_run",
            5,
            1,
            50,
            errors,
        )
    return controls


def build_resource_requirements(
    options: dict[str, Any],
    errors: list[str],
    *,
    default_execution_timeout_seconds: int = DEFAULT_EXECUTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build bounded per-workflow runtime resources with safe defaults."""

    gpu_count = DEFAULT_GPU_COUNT
    if options.get("gpu_count") is not None:
        try:
            requested_gpu_count = int(options["gpu_count"])
        except (TypeError, ValueError):
            errors.append("gpu_count must be the integer 0 because GPUs are unsupported.")
        else:
            if requested_gpu_count != 0:
                errors.append(
                    "gpu_count is not supported by the current Docker/YARN runtime; "
                    "use 0."
                )

    yarn_queue = options.get("yarn_queue")
    if yarn_queue is not None:
        yarn_queue = str(yarn_queue).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", yarn_queue):
            errors.append(
                "yarn_queue must start with a letter or number and contain only "
                "letters, numbers, '.', '_', or '-'."
            )
            yarn_queue = None

    resources: dict[str, Any] = {
        "cpu_vcpus": parse_float_option(
            options,
            "cpu_vcpus",
            DEFAULT_CPU_VCPUS,
            0.25,
            8.0,
            errors,
        ),
        "memory_mb": parse_int_option(
            options,
            "memory_mb",
            DEFAULT_MEMORY_MB,
            512,
            16384,
            errors,
        ),
        "gpu_count": gpu_count,
        "execution_timeout_seconds": parse_int_option(
            options,
            "execution_timeout_seconds",
            default_execution_timeout_seconds,
            60,
            10800,
            errors,
        ),
        "pids_limit": parse_int_option(
            options,
            "pids_limit",
            DEFAULT_PIDS_LIMIT,
            64,
            1024,
            errors,
        ),
    }
    if yarn_queue:
        resources["yarn_queue"] = yarn_queue
    return resources


TRUE_VALUES = {"true", "yes", "y", "on", "1"}
FALSE_VALUES = {"false", "no", "n", "off", "0"}


def parse_bool_option(
    options: dict[str, Any],
    key: str,
    default: bool,
    errors: list[str],
) -> bool:
    value = options.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    normalized = str(value).strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False

    errors.append(f"{key} must be true or false.")
    return default


def parse_int_option(
    options: dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
    errors: list[str],
) -> int:
    value = options.get(key)
    if value is None:
        return default

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        errors.append(f"{key} must be an integer between {minimum} and {maximum}.")
        return default

    if parsed < minimum or parsed > maximum:
        errors.append(f"{key} must be between {minimum} and {maximum}.")

    return parsed


def parse_float_option(
    options: dict[str, Any],
    key: str,
    default: float,
    minimum: float,
    maximum: float,
    errors: list[str],
) -> float:
    value = options.get(key)
    if value is None:
        return default

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        errors.append(f"{key} must be a number between {minimum} and {maximum}.")
        return default

    if not math.isfinite(parsed):
        errors.append(f"{key} must be a finite number.")
        return default

    if parsed < minimum or parsed > maximum:
        errors.append(f"{key} must be between {minimum} and {maximum}.")

    return parsed


def normalize_clone_url(value: Any) -> str | None:
    if not value:
        return None

    clone_url = str(value).strip()
    if re.fullmatch(r"bankingscience/[A-Za-z0-9_.-]+", clone_url):
        return f"https://github.com/{clone_url}.git"

    return clone_url


def resolve_strategy_type(value: Any, errors: list[str]) -> str:
    """Resolve the declared workflow type. Never inferred from the objective text.

    Guessing from prose used to route "refactor the backtest harness" to the engine,
    because the objective merely mentioned the word. Requiring the caller to declare
    the type keeps a misroute a loud validation error instead of a silent real run.
    Returns a schema-valid placeholder on error so schema validation can still run and
    report every problem at once.
    """
    if not isinstance(value, str) or not value.strip():
        errors.append(
            "strategy_type is required. Add one of: "
            f"{', '.join(sorted(WORKFLOW_TYPES))}, for example strategy_type=backtest."
        )
        return "other"

    strategy_type = value.strip().lower()
    if strategy_type not in WORKFLOW_TYPES:
        errors.append(
            f"strategy_type '{value.strip()}' is not supported. Use one of: "
            f"{', '.join(sorted(WORKFLOW_TYPES))}."
        )
        return "other"

    return strategy_type


def build_events_history(comments: list[Any]) -> list[dict[str, Any]]:
    events = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue

        timestamp = comment.get("updated") or comment.get("created")
        text = comment.get("text")
        if not isinstance(timestamp, str) or not isinstance(text, str):
            continue

        event = {
            "event_type": "comment",
            "timestamp": normalize_jira_timestamp(timestamp),
            "text": sanitize_ticket_text(text, MAX_COMMENT_CHARS),
        }
        author = comment.get("author")
        if isinstance(author, str) and author:
            event["author"] = author
        events.append(event)

    events.sort(key=lambda event: datetime.fromisoformat(event["timestamp"]))
    return events[-MAX_HISTORY_COMMENTS:]


def validate_runtime_request_schema(runtime_request: dict[str, Any]) -> list[str]:
    schema = json.loads(
        (Path(__file__).parents[1] / "schemas" / "runtime_request.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())

    return [
        f"Runtime request {format_schema_path(error.absolute_path)}: {error.message}"
        for error in sorted(validator.iter_errors(runtime_request), key=str)
    ]


def format_schema_path(path: Any) -> str:
    path_parts = [str(part) for part in path]
    if not path_parts:
        return "payload"
    return ".".join(path_parts)


def build_run_id(issue_key: str, timestamp: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "", timestamp)
    suffix = normalized or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"run_{issue_key}_{suffix}"


def parse_jira_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def normalize_jira_timestamp(value: str) -> str:
    parsed = parse_jira_datetime(value)
    return parsed.isoformat()
