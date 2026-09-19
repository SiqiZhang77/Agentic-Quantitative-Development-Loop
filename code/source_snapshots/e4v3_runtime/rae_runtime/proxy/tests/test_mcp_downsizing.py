"""Model-facing reads are reduced; integrity reads are not.

The read_file tool hands a model a reduced view of an oversized file. Artifact
validation and commit change-detection must keep seeing exact bytes, or a commit
would be compared against content that is not what is on the branch.
"""

import asyncio
import json

import pytest
from fastmcp import Client

import github_mcp_server as server


def _tool_data(result):
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"unexpected MCP result: {result!r}")


def _notebook():
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": 3,
                    "source": ["def evolve(pop):\n", "    return pop\n"],
                    "metadata": {},
                    "outputs": [
                        {"output_type": "display_data", "data": {"image/png": "A" * 80_000}}
                    ],
                }
            ],
            "metadata": {"kernelspec": {"name": "python3"}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


@pytest.fixture
def unrestricted(monkeypatch):
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES", ())
    monkeypatch.setattr(server, "_ALLOWED_DIRECTORIES_MAP", {})
    # Run-scoped in production (one server subprocess per run); isolate per test.
    monkeypatch.setattr(server, "_REDUCED_READS", {})


def _call(tool, args):
    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            return await client.call_tool(tool, args)

    return _tool_data(asyncio.run(invoke()))


# --- read_file reduces oversized content -----------------------------------


def test_read_file_strips_notebook_and_reports_it(unrestricted, monkeypatch):
    raw = _notebook()
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: raw)

    result = _call("read_file", {"branch": "develop", "path": "notebooks/GA.ipynb"})

    assert result["status"] == "success"
    assert "def evolve(pop):" in result["content"]
    assert "A" * 100 not in result["content"]  # base64 output dropped
    assert len(result["content"]) < len(raw) / 10
    assert result["downsized"]["kind"] == "notebook"
    assert "Do not commit this reduced view" in result["notice"]


def test_read_file_leaves_small_files_exact(unrestricted, monkeypatch):
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "print(1)\n")

    result = _call("read_file", {"branch": "develop", "path": "src/m.py"})

    assert result["content"] == "print(1)\n"
    assert "downsized" not in result
    assert "notice" not in result


def test_read_file_truncates_huge_plain_file(unrestricted, monkeypatch):
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "x" * 500_000)

    result = _call("read_file", {"branch": "develop", "path": "src/big.py"})

    assert result["downsized"]["kind"] == "truncated"
    assert len(result["content"]) < 500_000
    assert "TRUNCATED" in result["content"]


def test_repeated_read_of_the_same_large_file_is_compact(unrestricted, monkeypatch):
    raw = "START\n" + ("x" * 50_000) + "\nTARGET_AT_END\n"
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: raw)

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            first = await client.call_tool(
                "read_file", {"branch": "develop", "path": "src/big.py"}
            )
            second = await client.call_tool(
                "read_file", {"branch": "develop", "path": "src/big.py"}
            )
            return _tool_data(first), _tool_data(second)

    first, second = asyncio.run(invoke())
    assert first["status"] == "success"
    assert "TARGET_AT_END" in first["content"]
    assert second["status"] == "already_read"
    assert "find_in_file" in second["notice"]
    assert "content" not in second


def test_find_in_file_returns_bounded_context_for_a_tail_symbol(unrestricted, monkeypatch):
    raw = "\n".join(["header"] + [f"line_{index}" for index in range(2000)] + ["def target_symbol():", "    return 1"])
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: raw)

    result = _call(
        "find_in_file",
        {
            "branch": "develop",
            "path": "src/big.py",
            "query": "def target_symbol",
            "context_lines": 2,
        },
    )

    assert result["status"] == "success"
    assert result["match_count"] == 1
    assert "def target_symbol():" in result["matches"][0]["content"]
    assert len(result["matches"][0]["content"]) < 1000


def test_replace_in_file_preserves_an_unseen_large_file_tail(unrestricted, monkeypatch):
    old = "def target_symbol():\n    return 1\n"
    replacement = "def helper():\n    return 0\n\n\ndef target_symbol():\n    return helper()\n"
    raw = "HEADER\n" + ("x" * 20_000) + "\n" + old + ("y" * 20_000) + "\nTRAILER\n"
    writes = []
    validations = []
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: raw)
    monkeypatch.setattr(server, "_push_strategy_code", lambda **kw: writes.append(kw) or "Updated src/big.py")
    monkeypatch.setattr(
        server,
        "_validate_proposed_content",
        lambda **kw: validations.append(kw) or [],
    )

    result = _call(
        "replace_in_file",
        {
            "branch": "quant/SCRUM-200",
            "path": "src/big.py",
            "old_text": old,
            "new_text": replacement,
            "commit_message": "SCRUM-200: make target use helper",
        },
    )

    assert result["status"] == "success"
    assert writes[0]["code"] == raw.replace(old, replacement, 1)
    assert writes[0]["code"].endswith("TRAILER\n")
    assert validations[0]["allow_truncated_source"] is True


def test_replace_in_file_refuses_an_ambiguous_snippet(unrestricted, monkeypatch):
    writes = []
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "same\nsame\n")
    monkeypatch.setattr(server, "_push_strategy_code", lambda **kw: writes.append(kw) or "unexpected")

    result = _call(
        "replace_in_file",
        {
            "branch": "quant/SCRUM-200",
            "path": "src/big.py",
            "old_text": "same\n",
            "new_text": "changed\n",
            "commit_message": "SCRUM-200: change one line",
        },
    )

    assert result["status"] == "validation_failed"
    assert "exactly once" in " ".join(result["issues"])
    assert writes == []


# --- integrity reads must stay byte-exact ----------------------------------


def test_validation_reads_are_not_downsized(unrestricted, monkeypatch):
    """The regression that would hurt most: validation seeing a reduced file."""
    raw = _notebook()
    seen = []

    def reader(**kwargs):
        seen.append(kwargs)
        return raw

    monkeypatch.setattr(server, "_get_strategy_code", reader)

    captured = {}

    def fake_proposed_artifact_issues(**kwargs):
        captured.update(kwargs)
        # exercise the reader the validator was handed
        captured["read_back"] = kwargs["reader"](
            branch="develop", path="x.py", repo_name="bankingscience/BSLAgenticQuantDevLoop"
        )
        return []

    monkeypatch.setattr(
        server, "proposed_artifact_issues", fake_proposed_artifact_issues
    )

    server._validate_proposed_content(
        branch="quant/T-1",
        path="src/example.py",
        content="def x():\n    return 1\n",
        repo_name="bankingscience/BSLAgenticQuantDevLoop",
    )

    # the validator's reader returned the untouched notebook, not a stripped view
    assert captured["read_back"] == raw
    assert "[outputs omitted]" not in captured["read_back"]


# --- .ipynb commit guard ---------------------------------------------------


def test_committing_a_stripped_notebook_is_blocked(unrestricted, monkeypatch):
    writes = []
    monkeypatch.setattr(
        server,
        "_push_strategy_code",
        lambda **kw: writes.append(kw) or "unexpected write",
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: _notebook())

    # what read_file would have handed the model
    stripped = "# --- cell 1 (code) ---\ndef evolve(pop):\n    return pop"

    result = _call(
        "commit_and_push",
        {
            "branch": "quant/SCRUM-107",
            "path": "notebooks/GA.ipynb",
            "content": stripped,
            "commit_message": "SCRUM-107: edit notebook",
        },
    )

    assert result["status"] == "validation_failed"
    assert "not a valid Jupyter notebook" in " ".join(result["issues"])
    assert writes == []  # nothing reached GitHub


def test_valid_notebook_json_still_commits(unrestricted, monkeypatch):
    writes = []
    monkeypatch.setattr(
        server,
        "_push_strategy_code",
        lambda **kw: writes.append(kw) or "Updated notebooks/GA.ipynb",
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: _notebook())

    result = _call(
        "commit_and_push",
        {
            "branch": "quant/SCRUM-107",
            "path": "notebooks/GA.ipynb",
            "content": _notebook(),
            "commit_message": "SCRUM-107: real notebook",
        },
    )

    assert result["status"] == "success"
    assert len(writes) == 1


# --- truncated-read write guard --------------------------------------------


def _read_then_commit(path, read_content, commit_content):
    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            read = await client.call_tool(
                "read_file", {"branch": "develop", "path": path}
            )
            commit = await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": path,
                    "content": commit_content,
                    "commit_message": "SCRUM-107: edit",
                },
            )
            return _tool_data(read), _tool_data(commit)

    return asyncio.run(invoke())


def test_committing_a_file_that_was_truncated_on_read_is_blocked(
    unrestricted, monkeypatch
):
    """The regression this guard exists for: silent loss of a large file's tail."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "x" * 500_000)

    read, commit = _read_then_commit(
        "src/big.py", None, "def only_what_the_model_saw():\n    return 1\n"
    )

    assert read["downsized"]["kind"] == "truncated"
    assert commit["status"] == "validation_failed"
    assert "truncated form" in " ".join(commit["issues"])
    assert writes == []  # nothing reached GitHub


def test_fully_read_file_still_commits(unrestricted, monkeypatch):
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "def a():\n    return 1\n")

    read, commit = _read_then_commit("src/small.py", None, "def a():\n    return 2\n")

    assert "downsized" not in read
    assert commit["status"] == "success"
    assert len(writes) == 1


def test_committing_a_different_path_after_a_truncated_read_is_allowed(
    unrestricted, monkeypatch
):
    """Reading a huge reference file must not block writing a new module."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "x" * 500_000)

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            await client.call_tool("read_file", {"branch": "develop", "path": "src/big.py"})
            return await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "src/new_module.py",
                    "content": "def helper():\n    return 1\n",
                    "commit_message": "SCRUM-107: new module",
                },
            )

    result = _tool_data(asyncio.run(invoke()))
    assert result["status"] == "success"
    assert len(writes) == 1


def test_a_duplicate_read_does_not_clear_an_existing_truncation_block(
    unrestricted, monkeypatch
):
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    contents = iter(["x" * 500_000, "def a():\n    return 1\n"])
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: next(contents))

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            # Repeating an exact read is deliberately suppressed to avoid spending
            # model context twice. It therefore cannot replace the first truncated
            # view with a complete view or make overwriting the file safe.
            await client.call_tool("read_file", {"branch": "develop", "path": "src/f.py"})
            duplicate = await client.call_tool(
                "read_file", {"branch": "develop", "path": "src/f.py"}
            )
            commit = await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "src/f.py",
                    "content": "def a():\n    return 2\n",
                    "commit_message": "SCRUM-107: edit",
                },
            )
            return duplicate, commit

    duplicate, commit = asyncio.run(invoke())
    assert _tool_data(duplicate)["status"] == "already_read"
    assert _tool_data(commit)["status"] == "validation_failed"
    assert writes == []


def test_complete_read_of_another_branch_does_not_clear_the_block(
    unrestricted, monkeypatch
):
    """Regression: a small copy on one branch says nothing about a huge copy on
    another, so it must not unlock overwriting the file that was truncated."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    contents = iter(["x" * 500_000, "def a():\n    return 1\n"])
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: next(contents))

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            # truncated on the source branch, complete on a different branch
            await client.call_tool("read_file", {"branch": "develop", "path": "src/f.py"})
            await client.call_tool(
                "read_file", {"branch": "quant/SCRUM-107", "path": "src/f.py"}
            )
            return await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "src/f.py",
                    "content": "def a():\n    return 2\n",
                    "commit_message": "SCRUM-107: edit",
                },
            )

    result = _tool_data(asyncio.run(invoke()))
    assert result["status"] == "validation_failed"
    assert "truncated form" in " ".join(result["issues"])
    assert writes == []


def test_truncated_read_of_source_branch_blocks_commit_to_target(
    unrestricted, monkeypatch
):
    """The ordinary flow: read the source branch, commit to the target. Keying the
    block by branch would let this through."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "x" * 500_000)

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            await client.call_tool("read_file", {"branch": "develop", "path": "src/f.py"})
            return await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "src/f.py",
                    "content": "partial\n",
                    "commit_message": "SCRUM-107: edit",
                },
            )

    result = _tool_data(asyncio.run(invoke()))
    assert result["status"] == "validation_failed"
    assert writes == []


def test_reconstructed_notebook_json_after_reduced_read_is_blocked(
    unrestricted, monkeypatch
):
    """Regression: structural validity alone is not enough. A model that saw the
    reduced view can assemble parseable notebook JSON that drops every output."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: _notebook())

    # valid notebook JSON, rebuilt from the cell sources, with outputs gone
    rebuilt = json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "execution_count": None,
                    "source": ["def evolve(pop):\n", "    return pop\n"],
                    "outputs": [],
                    "metadata": {},
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            await client.call_tool(
                "read_file", {"branch": "develop", "path": "notebooks/GA.ipynb"}
            )
            return await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "notebooks/GA.ipynb",
                    "content": rebuilt,
                    "commit_message": "SCRUM-107: edit notebook",
                },
            )

    result = _tool_data(asyncio.run(invoke()))
    assert result["status"] == "validation_failed"
    assert "outputs or metadata" in " ".join(result["issues"])
    assert writes == []  # the outputs survive


def test_stripped_notebook_read_does_not_block_writing_elsewhere(
    unrestricted, monkeypatch
):
    """A notebook read is reduced but complete, so it must not gate other writes."""
    writes = []
    monkeypatch.setattr(
        server, "_push_strategy_code", lambda **kw: writes.append(kw) or "written"
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: _notebook())

    async def invoke():
        async with Client(server.create_mcp_server()) as client:
            read = await client.call_tool(
                "read_file", {"branch": "develop", "path": "notebooks/GA.ipynb"}
            )
            commit = await client.call_tool(
                "commit_and_push",
                {
                    "branch": "quant/SCRUM-107",
                    "path": "notebooks/operators.py",
                    "content": "def cs_rank(x):\n    return x\n",
                    "commit_message": "SCRUM-107: operators",
                },
            )
            return _tool_data(read), _tool_data(commit)

    read, commit = asyncio.run(invoke())
    assert read["downsized"]["kind"] == "notebook"
    assert commit["status"] == "success"
    assert len(writes) == 1


def test_notebook_guard_does_not_affect_other_files(unrestricted, monkeypatch):
    writes = []
    monkeypatch.setattr(
        server,
        "_push_strategy_code",
        lambda **kw: writes.append(kw) or "Updated src/m.py",
    )
    monkeypatch.setattr(server, "_get_strategy_code", lambda **kw: "old")

    result = _call(
        "commit_and_push",
        {
            "branch": "quant/SCRUM-107",
            "path": "src/m.py",
            "content": "def x():\n    return 1\n",
            "commit_message": "SCRUM-107: change",
        },
    )

    assert result["status"] == "success"
    assert len(writes) == 1
