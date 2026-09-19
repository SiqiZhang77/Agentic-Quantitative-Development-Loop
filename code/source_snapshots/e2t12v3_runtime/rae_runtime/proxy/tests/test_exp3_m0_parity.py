from __future__ import annotations

import copy

from exp3.architecture import SINGLE_AGENT, resolve_architecture_mode
from prompt_context import build_code_prompt, build_mcp_code_prompt


def _payload():
    return {
        "issue_key": "SCRUM-390",
        "command": "refactor",
        "execution_objectives": {
            "strategy_type": "refactor",
            "parsed_task_parameters": {
                "objective": "Preserve the existing single-agent prompt bytes"
            },
        },
        "strategy": {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "path": "rae_runtime/proxy/budget_guard.py",
            "source_path": "rae_runtime/proxy/budget_guard.py",
            "target_path": "rae_runtime/proxy/budget_guard.py",
        },
        "jira_context": {
            "ticket_id": "SCRUM-390",
            "summary": "M0 compatibility fixture",
            "description": "The existing prompt must not change.",
            "triggering_comment": {
                "timestamp": "2026-08-25T09:00:00Z",
                "author": "Example User",
                "text": "/quant Preserve the current behaviour",
            },
            "events_history": [],
        },
    }


def test_explicit_m0_control_metadata_does_not_change_scripted_prompt_bytes():
    legacy = _payload()
    explicit = {**copy.deepcopy(legacy), "architecture_mode": SINGLE_AGENT}

    assert resolve_architecture_mode(legacy) == SINGLE_AGENT
    assert resolve_architecture_mode(explicit) == SINGLE_AGENT
    assert build_code_prompt(legacy, "class BudgetGuard: pass\n") == build_code_prompt(
        explicit, "class BudgetGuard: pass\n"
    )


def test_explicit_m0_control_metadata_does_not_change_mcp_prompt_bytes():
    legacy = _payload()
    explicit = {**copy.deepcopy(legacy), "architecture_mode": SINGLE_AGENT}

    assert build_mcp_code_prompt(
        legacy,
        read_ref="frozen-source",
        branch_name="quant/SCRUM-390",
    ) == build_mcp_code_prompt(
        explicit,
        read_ref="frozen-source",
        branch_name="quant/SCRUM-390",
    )
