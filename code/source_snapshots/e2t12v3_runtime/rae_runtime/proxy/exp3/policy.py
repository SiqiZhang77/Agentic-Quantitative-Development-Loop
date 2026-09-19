"""Fail-closed Experiment 3 topology and GitHub-tool policy.

The policy is intentionally static.  A request may select the Experiment 3
architecture, but it cannot add a role, widen a role's tools, or alter the
manager-star transition order.
"""

from __future__ import annotations

from threading import RLock
from types import MappingProxyType
from typing import Final


ARCHITECTURE_MODE: Final = "manager_star"
MANAGER: Final = "manager"
ARCHITECT: Final = "architect"
DEVELOPER: Final = "developer"

ROLES: Final = frozenset({MANAGER, ARCHITECT, DEVELOPER})
ROLE_SEQUENCE: Final = (MANAGER, ARCHITECT, MANAGER, DEVELOPER, MANAGER)
TOPOLOGY: Final = ROLE_SEQUENCE
FINALIZER_ROLE: Final = MANAGER

READ_TOOLS: Final = frozenset(
    {"read_file", "find_in_file", "read_files", "list_files"}
)
DEVELOPER_WRITE_TOOLS: Final = frozenset(
    {
        "create_or_reuse_branch",
        "validate_content",
        "replace_in_file",
        "commit_and_push",
    }
)
ALL_TOOLS: Final = READ_TOOLS | DEVELOPER_WRITE_TOOLS

ROLE_TOOLS: Final = MappingProxyType(
    {
        MANAGER: READ_TOOLS,
        ARCHITECT: READ_TOOLS,
        DEVELOPER: ALL_TOOLS,
    }
)


class PolicyError(ValueError):
    """Base class for invalid, non-authorising policy inputs."""


class UnknownRoleError(PolicyError):
    """The caller supplied no role or a role outside the frozen role set."""


class UnknownToolError(PolicyError):
    """The caller supplied no tool or a tool outside the frozen tool set."""


class TopologyViolation(PolicyError):
    """A role attempted a transition outside the fixed manager-star sequence."""


class ToolPermissionError(PermissionError):
    """A role attempted to use a tool that the server-side policy denies."""

    def __init__(self, role: object, tool: object, reason: str):
        self.role = role
        self.tool = tool
        self.reason = reason
        super().__init__(
            f"Experiment 3 tool permission denied for role={role!r}, "
            f"tool={tool!r}: {reason}"
        )


def validate_role(role: object) -> str:
    """Return a known role, rejecting missing, non-string, and dynamic roles."""

    if type(role) is not str or role not in ROLES:
        raise UnknownRoleError(
            f"Unknown Experiment 3 role {role!r}; expected one of {sorted(ROLES)}"
        )
    return role


def validate_tool(tool: object) -> str:
    """Return a known tool name, rejecting missing and dynamic tool names."""

    if type(tool) is not str or tool not in ALL_TOOLS:
        raise UnknownToolError(
            f"Unknown Experiment 3 tool {tool!r}; expected one of {sorted(ALL_TOOLS)}"
        )
    return tool


def allowed_tools_for_role(role: object) -> frozenset[str]:
    """Return the immutable tool set for *role*; unknown roles fail closed."""

    return ROLE_TOOLS[validate_role(role)]


def tool_allowed(role: object, tool: object) -> bool:
    """Non-raising permission predicate. Any malformed input is denied."""

    try:
        known_role = validate_role(role)
        known_tool = validate_tool(tool)
    except PolicyError:
        return False
    return known_tool in ROLE_TOOLS[known_role]


is_tool_allowed = tool_allowed


def require_tool_permission(role: object, tool: object) -> None:
    """Raise :class:`ToolPermissionError` unless *role* may call *tool*.

    The error type deliberately covers unknown roles and unknown tools too.  This
    gives an MCP boundary one fail-closed API without needing to distinguish a
    malformed request from a recognised-but-disallowed write.
    """

    try:
        known_role = validate_role(role)
    except UnknownRoleError as exc:
        raise ToolPermissionError(role, tool, str(exc)) from exc
    try:
        known_tool = validate_tool(tool)
    except UnknownToolError as exc:
        raise ToolPermissionError(role, tool, str(exc)) from exc
    if known_tool not in ROLE_TOOLS[known_role]:
        raise ToolPermissionError(
            known_role,
            known_tool,
            f"allowed tools are {sorted(ROLE_TOOLS[known_role])}",
        )


raise_for_tool_permission = require_tool_permission


def role_server_env(role: object) -> dict[str, str]:
    """Environment binding consumed by a role-specific MCP subprocess."""

    known_role = validate_role(role)
    return {
        "RAE_ARCHITECTURE_MODE": ARCHITECTURE_MODE,
        "RAE_AGENT_ROLE": known_role,
    }


class TopologyGuard:
    """Cursor guard for the one permitted manager-star execution.

    ``advance`` is monotonic and mutates the cursor only after an exact match.
    Once all five role stages have run, the final manager must explicitly call
    ``finalize``.  No peer-to-peer shortcut, repeated stage, sixth stage, or
    dynamically named role can change the guard's state.
    """

    def __init__(self) -> None:
        self._cursor = 0
        self._finalized = False
        self._lock = RLock()

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._cursor

    @property
    def expected_role(self) -> str | None:
        with self._lock:
            if self._cursor >= len(ROLE_SEQUENCE):
                return None
            return ROLE_SEQUENCE[self._cursor]

    @property
    def sequence_complete(self) -> bool:
        with self._lock:
            return self._cursor == len(ROLE_SEQUENCE)

    @property
    def finalized(self) -> bool:
        with self._lock:
            return self._finalized

    def advance(self, role: object) -> int:
        """Consume the next exact role and return the new cursor position."""

        with self._lock:
            if self._finalized:
                raise TopologyViolation("The topology has already been finalized")
            if self._cursor >= len(ROLE_SEQUENCE):
                raise TopologyViolation(
                    "The fixed role sequence is complete; no additional role is permitted"
                )
            try:
                known_role = validate_role(role)
            except UnknownRoleError as exc:
                raise TopologyViolation(str(exc)) from exc
            expected = ROLE_SEQUENCE[self._cursor]
            if known_role != expected:
                raise TopologyViolation(
                    f"Role {known_role!r} cannot run at cursor {self._cursor}; "
                    f"expected {expected!r}"
                )
            self._cursor += 1
            return self._cursor

    def finalize(self, role: object) -> None:
        """Finalize once, only by manager, after the entire sequence completed."""

        with self._lock:
            if self._finalized:
                raise TopologyViolation("The topology has already been finalized")
            try:
                known_role = validate_role(role)
            except UnknownRoleError as exc:
                raise TopologyViolation(str(exc)) from exc
            if known_role != FINALIZER_ROLE:
                raise TopologyViolation(
                    f"Only {FINALIZER_ROLE!r} may finalize the topology"
                )
            if self._cursor != len(ROLE_SEQUENCE):
                raise TopologyViolation(
                    "The manager cannot finalize before every fixed role stage completes"
                )
            self._finalized = True

    def snapshot(self) -> dict[str, object]:
        """Return an immutable-by-copy view suitable for diagnostics."""

        with self._lock:
            return {
                "architecture_mode": ARCHITECTURE_MODE,
                "cursor": self._cursor,
                "expected_role": (
                    ROLE_SEQUENCE[self._cursor]
                    if self._cursor < len(ROLE_SEQUENCE)
                    else None
                ),
                "sequence_complete": self._cursor == len(ROLE_SEQUENCE),
                "finalized": self._finalized,
            }
