"""Parity test: run both pipeline paths against equivalent payloads and
confirm the output structure is equivalent. Requires UCL network (for
LiteLLM) and a real GITHUB_TOKEN.

Run with:
    python test_pipeline_parity.py
"""

import json
import os
from pipeline import run_pipeline
from pipeline_mcp import run_pipeline_mcp
from github_client import delete_feature_branch

SCRIPTED_TICKET = "PARITY-SCRIPTED-1"
MCP_TICKET = "PARITY-MCP-1"
FEATURE_REQUEST = "add a simple volume filter — only take signals when volume is above its 20-day average"
STRATEGY_PATH = "rae_runtime/proxy/strategy.py"

def make_payload(ticket_id: str) -> dict:
    return {
        "run_id": f"{ticket_id}-001",
        "issue_key": ticket_id,
        "command": "backtest",
        "args": {"strategy": FEATURE_REQUEST},
        "strategy": {
            "repo_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop",
            "ref": "main",
            "path": STRATEGY_PATH,
        },
        "result_path": "/outputs/result.json",
    }


def check_structure(result: dict, label: str) -> bool:
    """Verify the result dict has the expected keys and values."""
    issues = []

    if result.get("status") != "succeeded":
        issues.append(f"status is '{result.get('status')}', expected 'succeeded'")

    if not result.get("issue_key"):
        issues.append("missing issue_key")

    artifacts = result.get("artifacts", {})
    if not artifacts.get("feature_branch"):
        issues.append("missing artifacts.feature_branch")
    if not artifacts.get("changed_file"):
        issues.append("missing artifacts.changed_file")

    if issues:
        print(f"[{label}] FAILED structure check:")
        for issue in issues:
            print(f"  - {issue}")
        return False

    print(f"[{label}] Structure check passed.")
    return True


def cleanup(ticket_ids: list[str]) -> None:
    for ticket_id in ticket_ids:
        branch = f"quant/{ticket_id}"
        try:
            delete_feature_branch(branch)
            print(f"[cleanup] Deleted {branch}")
        except Exception as e:
            print(f"[cleanup] Could not delete {branch}: {e}")


def main():
    print("=" * 60)
    print("Pipeline Parity Test")
    print("=" * 60)

    # --- Scripted path ---
    print(f"\n[1] Running SCRIPTED pipeline (ticket: {SCRIPTED_TICKET})...")
    scripted_result = run_pipeline(make_payload(SCRIPTED_TICKET))
    print(json.dumps(scripted_result, indent=2))
    scripted_ok = check_structure(scripted_result, "SCRIPTED")

    # --- MCP path ---
    print(f"\n[2] Running MCP pipeline (ticket: {MCP_TICKET})...")
    mcp_result = run_pipeline_mcp(make_payload(MCP_TICKET))
    print(json.dumps(mcp_result, indent=2))
    mcp_ok = check_structure(mcp_result, "MCP")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("PARITY SUMMARY")
    print("=" * 60)
    print(f"  Scripted path: {'PASS' if scripted_ok else 'FAIL'}")
    print(f"  MCP path:      {'PASS' if mcp_ok else 'FAIL'}")

    if scripted_ok and mcp_ok:
        print("\n  Both paths produced structurally equivalent results.")
        print("  Check GitHub to verify both branches contain real code changes.")
        print(f"  - quant/{SCRIPTED_TICKET}")
        print(f"  - quant/{MCP_TICKET}")
    else:
        print("\n  Parity check FAILED — do not remove the scripted path yet.")

    # --- Cleanup ---
    print("\n[3] Cleaning up test branches...")
    cleanup([SCRIPTED_TICKET, MCP_TICKET])


if __name__ == "__main__":
    main()