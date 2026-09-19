import pytest

from exp3.policy import (
    ALL_TOOLS,
    ARCHITECT,
    ARCHITECTURE_MODE,
    DEVELOPER,
    DEVELOPER_WRITE_TOOLS,
    MANAGER,
    READ_TOOLS,
    ROLES,
    ROLE_SEQUENCE,
    ROLE_TOOLS,
    ToolPermissionError,
    TopologyGuard,
    TopologyViolation,
    allowed_tools_for_role,
    require_tool_permission,
    role_server_env,
    tool_allowed,
)


def test_policy_is_the_frozen_three_role_manager_star():
    assert ROLES == frozenset({"manager", "architect", "developer"})
    assert ROLE_SEQUENCE == (
        "manager",
        "architect",
        "manager",
        "developer",
        "manager",
    )
    assert ARCHITECTURE_MODE == "manager_star"


def test_topology_advances_only_through_the_exact_sequence():
    guard = TopologyGuard()

    for cursor, role in enumerate(ROLE_SEQUENCE, start=1):
        assert guard.expected_role == role
        assert guard.advance(role) == cursor

    assert guard.sequence_complete is True
    assert guard.expected_role is None
    assert guard.finalized is False
    guard.finalize(MANAGER)
    assert guard.finalized is True


@pytest.mark.parametrize(
    "prefix,attempt",
    [
        ((MANAGER,), DEVELOPER),  # architect -> developer direct-peer shortcut
        ((MANAGER,), MANAGER),  # repeated manager
        ((), ARCHITECT),  # cannot enter at architect
        ((), DEVELOPER),  # cannot enter at developer
        ((), "reviewer"),  # dynamic role
        ((), None),  # missing role
    ],
)
def test_invalid_transition_is_blocked_without_moving_cursor(prefix, attempt):
    guard = TopologyGuard()
    for role in prefix:
        guard.advance(role)
    before = guard.snapshot()

    with pytest.raises(TopologyViolation):
        guard.advance(attempt)

    assert guard.snapshot() == before


def test_repeat_or_extra_stage_after_sequence_is_blocked():
    guard = TopologyGuard()
    for role in ROLE_SEQUENCE:
        guard.advance(role)
    before = guard.snapshot()

    with pytest.raises(TopologyViolation):
        guard.advance(MANAGER)

    assert guard.snapshot() == before


def test_only_final_manager_can_finalize_once_and_not_early():
    early = TopologyGuard()
    with pytest.raises(TopologyViolation, match="before every fixed role stage"):
        early.finalize(MANAGER)
    assert early.snapshot()["finalized"] is False

    guard = TopologyGuard()
    for role in ROLE_SEQUENCE:
        guard.advance(role)
    with pytest.raises(TopologyViolation, match="Only 'manager'"):
        guard.finalize(ARCHITECT)
    assert guard.finalized is False

    guard.finalize(MANAGER)
    with pytest.raises(TopologyViolation, match="already been finalized"):
        guard.finalize(MANAGER)


def test_read_and_write_tool_sets_are_exact():
    assert READ_TOOLS == frozenset(
        {"read_file", "find_in_file", "read_files", "list_files"}
    )
    assert DEVELOPER_WRITE_TOOLS == frozenset(
        {
            "create_or_reuse_branch",
            "validate_content",
            "replace_in_file",
            "commit_and_push",
        }
    )
    assert ALL_TOOLS == READ_TOOLS | DEVELOPER_WRITE_TOOLS
    assert ROLE_TOOLS[MANAGER] == READ_TOOLS
    assert ROLE_TOOLS[ARCHITECT] == READ_TOOLS
    assert ROLE_TOOLS[DEVELOPER] == ALL_TOOLS


@pytest.mark.parametrize("role", [MANAGER, ARCHITECT, DEVELOPER])
@pytest.mark.parametrize("tool", sorted(READ_TOOLS))
def test_every_role_may_use_read_tools(role, tool):
    assert tool_allowed(role, tool) is True
    assert require_tool_permission(role, tool) is None


@pytest.mark.parametrize("role", [MANAGER, ARCHITECT])
@pytest.mark.parametrize("tool", sorted(DEVELOPER_WRITE_TOOLS))
def test_manager_and_architect_cannot_use_write_tools(role, tool):
    assert tool_allowed(role, tool) is False
    with pytest.raises(ToolPermissionError):
        require_tool_permission(role, tool)


@pytest.mark.parametrize("tool", sorted(DEVELOPER_WRITE_TOOLS))
def test_only_developer_receives_scoped_write_tools(tool):
    assert tool_allowed(DEVELOPER, tool) is True
    assert require_tool_permission(DEVELOPER, tool) is None


@pytest.mark.parametrize("role", [None, "", "reviewer", True])
def test_missing_or_unknown_role_fails_closed(role):
    assert tool_allowed(role, "read_file") is False
    with pytest.raises(ToolPermissionError):
        require_tool_permission(role, "read_file")
    with pytest.raises(ValueError):
        allowed_tools_for_role(role)


@pytest.mark.parametrize("tool", [None, "", "delete_branch", True])
def test_missing_or_unknown_tool_fails_closed(tool):
    assert tool_allowed(DEVELOPER, tool) is False
    with pytest.raises(ToolPermissionError):
        require_tool_permission(DEVELOPER, tool)


@pytest.mark.parametrize("role", [MANAGER, ARCHITECT, DEVELOPER])
def test_role_server_env_binds_mode_and_exact_role(role):
    assert role_server_env(role) == {
        "RAE_ARCHITECTURE_MODE": "manager_star",
        "RAE_AGENT_ROLE": role,
    }


def test_role_server_env_rejects_dynamic_role():
    with pytest.raises(ValueError):
        role_server_env("reviewer")
