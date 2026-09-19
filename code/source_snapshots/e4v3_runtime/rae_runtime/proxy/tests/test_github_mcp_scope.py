"""Server-side enforcement of the request's allowed_directories.

The prompt asks the agent to stay in scope; this is what makes it true. The paths
reaching these tools come from the model, so they are untrusted: a model that
ignores its instructions, or Jira content that talks it into straying, must still
be unable to read or write outside the run's scope.
"""

import asyncio

import pytest
from fastmcp import Client

import github_mcp_server as server


def _tool_data(result):
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"unexpected MCP result: {result!r}")


@pytest.fixture
def scoped(monkeypatch):
    monkeypatch.setattr(
        server, "_ALLOWED_DIRECTORIES", ("rae_runtime/proxy", "strategies")
    )


@pytest.fixture
def unrestricted(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", ())


@pytest.fixture
def whole_repository(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", (".",))


@pytest.mark.parametrize(
    "path",
    [
        "rae_runtime/proxy/github_client.py",
        "rae_runtime/proxy/nested/deeper/file.py",
        "strategies/momentum.py",
        "strategies",
    ],
)
def test_paths_inside_the_allowlist_are_permitted(scoped, path):
    assert server._check_path_scope(path) is None


@pytest.mark.parametrize(
    "path",
    [
        "rae_runtime/mcp/server.py",
        ".github/workflows/test.yml",
        "jira-chatops-gateway/dags/docker_sandbox_runner.py",
        # A sibling whose name merely starts with an allowed directory's name.
        "strategies-secret/leak.py",
    ],
)
def test_paths_outside_the_allowlist_are_blocked(scoped, path):
    error = server._check_path_scope(path)

    assert error is not None
    assert "outside the directories this run may touch" in error


@pytest.mark.parametrize(
    "path",
    [
        # Normalises back inside the repo but outside the allowlist — a plain
        # prefix check would wave this through.
        "rae_runtime/proxy/../mcp/server.py",
        "../../etc/passwd",
        "rae_runtime/proxy/../../../etc/passwd",
        "/etc/passwd",
        "/rae_runtime/proxy/github_client.py",
        "..",
        # This normalises into an allowed directory, but traversal syntax is itself
        # forbidden at the server boundary.
        "outside/../rae_runtime/proxy/github_client.py",
    ],
)
def test_traversal_and_absolute_paths_are_blocked(scoped, path):
    assert server._check_path_scope(path) is not None


@pytest.mark.parametrize(
    "path",
    ["../../etc/passwd", "/etc/passwd", "..", "safe/../secret.py", ""],
)
def test_traversal_is_blocked_even_with_no_allowlist(unrestricted, path):
    # An unscoped run is still not allowed to leave the repository.
    assert server._check_path_scope(path) is not None


def test_no_allowlist_leaves_the_repository_unrestricted(unrestricted):
    # Requests that supply no allowed_directories keep today's behaviour.
    assert server._check_path_scope("anything/at/all.py") is None
    assert server._check_path_scope("rae_runtime/proxy/strategy.request") is None


def test_exact_write_scope_keeps_broader_read_scope(monkeypatch):
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    root = "experiments/shared/t3-quant-suite-v1"
    output = f"{root}/submissions/quant_portfolio_analytics.json"
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", ())
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES_MAP", {repo: (root,)})
    monkeypatch.setattr(server, "_WRITABLE_PATHS_MAP", {repo: (output,)})

    assert server._check_path_scope(repo, f"{root}/input/returns.csv") is None
    assert server._check_path_scope(repo, f"{root}/output_schema_v1.json") is None
    assert server._check_write_path_scope(repo, output) is None
    error = server._check_write_path_scope(repo, f"{root}/input/returns.csv")
    assert "outside the exact writable files" in error


@pytest.mark.parametrize("raw", ["[]", "not-json", '{"owner/repo": []}'])
def test_configured_malformed_exact_write_scope_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("WRITABLE_PATHS_MAP", raw)
    with pytest.raises(RuntimeError, match="WRITABLE_PATHS_MAP"):
        server._load_writable_paths_map()


@pytest.mark.parametrize("raw", ["[]", "not-json", '{"owner/repo": []}'])
def test_configured_malformed_calculation_schema_scope_fails_closed(monkeypatch, raw):
    monkeypatch.setenv("CALCULATION_SCHEMA_PATHS_MAP", raw)
    with pytest.raises(RuntimeError, match="CALCULATION_SCHEMA_PATHS_MAP"):
        server._load_calculation_schema_paths_map()


def test_dot_scope_permits_normal_repository_paths(whole_repository):
    assert server._check_path_scope("rae_runtime/proxy/github_client.py") is None
    assert server._check_path_scope("jira-chatops-gateway/dags/runner.py") is None
    assert server._check_list_scope("rae_runtime") is None


@pytest.mark.parametrize(
    "path", ["../secret", "safe/../secret", "/etc/passwd", "C:\\secret"]
)
def test_dot_scope_still_blocks_traversal_and_absolute_paths(
    whole_repository, path
):
    assert server._check_path_scope(path) is not None


def test_listing_may_walk_down_to_an_allowed_directory(scoped):
    # Without this the agent cannot navigate to the directories it may work in.
    assert server._check_list_scope("rae_runtime") is None
    assert server._check_list_scope("") is None


def test_listing_an_unrelated_directory_is_blocked(scoped):
    assert server._check_list_scope("jira-chatops-gateway/dags") is not None


@pytest.mark.parametrize("directory", ["safe/..", "safe/../secret", "/"])
def test_list_traversal_and_absolute_paths_are_always_blocked(
    unrestricted, directory
):
    assert server._check_list_scope(directory) is not None


def test_scope_is_parsed_from_the_environment(monkeypatch):
    monkeypatch.setenv("ALLOWED_DIRECTORIES", "strategies, research/notebooks ,/pinned/")

    assert server._load_path_scope() == ("strategies", "research/notebooks", "pinned")


def test_absent_scope_env_means_unrestricted(monkeypatch):
    monkeypatch.delenv("ALLOWED_DIRECTORIES", raising=False)

    assert server._load_path_scope() == ()


def test_repository_scopes_do_not_leak_between_repositories(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", ("legacy",))
    monkeypatch.setattr(
        server,
        "_ALLOWED_DIRECTORIES_MAP",
        {
            "bankingscience/BSLAgenticQuantDevLoop": ("rae_runtime",),
            "bankingscience/ATPDataHandlersRepo": ("atp-handlers",),
        },
    )

    assert (
        server._check_path_scope(
            "bankingscience/BSLAgenticQuantDevLoop", "rae_runtime/proxy/run.py"
        )
        is None
    )
    assert (
        server._check_path_scope(
            "bankingscience/ATPDataHandlersRepo", "rae_runtime/proxy/run.py"
        )
        is not None
    )
    assert (
        server._check_path_scope(
            "bankingscience/ATPDataHandlersRepo",
            "atp-handlers/src/main/java/Handler.java",
        )
        is None
    )
    assert (
        server._check_path_scope(
            "bankingscience/BSLAgenticQuantDevLoop",
            "atp-handlers/src/main/java/Handler.java",
        )
        is not None
    )


def test_repository_source_branch_map_is_used_for_content_validation(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        server,
        "_SOURCE_BRANCH_MAP",
        {"bankingscience/ATPDataHandlersRepo": "develop"},
    )
    monkeypatch.setattr(
        server,
        "proposed_artifact_issues",
        lambda **kwargs: seen.update(kwargs) or [],
    )

    server._validate_proposed_content(
        branch="quant/SCRUM-5",
        path="README.md",
        content="# README",
        repo_name="bankingscience/ATPDataHandlersRepo",
    )

    assert seen["source_branch"] == "develop"


def test_write_scope_requires_each_repositorys_configured_target(monkeypatch):
    monkeypatch.setattr(
        server,
        "_ALLOWED_REPOS",
        {
            "bankingscience/BSLAgenticQuantDevLoop",
            "bankingscience/ATPDataHandlersRepo",
        },
    )
    monkeypatch.setattr(
        server,
        "_TARGET_BRANCH_MAP",
        {
            "bankingscience/BSLAgenticQuantDevLoop": "quant/SCRUM-5",
            "bankingscience/ATPDataHandlersRepo": "quant/SCRUM-5/data",
        },
    )

    assert (
        server._check_write_scope(
            "bankingscience/ATPDataHandlersRepo", "quant/SCRUM-5/data"
        )
        is None
    )
    error = server._check_write_scope(
        "bankingscience/ATPDataHandlersRepo", "quant/SCRUM-5"
    )
    assert "not the configured target branch" in error


def test_read_scope_allows_only_request_source_and_target(monkeypatch):
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: "exp2/formal-source-v2"})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: "quant/SCRUM-201"})

    assert server._check_read_scope(repo, "exp2/formal-source-v2") is None
    assert server._check_read_scope(repo, "quant/SCRUM-201") is None
    error = server._check_read_scope(repo, "quant/SCRUM-184")
    assert error is not None
    assert "only its source and target branches" in error


def test_read_scope_preserves_legacy_behaviour_without_request_maps(monkeypatch):
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {})
    monkeypatch.setattr(server, "_ENFORCE_READ_BRANCH_SCOPE", False)

    assert server._check_read_scope(server._DEFAULT_REPO_NAME, "any/ref") is None


def test_read_and_base_scope_fail_closed_when_enforced_map_is_missing(monkeypatch):
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {})
    monkeypatch.setattr(server, "_ENFORCE_READ_BRANCH_SCOPE", True)

    read_error = server._check_read_scope(server._DEFAULT_REPO_NAME, "main")
    base_error = server._check_base_branch_scope(server._DEFAULT_REPO_NAME, "main")
    assert "no configured source/target refs" in read_error
    assert "no configured source branch" in base_error


def test_read_tool_blocks_prior_experiment_branch_before_github_access(monkeypatch):
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: "exp2/formal-source-v2"})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: "quant/SCRUM-201"})
    reads = []
    monkeypatch.setattr(
        server,
        "_get_strategy_code",
        lambda **kwargs: reads.append(kwargs) or "must not be returned",
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "read_file",
                {
                    "repo_name": repo,
                    "branch": "quant/SCRUM-184",
                    "path": "jira-chatops-gateway/dags/repository_catalog.py",
                },
            )

    data = _tool_data(asyncio.run(invoke()))
    assert data["status"] == "blocked"
    assert reads == []


def test_branch_creation_rejects_unconfigured_base_before_github_write(monkeypatch):
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: "exp2/formal-source-v2"})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: "quant/SCRUM-201"})
    writes = []
    monkeypatch.setattr(
        server,
        "_create_feature_branch",
        lambda **kwargs: writes.append(kwargs) or "must not be created",
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "create_or_reuse_branch",
                {
                    "repo_name": repo,
                    "branch_name": "quant/SCRUM-201",
                    "base_branch": "quant/SCRUM-184",
                },
            )

    data = _tool_data(asyncio.run(invoke()))
    assert data["status"] == "blocked"
    assert "not the configured source branch" in data["error"]
    assert writes == []


@pytest.mark.parametrize("tool_name", ["find_in_file", "read_files", "list_files"])
def test_every_read_tool_blocks_prior_experiment_ref_before_backend(
    monkeypatch, tool_name
):
    repo = "bankingscience/BSLAgenticQuantDevLoop"
    monkeypatch.setattr(server, "_SOURCE_BRANCH_MAP", {repo: "exp2/formal-source-v2"})
    monkeypatch.setattr(server, "_TARGET_BRANCH_MAP", {repo: "quant/SCRUM-201"})
    reads = []
    monkeypatch.setattr(
        server,
        "_get_strategy_code",
        lambda **kwargs: reads.append(("read", kwargs)) or "needle",
    )
    monkeypatch.setattr(
        server,
        "_list_files",
        lambda **kwargs: reads.append(("list", kwargs)) or ["file.py"],
    )

    arguments = {
        "find_in_file": {
            "repo_name": repo,
            "branch": "quant/SCRUM-184",
            "path": "rae_runtime/proxy/budget_guard.py",
            "query": "BudgetGuard",
        },
        "read_files": {
            "repo_name": repo,
            "branch": "quant/SCRUM-184",
            "paths": ["rae_runtime/proxy/budget_guard.py"],
        },
        "list_files": {
            "repo_name": repo,
            "branch": "quant/SCRUM-184",
            "directory": "rae_runtime/proxy",
        },
    }[tool_name]

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(tool_name, arguments)

    data = _tool_data(asyncio.run(invoke()))
    assert data["status"] == "blocked"
    assert reads == []


def test_commit_tool_blocks_invalid_content_before_github_write(monkeypatch):
    writes = []
    monkeypatch.setattr(
        server,
        "_push_strategy_code",
        lambda **kwargs: writes.append(kwargs) or "unexpected write",
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-115",
                    "path": "README.md",
                    "content": (
                        "Detailed content for the comprehensive README will be "
                        "inserted here."
                    ),
                    "commit_message": "SCRUM-115: write README",
                },
            )

    result = _tool_data(asyncio.run(invoke()))

    assert result["status"] == "validation_failed"
    assert writes == []


def test_batch_read_returns_multiple_complete_scoped_files(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get_strategy_code",
        lambda *, path, **kwargs: {"pom.xml": "<project/>", "src/App.java": "class App {}"}[path],
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "read_files",
                {"branch": "develop", "paths": ["pom.xml", "src/App.java"]},
            )

    data = _tool_data(asyncio.run(invoke()))
    assert data["status"] == "success"
    assert [item["content"] for item in data["files"]] == ["<project/>", "class App {}"]


def test_batch_read_rejects_entire_request_when_one_path_is_out_of_scope(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", ("src",))
    called = []
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kwargs: called.append(kwargs))

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "read_files",
                {"branch": "develop", "paths": ["src/App.java", "pom.xml"]},
            )

    data = _tool_data(asyncio.run(invoke()))
    assert data["status"] == "blocked"
    assert called == []


def test_batch_read_distinguishes_oversized_from_deferred_small_file(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get_strategy_code",
        lambda *, path, **kwargs: "a" * (70 * 1024 if path == "first.txt" else 40 * 1024),
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(
                "read_files",
                {"branch": "develop", "paths": ["first.txt", "second.txt"]},
            )

    data = _tool_data(asyncio.run(invoke()))
    assert data["files"][0]["status"] == "success"
    assert data["files"][1]["status"] == "batch_limit_reached"


def test_validate_then_commit_uses_the_same_precommit_gate(monkeypatch):
    writes = []
    monkeypatch.setattr(
        server,
        "_push_strategy_code",
        lambda **kwargs: writes.append(kwargs) or "Updated src/example.py",
    )
    content = "def verified_change():\n    return True\n"

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            validation = await client.call_tool(
                "validate_content",
                {
                    "branch": "quant/SCRUM-115",
                    "path": "src/example.py",
                    "content": content,
                },
            )
            commit = await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-115",
                    "path": "src/example.py",
                    "content": content,
                    "commit_message": "SCRUM-115: verified change",
                },
            )
            return validation, commit

    validation, commit = asyncio.run(invoke())

    assert _tool_data(validation)["status"] == "success"
    assert _tool_data(commit)["status"] == "success"
    assert [write["code"] for write in writes] == [content]
