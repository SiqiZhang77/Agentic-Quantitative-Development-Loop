import asyncio
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from agents import Agent, Runner, set_tracing_disabled
from agents.mcp import MCPServerStdio
from provider_config import (
    build_agents_model,
    build_agents_model_settings,
    resolve_provider_config,
)
from github_client import (
    create_or_reuse_branch_details,
    get_branch_sha,
    get_strategy_code,
)
from artifact_validation import inspect_committed_artifacts
from prompt_context import build_mcp_code_prompt, mark_retrieval_prompt_delivery
from resource_discovery import build_file_map_block, repository_specs_from_payload

load_dotenv()
set_tracing_disabled(disabled=True)

GITHUB_MCP_SERVER_PATH = os.getenv(
    "GITHUB_MCP_SERVER_PATH", "/app/proxy/github_mcp_server.py"
)
DEFAULT_REPO_NAME = "bankingscience/BSLAgenticQuantDevLoop"
_WRITE_TOOLS = frozenset({"commit_and_push", "replace_in_file"})


class NoCodeChanges(RuntimeError):
    """Raised when the MCP run completes but leaves the target file unchanged."""


class MCPWritebackError(RuntimeError):
    """Raised when MCP tools did not complete the expected GitHub write."""


class InvalidStrategyRequest(RuntimeError):
    """Raised when an edited .request changes its parameter contract."""


class AgentTurnLimitExceeded(RuntimeError):
    """A retryable, sanitized terminal failure from the Agents SDK turn cap."""

    def __init__(self, *, turn_limit: int, turns_consumed: int, audit: list[dict]):
        self.turn_limit = turn_limit
        self.turns_consumed = turns_consumed
        self.audit = audit
        self.pipeline_out = {
            "status": "failed",
            "diagnostics": {
                "retry_safe": {
                    "agent_turn_limit": turn_limit,
                    "agent_turns_consumed": turns_consumed,
                    "tool_calls": audit,
                    "validation_reached": any(item.get("tool") == "validate_content" for item in audit),
                    "commit_reached": any(item.get("tool") in _WRITE_TOOLS for item in audit),
                }
            },
        }
        super().__init__(
            f"Agent turn limit ({turn_limit}) exceeded after {turns_consumed} turns; "
            "retry may proceed only while the configured failure, timeout, and token budgets remain."
        )


def _resolve_max_agent_turns(payload: dict, repository_count: int) -> int:
    """Return a bounded per-pass turn allowance without widening overall budgets."""
    raw = (payload.get("iteration_controls") or {}).get("max_agent_turns")
    if raw is not None:
        try:
            return max(10, min(60, int(raw)))
        except (TypeError, ValueError):
            pass
    strategy = payload.get("strategy") or {}
    source_path = strategy.get("source_path") or strategy.get("path")
    target_path = strategy.get("target_path") or source_path
    complex_task = not source_path or source_path != target_path
    base = 30 if complex_task else 12
    return min(60, base + max(0, repository_count - 1) * 5)


def _apply_github_mcp_request_limits(server_env: dict[str, str], payload: dict) -> None:
    """Forward explicit per-ticket limits without replacing operator defaults."""
    raw = (payload.get("iteration_controls") or {}).get("max_commits_per_run")
    if raw is not None:
        # Gateway and runtime-schema validation guarantee the 1-50 bound. Keep
        # the MCP server's existing environment-based interface at this boundary.
        server_env["MAX_COMMITS_PER_RUN"] = str(int(raw))


# Mirrors iteration_loop._iteration_allowed / _resolve_max_iterations in the
# sandbox. The dependency runs sandbox -> proxy only (run.py puts proxy/ on the
# path; proxy/tests/conftest.py does not add sandbox/), so this layer cannot
# import them and the resolution order is duplicated on purpose. Both copies must
# change together: if this one over-estimates the passes the loop will really run,
# the reused-target short-circuit below returns no_changes for work that then
# never gets a repair pass.
_DEFAULT_MAX_ITERATIONS = 3


def _iteration_allowed(payload: dict) -> bool:
    """iteration_controls.allow_iteration -> RAE_ALLOW_ITERATION env -> per-type default."""
    raw = (payload.get("iteration_controls") or {}).get("allow_iteration")
    if raw is None:
        raw = os.environ.get("RAE_ALLOW_ITERATION")
    if raw is None:
        return str(payload.get("command") or "").strip().lower() == "backtest"
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"false", "no", "n", "off", "0"}


def _resolve_max_iterations(payload: dict) -> int:
    """iteration_controls -> execution_objectives -> RAE_MAX_ITERATIONS -> default 3."""
    if not _iteration_allowed(payload):
        return 1
    raw = (payload.get("iteration_controls") or {}).get("max_iterations")
    if raw is None:
        raw = (payload.get("execution_objectives") or {}).get("max_iterations")
    if raw is None:
        raw = os.environ.get("RAE_MAX_ITERATIONS")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = _DEFAULT_MAX_ITERATIONS
    return max(1, n)


def _reused_target_noop_can_repair(payload: dict) -> bool:
    """Whether a rejected reused target has a guaranteed later edit pass."""
    return _resolve_max_iterations(payload) >= 2


def _read_tool_audit(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if not isinstance(item, dict):
                continue
            records.append(
                {
                    key: item.get(key)
                    for key in ("tool", "status", "repo_name", "branch", "path", "directory", "paths", "error")
                    if item.get(key) not in (None, "", [])
                }
            )
    except (OSError, json.JSONDecodeError):
        pass
    return records[-100:]


def _truncate(value, limit: int = 500) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _extract_commit_diagnostics(result, branch_name: str) -> dict:
    """Extraction of real branch/commit state from the MCP tool
    call outputs in `result.new_items`, instead of assuming every run
    committed a real change.

    github_mcp_server.py's write/create_or_reuse_branch tools wrap
    github_client.py, which already returns idempotency-aware messages
    (e.g. "No changes ... skipped commit", "reused"). We just need to read
    those back out of the agent run.

    Verified attribute names against the installed SDK version via diagnostic
    script (diagnose_runresult.py):
    - Tool name lives on ToolCallItem.tool_name, not on ToolCallOutputItem
    - ToolCallItem and ToolCallOutputItem are matched via call_id
    - ToolCallOutputItem.output is {'type': 'text', 'text': '<JSON string>'}
      so the actual tool result needs one json.loads() on output['text']
    """
    branch_action = "unknown"
    branch_message = "No create_or_reuse_branch tool output captured."
    commit_action = "unknown"
    commit_message = "No GitHub write-tool output captured."
    commit_changed = False
    tool_calls = []
    parse_errors = []
    # Paths whose write tool actually wrote a change. The status comes from
    # github_client's own push, which reports "no changes" when the content is
    # identical, so this is a real per-file record of what this pass changed — not
    # the agent's claim about it, and not confused by edits a previous iteration
    # already made to the branch.
    committed_paths: list[str] = []
    committed_artifacts: list[dict] = []
    commit_failures: list[dict] = []
    validation_failures: list[dict] = []

    # Build call_id -> tool metadata from ToolCallItem entries.
    call_id_to_tool = {}
    for item in getattr(result, "new_items", None) or []:
        if type(item).__name__ != "ToolCallItem":
            continue
        call_id = getattr(item, "call_id", None)
        tool_name = getattr(item, "tool_name", None)
        if call_id and tool_name:
            call_id_to_tool[call_id] = tool_name

    # Read outputs from ToolCallOutputItem entries, matched by call_id.
    for item in getattr(result, "new_items", None) or []:
        if type(item).__name__ != "ToolCallOutputItem":
            continue
        call_id = getattr(item, "call_id", None)
        tool_name = call_id_to_tool.get(call_id) or "unknown"
        raw = getattr(item, "output", None)
        text = raw.get("text") if isinstance(raw, dict) else None
        output = None
        if text:
            try:
                output = json.loads(text)
            except (json.JSONDecodeError, TypeError) as e:
                parse_errors.append(
                    {
                        "tool": tool_name,
                        "call_id": call_id,
                        "error": f"{type(e).__name__}: {e}",
                        "raw_output": _truncate(text),
                    }
                )
        if not isinstance(output, dict):
            output = {}

        status = str(output.get("status") or "unknown")
        message = output.get("message") or output.get("error") or ""
        tool_calls.append(
            {
                "tool": tool_name,
                "call_id": call_id,
                "status": status,
                "message": _truncate(message),
                **({"repo_name": output.get("repo_name")} if output.get("repo_name") else {}),
                **({"branch": output.get("branch")} if output.get("branch") else {}),
                **({"path": output.get("path")} if output.get("path") else {}),
                **(
                    {"paths": list(output.get("paths"))}
                    if isinstance(output.get("paths"), list)
                    and output.get("paths")
                    else {}
                ),
            }
        )

        if tool_name == "create_or_reuse_branch":
            if status == "reused":
                branch_action = "reused"
            elif status in {"created", "success"}:
                branch_action = "created"
            else:
                branch_action = status
            branch_message = _truncate(message or branch_message)

        elif tool_name == "validate_content":
            path = output.get("path")
            repo_name = output.get("repo_name")
            if status == "validation_failed":
                validation_failures.append(
                    {
                        "call_id": call_id,
                        "status": status,
                        "message": _truncate(message),
                        "path": path,
                        "repo_name": repo_name,
                        "branch": output.get("branch"),
                        "issues": output.get("issues") or [],
                    }
                )
            elif status == "success":
                validation_failures = [
                    failure
                    for failure in validation_failures
                    if not (
                        failure.get("path") == path
                        and failure.get("repo_name") == repo_name
                    )
                ]

        elif tool_name in _WRITE_TOOLS:
            low = str(message).lower()
            commit_message = _truncate(message or commit_message)
            if status == "no_changes" or "no changes" in low or "skipped" in low:
                commit_action = "skipped"
                commit_changed = False
            elif status == "success":
                commit_action = "committed"
                commit_changed = True
                path = output.get("path")
                if isinstance(path, str) and path and path not in committed_paths:
                    committed_paths.append(path)
                artifact = {
                    "repo_name": output.get("repo_name"),
                    "branch": output.get("branch"),
                    "path": path,
                }
                if (
                    artifact["repo_name"]
                    and artifact["branch"]
                    and artifact["path"]
                    and artifact not in committed_artifacts
                ):
                    committed_artifacts.append(artifact)
                # A successful retry for the same path resolves an earlier
                # deterministic rejection from the pre-commit gate.
                commit_failures = [
                    failure
                    for failure in commit_failures
                    if not (
                        failure.get("status") == "validation_failed"
                        and failure.get("path") == path
                        and failure.get("repo_name") == output.get("repo_name")
                    )
                ]
            else:
                commit_action = "failed"
                commit_changed = False
                commit_failures.append(
                    {
                        "call_id": call_id,
                        "status": status,
                        "message": _truncate(message),
                        "path": output.get("path"),
                        "repo_name": output.get("repo_name"),
                        "branch": output.get("branch"),
                        "issues": output.get("issues") or [],
                    }
                )

    # A multi-file run's verdict is the aggregate, not merely the last call's.
    # A trailing no-op must not erase a real commit, but a blocked/failed write must
    # not be hidden by an earlier successful file either: that is a partial change,
    # not a successful implementation of the ticket.
    if commit_failures:
        commit_action = "failed"
        commit_changed = bool(committed_paths)
        commit_message = commit_failures[-1]["message"] or commit_message
    elif committed_paths:
        commit_action = "committed"
        commit_changed = True

    final_output = _truncate(getattr(result, "final_output", ""), 500)
    if commit_action == "unknown" and final_output:
        commit_message = final_output

    diagnostics = {
        "branch": {
            "name": branch_name,
            "action": branch_action,
            "message": branch_message,
        },
        "commit": {
            "action": commit_action,
            "message": commit_message,
            "changed": commit_changed,
            "paths": committed_paths,
            "artifacts": committed_artifacts,
            "failures": commit_failures,
        },
        "validation": {"failures": validation_failures},
        "tool_calls": tool_calls,
    }
    if parse_errors:
        diagnostics["tool_parse_errors"] = parse_errors
    if final_output:
        diagnostics["agent_final_output"] = final_output
    return diagnostics


def _raise_if_mcp_write_failed(retry_safe: dict) -> None:
    commit = retry_safe.get("commit") or {}
    action = commit.get("action")
    if action in {"committed", "skipped"}:
        return

    tool_calls = retry_safe.get("tool_calls") or []
    if not tool_calls:
        raise MCPWritebackError(
            f"MCP agent did not call GitHub tools. Final output: "
            f"{retry_safe.get('agent_final_output', '')}"
        )

    commit_calls = [c for c in tool_calls if c.get("tool") in _WRITE_TOOLS]
    if not commit_calls:
        raise MCPWritebackError(
            f"MCP agent did not call a GitHub write tool. Tool calls: {tool_calls}"
        )

    failures = commit.get("failures") or []
    detail = failures[-1] if failures else commit_calls[-1]
    raise MCPWritebackError(f"MCP write tool failed: {detail}")


def _missing_commit_call(retry_safe: dict) -> bool:
    return not any(
        call.get("tool") in _WRITE_TOOLS
        for call in retry_safe.get("tool_calls") or []
    )


def _precommit_validation_issues(retry_safe: dict) -> list[str]:
    """Return unresolved deterministic commit rejections for outer-loop repair."""

    issues: list[str] = []
    failures = list((retry_safe.get("validation") or {}).get("failures") or [])
    failures.extend((retry_safe.get("commit") or {}).get("failures") or [])
    for failure in failures:
        if failure.get("status") != "validation_failed":
            continue
        reported = failure.get("issues") or []
        if reported:
            issues.extend(str(issue) for issue in reported)
        elif failure.get("message"):
            issues.append(str(failure["message"]))
    return sorted(set(issues))


def _has_nonvalidation_commit_failure(retry_safe: dict) -> bool:
    return any(
        failure.get("status") != "validation_failed"
        for failure in (retry_safe.get("commit") or {}).get("failures") or []
    )


def _repository_discovery_issues(
    *,
    source_path: str | None,
    target_path: str | None,
    committed_artifacts: list[dict],
    tool_calls: list[dict],
) -> list[str]:
    """Verify that generated artifacts were grounded in declared source data.

    ``read_files`` was added after the original guard and is the preferred tool
    for multiple public inputs. Count only successful calls and, for an explicit
    source-to-target task, prove that the declared source itself was read. This
    admits a legitimate one-input task without accepting unrelated reads.
    """

    successful = [
        call
        for call in tool_calls
        if isinstance(call, dict) and call.get("status") == "success"
    ]
    if source_path and source_path != target_path:
        listed_repository = any(
            call.get("tool") == "list_files" for call in successful
        )
        read_declared_source = any(
            (
                call.get("tool") == "read_file"
                and call.get("path") == source_path
            )
            or (
                call.get("tool") == "read_files"
                and source_path in (call.get("paths") or [])
            )
            for call in successful
        )
        if not listed_repository or not read_declared_source:
            return [
                "The source and target paths differ, but the run did not "
                "demonstrate repository discovery with list_files and a "
                "successful read of the declared source_path."
            ]
    elif not source_path and committed_artifacts:
        read_repository_content = any(
            call.get("tool") in {"read_file", "read_files"}
            for call in successful
        )
        if not read_repository_content:
            return [
                "The run committed content without reading any repository file "
                "first, so the change was not grounded in repository evidence."
            ]
    return []


def _read_ref(strategy_ref: str, branch_name: str, branch_has_file: bool) -> str:
    """Which ref the agent should read the current strategy from: the ticket branch
    once it already holds prior tuning (re-iteration), else the source ref for the
    first iteration. Keeps successive iterations building on each other instead of
    overwriting from main."""
    return branch_name if branch_has_file else strategy_ref


def _read_strategy_or_none(branch: str, path: str, repo_name: str) -> str | None:
    try:
        return get_strategy_code(branch=branch, path=path, repo_name=repo_name)
    except Exception as e:
        if getattr(e, "status", None) == 404 or "404" in str(e):
            return None
        if "not found" in str(e).lower():
            return None
        raise RuntimeError(
            f"Could not read existing {path} on {branch} before MCP run: {e}"
        ) from e


def _request_parameters(text: str, path: str) -> list[tuple[str, str]]:
    """Parse ordered ``KEY = value`` parameters and reject malformed/duplicate keys."""
    parameters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in raw_line:
            raise InvalidStrategyRequest(
                f"{path}:{line_number} is not a KEY = value parameter line"
            )
        raw_key, raw_value = raw_line.split("=", 1)
        key = raw_key.strip()
        if not key or any(character.isspace() for character in key):
            raise InvalidStrategyRequest(
                f"{path}:{line_number} has an invalid parameter key {key!r}"
            )
        if key in seen:
            raise InvalidStrategyRequest(
                f"{path}:{line_number} duplicates parameter {key!r}"
            )
        seen.add(key)
        parameters.append((key, raw_value.strip()))
    return parameters


def _validate_request_value_only_change(before: str, after: str, path: str) -> None:
    """Require the agent to change values without altering the .request key contract."""
    before_parameters = _request_parameters(before, path)
    after_parameters = _request_parameters(after, path)
    before_keys = [key for key, _ in before_parameters]
    after_keys = [key for key, _ in after_parameters]
    if after_keys != before_keys:
        added = sorted(set(after_keys) - set(before_keys))
        removed = sorted(set(before_keys) - set(after_keys))
        detail = []
        if added:
            detail.append(f"added keys: {added}")
        if removed:
            detail.append(f"removed keys: {removed}")
        if not detail:
            detail.append("parameter keys were reordered")
        raise InvalidStrategyRequest(
            f"{path} must preserve the existing ordered parameter keys; " + "; ".join(detail)
        )
    if [value for _, value in after_parameters] == [
        value for _, value in before_parameters
    ]:
        raise NoCodeChanges(
            f"MCP pipeline changed {path} without changing an engine parameter value"
        )


def _validate_branch_file_changed(
    *,
    branch_name: str,
    strategy_ref: str,
    strategy_path: str,
    repo_name: str = DEFAULT_REPO_NAME,
    branch_code_before: str | None,
) -> None:
    read_kwargs = {} if repo_name == DEFAULT_REPO_NAME else {"repo_name": repo_name}
    source_code = get_strategy_code(
        branch=strategy_ref, path=strategy_path, **read_kwargs
    )
    branch_code_after = get_strategy_code(
        branch=branch_name, path=strategy_path, **read_kwargs
    )
    starting_code = branch_code_before if branch_code_before is not None else source_code

    if branch_code_after == starting_code:
        raise NoCodeChanges(
            f"MCP pipeline did not change {strategy_path} on {branch_name}"
        )
    if str(strategy_path).endswith(".request"):
        _validate_request_value_only_change(starting_code, branch_code_after, strategy_path)


def _validate_run_changed_something(
    *,
    payload: dict,
    branch_name: str,
    strategy_ref: str,
    strategy_path: str,
    repo_name: str = DEFAULT_REPO_NAME,
    branch_code_before: str | None,
    committed_paths: list[str],
    committed_artifacts: list[dict] | None = None,
) -> None:
    """Check this pass actually changed the repository, and changed the right thing.

    A backtest must have changed its own target: the engine runs that exact
    .request, so committing something else and backtesting an untouched baseline
    would report metrics for a strategy nobody asked for. Its target is therefore
    verified against GitHub as before.

    A general request has no such file — a refactor may legitimately need to touch
    files the ticket never named, and may leave the named one alone — so it only
    has to have changed at least one file in scope.
    """
    if str(payload.get("command") or "").strip().lower() == "backtest":
        _validate_branch_file_changed(
            branch_name=branch_name,
            strategy_ref=strategy_ref,
            strategy_path=strategy_path,
            repo_name=repo_name,
            branch_code_before=branch_code_before,
        )
        return

    if not committed_paths and not committed_artifacts:
        raise NoCodeChanges(
            f"MCP pipeline committed no file changes on {branch_name}"
        )


def _normalise_committed_artifacts(
    retry_safe: dict,
    *,
    primary_repo_name: str,
    primary_branch: str,
) -> list[dict]:
    """Return repository-qualified commit records, including legacy tool output."""

    commit = retry_safe.get("commit") or {}
    artifacts = []
    for item in commit.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        repo_name = item.get("repo_name") or primary_repo_name
        branch = item.get("branch") or primary_branch
        path = item.get("path")
        record = {"repo_name": repo_name, "branch": branch, "path": path}
        if path and record not in artifacts:
            artifacts.append(record)
    if not artifacts:
        for path in commit.get("paths") or []:
            if path:
                artifacts.append(
                    {
                        "repo_name": primary_repo_name,
                        "branch": primary_branch,
                        "path": path,
                    }
                )
    return artifacts


def _inspect_repository_artifacts(
    *,
    repository_branches: list[dict],
    committed_artifacts: list[dict],
    primary_key: tuple[str, str],
    required_target_path: str | None,
    reader=get_strategy_code,
) -> tuple[dict[tuple[str, str], dict[str, list[str]]], list[str]]:
    """Validate and classify each commit against its own repository source."""

    branch_lookup = {
        (item.get("repo_full_name"), item.get("target_branch")): item
        for item in repository_branches
    }
    paths_by_branch: dict[tuple[str, str], list[str]] = {}
    issues: list[str] = []
    for artifact in committed_artifacts:
        key = (artifact.get("repo_name"), artifact.get("branch"))
        path = artifact.get("path")
        if key not in branch_lookup:
            issues.append(
                f"Committed artifact {path} used an unconfigured repository or branch "
                f"{key[0]}@{key[1]}."
            )
            continue
        paths_by_branch.setdefault(key, []).append(path)

    if required_target_path and primary_key not in paths_by_branch:
        paths_by_branch[primary_key] = []

    files_by_branch: dict[tuple[str, str], dict[str, list[str]]] = {}
    for key, paths in paths_by_branch.items():
        branch_record = branch_lookup[key]
        modified_files, new_files, current_issues = inspect_committed_artifacts(
            committed_paths=paths,
            source_branch=branch_record.get("source_branch") or "main",
            target_branch=key[1],
            repo_name=key[0],
            reader=reader,
            required_target_path=(
                required_target_path if key == primary_key else None
            ),
        )
        files_by_branch[key] = {
            "modified_files": modified_files,
            "new_files": new_files,
        }
        issues.extend(f"{key[0]}@{key[1]}: {issue}" for issue in current_issues)

    return files_by_branch, issues


def _no_changes_result(
    *,
    payload: dict,
    ticket_id: str,
    branch_name: str,
    strategy_path: str,
    summary: str,
    model_name: str,
    provider_mode: str,
    sdk_usage,
    retry_safe: dict,
    repository_branches: list[dict] | None = None,
    repo_name: str | None = None,
    existing_files: list[str] | None = None,
) -> dict:
    marked_branches = _mark_primary_repository_files(
        repository_branches or [],
        repo_name or "",
        branch_name,
        [],
        [],
        refresh_commit_sha=True,
    ) if repository_branches is not None else []
    if existing_files:
        for record in marked_branches:
            if record.get("repo_full_name") == repo_name and record.get("target_branch") == branch_name:
                record["existing_files"] = sorted(set(existing_files))
    return {
        "status": "no_changes",
        "issue_key": ticket_id,
        "summary": summary,
        "artifacts": {
            "feature_branch": branch_name,
            "modified_files": [],
            "new_files": [],
            "no_code_changes": True,
            **(
                {
                    "repository_branches": marked_branches
                }
                if repository_branches is not None
                else {}
            ),
        },
        "usage": {
            "model": model_name,
            "provider_mode": provider_mode,
            "calls": sdk_usage.requests,
            "prompt_tokens": sdk_usage.input_tokens,
            "completion_tokens": sdk_usage.output_tokens,
            "total_tokens": sdk_usage.total_tokens,
            "cost_usd": None,
        },
        "diagnostics": {
            "retry_safe": {
                "ticket_id": ticket_id,
                "run_id": payload.get("run_id", ticket_id),
                **{
                    **retry_safe,
                    "commit": {
                        **retry_safe.get("commit", {}),
                        "action": "skipped",
                        "message": summary,
                        "changed": False,
                    },
                },
            },
        },
    }


async def _run_pipeline_mcp_async(
    payload: dict,
    tracer=None,
    exp3_provider_boundary=None,
) -> dict:
    if "architecture_mode" in payload:
        from exp3.architecture import SINGLE_AGENT, validate_explicit_e3_request
        from exp3.production_provider import ProductionProviderBoundaryUnavailable

        if validate_explicit_e3_request(payload) != SINGLE_AGENT:
            raise ProductionProviderBoundaryUnavailable(
                "manager_star cannot enter legacy pipeline_mcp"
            )
        if exp3_provider_boundary is None:
            raise ProductionProviderBoundaryUnavailable(
                "explicit E3 M0 requires the production provider boundary"
            )
        # This runs before _prepare_repository_branches: contamination and target
        # absence are proven before legacy M0 can create its developer branch.
        exp3_provider_boundary.start(payload)
    elif exp3_provider_boundary is not None:
        from exp3.production_provider import ProductionProviderBoundaryError

        raise ProductionProviderBoundaryError(
            "an E3 provider boundary cannot be attached to a legacy request"
        )
    provider_config = (
        exp3_provider_boundary.provider_config
        if exp3_provider_boundary is not None
        else resolve_provider_config()
    )
    ticket_id = payload["issue_key"]
    strategy_ref = payload["strategy"]["ref"]
    repo_name = payload["strategy"].get("repo_full_name") or DEFAULT_REPO_NAME
    strategy_path = payload["strategy"]["path"]
    branch_name = payload["strategy"].get("target_branch") or f"quant/{ticket_id}"
    repository_branches = _prepare_repository_branches(payload, tracer=tracer)
    # With no named file there is nothing to pre-read; discovery finds the target.
    branch_code_before = (
        _read_strategy_or_none(branch_name, strategy_path, repo_name)
        if strategy_path
        else None
    )
    # On re-iteration the quant branch already holds the previous tuning; read from
    # it so the agent builds on that edit instead of overwriting it with a fresh
    # change off main. The first iteration (no file on the branch yet) reads the
    # source ref.
    read_ref = _read_ref(strategy_ref, branch_name, branch_code_before is not None)

    # A ticket branch is intentionally reusable. If it already contains the
    # explicit deliverable, validate it before spending another agent pass. The
    # normal general-task reviewer receives existing_files and decides whether the
    # Jira objective is satisfied; rejected work falls through to a real repair.
    strategy = payload.get("strategy") or {}
    # `strategy_path` is the legacy compatibility field.  Discovery requests
    # intentionally leave it empty, so keep a local source-path value for the
    # validation below instead of assuming a named source file exists.
    source_path = strategy.get("source_path") or strategy_path
    target_path = strategy.get("target_path") or strategy_path
    primary_record = next(
        (
            record for record in repository_branches
            if record.get("repo_full_name") == repo_name
            and record.get("target_branch") == branch_name
        ),
        None,
    )
    if (
        strategy.get("target_path_explicit")
        and not payload.get("_skip_reused_target_noop")
        and _reused_target_noop_can_repair(payload)
        and target_path
        and primary_record
        and primary_record.get("branch_action") == "reused"
        and _read_strategy_or_none(branch_name, target_path, repo_name) is not None
    ):
        _, existing_issues = _inspect_repository_artifacts(
            repository_branches=repository_branches,
            committed_artifacts=[
                {"repo_name": repo_name, "branch": branch_name, "path": target_path}
            ],
            primary_key=(repo_name, branch_name),
            required_target_path=target_path,
            reader=get_strategy_code,
        )
        if not existing_issues:
            return _no_changes_result(
                payload=payload,
                ticket_id=ticket_id,
                branch_name=branch_name,
                strategy_path=strategy_path,
                summary=(
                    f"Reused branch {branch_name} already contains validated target "
                    f"{target_path}; awaiting objective review without a new commit."
                ),
                model_name=provider_config.transport_model,
                provider_mode=provider_config.provider_mode,
                sdk_usage=type("Usage", (), {"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0})(),
                retry_safe={"existing_target": {"repo_name": repo_name, "branch": branch_name, "path": target_path}},
                repository_branches=repository_branches,
                repo_name=repo_name,
                existing_files=[target_path],
            )

    # When the ticket named no file, hand the agent a map of every repository this
    # request selected, so it can find what it needs wherever the file actually
    # lives instead of being pointed at a specific path.
    file_map = None
    if not strategy_path:
        # Discovery has no named file to detect prior work with, so read from each
        # target branch that exists: it was cut from the source branch and also
        # carries earlier iterations' commits, which the source ref would hide.
        prepared_targets = {
            (record.get("repo_full_name"), record.get("target_branch"))
            for record in repository_branches
        }
        if (repo_name, branch_name) in prepared_targets:
            read_ref = branch_name
        file_map = build_file_map_block(
            repository_specs_from_payload(
                payload,
                prefer_target_branch=True,
                available_target_branches=prepared_targets,
            )
        )

    prompt = build_mcp_code_prompt(
        payload,
        read_ref=read_ref,
        branch_name=branch_name,
        file_map=file_map,
    )

    # The MCP server runs as a subprocess and enforces repository-specific source
    # branches and path scopes itself, so the complete mapping must reach that
    # boundary rather than relying on prompt instructions.
    server_env = os.environ.copy()
    _apply_github_mcp_request_limits(server_env, payload)
    allowed_directories = (payload.get("strategy") or {}).get("allowed_directories") or []
    if allowed_directories:
        server_env["ALLOWED_DIRECTORIES"] = ",".join(allowed_directories)
    else:
        server_env.pop("ALLOWED_DIRECTORIES", None)
    server_env["SOURCE_BRANCH"] = strategy_ref
    repositories = [
        repo for repo in payload.get("repositories") or [] if isinstance(repo, dict)
    ]
    server_env["ALLOWED_DIRECTORIES_MAP"] = json.dumps(
        {
            repo["repo_full_name"]: repo.get("allowed_directories") or []
            for repo in repositories
            if repo.get("repo_full_name")
        },
        sort_keys=True,
    )
    server_env["SOURCE_BRANCH_MAP"] = json.dumps(
        {
            repo["repo_full_name"]: repo.get("source_branch") or "main"
            for repo in repositories
            if repo.get("repo_full_name")
        },
        sort_keys=True,
    )
    server_env["TARGET_BRANCH_MAP"] = json.dumps(
        {
            repo["repo_full_name"]: repo.get("target_branch") or branch_name
            for repo in repositories
            if repo.get("repo_full_name")
        },
        sort_keys=True,
    )
    # The model-facing GitHub server must fail closed if request-scoped source
    # and target mappings are ever missing, and may read no sibling/legacy ref.
    server_env["ENFORCE_READ_BRANCH_SCOPE"] = "true"
    if exp3_provider_boundary is not None:
        # M0 and M1 use the same manifest/preflight binding at the MCP boundary.
        # Explicit M0 has no role marker but must still prove the frozen deny set.
        server_env["RAE_ARCHITECTURE_MODE"] = "single_agent"
        server_env.pop("RAE_AGENT_ROLE", None)
        server_env.update(
            exp3_provider_boundary.preflight.mcp_environment_binding()
        )
    artifact_dir = Path((payload.get("output_paths") or {}).get("artifact_dir") or "/workspace/output/artifacts")
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        artifact_dir = Path("/tmp")
    audit_path = artifact_dir / "mcp_tool_audit.jsonl"
    try:
        audit_path.unlink(missing_ok=True)
    except OSError:
        pass
    server_env["MCP_TOOL_AUDIT_PATH"] = str(audit_path)

    async with MCPServerStdio(
        name="GitHub MCP",
        params={
            "command": "python",
            "args": [GITHUB_MCP_SERVER_PATH, "--serve"],
            "env": server_env,
        },
        client_session_timeout_seconds=30,
    ) as github_server:
        model = build_agents_model(
            provider_config,
        )
        if exp3_provider_boundary is not None:
            from exp3.policy import DEVELOPER

            model = exp3_provider_boundary.wrap_agents_model(
                model,
                role=DEVELOPER,
                phase="single_agent_developer",
            )
        agent = Agent(
            name="GitHub Strategy Agent",
            instructions=(
                "You are a repository implementation agent. Follow the editing "
                "contract in the user prompt, inspect the repository, and do not "
                "finish an editing run before calling a GitHub write tool for the "
                "required target. Report actual tool failures accurately."
            ),
            model=model,
            model_settings=build_agents_model_settings(provider_config),
            mcp_servers=[github_server],
        )

        max_turns = _resolve_max_agent_turns(payload, len(repositories))
        try:
            # Mark generator-only delivery immediately before the coding-model
            # boundary; the review agent uses build_ticket_context and never this
            # retrieved-memory renderer.
            mark_retrieval_prompt_delivery(payload)
            result = await Runner.run(agent, prompt, max_turns=max_turns)
        except Exception as exc:
            if type(exc).__name__ == "MaxTurnsExceeded":
                raise AgentTurnLimitExceeded(
                    turn_limit=max_turns,
                    turns_consumed=max_turns,
                    audit=_read_tool_audit(audit_path),
                ) from exc
            raise

    sdk_usage = result.context_wrapper.usage
    retry_safe = _extract_commit_diagnostics(result, branch_name)
    precommit_issues = _precommit_validation_issues(retry_safe)
    missing_general_commit = (
        str(payload.get("command") or "").strip().lower() != "backtest"
        and _missing_commit_call(retry_safe)
    )
    if not missing_general_commit and (
        not precommit_issues or _has_nonvalidation_commit_failure(retry_safe)
    ):
        try:
            _raise_if_mcp_write_failed(retry_safe)
        except MCPWritebackError as e:
            detail = json.dumps(retry_safe, sort_keys=True)[:1500]
            message = f"{e}; retry_safe={detail}"
            if tracer:
                tracer.record("mcp_agent", "commit_and_push", "failed", message)
            raise RuntimeError(message) from e

    committed_paths = (retry_safe.get("commit") or {}).get("paths") or []
    committed_artifacts = _normalise_committed_artifacts(
        retry_safe,
        primary_repo_name=repo_name,
        primary_branch=branch_name,
    )
    try:
        if not missing_general_commit and not precommit_issues:
            _validate_run_changed_something(
                payload=payload,
                branch_name=branch_name,
                strategy_ref=strategy_ref,
                strategy_path=strategy_path,
                repo_name=repo_name,
                branch_code_before=branch_code_before,
                committed_paths=committed_paths,
                committed_artifacts=committed_artifacts,
            )
    except NoCodeChanges as e:
        no_change_summary = str(e)

        if tracer:
            tracer.record("mcp_agent", "commit_and_push", "skipped", no_change_summary)
        final_output = getattr(result, "final_output", "")
        summary = (
            f"{no_change_summary}; agent final output: {final_output[:500]}"
            if final_output
            else no_change_summary
        )
        return _no_changes_result(
            payload=payload,
            ticket_id=ticket_id,
            branch_name=branch_name,
            strategy_path=strategy_path,
            summary=summary,
            model_name=provider_config.transport_model,
            provider_mode=provider_config.provider_mode,
            sdk_usage=sdk_usage,
            retry_safe=retry_safe,
            repository_branches=repository_branches,
            repo_name=repo_name,
        )

    if tracer:
        tracer.record(
            "mcp_agent",
            "create_or_reuse_branch",
            "succeeded",
            f"verified {branch_name} is readable",
        )

    # A run may touch more than the file the ticket named, so report what was
    # actually committed. Fall back to the target for the backtest path, whose
    # target is verified against GitHub rather than through the commit tool.
    required_target = (
        target_path
        if target_path
        and (strategy.get("target_path_explicit") or target_path != source_path)
        else None
    )
    primary_key = (repo_name, branch_name)
    files_by_branch, repository_validation_issues = _inspect_repository_artifacts(
        repository_branches=repository_branches,
        committed_artifacts=committed_artifacts,
        primary_key=primary_key,
        required_target_path=required_target,
        reader=get_strategy_code,
    )
    validation_issues = list(precommit_issues) + repository_validation_issues

    primary_files = files_by_branch.get(primary_key) or {
        "modified_files": [],
        "new_files": [],
    }
    modified_files = primary_files["modified_files"]
    new_files = primary_files["new_files"]
    validation_issues = sorted(set(validation_issues))
    if missing_general_commit and not required_target:
        validation_issues.append(
            "The editing agent ended without calling commit_and_push for any artifact."
        )
    validation_issues.extend(
        _repository_discovery_issues(
            source_path=source_path,
            target_path=target_path,
            committed_artifacts=committed_artifacts,
            tool_calls=retry_safe.get("tool_calls") or [],
        )
    )

    validated_artifacts = sorted(
        f"{current_repo}@{current_branch}:{path}"
        for (current_repo, current_branch), classified in files_by_branch.items()
        for path in classified["modified_files"] + classified["new_files"]
    )
    actual_paths = sorted(set(modified_files + new_files))
    if tracer:
        tracer.record(
            "mcp_agent",
            "read_file",
            "failed" if validation_issues else "succeeded",
            (
                "validated committed artifacts: "
                f"{', '.join(validated_artifacts or ['none'])}"
            ),
        )
        tracer.record(
            "mcp_agent",
            "commit_and_push",
            "failed" if precommit_issues else "succeeded",
            (
                "commit blocked by pre-commit validation: "
                + " ".join(precommit_issues)
                if precommit_issues
                else "committed artifacts: "
                f"{', '.join(validated_artifacts or ['none'])}"
            ),
        )
    return {
        "status": "succeeded",
        "issue_key": ticket_id,
        "summary": result.final_output,
        "artifacts": {
            "feature_branch": branch_name,
            "changed_file": target_path,
            "modified_files": modified_files,
            "new_files": new_files,
            **({"validation_issues": validation_issues} if validation_issues else {}),
            "repository_branches": _mark_repository_files(
                repository_branches,
                files_by_branch,
            ),
        },
        "usage": {
            "model": provider_config.transport_model,
            "provider_mode": provider_config.provider_mode,
            "calls": sdk_usage.requests,
            "prompt_tokens": sdk_usage.input_tokens,
            "completion_tokens": sdk_usage.output_tokens,
            "total_tokens": sdk_usage.total_tokens,
            "cost_usd": None,  # not exposed by Agents SDK
        },
        "diagnostics": {
            "retry_safe": {
                "ticket_id": ticket_id,
                "run_id": payload.get("run_id", ticket_id),
                **retry_safe,
                "validated_paths": actual_paths,
                "validated_artifacts": validated_artifacts,
            },
        },
    }


def _prepare_repository_branches(payload: dict, tracer=None) -> list[dict]:
    branches = []
    for repo in payload.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        repo_name = repo.get("repo_full_name")
        target_branch = repo.get("target_branch")
        source_branch = repo.get("source_branch") or "main"
        if not repo_name or not target_branch:
            continue
        details = create_or_reuse_branch_details(
            repo_name=repo_name,
            branch_name=target_branch,
            base_branch=source_branch,
        )
        if tracer:
            tracer.record(
                "planner_agent",
                "create_feature_branch",
                "succeeded",
                f"{repo_name}:{target_branch} {details.get('branch_action')}",
            )
        branches.append(
            {
                "alias": repo.get("alias") or "current",
                "repo_full_name": repo_name,
                "source_branch": source_branch,
                "target_branch": target_branch,
                "branch_action": details.get("branch_action") or "unknown",
                "commit_sha": details.get("commit_sha"),
                "modified_files": [],
                "new_files": [],
            }
        )
    return branches


def _mark_primary_repository_files(
    repository_branches: list[dict],
    repo_name: str,
    branch_name: str,
    modified_files: list[str],
    new_files: list[str],
    *,
    refresh_commit_sha: bool = False,
) -> list[dict]:
    files_by_branch = {
        (repo_name, branch_name): {
            "modified_files": modified_files,
            "new_files": new_files,
        }
    }
    return _mark_repository_files(
        repository_branches,
        files_by_branch,
        refresh_commit_shas=refresh_commit_sha,
    )


def _mark_repository_files(
    repository_branches: list[dict],
    files_by_branch: dict[tuple[str, str], dict[str, list[str]]],
    *,
    refresh_commit_shas: bool = True,
) -> list[dict]:
    """Attach per-repository files and post-commit branch heads to the report."""

    marked = []
    for item in repository_branches:
        record = dict(item)
        key = (record.get("repo_full_name"), record.get("target_branch"))
        classified = files_by_branch.get(key)
        if classified is not None:
            record["modified_files"] = classified.get("modified_files") or []
            record["new_files"] = classified.get("new_files") or []
            if refresh_commit_shas:
                record["commit_sha"] = get_branch_sha(
                    key[1], repo_name=key[0]
                )
        marked.append(record)
    return marked


def _successful_commit_count(result: dict) -> int:
    retry_safe = ((result.get("diagnostics") or {}).get("retry_safe") or {})
    return sum(
        item.get("tool") in {"commit_and_push", "replace_in_file"}
        and item.get("status") == "success"
        for item in retry_safe.get("tool_calls") or []
        if isinstance(item, dict)
    )


def run_pipeline_mcp(payload: dict, tracer=None, exp3_provider_boundary=None) -> dict:
    """Sync wrapper, same pattern as Ben's run_backtest_via_mcp."""
    usage_before = None
    if exp3_provider_boundary is not None:
        usage_before = exp3_provider_boundary.ledger.snapshot()["shared"]
    try:
        result = asyncio.run(
            _run_pipeline_mcp_async(
                payload,
                tracer=tracer,
                exp3_provider_boundary=exp3_provider_boundary,
            )
        )
    except BaseException as exc:
        if exp3_provider_boundary is not None:
            exc.exp3_provider_accounting = exp3_provider_boundary.snapshot()
        raise
    if exp3_provider_boundary is not None:
        exp3_provider_boundary.reconcile_runner_usage(
            result.get("usage") or {},
            before=usage_before,
        )
        accounting = exp3_provider_boundary.snapshot()
        accounting["commit_count"] = _successful_commit_count(result)
        result["experiment3_provider_accounting"] = accounting
    return result


if __name__ == "__main__":
    import json

    test_payload = {
        "run_id": "ALPHA-101-20001",
        "issue_key": "ALPHA-101",
        "command": "backtest",
        "args": {
            "strategy": "add RSI filter so we only take long signals when RSI is below 70"
        },
        "strategy": {
            "repo_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop",
            "ref": "main",
            "path": "rae_runtime/proxy/strategy.py",
        },
        "result_path": "/outputs/result.json",
    }

    result = run_pipeline_mcp(test_payload)
    print(f"\nResult: {json.dumps(result, indent=2)}")
