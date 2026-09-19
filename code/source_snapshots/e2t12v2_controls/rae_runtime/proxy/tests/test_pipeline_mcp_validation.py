"""What counts as "this run changed something", per workflow type.

A backtest must have changed its own .request: the engine runs that exact file,
so committing something else and backtesting an untouched baseline would report
metrics for a strategy nobody asked for. A general request has no such file, so
it only has to have changed at least one file in scope.
"""

import pytest

import pipeline_mcp
import pipeline
from pipeline_mcp import NoCodeChanges, _validate_run_changed_something


def _validate(command, committed_paths, monkeypatch, target="proxy/strategy.request"):
    # The backtest branch verifies its target against GitHub; stub that read.
    monkeypatch.setattr(
        pipeline_mcp, "get_strategy_code", lambda branch, path: f"contents of {branch}"
    )
    _validate_run_changed_something(
        payload={"command": command},
        branch_name="quant/SCRUM-1",
        strategy_ref="main",
        strategy_path=target,
        branch_code_before=None,
        committed_paths=committed_paths,
    )


def test_general_request_passes_when_any_file_changed(monkeypatch):
    _validate("refactor", ["proxy/a.py", "proxy/b.py"], monkeypatch)


def test_multi_repository_edit_rejects_scripted_pipeline(monkeypatch):
    monkeypatch.setenv("USE_MCP_GITHUB", "false")
    monkeypatch.delenv("RAE_OFFLINE", raising=False)

    with pytest.raises(RuntimeError, match="Multi-repository editing requires"):
        pipeline.run_pipeline_auto(
            {
                "repositories": [
                    {"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"},
                    {"repo_full_name": "bankingscience/ATPDataHandlersRepo"},
                ]
            }
        )


def test_scripted_reporting_refreshes_primary_branch_sha_after_commit(monkeypatch):
    monkeypatch.setattr(pipeline, "get_branch_sha", lambda branch, repo_name: "final-sha")

    reported = pipeline.mark_primary_repository_files(
        [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "target_branch": "quant/SCRUM-1",
                "commit_sha": "pre-commit-sha",
            },
            {
                "repo_full_name": "bankingscience/ATPConnectorsRepo",
                "target_branch": "quant/SCRUM-1",
                "commit_sha": "untouched-sha",
            },
        ],
        "bankingscience/BSLAgenticQuantDevLoop",
        "quant/SCRUM-1",
        ["rae_runtime/proxy/strategy.py"],
        [],
        refresh_commit_sha=True,
    )

    assert reported[0]["commit_sha"] == "final-sha"
    assert reported[1]["commit_sha"] == "untouched-sha"


def test_mcp_reporting_refreshes_primary_branch_sha_after_commit(monkeypatch):
    monkeypatch.setattr(
        pipeline_mcp, "get_branch_sha", lambda branch, repo_name: "mcp-final-sha"
    )

    reported = pipeline_mcp._mark_primary_repository_files(
        [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "target_branch": "quant/SCRUM-1",
                "commit_sha": "pre-commit-sha",
            }
        ],
        "bankingscience/BSLAgenticQuantDevLoop",
        "quant/SCRUM-1",
        ["rae_runtime/proxy/strategy.py"],
        [],
        refresh_commit_sha=True,
    )

    assert reported[0]["commit_sha"] == "mcp-final-sha"


def test_mcp_reporting_refreshes_every_touched_repository_sha(monkeypatch):
    seen = []
    monkeypatch.setattr(
        pipeline_mcp,
        "get_branch_sha",
        lambda branch, repo_name: seen.append((repo_name, branch))
        or f"final-{repo_name.split('/')[-1]}",
    )
    branches = [
        {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "target_branch": "quant/SCRUM-1",
            "commit_sha": "old-primary",
        },
        {
            "repo_full_name": "bankingscience/ATPDataHandlersRepo",
            "target_branch": "quant/SCRUM-1",
            "commit_sha": "old-secondary",
        },
        {
            "repo_full_name": "bankingscience/ATPConnectorsRepo",
            "target_branch": "quant/SCRUM-1",
            "commit_sha": "untouched",
        },
    ]

    reported = pipeline_mcp._mark_repository_files(
        branches,
        {
            (
                "bankingscience/BSLAgenticQuantDevLoop",
                "quant/SCRUM-1",
            ): {"modified_files": ["rae_runtime/a.py"], "new_files": []},
            (
                "bankingscience/ATPDataHandlersRepo",
                "quant/SCRUM-1",
            ): {"modified_files": [], "new_files": ["SCRUM-1.txt"]},
        },
    )

    assert reported[0]["commit_sha"] == "final-BSLAgenticQuantDevLoop"
    assert reported[1]["commit_sha"] == "final-ATPDataHandlersRepo"
    assert reported[2]["commit_sha"] == "untouched"
    assert reported[1]["new_files"] == ["SCRUM-1.txt"]
    assert len(seen) == 2


def test_mcp_classifies_each_artifact_against_its_repository_source_branch():
    reads = []

    class MissingFile(Exception):
        status = 404

    contents = {
        (
            "bankingscience/BSLAgenticQuantDevLoop",
            "quant/SCRUM-1",
            "rae_runtime/a.py",
        ): "updated",
        (
            "bankingscience/BSLAgenticQuantDevLoop",
            "main",
            "rae_runtime/a.py",
        ): "original",
        (
            "bankingscience/ATPDataHandlersRepo",
            "quant/SCRUM-1",
            "SCRUM-1.txt",
        ): "new canary file",
    }

    def reader(branch, path, repo_name):
        reads.append((repo_name, branch, path))
        key = (repo_name, branch, path)
        if key not in contents:
            raise MissingFile("not found")
        return contents[key]

    files, issues = pipeline_mcp._inspect_repository_artifacts(
        repository_branches=[
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "main",
                "target_branch": "quant/SCRUM-1",
            },
            {
                "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                "source_branch": "develop",
                "target_branch": "quant/SCRUM-1",
            },
        ],
        committed_artifacts=[
            {
                "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
                "branch": "quant/SCRUM-1",
                "path": "rae_runtime/a.py",
            },
            {
                "repo_name": "bankingscience/ATPDataHandlersRepo",
                "branch": "quant/SCRUM-1",
                "path": "SCRUM-1.txt",
            },
        ],
        primary_key=(
            "bankingscience/BSLAgenticQuantDevLoop",
            "quant/SCRUM-1",
        ),
        required_target_path="rae_runtime/a.py",
        reader=reader,
    )

    assert issues == []
    assert files[
        ("bankingscience/BSLAgenticQuantDevLoop", "quant/SCRUM-1")
    ]["modified_files"] == ["rae_runtime/a.py"]
    assert files[
        ("bankingscience/ATPDataHandlersRepo", "quant/SCRUM-1")
    ]["new_files"] == ["SCRUM-1.txt"]
    assert (
        "bankingscience/ATPDataHandlersRepo",
        "develop",
        "SCRUM-1.txt",
    ) in reads


def test_general_request_passes_when_the_named_file_was_left_alone(monkeypatch):
    # A refactor may legitimately need to change files the ticket never named,
    # and leave the named one alone.
    _validate("refactor", ["proxy/helper.py"], monkeypatch, target="proxy/main.py")


def test_general_request_fails_when_nothing_was_committed(monkeypatch):
    with pytest.raises(NoCodeChanges, match="committed no file changes"):
        _validate("refactor", [], monkeypatch)


def test_backtest_still_requires_its_own_target_to_change(monkeypatch):
    # Committing some other file must not let an untouched baseline be backtested.
    monkeypatch.setattr(
        pipeline_mcp, "get_strategy_code", lambda branch, path: "IDENTICAL = 1"
    )

    with pytest.raises(NoCodeChanges, match="did not change"):
        _validate_run_changed_something(
            payload={"command": "backtest"},
            branch_name="quant/SCRUM-1",
            strategy_ref="main",
            strategy_path="proxy/strategy.request",
            branch_code_before=None,
            committed_paths=["proxy/unrelated.py"],
        )


def test_backtest_passes_when_its_target_changed(monkeypatch):
    contents = {
        ("main", "proxy/strategy.request"): "NPORT = 50",
        ("quant/SCRUM-1", "proxy/strategy.request"): "NPORT = 80",
    }
    monkeypatch.setattr(
        pipeline_mcp, "get_strategy_code", lambda branch, path: contents[(branch, path)]
    )

    _validate_run_changed_something(
        payload={"command": "backtest"},
        branch_name="quant/SCRUM-1",
        strategy_ref="main",
        strategy_path="proxy/strategy.request",
        branch_code_before=None,
        committed_paths=["proxy/strategy.request"],
    )


def test_backtest_rejects_a_request_edit_that_breaks_the_key_contract(monkeypatch):
    # The engine ignores unknown keys, so adding one silently does nothing.
    contents = {
        ("main", "proxy/strategy.request"): "NPORT = 50",
        ("quant/SCRUM-1", "proxy/strategy.request"): "NPORT = 50\nINVENTED_KEY = 1",
    }
    monkeypatch.setattr(
        pipeline_mcp, "get_strategy_code", lambda branch, path: contents[(branch, path)]
    )

    with pytest.raises(pipeline_mcp.InvalidStrategyRequest, match="added keys"):
        _validate_run_changed_something(
            payload={"command": "backtest"},
            branch_name="quant/SCRUM-1",
            strategy_ref="main",
            strategy_path="proxy/strategy.request",
            branch_code_before=None,
            committed_paths=["proxy/strategy.request"],
        )
