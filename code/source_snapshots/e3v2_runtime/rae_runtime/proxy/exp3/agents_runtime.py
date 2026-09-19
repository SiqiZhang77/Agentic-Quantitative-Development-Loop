"""Dependency-backed M1 Agents SDK/MCP adapter with offline injection seams.

Imports of ``agents`` and LiteLLM are deliberately lazy. Production constructs
one bounded Agent/MCP session per fixed topology stage and gives the Agent only
the role-scoped MCP launch specification. Every underlying model request passes
through :class:`Experiment3ProductionProviderBoundary`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from exp3.contracts import canonical_json
from exp3.policy import ALL_TOOLS, ARCHITECT, DEVELOPER, MANAGER, validate_role
from exp3.production_provider import Experiment3ProductionProviderBoundary
from exp3.role_adapters import MCPLaunchSpec, build_mcp_launch_spec
from provider_config import build_agents_model, build_agents_model_settings


_PHASE_MAX_TURNS: Final = {
    "manager_to_architect": 1,
    "architect_to_manager": 3,
    "manager_to_developer": 1,
    "developer_to_manager": 9,
    "manager_final": 1,
}
_PHASE_ROLE: Final = {
    "manager_to_architect": MANAGER,
    "architect_to_manager": ARCHITECT,
    "manager_to_developer": MANAGER,
    "developer_to_manager": DEVELOPER,
    "manager_final": MANAGER,
}
_HANDOFF_SCHEMA_FILES: Final = {
    "architect_to_manager": "architect_plan_v1.schema.json",
    "developer_to_manager": "developer_result_v1.schema.json",
    "manager_final": "manager_final_v1.schema.json",
}
_INSTRUCTION_SCHEMA: Final = {
    "type": "object",
    "additionalProperties": False,
    "required": ["instruction"],
    "properties": {"instruction": {"type": "string", "minLength": 1}},
}
_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
_MCP_AUDIT_SCHEMA_VERSION: Final = "exp3-mcp-tool-audit-v1"


class AgentsRuntimeError(RuntimeError):
    """The real M1 role/session adapter cannot run safely."""


class AgentsRuntimeUnavailable(AgentsRuntimeError):
    """Pinned Agents SDK dependencies are absent or incompatible."""


class AgentOutputError(AgentsRuntimeError, ValueError):
    """A role final output was not one strict JSON object."""


class McpAuditError(AgentsRuntimeError):
    """The content-free MCP audit is malformed or inconsistent."""


def _mcp_child_environment(audit_path: Path) -> dict[str, str]:
    """Build the minimal environment inherited by a role-scoped MCP server.

    ``MCPServerStdio`` treats its explicit ``env`` mapping as the child process
    environment.  Provider credentials stay in the parent runtime, while the
    GitHub MCP child receives only its required repository credential and the
    optional asserted login.  Formal execution fails before a role/model call
    if that credential is unavailable.
    """

    environment = {"MCP_TOOL_AUDIT_PATH": str(audit_path)}
    offline = os.getenv("RAE_OFFLINE") == "1"
    github_token = os.getenv("GITHUB_TOKEN")
    if not offline:
        if not github_token or any(ch.isspace() for ch in github_token):
            raise AgentsRuntimeError(
                "GITHUB_TOKEN is required for role-scoped MCP repository access"
            )
        environment["GITHUB_TOKEN"] = github_token
    else:
        environment["RAE_OFFLINE"] = "1"
        offline_push_dir = os.getenv("OFFLINE_PUSH_DIR")
        if offline_push_dir:
            environment["OFFLINE_PUSH_DIR"] = offline_push_dir
        if github_token:
            environment["GITHUB_TOKEN"] = github_token

    github_username = os.getenv("GITHUB_USERNAME")
    if github_username:
        if any(ch.isspace() for ch in github_username):
            raise AgentsRuntimeError("GITHUB_USERNAME contains whitespace")
        environment["GITHUB_USERNAME"] = github_username
    return environment


@dataclass(frozen=True)
class AgentsSdkComponents:
    Agent: Any
    Runner: Any
    RunConfig: Any
    MCPServerStdio: Any
    LitellmModel: Any
    ModelSettings: Any


def load_agents_sdk_components() -> AgentsSdkComponents:
    """Load the pinned production dependencies only when M1 is enabled."""

    try:
        from agents import Agent, ModelSettings, RunConfig, Runner
        from agents.mcp import MCPServerStdio
    except ImportError as exc:
        raise AgentsRuntimeUnavailable(
            "pinned openai-agents/LiteLLM dependencies are unavailable"
        ) from exc
    return AgentsSdkComponents(
        Agent=Agent,
        Runner=Runner,
        RunConfig=RunConfig,
        MCPServerStdio=MCPServerStdio,
        # Direct OpenAI must not import LiteLLM (whose import may perform a
        # model-cost metadata fetch). The company factory imports it lazily only
        # when that provider mode is explicitly selected.
        LitellmModel=None,
        ModelSettings=ModelSettings,
    )


def _manager_final_acceptance_identity(
    context: Mapping[str, Any],
) -> list[dict[str, str]]:
    """Extract immutable acceptance identities from the validated architect plan."""

    approved_plan = context.get("approved_plan")
    mapping = (
        approved_plan.get("acceptance_mapping")
        if isinstance(approved_plan, Mapping)
        else None
    )
    if not isinstance(mapping, list) or not mapping:
        raise AgentsRuntimeError(
            "manager_final requires a non-empty approved acceptance mapping"
        )
    identity: list[dict[str, str]] = []
    for item in mapping:
        if not isinstance(item, Mapping):
            raise AgentsRuntimeError("approved acceptance mapping item is invalid")
        requirement_id = item.get("requirement_id")
        requirement = item.get("requirement")
        if (
            type(requirement_id) is not str
            or not requirement_id
            or type(requirement) is not str
            or not requirement
        ):
            raise AgentsRuntimeError(
                "approved acceptance mapping identity is incomplete"
            )
        identity.append(
            {
                "requirement_id": requirement_id,
                "requirement": requirement,
            }
        )
    return identity


def _output_contract(
    phase: str,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    filename = _HANDOFF_SCHEMA_FILES.get(phase)
    if filename is None:
        return copy.deepcopy(_INSTRUCTION_SCHEMA)
    contract = json.loads((_SCHEMA_DIR / filename).read_text(encoding="utf-8"))
    if phase == "manager_final":
        identity = _manager_final_acceptance_identity(context or {})
        acceptance = contract["properties"]["acceptance_mapping"]
        acceptance["minItems"] = len(identity)
        acceptance["maxItems"] = len(identity)
        acceptance["prefixItems"] = [
            {
                "properties": {
                    "requirement_id": {"const": item["requirement_id"]},
                    "requirement": {"const": item["requirement"]},
                }
            }
            for item in identity
        ]
    return contract


def _role_instructions(role: str, phase: str) -> str:
    if role == MANAGER:
        scope = (
            "Do not call repository tools in this phase; use only the mediated "
            "context supplied in the prompt."
        )
    elif role == ARCHITECT:
        scope = (
            "Use repository tools only for read operations. Never create a branch, "
            "edit, validate a proposed write, commit or push."
        )
    else:
        scope = "Use only the scoped repository tools supplied to this developer session."
    mediation = {
        MANAGER: "You are the only coordinator and final decision maker.",
        ARCHITECT: "Communicate only through the manager-provided context.",
        DEVELOPER: "Implement only the manager-approved plan and report to manager.",
    }[role]
    final_mapping_instruction = (
        " In manager_final, copy every requirement_id and requirement verbatim "
        "from required_acceptance_mapping_identity, in the supplied order. "
        "Those two fields are immutable; decide only status/evidence plus the "
        "overall decision, failure_phase and summary."
        if phase == "manager_final"
        else ""
    )
    return (
        f"Experiment 3 fixed role: {role}. Fixed phase: {phase}. {mediation} "
        f"{scope} Do not invoke retrieval or RAG. Return exactly one JSON object "
        "matching the supplied output_contract; do not use Markdown fences."
        f"{final_mapping_instruction}"
    )


def _parse_final_output(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        parsed = dict(value)
    elif type(value) is str:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AgentOutputError("role output is not valid JSON") from exc
    else:
        model_dump = getattr(value, "model_dump", None)
        if not callable(model_dump):
            raise AgentOutputError("role output is not a JSON object")
        parsed = model_dump(mode="json")
    if not isinstance(parsed, dict):
        raise AgentOutputError("role output root must be an object")
    try:
        return json.loads(canonical_json(parsed))
    except (TypeError, ValueError) as exc:
        raise AgentOutputError("role output is not canonical JSON-compatible") from exc


def _runner_usage(result: object) -> dict[str, int]:
    wrapper = getattr(result, "context_wrapper", None)
    usage = getattr(wrapper, "usage", None)
    if usage is None:
        raise AgentsRuntimeError("Agents SDK result omitted aggregate usage")
    return {
        "calls": getattr(usage, "requests", None),
        "prompt_tokens": getattr(usage, "input_tokens", None),
        "completion_tokens": getattr(usage, "output_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


class AgentsSdkRoleRunner:
    """Open one role-scoped MCP session and execute one fixed topology stage."""

    def __init__(
        self,
        *,
        components_loader: Callable[[], AgentsSdkComponents] = load_agents_sdk_components,
        model_factory: Callable[[str, AgentsSdkComponents], object] | None = None,
    ) -> None:
        if not callable(components_loader):
            raise TypeError("components_loader must be callable")
        if model_factory is not None and not callable(model_factory):
            raise TypeError("model_factory must be callable or None")
        self._components_loader = components_loader
        self._model_factory = model_factory

    def _model(
        self,
        role: str,
        components: AgentsSdkComponents,
        boundary: Experiment3ProductionProviderBoundary,
    ) -> object:
        if self._model_factory is not None:
            return self._model_factory(role, components)
        return build_agents_model(
            boundary.provider_config,
            litellm_model_cls=components.LitellmModel,
        )

    def __call__(
        self,
        *,
        role: str,
        phase: str,
        context: Mapping[str, Any],
        boundary: Experiment3ProductionProviderBoundary,
        mcp_launch_spec: MCPLaunchSpec,
    ) -> dict[str, Any]:
        return asyncio.run(
            self._run_stage(
                role=role,
                phase=phase,
                context=context,
                boundary=boundary,
                mcp_launch_spec=mcp_launch_spec,
            )
        )

    async def _run_stage(
        self,
        *,
        role: str,
        phase: str,
        context: Mapping[str, Any],
        boundary: Experiment3ProductionProviderBoundary,
        mcp_launch_spec: MCPLaunchSpec,
    ) -> dict[str, Any]:
        known_role = validate_role(role)
        if _PHASE_ROLE.get(phase) != known_role:
            raise AgentsRuntimeError("role does not match the fixed runtime phase")
        if mcp_launch_spec.role != known_role or mcp_launch_spec.precreate_branch:
            raise AgentsRuntimeError("MCP launch spec does not match fixed role scope")
        components = self._components_loader()
        raw_model = self._model(known_role, components, boundary)
        model = boundary.wrap_agents_model(
            raw_model,
            role=known_role,
            phase=phase,
        )
        prompt_value: dict[str, Any] = {
            "phase": phase,
            "context": dict(context),
            "output_contract": _output_contract(phase, context),
        }
        if phase == "manager_final":
            prompt_value["required_acceptance_mapping_identity"] = (
                _manager_final_acceptance_identity(context)
            )
        prompt = canonical_json(prompt_value)
        before = boundary.ledger.snapshot()["shared"]
        async with components.MCPServerStdio(
            name=f"E3 GitHub MCP ({known_role})",
            params={
                "command": mcp_launch_spec.command,
                "args": list(mcp_launch_spec.args),
                "env": dict(mcp_launch_spec.env),
            },
            client_session_timeout_seconds=30,
        ) as server:
            agent = components.Agent(
                name=f"E3 {known_role} ({phase})",
                instructions=_role_instructions(known_role, phase),
                model=model,
                model_settings=build_agents_model_settings(
                    boundary.provider_config,
                    model_settings_cls=components.ModelSettings,
                    # Manager owns exactly three calls for exactly three fixed
                    # topology stages. A tool call would require a fourth model
                    # continuation, so manager stages are deterministically
                    # tool-free while server-side read-only policy stays bound.
                    tool_choice="none" if known_role == MANAGER else None,
                ),
                mcp_servers=[server],
            )
            result = await components.Runner.run(
                agent,
                prompt,
                max_turns=_PHASE_MAX_TURNS[phase],
                run_config=components.RunConfig(
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                    workflow_name=f"Experiment 3 {known_role} {phase}",
                ),
            )
        boundary.reconcile_runner_usage(_runner_usage(result), before=before)
        return _parse_final_output(getattr(result, "final_output", None))


class ProductionManagerStarRoleAdapters:
    """Router-compatible fixed role registry backed by Agents SDK sessions."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        boundary: Experiment3ProductionProviderBoundary,
        runner: Callable[..., dict[str, Any]],
        audit_path: Path,
    ) -> None:
        if boundary.architecture_mode != "manager_star" or not boundary.started:
            raise AgentsRuntimeError("M1 production boundary must be started")
        if not callable(runner):
            raise TypeError("runner must be callable")
        self.provider_boundary = boundary
        self.preflight = boundary.preflight
        self.ledger = boundary.ledger
        self._runner = runner
        self._audit_path = audit_path
        mcp_child_environment = _mcp_child_environment(audit_path)
        self._specs = {
            role: build_mcp_launch_spec(
                payload,
                role=role,
                preflight=self.preflight,
                base_environment=mcp_child_environment,
            )
            for role in (MANAGER, ARCHITECT, DEVELOPER)
        }

    def execute(self, *, role: str, phase: str, context: Mapping[str, Any]) -> Any:
        known_role = validate_role(role)
        return self._runner(
            role=known_role,
            phase=phase,
            context=copy.deepcopy(dict(context)),
            boundary=self.provider_boundary,
            mcp_launch_spec=self._specs[known_role],
        )

    def spec_for(self, role: str) -> MCPLaunchSpec:
        return self._specs[validate_role(role)]

    def provider_records(self) -> list[dict[str, Any]]:
        return self.provider_boundary.records()

    def commit_count(self) -> int:
        if not self._audit_path.exists():
            raise McpAuditError("MCP audit file is missing")
        count = 0
        records = []
        for raw in self._audit_path.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                raise McpAuditError("MCP audit contains an empty record")
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise McpAuditError("MCP audit contains malformed JSON") from exc
            if not isinstance(item, dict):
                raise McpAuditError("MCP audit record must be an object")
            records.append(item)
        if not records:
            raise McpAuditError("MCP audit omitted its initialization record")
        expected_header = {
            "schema_version": _MCP_AUDIT_SCHEMA_VERSION,
            "record_type": "audit_initialized",
            "architecture_mode": "manager_star",
            "manifest_sha256": self.preflight.manifest.sha256,
            "deny_ref_set_sha256": self.preflight.manifest.deny_ref_set_sha256,
        }
        if records[0] != expected_header:
            raise McpAuditError("MCP audit initialization identity is invalid")
        for item in records[1:]:
            if item.get("schema_version") != _MCP_AUDIT_SCHEMA_VERSION:
                raise McpAuditError("MCP audit schema version is invalid")
            if item.get("record_type") != "tool_call":
                raise McpAuditError("MCP audit record type is invalid")
            if item.get("architecture_mode") != "manager_star":
                raise McpAuditError("MCP audit architecture identity is invalid")
            try:
                role = validate_role(item.get("role"))
            except Exception as exc:
                raise McpAuditError("MCP audit role is invalid") from exc
            tool = item.get("tool")
            if tool not in ALL_TOOLS:
                raise McpAuditError("MCP audit tool is invalid")
            if type(item.get("status")) is not str or not item["status"]:
                raise McpAuditError("MCP audit status is invalid")
            if "content" in item or "arguments" in item:
                raise McpAuditError("MCP audit contains forbidden model content")
            if (
                role == DEVELOPER
                and tool in {"commit_and_push", "replace_in_file"}
                and item.get("status") == "success"
            ):
                count += 1
        return count


def build_production_role_adapters(
    payload: Mapping[str, Any],
    *,
    boundary: Experiment3ProductionProviderBoundary,
    runner: Callable[..., dict[str, Any]] | None = None,
    audit_directory: str | Path | None = None,
) -> ProductionManagerStarRoleAdapters:
    """Build the three production role adapters after unified preflight passes."""

    artifact_value = (payload.get("output_paths") or {}).get("artifact_dir")
    artifact_dir = Path(
        audit_directory
        if audit_directory is not None
        else artifact_value or "/workspace/output/artifacts"
    )
    try:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AgentsRuntimeError("M1 MCP audit directory is unavailable") from exc
    audit_path = artifact_dir / "exp3_mcp_tool_audit.jsonl"
    audit_header = {
        "schema_version": _MCP_AUDIT_SCHEMA_VERSION,
        "record_type": "audit_initialized",
        "architecture_mode": "manager_star",
        "manifest_sha256": boundary.preflight.manifest.sha256,
        "deny_ref_set_sha256": boundary.preflight.manifest.deny_ref_set_sha256,
    }
    try:
        audit_path.write_text(
            json.dumps(audit_header, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise AgentsRuntimeError("M1 MCP audit cannot be initialized") from exc
    return ProductionManagerStarRoleAdapters(
        payload,
        boundary=boundary,
        runner=runner or AgentsSdkRoleRunner(),
        audit_path=audit_path,
    )
