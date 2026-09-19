"""No-network unit tests for pipeline_mcp.py's MCP tool-output extraction.

Fakes the shape of an Agents SDK RunResult (new_items with ToolCallItem /
ToolCallOutputItem pairs matched by call_id) and checks that
_extract_commit_diagnostics parses it correctly. This is the fastest way to
catch a wrong attribute name before running the real (network-bound) MCP
path.

NOTE: the attribute names being asserted here (new_items, .call_id,
.tool_name, .output) are pipeline_mcp.py's best-effort understanding of the
openai-agents SDK's RunItem shape - see the docstring on
_extract_commit_diagnostics. If the real SDK shape differs, these fakes
won't catch it; they only prove our parsing logic is correct against the
shape we believe is real.
"""

import json
from types import SimpleNamespace

import pytest

from pipeline_mcp import (
    MCPWritebackError,
    _attach_post_model_pipeline_evidence,
    _extract_commit_diagnostics,
    _has_nonvalidation_commit_failure,
    _missing_commit_call,
    _precommit_validation_issues,
    _raise_if_mcp_write_failed,
)


class ToolCallItem:
    def __init__(self, call_id, tool_name):
        self.call_id = call_id
        self.tool_name = tool_name


class ToolCallOutputItem:
    def __init__(self, call_id, output):
        self.call_id = call_id
        self.output = output


def _fake_call(call_id, tool_name, output):
    """Build the ToolCallItem/ToolCallOutputItem pair for one MCP tool call."""
    return [
        ToolCallItem(call_id=call_id, tool_name=tool_name),
        ToolCallOutputItem(
            call_id=call_id,
            output={"type": "text", "text": json.dumps(output)},
        ),
    ]


def test_extract_commit_diagnostics_detects_skipped_commit():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "create_or_reuse_branch",
                {"status": "reused", "message": "Branch already exists."},
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {
                    "status": "success",
                    "message": "No changes for strategy.py on quant/X; skipped commit",
                },
            ),
        ],
    )
    diag = _extract_commit_diagnostics(result, "quant/X")
    assert diag["branch"]["action"] == "reused"
    assert diag["commit"]["action"] == "skipped"
    assert diag["commit"]["changed"] is False


def test_extract_commit_diagnostics_detects_real_commit():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "create_or_reuse_branch",
                {"status": "created", "message": "Created branch quant/X from main"},
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {"status": "success", "message": "Updated strategy.py on quant/X"},
            ),
        ],
    )
    diag = _extract_commit_diagnostics(result, "quant/X")
    assert diag["branch"]["action"] == "created"
    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["changed"] is True


def test_extract_commit_diagnostics_accepts_server_side_exact_replacement():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "replace_in_file",
                {
                    "status": "success",
                    "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
                    "branch": "quant/X",
                    "path": "proxy/large.py",
                    "message": "Applied one exact server-side replacement and committed it.",
                },
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["paths"] == ["proxy/large.py"]
    assert _missing_commit_call(diag) is False


def test_extract_commit_diagnostics_records_every_committed_path():
    # A real refactor touches more than the one file the ticket named.
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call("call_1", "create_or_reuse_branch", {"status": "created"}),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {"status": "success", "path": "proxy/a.py", "message": "Updated a.py"},
            ),
            *_fake_call(
                "call_3",
                "commit_and_push",
                {"status": "success", "path": "proxy/b.py", "message": "Updated b.py"},
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["paths"] == ["proxy/a.py", "proxy/b.py"]
    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["changed"] is True


def test_extract_commit_diagnostics_keeps_same_path_in_different_repositories():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {
                    "status": "success",
                    "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
                    "branch": "quant/X",
                    "path": "README.md",
                    "message": "Updated primary README",
                },
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {
                    "status": "success",
                    "repo_name": "bankingscience/ATPDataHandlersRepo",
                    "branch": "quant/X",
                    "path": "README.md",
                    "message": "Updated secondary README",
                },
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["artifacts"] == [
        {
            "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
            "branch": "quant/X",
            "path": "README.md",
        },
        {
            "repo_name": "bankingscience/ATPDataHandlersRepo",
            "branch": "quant/X",
            "path": "README.md",
        },
    ]


def test_a_trailing_no_op_does_not_erase_an_earlier_real_commit():
    # The verdict is the aggregate: the branch still changed.
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {"status": "success", "path": "proxy/a.py", "message": "Updated a.py"},
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {"status": "no_changes", "path": "proxy/b.py", "message": "No changes"},
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["changed"] is True
    assert diag["commit"]["paths"] == ["proxy/a.py"]  # only the file that changed


def test_a_failed_later_commit_is_not_hidden_by_an_earlier_success():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {"status": "success", "path": "proxy/a.py", "message": "Updated a.py"},
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {"status": "failed", "error": "push failed for b.py"},
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["action"] == "failed"
    assert diag["commit"]["changed"] is True  # records that the branch is partial
    assert diag["commit"]["paths"] == ["proxy/a.py"]
    assert diag["commit"]["failures"][0]["status"] == "failed"
    with pytest.raises(MCPWritebackError, match="push failed for b.py"):
        _raise_if_mcp_write_failed(diag)


def test_successful_retry_resolves_prior_precommit_rejection_for_same_path():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {
                    "status": "validation_failed",
                    "path": "README.md",
                    "issues": ["README artifact README.md is too short."],
                    "error": "proposed content failed validation",
                },
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {
                    "status": "success",
                    "path": "README.md",
                    "message": "Updated README.md",
                },
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/SCRUM-115")

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["failures"] == []
    assert _precommit_validation_issues(diag) == []


def test_unresolved_precommit_rejection_is_available_for_outer_iteration():
    result = SimpleNamespace(
        final_output="could not complete",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {
                    "status": "validation_failed",
                    "path": "README.md",
                    "issues": ["README artifact README.md is too short."],
                    "error": "proposed content failed validation",
                },
            )
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/SCRUM-115")

    assert diag["commit"]["action"] == "failed"
    assert _precommit_validation_issues(diag) == [
        "README artifact README.md is too short."
    ]
    assert _has_nonvalidation_commit_failure(diag) is False


def test_validate_content_rejection_is_preserved_without_a_commit_attempt():
    result = SimpleNamespace(
        final_output="could not complete",
        new_items=[
            *_fake_call(
                "call_1",
                "validate_content",
                {
                    "status": "validation_failed",
                    "path": "README.md",
                    "issues": ["README artifact README.md has no Markdown heading."],
                    "error": "proposed content failed validation",
                },
            )
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/SCRUM-115")

    assert _precommit_validation_issues(diag) == [
        "README artifact README.md has no Markdown heading."
    ]


def test_real_write_failure_is_not_masked_by_validation_feedback():
    result = SimpleNamespace(
        final_output="failed",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {
                    "status": "validation_failed",
                    "path": "README.md",
                    "issues": ["README artifact README.md is too short."],
                },
            ),
            *_fake_call(
                "call_2",
                "commit_and_push",
                {
                    "status": "failed",
                    "path": "src/example.py",
                    "error": "GitHub API unavailable",
                },
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/SCRUM-115")

    assert _has_nonvalidation_commit_failure(diag) is True
    with pytest.raises(MCPWritebackError, match="GitHub API unavailable"):
        _raise_if_mcp_write_failed(diag)


def test_no_op_commits_record_no_paths():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {"status": "no_changes", "path": "proxy/a.py", "message": "No changes"},
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["paths"] == []
    assert diag["commit"]["action"] == "skipped"


def test_missing_commit_call_is_distinct_from_a_failed_or_no_op_write():
    missing = {
        "tool_calls": [
            {"tool": "list_files", "status": "success"},
            {"tool": "read_file", "status": "success"},
        ]
    }
    attempted = {
        "tool_calls": [{"tool": "commit_and_push", "status": "failed"}]
    }

    assert _missing_commit_call(missing) is True
    assert _missing_commit_call(attempted) is False


def test_blocked_out_of_scope_commit_records_no_path():
    # The server refused the write, so nothing was committed.
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {"status": "blocked", "error": "Path '/etc/passwd' is outside..."},
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["paths"] == []
    assert diag["commit"]["changed"] is False


def test_extract_commit_diagnostics_falls_back_safely_on_unknown_shape():
    result = SimpleNamespace(final_output="done")  # no new_items at all
    diag = _extract_commit_diagnostics(result, "quant/X")
    assert diag["branch"]["action"] == "unknown"
    assert diag["commit"]["action"] == "unknown"
    assert diag["commit"]["changed"] is False
    assert diag["tool_calls"] == []


def test_extract_commit_diagnostics_accepts_json_string_output():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            ToolCallItem(call_id="call_1", tool_name="commit_and_push"),
            ToolCallOutputItem(
                call_id="call_1",
                output=json.dumps(
                    {
                        "status": "success",
                        "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
                        "branch": "quant/X",
                        "path": "src/example.py",
                    }
                ),
            ),
        ],
    )

    diag = _extract_commit_diagnostics(result, "quant/X")

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["paths"] == ["src/example.py"]


def test_backend_audit_repairs_an_unparsed_sdk_wrapper():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            ToolCallItem(call_id="call_1", tool_name="commit_and_push"),
            ToolCallOutputItem(call_id="call_1", output=object()),
        ],
    )
    audit = [
        {
            "tool": "commit_and_push",
            "status": "success",
            "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
            "branch": "quant/X",
            "path": "src/example.py",
        }
    ]

    diag = _extract_commit_diagnostics(result, "quant/X", tool_audit=audit)

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["paths"] == ["src/example.py"]


def test_unaudited_unparsed_duplicate_does_not_overwrite_verified_commit():
    result = SimpleNamespace(
        final_output="done",
        new_items=[
            *_fake_call(
                "call_1",
                "commit_and_push",
                {
                    "status": "success",
                    "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
                    "branch": "quant/X",
                    "path": "src/example.py",
                },
            ),
            ToolCallItem(call_id="call_2", tool_name="commit_and_push"),
            ToolCallOutputItem(call_id="call_2", output=object()),
        ],
    )
    audit = [
        {
            "tool": "commit_and_push",
            "status": "success",
            "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
            "branch": "quant/X",
            "path": "src/example.py",
        }
    ]

    diag = _extract_commit_diagnostics(result, "quant/X", tool_audit=audit)

    assert diag["commit"]["action"] == "committed"
    assert diag["commit"]["changed"] is True
    assert diag["commit"]["unverified_calls"] == [
        {
            "call_id": "call_2",
            "status": "unverified",
            "message": "SDK item had no matching backend write record",
        }
    ]
    _raise_if_mcp_write_failed(diag)


def test_backend_audit_failure_still_fails_closed():
    result = SimpleNamespace(
        final_output="failed",
        new_items=[
            ToolCallItem(call_id="call_1", tool_name="commit_and_push"),
            ToolCallOutputItem(call_id="call_1", output=object()),
        ],
    )
    audit = [
        {
            "tool": "commit_and_push",
            "status": "failed",
            "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
            "branch": "quant/X",
            "path": "src/example.py",
            "error": "push failed",
        }
    ]

    diag = _extract_commit_diagnostics(result, "quant/X", tool_audit=audit)

    with pytest.raises(MCPWritebackError, match="push failed"):
        _raise_if_mcp_write_failed(diag)


def test_post_model_failure_keeps_usage_and_commit_evidence():
    error = RuntimeError("later bookkeeping failed")
    retry_safe = {
        "commit": {"paths": ["src/example.py"]},
        "tool_calls": [{"tool": "commit_and_push", "status": "success"}],
    }
    usage = {
        "model": "gpt-5.6-terra",
        "calls": 6,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": None,
    }

    _attach_post_model_pipeline_evidence(
        error,
        usage=usage,
        retry_safe=retry_safe,
        branch_name="quant/X",
    )

    assert error.pipeline_out["usage"] == usage
    assert error.pipeline_out["artifacts"]["modified_files"] == ["src/example.py"]
    assert error.pipeline_out["diagnostics"]["retry_safe"] == retry_safe
