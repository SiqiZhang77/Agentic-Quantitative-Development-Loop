"""Local role-adapter structure for the fixed Experiment 3 star topology.

This module intentionally imports no Agents SDK, FastMCP, GitHub client or
legacy ``pipeline_mcp`` entry point.  It produces immutable MCP launch specs and
delegates execution to an injected role runner.  A production runner may open
the described stdio session, but offline tests use fakes only.

Manager and architect specs are read-only and have zero branch/commit capacity.
Developer alone receives the existing scoped write tool names.  None of the
adapters pre-creates a branch; the developer can use the server-enforced
``create_or_reuse_branch`` tool inside its own bounded session.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from exp3.budget import RoleBudgetLedger
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.policy import (
    ARCHITECT,
    DEVELOPER,
    MANAGER,
    READ_TOOLS,
    allowed_tools_for_role,
    role_server_env,
    validate_role,
)
from exp3.provider_accounting import (
    ProviderCallAccountingAdapter,
    ProviderCallResult,
)


READ_ONLY_MODE: Final = "read_only"
SCOPED_WRITE_MODE: Final = "developer_scoped_write"


class RoleAdapterError(RuntimeError):
    """A role adapter cannot honour its frozen identity or scope."""


@dataclass(frozen=True)
class MCPLaunchSpec:
    """Dependency-free description of one role-specific MCP subprocess."""

    role: str
    tool_mode: str
    allowed_tools: tuple[str, ...]
    command: str
    args: tuple[str, ...]
    env: Mapping[str, str]
    precreate_branch: bool = False


ProviderTurn = Callable[..., Any]
RoleRunner = Callable[..., Any]


def _repository_maps(payload: Mapping[str, Any]) -> tuple[list[dict], dict, dict, dict]:
    repositories = [
        copy.deepcopy(item)
        for item in payload.get("repositories") or []
        if isinstance(item, Mapping)
    ]
    if not repositories:
        raise RoleAdapterError("manager-star role adapters require repository scope")
    allowed: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    targets: dict[str, str] = {}
    for item in repositories:
        repo = item.get("repo_full_name")
        source = item.get("source_branch")
        target = item.get("target_branch")
        if type(repo) is not str or type(source) is not str or type(target) is not str:
            raise RoleAdapterError(
                "every manager-star repository requires explicit repo/source/target"
            )
        if repo in sources:
            raise RoleAdapterError("manager-star repository scope must be unique")
        allowed[repo] = list(item.get("allowed_directories") or [])
        sources[repo] = source
        targets[repo] = target
    return repositories, allowed, sources, targets


def _validate_manifest_matches_maps(
    manifest: NegativeRefManifest,
    *,
    sources: Mapping[str, str],
    targets: Mapping[str, str],
) -> None:
    manifest_repos = {item.repo_full_name for item in manifest.repositories}
    if manifest_repos != set(sources) or manifest_repos != set(targets):
        raise RoleAdapterError("negative-ref manifest repositories do not match request")
    for policy in manifest.repositories:
        if sources[policy.repo_full_name] != policy.source_ref:
            raise RoleAdapterError("negative-ref source does not match request source")
        if targets[policy.repo_full_name] != policy.current_target_ref:
            raise RoleAdapterError("negative-ref target does not match request target")


def build_mcp_launch_spec(
    payload: Mapping[str, Any],
    *,
    role: str,
    preflight: NegativeRefPreflight,
    server_path: str | None = None,
    base_environment: Mapping[str, str] | None = None,
) -> MCPLaunchSpec:
    """Build one immutable role scope after passing pre-orchestration checks."""

    known_role = validate_role(role)
    if not isinstance(preflight, NegativeRefPreflight):
        raise TypeError("preflight must be a NegativeRefPreflight")
    _, allowed, sources, targets = _repository_maps(payload)
    _validate_manifest_matches_maps(
        preflight.manifest,
        sources=sources,
        targets=targets,
    )
    binding = preflight.mcp_environment_binding()
    env = dict(base_environment or {})
    env.update(role_server_env(known_role))
    env.update(binding)
    env.update(
        {
            "ALLOWED_REPOS": ",".join(sorted(sources)),
            "ALLOWED_DIRECTORIES_MAP": json.dumps(allowed, sort_keys=True),
            "SOURCE_BRANCH_MAP": json.dumps(sources, sort_keys=True),
            "TARGET_BRANCH_MAP": json.dumps(targets, sort_keys=True),
            "ENFORCE_READ_BRANCH_SCOPE": "true",
        }
    )
    if known_role in {MANAGER, ARCHITECT}:
        env["MAX_BRANCHES_PER_RUN"] = "0"
        env["MAX_COMMITS_PER_RUN"] = "0"
        tool_mode = READ_ONLY_MODE
    else:
        controls = payload.get("iteration_controls") or {}
        max_commits = controls.get("max_commits_per_run", 3)
        if type(max_commits) is not int or not 1 <= max_commits <= 50:
            raise RoleAdapterError("developer max_commits_per_run must be 1-50")
        env["MAX_BRANCHES_PER_RUN"] = str(len(sources))
        env["MAX_COMMITS_PER_RUN"] = str(max_commits)
        tool_mode = SCOPED_WRITE_MODE

    executable = server_path or os.getenv(
        "GITHUB_MCP_SERVER_PATH", "/app/proxy/github_mcp_server.py"
    )
    if type(executable) is not str or not executable:
        raise RoleAdapterError("GitHub MCP server path must be a non-empty string")
    return MCPLaunchSpec(
        role=known_role,
        tool_mode=tool_mode,
        allowed_tools=tuple(sorted(allowed_tools_for_role(known_role))),
        command="python",
        args=(executable, "--serve"),
        env=copy.deepcopy(env),
        precreate_branch=False,
    )


def _single_provider_turn_runner(
    *,
    role: str,
    phase: str,
    context: Mapping[str, Any],
    provider_call: ProviderTurn,
    mcp_launch_spec: MCPLaunchSpec,
) -> Any:
    del role, phase, mcp_launch_spec
    return provider_call(request=context)


class BaseRoleAdapter:
    """Persistent dedicated role context plus immutable tool scope."""

    def __init__(
        self,
        *,
        role: str,
        launch_spec: MCPLaunchSpec,
        provider_calls: ProviderCallAccountingAdapter,
        runner: RoleRunner,
    ) -> None:
        known_role = validate_role(role)
        if launch_spec.role != known_role:
            raise RoleAdapterError("launch spec role does not match adapter role")
        if not isinstance(provider_calls, ProviderCallAccountingAdapter):
            raise TypeError("provider_calls must be ProviderCallAccountingAdapter")
        if not callable(runner):
            raise TypeError("runner must be callable")
        self.role = known_role
        self.launch_spec = launch_spec
        self._provider_calls = provider_calls
        self._runner = runner
        self._executions = 0

    @property
    def executions(self) -> int:
        return self._executions

    def execute(self, *, phase: str, context: Mapping[str, Any]) -> Any:
        if type(phase) is not str or not phase:
            raise RoleAdapterError("role phase must be a non-empty string")
        if not isinstance(context, Mapping):
            raise RoleAdapterError("role context must be a mapping")

        def provider_turn(*, request: Any) -> Any:
            return self._provider_calls.invoke(
                role=self.role,
                phase=phase,
                request=request,
            )

        self._executions += 1
        return self._runner(
            role=self.role,
            phase=phase,
            context=copy.deepcopy(dict(context)),
            provider_call=provider_turn,
            mcp_launch_spec=self.launch_spec,
        )


class ReadOnlyRoleAdapter(BaseRoleAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.role not in {MANAGER, ARCHITECT}:
            raise RoleAdapterError("read-only adapter is limited to manager/architect")
        if self.launch_spec.tool_mode != READ_ONLY_MODE:
            raise RoleAdapterError("read-only adapter requires read-only launch spec")
        if set(self.launch_spec.allowed_tools) != set(READ_TOOLS):
            raise RoleAdapterError("read-only launch spec contains a non-read tool")


class DeveloperScopedWriteRoleAdapter(BaseRoleAdapter):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.role != DEVELOPER:
            raise RoleAdapterError("developer adapter requires developer role")
        if self.launch_spec.tool_mode != SCOPED_WRITE_MODE:
            raise RoleAdapterError("developer adapter requires scoped-write launch spec")


class ManagerStarRoleAdapters:
    """The only three persistent role adapters admitted by the router."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, BaseRoleAdapter],
        provider_calls: ProviderCallAccountingAdapter,
        preflight: NegativeRefPreflight,
    ) -> None:
        if set(adapters) != {MANAGER, ARCHITECT, DEVELOPER}:
            raise RoleAdapterError("manager-star requires exactly three fixed adapters")
        self._adapters = dict(adapters)
        self.provider_calls = provider_calls
        self.preflight = preflight

    @property
    def ledger(self) -> RoleBudgetLedger:
        return self.provider_calls.ledger

    def execute(self, *, role: str, phase: str, context: Mapping[str, Any]) -> Any:
        known_role = validate_role(role)
        return self._adapters[known_role].execute(phase=phase, context=context)

    def spec_for(self, role: str) -> MCPLaunchSpec:
        return self._adapters[validate_role(role)].launch_spec

    def provider_records(self) -> list[dict[str, Any]]:
        return self.provider_calls.records()


def build_role_adapters(
    payload: Mapping[str, Any],
    *,
    provider: Callable[..., ProviderCallResult],
    ledger: RoleBudgetLedger,
    preflight: NegativeRefPreflight,
    runner: RoleRunner = _single_provider_turn_runner,
    clock: Callable[[], float] | None = None,
    server_path: str | None = None,
) -> ManagerStarRoleAdapters:
    """Compose the fixed local adapters around an injected provider fake."""

    kwargs: dict[str, Any] = {
        "provider": provider,
        "ledger": ledger,
        "pre_call_check": preflight.before_provider_call,
    }
    if clock is not None:
        kwargs["clock"] = clock
    provider_calls = ProviderCallAccountingAdapter(**kwargs)
    adapters: dict[str, BaseRoleAdapter] = {}
    for role in (MANAGER, ARCHITECT, DEVELOPER):
        launch_spec = build_mcp_launch_spec(
            payload,
            role=role,
            preflight=preflight,
            server_path=server_path,
        )
        adapter_type = (
            DeveloperScopedWriteRoleAdapter
            if role == DEVELOPER
            else ReadOnlyRoleAdapter
        )
        adapters[role] = adapter_type(
            role=role,
            launch_spec=launch_spec,
            provider_calls=provider_calls,
            runner=runner,
        )
    return ManagerStarRoleAdapters(
        adapters=adapters,
        provider_calls=provider_calls,
        preflight=preflight,
    )
