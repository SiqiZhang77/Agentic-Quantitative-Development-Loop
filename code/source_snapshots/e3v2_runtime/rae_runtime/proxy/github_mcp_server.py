"""
MCP server exposing GitHub read and branch operations as tools the LLM can call.

Wraps the existing github_client.py functions (read, branch create/reuse,
commit/push). Every tool validates the requested repo/branch against an
allowlist before doing anything — see _check_scope.

Modes:
1) Click-run / demo mode (default):
   python github_mcp_server.py
   Runs a quick local sanity check and exits.

2) Real MCP server mode:
   python github_mcp_server.py --serve
   Starts FastMCP and waits for tool calls over stdio.
"""

from __future__ import annotations
try:
    from github.GithubException import RateLimitExceededException, GithubException
except ImportError:  # pragma: no cover - exercised only in lightweight local envs
    class GithubException(Exception):
        status = None

    class RateLimitExceededException(GithubException):
        pass

import argparse
import fnmatch
from functools import wraps
import inspect
import json
import os
import posixpath
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_BATCH_READ_FILES = 8
MAX_BATCH_READ_BYTES = 96 * 1024
MAX_SEARCH_QUERY_CHARS = 200
MAX_SEARCH_MATCHES = 8
MAX_SEARCH_CONTEXT_LINES = 12
MAX_SEARCH_LINE_CHARS = 1_000
MAX_SEARCH_RETURN_CHARS = 12_000
MAX_SURGICAL_REPLACEMENT_CHARS = 24_000
E3_MCP_AUDIT_SCHEMA_VERSION = "exp3-mcp-tool-audit-v1"


class McpAuditPersistenceError(RuntimeError):
    """An explicit E3 tool call cannot persist its content-free audit."""


def _explicit_e3_mode() -> bool:
    return os.getenv("RAE_ARCHITECTURE_MODE") in {"single_agent", "manager_star"}


def _open_audit_fd(destination: str) -> int:
    path = Path(destination)
    if not path.is_absolute():
        raise McpAuditPersistenceError("E3 MCP audit path must be absolute")
    if not path.parent.is_dir():
        raise McpAuditPersistenceError("E3 MCP audit parent directory is unavailable")
    if path.is_symlink():
        raise McpAuditPersistenceError("E3 MCP audit path must not be a symlink")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise McpAuditPersistenceError("E3 MCP audit sink is unavailable") from exc


def _require_e3_audit_sink() -> str | None:
    """Validate the explicit-E3 audit sink before a repository backend call."""

    destination = os.getenv("MCP_TOOL_AUDIT_PATH")
    if not _explicit_e3_mode():
        return destination
    if not destination:
        raise McpAuditPersistenceError("explicit E3 requires MCP_TOOL_AUDIT_PATH")
    fd = _open_audit_fd(destination)
    try:
        os.fsync(fd)
    except OSError as exc:
        raise McpAuditPersistenceError("E3 MCP audit sink cannot be synchronized") from exc
    finally:
        os.close(fd)
    return destination


def _append_audit_record(destination: str, record: dict[str, Any]) -> None:
    payload = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd = _open_audit_fd(destination)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if type(written) is not int or written <= 0:
                raise McpAuditPersistenceError("E3 MCP audit write was incomplete")
            offset += written
        os.fsync(fd)
    except OSError as exc:
        raise McpAuditPersistenceError("E3 MCP audit record could not be persisted") from exc
    finally:
        os.close(fd)


def _write_tool_audit(tool: str, arguments: dict[str, Any], result: Any) -> None:
    """Persist bounded, content-free tool metadata for terminal diagnostics."""
    destination = os.getenv("MCP_TOOL_AUDIT_PATH")
    if not destination:
        if _explicit_e3_mode():
            raise McpAuditPersistenceError(
                "explicit E3 requires MCP_TOOL_AUDIT_PATH"
            )
        return
    safe = result if isinstance(result, dict) else {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "status": str(safe.get("status") or "unknown"),
        "repo_name": safe.get("repo_name") or arguments.get("repo_name"),
        "branch": safe.get("branch") or arguments.get("branch"),
        "path": safe.get("path") or arguments.get("path"),
        "directory": safe.get("directory") or arguments.get("directory"),
        "paths": safe.get("paths") or arguments.get("paths"),
        "error": str(safe.get("error") or "")[:500] or None,
    }
    if os.getenv("RAE_ARCHITECTURE_MODE") == "manager_star":
        record["architecture_mode"] = "manager_star"
        record["role"] = os.getenv("RAE_AGENT_ROLE")
    try:
        if _explicit_e3_mode():
            record["schema_version"] = E3_MCP_AUDIT_SCHEMA_VERSION
            record["record_type"] = "tool_call"
            _append_audit_record(destination, record)
        else:
            # Preserve the legacy optional-audit behaviour, including relative
            # destinations configured by existing local callers.
            with open(destination, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, McpAuditPersistenceError) as exc:
        if _explicit_e3_mode():
            if isinstance(exc, McpAuditPersistenceError):
                raise
            raise McpAuditPersistenceError(
                "E3 MCP audit record could not be persisted"
            ) from exc


def _manager_star_permission_error(tool_name: str) -> str | None:
    """Return a safe denial for missing E3 isolation or M1 role permission.

    The check is intentionally performed at the MCP server boundary rather than
    relying on which tools a prompt advertises. Legacy requests omit architecture
    metadata and retain their existing behaviour. Both explicit E3 arms require
    the same passing negative-ref binding; only M1 additionally applies roles.
    """

    architecture_mode = os.getenv("RAE_ARCHITECTURE_MODE")
    role = os.getenv("RAE_AGENT_ROLE")
    if architecture_mode in {None, ""}:
        if role:
            return (
                "Experiment 3 server-side role policy denied an incomplete "
                "role/architecture binding."
            )
        return None
    if architecture_mode not in {"single_agent", "manager_star"}:
        return "Experiment 3 server-side role policy denied an unknown architecture mode."

    binding_values = (
        os.getenv("E3_NEGATIVE_REF_MANIFEST_SHA256"),
        os.getenv("E3_NEGATIVE_REF_SET_SHA256"),
    )
    binding_valid = (
        os.getenv("E3_REF_SCOPE_PREFLIGHT_PASSED") == "true"
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[A-Fa-f0-9]{64}", value) is not None
            for value in binding_values
        )
    )
    if not binding_valid:
        return (
            "Experiment 3 server-side role policy denied a call without a "
            "passing negative-ref preflight binding."
        )

    if architecture_mode == "single_agent":
        if role:
            return (
                "Experiment 3 server-side role policy denied a role marker in "
                "single-agent mode."
            )
        return None

    try:
        from exp3.policy import require_tool_permission

        require_tool_permission(role, tool_name)
    except Exception:
        # Missing/unknown roles, denied tools, and an unavailable policy module
        # all fail closed.  Keep the model-facing error content-free and avoid
        # echoing exception details that could expose process configuration.
        return (
            "Experiment 3 server-side role policy denied this tool call "
            f"(tool={tool_name!r})."
        )
    return None


def _audited(tool_name: str):
    """Enforce E3 isolation/M1 role policy and audit without model content."""
    def decorate(fn):
        signature = inspect.signature(fn)

        @wraps(fn)
        def wrapped(*args, **kwargs):
            bound = signature.bind_partial(*args, **kwargs)
            # Explicit E3 must prove the audit sink is usable before a read or
            # write backend can run. Legacy traffic retains optional auditing.
            _require_e3_audit_sink()
            permission_error = _manager_star_permission_error(tool_name)
            if permission_error:
                result = {"status": "blocked", "error": permission_error}
            else:
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:  # pragma: no cover - defensive tool boundary
                    result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            _write_tool_audit(tool_name, dict(bound.arguments), result)
            return result

        wrapped.__signature__ = signature
        return wrapped
    return decorate


def _register_mcp_tool(mcp_server: Any, tool_name: str):
    """Register only tools visible to the process' fail-closed E3 identity.

    The audited wrapper remains the authoritative call-time permission check.
    This registration-time filter additionally keeps M1 write tools out of the
    manager/architect tool catalogue and exposes no tools when an explicit E3
    isolation binding is incomplete or invalid.
    """

    def decorate(fn):
        if _manager_star_permission_error(tool_name) is not None:
            return fn
        return mcp_server.tool()(fn)

    return decorate

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from github_client import (
    NoChangesError,
    REPO_NAME as _DEFAULT_REPO_NAME,
    get_strategy_code as _get_strategy_code,
    create_feature_branch as _create_feature_branch,
    list_files as _list_files,
    push_strategy_code as _push_strategy_code,
)
from artifact_validation import proposed_artifact_issues
from content_downsizing import (
    downsize_for_model,
    is_notebook,
    notebook_structure_errors,
)

try:
    from fastmcp import FastMCP
except ImportError as exc:
    FastMCP = None
    _FASTMCP_IMPORT_ERROR = exc
else:
    _FASTMCP_IMPORT_ERROR = None


# ------------------------- ALLOWLIST SCOPING -------------------------
# Configured at server startup via env vars, consistent with how
# GITHUB_TOKEN / API_KEY are already configured elsewhere in this project.
#
# ALLOWED_REPOS: comma-separated list of "owner/repo" strings.
#   Defaults to just the repo github_client.py already targets, so today's
#   single-repo behaviour is unchanged. Written as a list now so RAE-20/21's
#   multi-repo work later only needs to grow this list, not rewrite the check.
# ALLOWED_BRANCH_PATTERN: a glob-style pattern (fnmatch). Defaults to
#   "quant/*" — this also has the side effect of blocking direct writes
#   to "main", since "main" won't match the pattern.

def _load_allowlist() -> tuple[set[str], str]:
    repos_raw = os.getenv("ALLOWED_REPOS", _DEFAULT_REPO_NAME)
    allowed_repos = {r.strip() for r in repos_raw.split(",") if r.strip()}
    branch_pattern = os.getenv("ALLOWED_BRANCH_PATTERN", "quant/*")
    return allowed_repos, branch_pattern


# ALLOWED_DIRECTORIES: comma/space-separated repo-relative directories the run may
#   read or write, from the request's repository_details[].allowed_directories.
#   Empty (the default) means unrestricted, preserving single-target behaviour for
#   requests that supply no allowlist.
#
# Enforced here rather than in the prompt on purpose: the prompt is a request, the
# server is the boundary. A model that ignores its instructions, or Jira-authored
# content that talks it into straying, still cannot read or write out of scope.

def _load_path_scope() -> tuple[str, ...]:
    raw = os.getenv("ALLOWED_DIRECTORIES", "")
    return tuple(
        entry.strip().strip("/")
        for entry in re.split(r"[,\s]+", raw)
        if entry.strip().strip("/")
    )


def _load_path_scope_map() -> dict[str, tuple[str, ...]]:
    raw = os.getenv("ALLOWED_DIRECTORIES_MAP", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for repo_name, directories in parsed.items():
        if not isinstance(repo_name, str) or not isinstance(directories, list):
            continue
        result[repo_name] = tuple(
            str(entry).strip().strip("/")
            for entry in directories
            if str(entry).strip().strip("/")
        )
    return result


def _load_branch_map(variable_name: str) -> dict[str, str]:
    raw = os.getenv(variable_name, "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(repo_name): str(branch)
        for repo_name, branch in parsed.items()
        if str(repo_name).strip() and str(branch).strip()
    }


_ALLOWED_REPOS, _ALLOWED_BRANCH_PATTERN = _load_allowlist()
_ALLOWED_DIRECTORIES = _load_path_scope()
_ALLOWED_DIRECTORIES_MAP = _load_path_scope_map()
_SOURCE_BRANCH_MAP = _load_branch_map("SOURCE_BRANCH_MAP")
_TARGET_BRANCH_MAP = _load_branch_map("TARGET_BRANCH_MAP")
_ENFORCE_READ_BRANCH_SCOPE = os.getenv(
    "ENFORCE_READ_BRANCH_SCOPE", "false"
).strip().lower() in {"1", "true", "yes", "on"}


def _path_scope_for(repo_name: str) -> tuple[str, ...]:
    return _ALLOWED_DIRECTORIES_MAP.get(repo_name, _ALLOWED_DIRECTORIES)


# Reads this run served in reduced form, keyed by (repo, branch, path). A commit
# built on one of these would write back content the model never fully saw. The
# server is a per-run subprocess, so this state is run-scoped.
#
# Branch handling is deliberately asymmetric:
# - Clearing is per-branch. Reading one branch's copy in full says nothing about
#   another branch's copy, so it may only supersede its own earlier reduced read.
# - Blocking ignores the branch. A target branch is cut from its source, so a
#   reduced read of the source is a reduced read of what the commit overwrites.
#   Keying the block by branch would miss the ordinary flow entirely, which reads
#   the source branch and commits to the target.
_REDUCED_READS: dict[tuple[str, str, str], str] = {}


def _read_key(repo_name: str, branch: str, path: str) -> tuple[str, str, str]:
    return repo_name, branch, _normalise_repo_path(path) or path


def _record_read_reduction(repo_name: str, branch: str, path: str, kind: str) -> None:
    """Record how much of a file the model actually saw, so writes can be gated."""

    key = _read_key(repo_name, branch, path)
    if kind == "none":
        # A complete read supersedes an earlier reduced read of the same copy.
        _REDUCED_READS.pop(key, None)
    else:
        _REDUCED_READS[key] = kind


def _reduction_kinds_for(repo_name: str, path: str) -> set[str]:
    """Every reduction recorded for this path, across all branches read."""

    normalised = _normalise_repo_path(path) or path
    return {
        kind
        for (repo, _branch, recorded_path), kind in _REDUCED_READS.items()
        if repo == repo_name and recorded_path == normalised
    }


def _validate_proposed_content(
    *,
    branch: str,
    path: str,
    content: str,
    repo_name: str,
    allow_truncated_source: bool = False,
) -> list[str]:
    """Run deterministic checks without writing the proposed content."""

    reductions = _reduction_kinds_for(repo_name, path)

    # read_file hands the model a stripped, plain-text view of a notebook. Writing
    # that back would replace a real notebook with something structurally invalid
    # and destroy its outputs, so refuse anything that is not a genuine notebook.
    if is_notebook(path):
        structure_errors = notebook_structure_errors(content)
        if structure_errors:
            return [
                f"Committed artifact {path} is not a valid Jupyter notebook "
                f"({'; '.join(structure_errors)}). Notebooks are read as a reduced, "
                "plain-text view of their cells, and that view cannot be committed "
                "back. Write changes to a source file instead, or supply complete "
                "valid .ipynb JSON."
            ]

    # Structural validity is not enough on its own: a model that saw a reduced
    # notebook knows its cell sources but not its outputs or metadata, so it can
    # assemble notebook JSON that parses and still silently drops everything it
    # never saw. Only a notebook this run never read in reduced form may be
    # written, which in practice means notebooks are reference-only.
    if is_notebook(path) and any(kind.startswith("notebook") for kind in reductions):
        return [
            f"Committed artifact {path} was read as a reduced view without its "
            "outputs or metadata, so committing any notebook for that path would "
            "discard them. Write the change to a source file instead."
        ]

    if any("truncated" in kind for kind in reductions) and not allow_truncated_source:
        return [
            f"Committed artifact {path} was only read in truncated form, so "
            "committing it would overwrite the file with a partial copy and drop "
            "the remainder. Make the change in a smaller file, or split the work "
            "so the whole file fits."
        ]

    source_branch = _SOURCE_BRANCH_MAP.get(repo_name) or os.getenv("SOURCE_BRANCH") or branch
    return proposed_artifact_issues(
        path=path,
        content=content,
        source_branch=source_branch,
        target_branch=branch,
        repo_name=repo_name,
        reader=_get_strategy_code,
    )


def _matching_line_windows(
    content: str,
    query: str,
    *,
    context_lines: int,
    max_matches: int,
) -> list[dict[str, Any]]:
    """Return bounded, line-numbered literal-query windows from a text file."""

    lines = content.splitlines()
    matches: list[dict[str, Any]] = []
    returned_chars = 0
    for index, line in enumerate(lines):
        if query not in line:
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        rendered_lines = []
        for line_number in range(start, end):
            line_text = lines[line_number]
            if len(line_text) > MAX_SEARCH_LINE_CHARS:
                line_text = line_text[:MAX_SEARCH_LINE_CHARS] + " …[line truncated]"
            rendered_lines.append(f"{line_number + 1}: {line_text}")
        rendered = "\n".join(rendered_lines)
        if matches and returned_chars + len(rendered) > MAX_SEARCH_RETURN_CHARS:
            break
        matches.append(
            {
                "line": index + 1,
                "start_line": start + 1,
                "end_line": end,
                "content": rendered,
            }
        )
        returned_chars += len(rendered)
        if len(matches) >= max_matches:
            break
    return matches


def _normalise_repo_path(path: str) -> str | None:
    """Repo-relative posix path, or None if it escapes the repository.

    The gateway validates the ticket's resource_path, but the agent passes these
    paths itself, so they are untrusted here and must be re-checked.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = path.strip()
    if candidate.startswith("/") or "\\" in candidate:
        return None
    # Reject traversal syntax itself before normalising.  Otherwise normpath turns
    # ``safe/../secret.py`` into ``secret.py`` and an unrestricted run accepts the
    # original untrusted path even though the public contract says ``..`` is always
    # forbidden.
    if ".." in candidate.split("/"):
        return None
    normalised = posixpath.normpath(candidate)
    if normalised == ".." or normalised.startswith("../") or normalised.startswith("/"):
        return None
    return normalised


def _in_scope(normalised: str, allowed_directories: tuple[str, ...]) -> bool:
    if "." in allowed_directories:
        return True
    return any(
        normalised == directory or normalised.startswith(directory + "/")
        for directory in allowed_directories
    )


def _check_path_scope(repo_name: str, path: str | None = None) -> str | None:
    """Validate a file path for read/write. Blocks traversal always, and paths
    outside allowed_directories when the request supplied one."""
    if path is None:
        path = repo_name
        repo_name = _DEFAULT_REPO_NAME
    normalised = _normalise_repo_path(path)
    if normalised is None:
        return (
            f"Path '{path}' is not a repository-relative path. Paths must not be "
            "absolute or contain '..'."
        )
    allowed_directories = _path_scope_for(repo_name)
    if not allowed_directories:
        return None
    if _in_scope(normalised, allowed_directories):
        return None
    return (
        f"Path '{path}' is outside the directories this run may touch in "
        f"'{repo_name}' ({list(allowed_directories)})."
    )


def _check_list_scope(repo_name: str, directory: str | None = None) -> str | None:
    """As _check_path_scope, but a directory listing may also walk the ancestors of
    an allowed directory: that reveals only names on the way down, and without it
    the agent cannot navigate to the directories it is allowed to work in."""
    if directory is None:
        directory = repo_name
        repo_name = _DEFAULT_REPO_NAME
    # Listing the repo root reveals top-level names only, which the agent needs in
    # order to find the allowed directories at all.
    if not directory or directory.strip() in {"", "."}:
        return None
    normalised = _normalise_repo_path(directory)
    if normalised is None:
        return (
            f"Directory '{directory}' is not a repository-relative path. Paths must "
            "not be absolute or contain '..'."
        )
    allowed_directories = _path_scope_for(repo_name)
    if not allowed_directories:
        return None
    if "." in allowed_directories:
        return None
    if _in_scope(normalised, allowed_directories) or any(
        allowed.startswith(normalised + "/") for allowed in allowed_directories
    ):
        return None
    return (
        f"Directory '{directory}' is outside the directories this run may touch in "
        f"'{repo_name}' ({list(allowed_directories)})."
    )


def _check_repo_scope(repo_name: str) -> str | None:
    """Validate a requested repository against the run allowlist."""
    if repo_name not in _ALLOWED_REPOS:
        return (
            f"Repository '{repo_name}' is not in the allowed repository list "
            f"({sorted(_ALLOWED_REPOS)})."
        )
    return None


def _check_read_scope(repo_name: str, branch: str) -> str | None:
    """Restrict model-facing reads to this run's source and target refs.

    The request-scoped maps are populated by ``pipeline_mcp``.  Keeping the
    legacy unrestricted behaviour when neither map names a repository avoids
    changing standalone/demo callers, while an ordinary runtime request cannot
    inspect an earlier experiment or another ticket's generated branch.
    """

    configured = {
        value
        for value in (
            _SOURCE_BRANCH_MAP.get(repo_name),
            _TARGET_BRANCH_MAP.get(repo_name),
        )
        if value
    }
    if branch in configured:
        return None
    if not configured and not _ENFORCE_READ_BRANCH_SCOPE:
        return None
    if not configured:
        return (
            f"Repository '{repo_name}' has no configured source/target refs "
            "while readable-ref enforcement is enabled."
        )
    return (
        f"Branch '{branch}' is outside the readable refs configured for "
        f"repository '{repo_name}'. This run may read only its source and "
        "target branches."
    )


def _check_base_branch_scope(repo_name: str, base_branch: str) -> str | None:
    """Require new target branches to originate at the configured source ref."""

    configured_source = _SOURCE_BRANCH_MAP.get(repo_name)
    if base_branch == configured_source:
        return None
    if not configured_source and not _ENFORCE_READ_BRANCH_SCOPE:
        return None
    if not configured_source:
        return (
            f"Repository '{repo_name}' has no configured source branch while "
            "readable-ref enforcement is enabled."
        )
    return (
        f"Base branch '{base_branch}' is not the configured source branch "
        f"'{configured_source}' for repository '{repo_name}'."
    )


def _check_write_scope(repo_name: str, branch: str) -> str | None:
    """Validate a requested repo + branch for write/branch-creation
    operations. Branch must match the allowed pattern — this is what
    actually blocks writes to 'main'."""
    repo_error = _check_repo_scope(repo_name)
    if repo_error:
        return repo_error

    configured_branch = _TARGET_BRANCH_MAP.get(repo_name)
    if configured_branch and branch != configured_branch:
        return (
            f"Branch '{branch}' is not the configured target branch "
            f"'{configured_branch}' for repository '{repo_name}'."
        )

    if not fnmatch.fnmatch(branch, _ALLOWED_BRANCH_PATTERN):
        return (
            f"Branch '{branch}' does not match the allowed pattern "
            f"'{_ALLOWED_BRANCH_PATTERN}'. Direct writes to protected "
            f"branches like 'main' are not permitted."
        )
    return None

def create_mcp_server():
    """Create and configure the FastMCP server."""
    if FastMCP is None:
        raise RuntimeError(
            "FastMCP is not installed. Install it before running MCP server mode, "
            "for example: pip install fastmcp"
        ) from _FASTMCP_IMPORT_ERROR

    _require_e3_audit_sink()

    mcp = FastMCP("BSL Agentic Quant GitHub MCP")

    # ------------------------- PER-RUN CAPS -------------------------
    # Each MCPServerStdio spawn is one run (fresh process per async with
    # block), so these closure variables scope to exactly one run — same
    # semantics as before, without module-level mutable state (RAE-19).
    run_branch_count = 0
    run_commit_count = 0
    delivered_model_reads: set[tuple[str, str, str]] = set()
    max_branches_per_run = int(os.getenv("MAX_BRANCHES_PER_RUN", "3"))
    max_commits_per_run = int(os.getenv("MAX_COMMITS_PER_RUN", "5"))

    @_register_mcp_tool(mcp, "read_file")
    @_audited("read_file")
    def read_file(branch: str, path: str, repo_name: str = _DEFAULT_REPO_NAME) -> dict[str, Any]:
        """Read a file's contents from a given branch in the repo.

        Use this to fetch the current strategy code before modifying it.
        """
        scope_error = (
            _check_repo_scope(repo_name)
            or _check_read_scope(repo_name, branch)
            or _check_path_scope(repo_name, path)
        )
        if scope_error:
            print(f"[read_file] Blocked out-of-scope request: {scope_error}", file=sys.stderr)
            return {"status": "blocked", "error": scope_error}

        read_key = _read_key(repo_name, branch, path)
        if read_key in delivered_model_reads:
            return {
                "status": "already_read",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "notice": (
                    "This exact file was already returned during this run. Do not "
                    "repeat the read: use find_in_file to locate the needed symbol, "
                    "or replace_in_file for a focused edit to a large existing file."
                ),
            }

        try:
            raw = _get_strategy_code(branch=branch, path=path, repo_name=repo_name)
            # Model-facing read: an unreduced notebook or very large file would
            # exhaust the context. Validation and commit change-detection read
            # through _get_strategy_code directly and still see exact bytes.
            content, downsizing = downsize_for_model(path, raw)
            # Gate later writes on whether the model actually saw the whole file.
            _record_read_reduction(repo_name, branch, path, downsizing["kind"])
            delivered_model_reads.add(read_key)
            result = {
                "status": "success",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "content": content,
            }
            if downsizing["kind"] != "none":
                print(
                    f"[read_file] Reduced {repo_name}:{path} "
                    f"({downsizing['kind']}): {downsizing['original_chars']} -> "
                    f"{downsizing['returned_chars']} chars",
                    file=sys.stderr,
                )
                result["downsized"] = downsizing
                result["notice"] = downsizing["notice"]
            return result
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    @_register_mcp_tool(mcp, "find_in_file")
    @_audited("find_in_file")
    def find_in_file(
        branch: str,
        path: str,
        query: str,
        repo_name: str = _DEFAULT_REPO_NAME,
        context_lines: int = 8,
        max_matches: int = 4,
    ) -> dict[str, Any]:
        """Find a literal symbol or phrase in an in-scope text file.

        Returns small, line-numbered windows instead of the full file. Use this
        first when a named source file is large or a normal read was truncated.
        """
        scope_error = (
            _check_repo_scope(repo_name)
            or _check_read_scope(repo_name, branch)
            or _check_path_scope(repo_name, path)
        )
        if scope_error:
            return {"status": "blocked", "error": scope_error}
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY_CHARS:
            return {
                "status": "failed",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "error": f"query must be a non-empty string of at most {MAX_SEARCH_QUERY_CHARS} characters",
            }
        if not isinstance(context_lines, int) or not 0 <= context_lines <= MAX_SEARCH_CONTEXT_LINES:
            return {"status": "failed", "error": f"context_lines must be 0-{MAX_SEARCH_CONTEXT_LINES}"}
        if not isinstance(max_matches, int) or not 1 <= max_matches <= MAX_SEARCH_MATCHES:
            return {"status": "failed", "error": f"max_matches must be 1-{MAX_SEARCH_MATCHES}"}
        try:
            raw = _get_strategy_code(branch=branch, path=path, repo_name=repo_name)
            matches = _matching_line_windows(
                raw,
                query.strip(),
                context_lines=context_lines,
                max_matches=max_matches,
            )
            return {
                "status": "success",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "query": query.strip(),
                "match_count": len(matches),
                "matches": matches,
            }
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    @_register_mcp_tool(mcp, "read_files")
    @_audited("read_files")
    def read_files(
        branch: str,
        paths: list[str],
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """Read up to eight complete, in-scope text files in one repository turn.

        The request is atomic from a scope perspective: an invalid or out-of-scope
        path blocks the whole call. Large files are reported without content rather
        than truncated, so generated documentation never treats a partial file as
        verified evidence.
        """
        if not isinstance(paths, list) or not paths or len(paths) > MAX_BATCH_READ_FILES:
            return {
                "status": "failed",
                "repo_name": repo_name,
                "branch": branch,
                "error": f"paths must contain 1-{MAX_BATCH_READ_FILES} entries",
            }
        repo_error = _check_repo_scope(repo_name) or _check_read_scope(
            repo_name, branch
        )
        if repo_error:
            return {"status": "blocked", "repo_name": repo_name, "branch": branch, "error": repo_error}
        normalised_paths: list[str] = []
        for path in paths:
            scope_error = _check_path_scope(repo_name, path)
            if scope_error:
                return {
                    "status": "blocked",
                    "repo_name": repo_name,
                    "branch": branch,
                    "paths": paths,
                    "error": scope_error,
                }
            normalised = _normalise_repo_path(path)
            if normalised and normalised not in normalised_paths:
                normalised_paths.append(normalised)

        used_bytes = 0
        files = []
        for path in normalised_paths:
            try:
                content = _get_strategy_code(branch=branch, path=path, repo_name=repo_name)
            except Exception as exc:
                files.append({"path": path, "status": "failed", "error": f"{type(exc).__name__}: {exc}"[:500]})
                continue
            size = len(content.encode("utf-8"))
            if size > MAX_BATCH_READ_BYTES:
                files.append({"path": path, "status": "too_large", "size_bytes": size})
                continue
            if used_bytes + size > MAX_BATCH_READ_BYTES:
                files.append(
                    {"path": path, "status": "batch_limit_reached", "size_bytes": size}
                )
                continue
            used_bytes += size
            files.append({"path": path, "status": "success", "size_bytes": size, "content": content})
        status = "success" if all(item["status"] == "success" for item in files) else "partial"
        return {
            "status": status,
            "repo_name": repo_name,
            "branch": branch,
            "paths": normalised_paths,
            "total_bytes": used_bytes,
            "files": files,
        }

    @_register_mcp_tool(mcp, "create_or_reuse_branch")
    @_audited("create_or_reuse_branch")
    def create_or_reuse_branch(
        branch_name: str,
        base_branch: str = "main",
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """Create a feature branch off a base branch. If the branch already
        exists, treat that as success rather than an error (idempotent).

        branch_name must match the allowed branch pattern (e.g. quant/<ticket>).
        """
        scope_error = _check_write_scope(
            repo_name, branch_name
        ) or _check_base_branch_scope(repo_name, base_branch)
        if scope_error:
            print(f"[create_or_reuse_branch] Blocked out-of-scope request: {scope_error}", file=sys.stderr)
            return {"status": "blocked", "error": scope_error}

        nonlocal run_branch_count
        if run_branch_count >= max_branches_per_run:
            print(
                f"[create_or_reuse_branch] Per-run branch cap ({max_branches_per_run}) reached.",
                file=sys.stderr,
            )
            return {
                "status": "cap_exceeded",
                "error": (
                    f"Per-run branch cap of {max_branches_per_run} reached. "
                    "Do not attempt to create more branches in this run."
                ),
            }
        run_branch_count += 1

        try:
            result = _create_feature_branch(
                branch_name=branch_name,
                base_branch=base_branch,
                repo_name=repo_name,
            )
            status = "reused" if "reused" in str(result).lower() else "created"
            return {"status": status, "repo_name": repo_name, "branch": branch_name, "message": result}

        except RateLimitExceededException as e:
            print(f"[create_or_reuse_branch] GitHub rate limit hit: {e}", file=sys.stderr)
            return {
                "status": "rate_limited",
                "error": "GitHub API rate limit exceeded. Wait before retrying.",
            }

        except GithubException as e:
            if "already exists" in str(e).lower():
                print(f"[create_or_reuse_branch] Branch '{branch_name}' already exists, reusing.", file=sys.stderr)
                return {"status": "reused", "repo_name": repo_name, "branch": branch_name, "message": "Branch already exists."}
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    @_register_mcp_tool(mcp, "list_files")
    @_audited("list_files")
    def list_files(
        branch: str,
        directory: str = "",
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """List files in a directory on a given branch. Pass an empty
        directory to list the repo root. Use this to discover what files
        exist before reading or modifying them.
        """
        scope_error = (
            _check_repo_scope(repo_name)
            or _check_read_scope(repo_name, branch)
            or _check_list_scope(repo_name, directory)
        )
        if scope_error:
            print(f"[list_files] Blocked out-of-scope request: {scope_error}", file=sys.stderr)
            return {"status": "blocked", "error": scope_error}

        try:
            files = _list_files(branch=branch, directory=directory, repo_name=repo_name)
            return {"status": "success", "repo_name": repo_name, "branch": branch, "directory": directory, "files": files}
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    @_register_mcp_tool(mcp, "validate_content")
    @_audited("validate_content")
    def validate_content(
        branch: str,
        path: str,
        content: str,
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """Validate complete proposed file content without committing it.

        Repair every reported issue and call this tool again. Only call
        commit_and_push after this tool returns success.
        """
        scope_error = _check_write_scope(repo_name, branch) or _check_path_scope(repo_name, path)
        if scope_error:
            print(f"[validate_content] Blocked out-of-scope request: {scope_error}", file=sys.stderr)
            return {"status": "blocked", "error": scope_error}

        try:
            issues = _validate_proposed_content(
                branch=branch,
                path=path,
                content=content,
                repo_name=repo_name,
            )
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

        if issues:
            return {
                "status": "validation_failed",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "issues": issues,
                "error": "Proposed content failed validation: " + " ".join(issues),
            }
        return {
            "status": "success",
            "repo_name": repo_name,
            "branch": branch,
            "path": path,
            "message": "Proposed content passed deterministic validation.",
        }

    @_register_mcp_tool(mcp, "replace_in_file")
    @_audited("replace_in_file")
    def replace_in_file(
        branch: str,
        path: str,
        old_text: str,
        new_text: str,
        commit_message: str,
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """Atomically replace one exact small snippet in an existing text file.

        The server reads the complete current file, requires ``old_text`` to occur
        exactly once, validates the reconstructed complete file, and commits that
        reconstructed file. This is for focused edits to a file that was too large
        to send to the model in full. It never accepts notebooks and never writes
        a model-produced partial file.
        """
        scope_error = _check_write_scope(repo_name, branch) or _check_path_scope(repo_name, path)
        if scope_error:
            return {"status": "blocked", "error": scope_error}
        if is_notebook(path):
            return {
                "status": "blocked",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "error": "replace_in_file does not support notebooks; use a source file instead.",
            }
        if not isinstance(old_text, str) or not old_text:
            return {"status": "failed", "error": "old_text must be a non-empty exact text snippet"}
        if not isinstance(new_text, str):
            return {"status": "failed", "error": "new_text must be a string"}
        if not isinstance(commit_message, str) or not commit_message.strip():
            return {"status": "failed", "error": "commit_message must be a non-empty string"}
        if len(old_text) > MAX_SURGICAL_REPLACEMENT_CHARS or len(new_text) > MAX_SURGICAL_REPLACEMENT_CHARS:
            return {
                "status": "failed",
                "error": (
                    "old_text and new_text must each be at most "
                    f"{MAX_SURGICAL_REPLACEMENT_CHARS} characters"
                ),
            }

        try:
            raw = _get_strategy_code(branch=branch, path=path, repo_name=repo_name)
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

        occurrence_count = raw.count(old_text)
        if occurrence_count != 1:
            return {
                "status": "validation_failed",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "issues": [
                    "The exact old_text snippet must occur exactly once in the current file "
                    f"(found {occurrence_count}). Re-read a narrower, unique context before retrying."
                ],
                "error": "Exact replacement could not be applied safely.",
            }

        proposed = raw.replace(old_text, new_text, 1)
        try:
            validation_issues = _validate_proposed_content(
                branch=branch,
                path=path,
                content=proposed,
                repo_name=repo_name,
                # This tool preserves every byte outside the exact replacement,
                # so a prior truncated model view cannot cause tail loss.
                allow_truncated_source=True,
            )
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        if validation_issues:
            return {
                "status": "validation_failed",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "issues": validation_issues,
                "error": "Server-built replacement failed deterministic validation: " + " ".join(validation_issues),
            }

        nonlocal run_commit_count
        if run_commit_count >= max_commits_per_run:
            return {
                "status": "cap_exceeded",
                "error": f"Per-run commit cap of {max_commits_per_run} reached.",
            }
        run_commit_count += 1
        try:
            result = _push_strategy_code(
                code=proposed,
                branch=branch,
                path=path,
                commit_message=commit_message.strip(),
                repo_name=repo_name,
            )
            if "no changes" in str(result).lower():
                return {
                    "status": "no_changes",
                    "repo_name": repo_name,
                    "branch": branch,
                    "path": path,
                    "message": result,
                }
            return {
                "status": "success",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "message": "Applied one exact server-side replacement and committed it.",
            }
        except NoChangesError as e:
            return {"status": "no_changes", "error": str(e)}
        except RateLimitExceededException as e:
            print(f"[replace_in_file] GitHub rate limit hit: {e}", file=sys.stderr)
            return {"status": "rate_limited", "error": "GitHub API rate limit exceeded. Wait before retrying."}
        except GithubException as e:
            if e.status in (409, 422):
                return {
                    "status": "conflict",
                    "error": f"Push rejected for '{branch}'; re-read the current snippet before retrying.",
                }
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    @_register_mcp_tool(mcp, "commit_and_push")
    @_audited("commit_and_push")
    def commit_and_push(
        branch: str,
        path: str,
        content: str,
        commit_message: str,
        repo_name: str = _DEFAULT_REPO_NAME,
    ) -> dict[str, Any]:
        """Write file content to a branch and commit it. The branch must
        already exist, and must match the allowed branch pattern.
        """
        scope_error = _check_write_scope(repo_name, branch) or _check_path_scope(repo_name, path)
        if scope_error:
            print(f"[commit_and_push] Blocked out-of-scope request: {scope_error}", file=sys.stderr)
            return {"status": "blocked", "error": scope_error}

        try:
            validation_issues = _validate_proposed_content(
                branch=branch,
                path=path,
                content=content,
                repo_name=repo_name,
            )
        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        if validation_issues:
            return {
                "status": "validation_failed",
                "repo_name": repo_name,
                "branch": branch,
                "path": path,
                "issues": validation_issues,
                "error": (
                    "Commit blocked because proposed content failed validation: "
                    + " ".join(validation_issues)
                ),
            }

        nonlocal run_commit_count
        if run_commit_count >= max_commits_per_run:
            print(
                f"[commit_and_push] Per-run commit cap ({max_commits_per_run}) reached.",
                file=sys.stderr,
            )
            return {
                "status": "cap_exceeded",
                "error": (
                    f"Per-run commit cap of {max_commits_per_run} reached. "
                    "Do not attempt further commits in this run."
                ),
            }
        run_commit_count += 1

        try:
            result = _push_strategy_code(
                code=content,
                branch=branch,
                path=path,
                commit_message=commit_message,
                repo_name=repo_name,
            )
            if "no changes" in str(result).lower():
                return {
                    "status": "no_changes",
                    "repo_name": repo_name,
                    "branch": branch,
                    "path": path,
                    "message": result,
                }
            return {"status": "success", "repo_name": repo_name, "branch": branch, "path": path, "message": result}

        except NoChangesError as e:
            return {"status": "no_changes", "error": str(e)}

        except RateLimitExceededException as e:
            print(f"[commit_and_push] GitHub rate limit hit: {e}", file=sys.stderr)
            return {
                "status": "rate_limited",
                "error": "GitHub API rate limit exceeded. Wait before retrying — do not call this tool again immediately.",
            }

        except GithubException as e:
            if e.status in (409, 422):
                print(f"[commit_and_push] Push conflict on {branch}: {e}", file=sys.stderr)
                return {
                    "status": "conflict",
                    "error": f"Push rejected, likely a remote conflict on '{branch}'. "
                             "Read the current file content again before retrying, rather than overwriting blindly.",
                }
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

        except Exception as e:
            return {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    return mcp


def run_click_demo() -> dict[str, Any]:
    """Quick local sanity check — does not start the long-running server."""
    branch_result = _create_feature_branch(branch_name="quant/MCP-DEMO", base_branch="main")
    read_result = _get_strategy_code(branch="main", path="rae_runtime/proxy/strategy.py")

    return {
        "mode": "click_run_demo",
        "branch_result": branch_result,
        "read_result_preview": read_result[:100],
        "allowed_repos": sorted(_ALLOWED_REPOS),
        "allowed_branch_pattern": _ALLOWED_BRANCH_PATTERN,
        "note": "Use `python github_mcp_server.py --serve` to start the long-running FastMCP server.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GitHub MCP server. Default mode runs a local demo once and exits."
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the real FastMCP server. This will keep running and wait for MCP calls.",
    )
    args = parser.parse_args()

    if args.serve:
        (mcp if mcp is not None else create_mcp_server()).run()
        return

    result = run_click_demo()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if FastMCP is not None:
    mcp = create_mcp_server()
else:
    mcp = None


if __name__ == "__main__":
    main()
