from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest


PROXY_ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = PROXY_ROOT / "github_mcp_server.py"
if str(PROXY_ROOT) not in sys.path:
    sys.path.insert(0, str(PROXY_ROOT))

import github_mcp_server as server


READ_TOOLS = {"read_file", "find_in_file", "read_files", "list_files"}
WRITE_TOOLS = {
    "create_or_reuse_branch",
    "validate_content",
    "replace_in_file",
    "commit_and_push",
}
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


@pytest.fixture(autouse=True)
def _passing_negative_ref_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("E3_REF_SCOPE_PREFLIGHT_PASSED", "true")
    monkeypatch.setenv("E3_NEGATIVE_REF_MANIFEST_SHA256", "a" * 64)
    monkeypatch.setenv("E3_NEGATIVE_REF_SET_SHA256", "b" * 64)
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", str(tmp_path / "mcp-audit.jsonl"))


def _wrapped_backend(tool_name: str, calls: list[dict[str, object]]):
    @server._audited(tool_name)
    def backend(**kwargs):
        calls.append(kwargs)
        return {"status": "success", **kwargs}

    return backend


class _RecordingMcpServer:
    def __init__(self) -> None:
        self.registered: list[str] = []

    def tool(self):
        def register(fn):
            self.registered.append(fn.__name__)
            return fn

        return register


def _visible_tools() -> set[str]:
    mcp = _RecordingMcpServer()
    for tool_name in sorted(ALL_TOOLS):
        def backend(_tool_name=tool_name):
            return _tool_name

        backend.__name__ = tool_name
        server._register_mcp_tool(mcp, tool_name)(backend)
    return set(mcp.registered)


@pytest.mark.parametrize("role", ["manager", "architect"])
@pytest.mark.parametrize("tool_name", sorted(READ_TOOLS))
def test_manager_and_architect_can_call_read_tools(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    tool_name: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", role)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend(tool_name, calls)(path="safe.py")

    assert result["status"] == "success"
    assert calls == [{"path": "safe.py"}]


@pytest.mark.parametrize("role", ["manager", "architect"])
@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOLS))
def test_manager_and_architect_are_blocked_before_write_backend(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    tool_name: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", role)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend(tool_name, calls)(content="must-not-reach-backend")

    assert result["status"] == "blocked"
    assert calls == []
    assert "must-not-reach-backend" not in result["error"]


@pytest.mark.parametrize("role", ["manager", "architect"])
def test_manager_and_architect_mcp_catalogue_exposes_only_read_tools(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", role)

    assert _visible_tools() == READ_TOOLS


def test_developer_mcp_catalogue_exposes_scoped_read_and_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")

    assert _visible_tools() == ALL_TOOLS


@pytest.mark.parametrize("architecture_mode", [None, "single_agent"])
def test_legacy_and_explicit_m0_catalogues_expose_existing_tool_surface(
    monkeypatch: pytest.MonkeyPatch,
    architecture_mode: str | None,
) -> None:
    if architecture_mode is None:
        monkeypatch.delenv("RAE_ARCHITECTURE_MODE", raising=False)
    else:
        monkeypatch.setenv("RAE_ARCHITECTURE_MODE", architecture_mode)
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)

    assert _visible_tools() == ALL_TOOLS


@pytest.mark.parametrize(
    "invalid_binding",
    [
        {"RAE_ARCHITECTURE_MODE": "manager_star"},
        {"RAE_ARCHITECTURE_MODE": "manager_star", "RAE_AGENT_ROLE": "reviewer"},
        {"RAE_ARCHITECTURE_MODE": "unsupported"},
    ],
)
def test_invalid_explicit_e3_identity_exposes_no_mcp_tools(
    monkeypatch: pytest.MonkeyPatch,
    invalid_binding: dict[str, str],
) -> None:
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    for key, value in invalid_binding.items():
        monkeypatch.setenv(key, value)

    assert _visible_tools() == set()


@pytest.mark.parametrize("tool_name", sorted(ALL_TOOLS))
def test_developer_can_call_every_existing_tool(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    calls: list[dict[str, object]] = []

    result = _wrapped_backend(tool_name, calls)(path="safe.py")

    assert result["status"] == "success"
    assert calls == [{"path": "safe.py"}]


@pytest.mark.parametrize("role", [None, "reviewer", "MANAGER"])
def test_manager_star_missing_or_unknown_role_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    role: str | None,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    if role is None:
        monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    else:
        monkeypatch.setenv("RAE_AGENT_ROLE", role)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("read_file", calls)(path="safe.py")

    assert result["status"] == "blocked"
    assert calls == []


@pytest.mark.parametrize(
    "missing",
    [
        "E3_REF_SCOPE_PREFLIGHT_PASSED",
        "E3_NEGATIVE_REF_MANIFEST_SHA256",
        "E3_NEGATIVE_REF_SET_SHA256",
    ],
)
def test_manager_star_missing_negative_ref_binding_fails_before_backend(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    monkeypatch.delenv(missing, raising=False)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("read_file", calls)(path="safe.py")

    assert result["status"] == "blocked"
    assert calls == []
    assert "preflight" in result["error"]


@pytest.mark.parametrize(
    "missing",
    [
        "E3_REF_SCOPE_PREFLIGHT_PASSED",
        "E3_NEGATIVE_REF_MANIFEST_SHA256",
        "E3_NEGATIVE_REF_SET_SHA256",
    ],
)
def test_explicit_m0_missing_negative_ref_binding_fails_before_backend(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    monkeypatch.delenv(missing, raising=False)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("read_file", calls)(path="safe.py")

    assert result["status"] == "blocked"
    assert calls == []
    assert "preflight" in result["error"]


def test_unknown_architecture_mode_cannot_bypass_role_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager-star-typo")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("commit_and_push", calls)(content="blocked")

    assert result["status"] == "blocked"
    assert calls == []


def test_role_marker_without_architecture_mode_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAE_ARCHITECTURE_MODE", raising=False)
    monkeypatch.setenv("RAE_AGENT_ROLE", "manager")
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("read_file", calls)(path="safe.py")

    assert result["status"] == "blocked"
    assert calls == []


def test_role_marker_in_single_agent_mode_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.setenv("RAE_AGENT_ROLE", "manager")
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("commit_and_push", calls)(content="blocked")

    assert result["status"] == "blocked"
    assert calls == []


@pytest.mark.parametrize("architecture_mode", [None, "single_agent"])
def test_legacy_and_m0_servers_retain_existing_behavior_without_role(
    monkeypatch: pytest.MonkeyPatch,
    architecture_mode: str | None,
) -> None:
    if architecture_mode is None:
        monkeypatch.delenv("RAE_ARCHITECTURE_MODE", raising=False)
    else:
        monkeypatch.setenv("RAE_ARCHITECTURE_MODE", architecture_mode)
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    calls: list[dict[str, object]] = []

    result = _wrapped_backend("commit_and_push", calls)(content="legacy-content")

    assert result["status"] == "success"
    assert calls == [{"content": "legacy-content"}]


def test_blocked_call_is_audited_without_argument_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "tool-audit.jsonl"
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "manager")
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", str(audit_path))
    calls: list[dict[str, object]] = []
    secret_content = "highly-sensitive-model-output"

    @server._audited("commit_and_push")
    def backend(path: str, content: str):
        calls.append({"path": path, "content": content})
        return {"status": "success", "path": path}

    result = backend(
        path="strategy.py",
        content=secret_content,
    )

    assert result["status"] == "blocked"
    assert calls == []
    raw_audit = audit_path.read_text(encoding="utf-8")
    assert secret_content not in raw_audit
    record = json.loads(raw_audit)
    assert record["status"] == "blocked"
    assert record["tool"] == "commit_and_push"
    assert record["architecture_mode"] == "manager_star"
    assert record["role"] == "manager"
    assert record["path"] == "strategy.py"


def test_every_existing_mcp_tool_uses_the_common_audited_gate() -> None:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
    decorated: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        audited_names = []
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            if not isinstance(decorator.func, ast.Name) or decorator.func.id != "_audited":
                continue
            if len(decorator.args) == 1 and isinstance(decorator.args[0], ast.Constant):
                audited_names.append(decorator.args[0].value)
        if audited_names:
            decorated[node.name] = audited_names

    assert set(decorated) == ALL_TOOLS
    assert decorated == {tool_name: [tool_name] for tool_name in ALL_TOOLS}


def test_common_wrapper_checks_permission_before_backend_call() -> None:
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
    audited = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_audited"
    )
    wrapped = next(
        node
        for node in ast.walk(audited)
        if isinstance(node, ast.FunctionDef) and node.name == "wrapped"
    )
    calls = [node for node in ast.walk(wrapped) if isinstance(node, ast.Call)]
    permission_line = min(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name)
        and node.func.id == "_manager_star_permission_error"
    )
    backend_line = min(
        node.lineno
        for node in calls
        if isinstance(node.func, ast.Name) and node.func.id == "fn"
    )
    assert permission_line < backend_line


def test_explicit_e3_missing_audit_sink_fails_before_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    monkeypatch.setenv(
        "MCP_TOOL_AUDIT_PATH",
        str(tmp_path / "missing-parent" / "audit.jsonl"),
    )
    calls: list[dict[str, object]] = []

    with pytest.raises(server.McpAuditPersistenceError, match="parent"):
        _wrapped_backend("read_file", calls)(path="safe.py")

    assert calls == []


def test_explicit_e3_rejects_relative_audit_sink_before_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "single_agent")
    monkeypatch.delenv("RAE_AGENT_ROLE", raising=False)
    monkeypatch.setenv("MCP_TOOL_AUDIT_PATH", "relative-audit.jsonl")
    calls: list[dict[str, object]] = []

    with pytest.raises(server.McpAuditPersistenceError, match="absolute"):
        _wrapped_backend("read_file", calls)(path="safe.py")

    assert calls == []


def test_explicit_e3_partial_audit_write_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAE_ARCHITECTURE_MODE", "manager_star")
    monkeypatch.setenv("RAE_AGENT_ROLE", "developer")
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(server.os, "write", lambda _fd, _payload: 0)

    with pytest.raises(server.McpAuditPersistenceError, match="incomplete"):
        _wrapped_backend("read_file", calls)(path="safe.py")

    # Sink readiness is checked first; this injected failure occurs only while
    # persisting the completed tool record and is never silently swallowed.
    assert calls == [{"path": "safe.py"}]
