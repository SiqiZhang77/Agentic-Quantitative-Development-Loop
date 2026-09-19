import json
import os
import logging
from dotenv import load_dotenv
from github_client import (
    NoChangesError,
    get_strategy_code,
    get_branch_sha,
    push_strategy_code,
    create_feature_branch,
    create_or_reuse_branch_details,
)
from llm_client import call_llm
from budget_guard import BudgetGuard
from artifact_validation import inspect_committed_artifacts, proposed_artifact_issues
from prompt_context import build_code_prompt, mark_retrieval_prompt_delivery

log = logging.getLogger("rae.pipeline")  # run.py configures the root logger to stderr

# The scripted path sends a whole file and commits the whole reply, so its input
# cannot be reduced without losing content. Refuse above this size instead.
MAX_SCRIPTED_SOURCE_CHARS = 120_000

load_dotenv()


def run_pipeline(payload: dict, tracer=None):
    ticket_id = payload["issue_key"]
    strategy_ref = payload["strategy"]["ref"]
    repo_name = payload["strategy"].get("repo_full_name") or "bankingscience/BSLAgenticQuantDevLoop"
    strategy = payload["strategy"]
    source_path = strategy.get("source_path") or strategy["path"]
    target_path = strategy.get("target_path") or source_path
    branch_name = payload["strategy"].get("target_branch") or f"quant/{ticket_id}"
    repository_branches = prepare_repository_branches(payload, tracer=tracer)

    def _step(agent, tool, fn, *args, **kwargs):
        """Run a step, record a trace entry, and re-raise on failure."""
        try:
            out = fn(*args, **kwargs)
            if tracer:
                tracer.record(agent, tool, "succeeded", f"{tool} ok")
            return out
        except NoChangesError as e:
            if tracer:
                tracer.record(agent, tool, "skipped", f"{tool}: {e}")
            raise
        except Exception as e:
            if tracer:
                tracer.record(agent, tool, "failed", f"{tool}: {e}")
            raise

    # Step 1 - ensure the feature branch exists.
    log.info("[1] Ensuring branch %s exists...", branch_name)
    branch_action = "created"
    try:
        branch_result = _step(
            "planner_agent",
            "create_feature_branch",
            create_feature_branch,
            branch_name=branch_name,
            base_branch=strategy_ref,
            repo_name=repo_name,
        )
        if "reused" in branch_result.lower():
            branch_action = "reused"
            log.info("    Reused existing branch.")
        else:
            log.info("    Created.")
    except Exception as e:
        if "already exists" in str(e).lower():
            branch_action = "reused"
            branch_result = f"branch {branch_name} already exists"
            log.info("    Already exists (created by DAG) - continuing.")
            if tracer:
                tracer.record(
                    "planner_agent",
                    "create_feature_branch",
                    "skipped",
                    "branch already exists (created by DAG)",
                )
        else:
            raise

    # Step 2 - fetch current strategy code
    log.info("[2] Fetching strategy code from GitHub...")
    current_code = _step(
        "planner_agent",
        "get_strategy_code",
        get_strategy_code,
        branch=strategy_ref,
        path=source_path,
        repo_name=repo_name,
    )
    log.info("    Done.")

    # This path rewrites the whole file from the model's reply, so the model must
    # see all of it. Downsizing the input here would commit a file rebuilt from a
    # reduced view and silently drop the rest, so refuse instead. The MCP pipeline
    # reads through tools and edits in place, and has no such constraint.
    if len(current_code) > MAX_SCRIPTED_SOURCE_CHARS:
        raise RuntimeError(
            f"{source_path} is {len(current_code)} characters, over the "
            f"{MAX_SCRIPTED_SOURCE_CHARS} limit for the scripted pipeline, which "
            "rewrites the whole file. Run with USE_MCP_GITHUB=true, or target a "
            "smaller file."
        )

    # Step 3 - build prompt and call Claude
    log.info("[3] Sending to Claude via LiteLLM...")
    prompt = build_code_prompt(payload, current_code)
    guard = BudgetGuard(
        max_token_budget=(payload.get("iteration_controls") or {}).get("max_token_budget"),
        timeout_seconds=(payload.get("iteration_controls") or {}).get("timeout_seconds"),
        max_iterations=(payload.get("iteration_controls") or {}).get("max_iterations"),
    )
    # Record only hashes/IDs at the exact coding-model call boundary. The raw
    # evidence remains solely inside the prompt and is never added to telemetry.
    mark_retrieval_prompt_delivery(payload)
    modified_code = _step("coder_agent", "call_llm", call_llm, prompt, budget_guard=guard)
    log.info("    Done. Claude returned %d characters.", len(modified_code))

    # Step 4 - validate generated content before any GitHub write.
    log.info("[4] Validating generated content before commit...")
    validation_issues = proposed_artifact_issues(
        path=target_path,
        content=modified_code,
        source_branch=strategy_ref,
        target_branch=branch_name,
        repo_name=repo_name,
        reader=get_strategy_code,
    )
    if validation_issues:
        commit_result = "Pre-commit validation failed: " + " ".join(validation_issues)
        commit_action = "validation_failed"
        if tracer:
            tracer.record(
                "coder_agent", "validate_content", "failed", str(commit_result)
            )
        log.info("    Commit blocked by deterministic validation.")
    else:
        if tracer:
            tracer.record(
                "coder_agent", "validate_content", "succeeded", f"validated {target_path}"
            )
        log.info("[5] Pushing validated content to GitHub...")
        try:
            commit_result = _step(
                "coder_agent",
                "push_strategy_code",
                push_strategy_code,
                code=modified_code,
                branch=branch_name,
                path=target_path,
                commit_message=f"{ticket_id}: implement Jira ticket requirements",
                repo_name=repo_name,
            )
        except NoChangesError as e:
            commit_result = str(e)
            commit_action = "skipped"
            log.info("    No code changes required.")
        else:
            commit_action = (
                "skipped" if "no changes" in str(commit_result).lower() else "committed"
            )
    log.info("    Done. (%s)", commit_action)
    no_changes = commit_action != "committed"
    modified_files: list[str] = []
    new_files: list[str] = []
    if not no_changes:
        required_target = (
            target_path
            if strategy.get("target_path_explicit") or target_path != source_path
            else None
        )
        modified_files, new_files, validation_issues = inspect_committed_artifacts(
            committed_paths=[target_path],
            source_branch=strategy_ref,
            target_branch=branch_name,
            repo_name=repo_name,
            reader=get_strategy_code,
            required_target_path=required_target,
        )

    # Step 5 - return structured result for run.py / the response builder
    return {
        "status": "no_changes" if no_changes else "succeeded",
        "issue_key": ticket_id,
        "summary": str(commit_result) if no_changes else None,
        "artifacts": {
            "feature_branch": branch_name,
            **({} if no_changes else {"changed_file": target_path}),
            "modified_files": modified_files,
            "new_files": new_files,
            **({"validation_issues": validation_issues} if validation_issues else {}),
            "no_code_changes": no_changes,
            "repository_branches": mark_primary_repository_files(
                repository_branches,
                repo_name,
                branch_name,
                modified_files,
                new_files,
                refresh_commit_sha=not no_changes,
            ),
        },
        "usage": guard.usage_dict(),
        "diagnostics": {
            "retry_safe": {
                "ticket_id": ticket_id,
                "run_id": payload.get("run_id", ticket_id),
                "branch": {
                    "name": branch_name,
                    "action": branch_action,
                    "message": branch_result,
                },
                "commit": {
                    "action": commit_action,
                    "message": str(commit_result),
                    "changed": commit_action == "committed",
                },
            },
        },
    }


def prepare_repository_branches(payload: dict, tracer=None) -> list[dict]:
    branches = []
    for repo in payload.get("repositories") or []:
        if not isinstance(repo, dict):
            continue
        repo_name = repo.get("repo_full_name")
        target_branch = repo.get("target_branch")
        source_branch = repo.get("source_branch") or "main"
        if not repo_name or not target_branch:
            continue
        try:
            details = create_or_reuse_branch_details(
                repo_name=repo_name,
                branch_name=target_branch,
                base_branch=source_branch,
            )
            if tracer:
                tracer.record(
                    "planner_agent",
                    "create_feature_branch",
                    "succeeded",
                    f"{repo_name}:{target_branch} {details.get('branch_action')}",
                )
            branches.append(
                {
                    "alias": repo.get("alias") or "current",
                    "repo_full_name": repo_name,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "branch_action": details.get("branch_action") or "unknown",
                    "commit_sha": details.get("commit_sha"),
                    "modified_files": [],
                    "new_files": [],
                }
            )
        except Exception as e:
            if tracer:
                tracer.record(
                    "planner_agent",
                    "create_feature_branch",
                    "failed",
                    f"{repo_name}:{target_branch}: {e}",
                )
            raise
    return branches


def mark_primary_repository_files(
    repository_branches: list[dict],
    repo_name: str,
    branch_name: str,
    modified_files: list[str],
    new_files: list[str],
    *,
    refresh_commit_sha: bool = False,
) -> list[dict]:
    marked = []
    found = False
    for item in repository_branches:
        record = dict(item)
        if (
            record.get("repo_full_name") == repo_name
            and record.get("target_branch") == branch_name
        ):
            record["modified_files"] = modified_files
            record["new_files"] = new_files
            if refresh_commit_sha:
                record["commit_sha"] = get_branch_sha(
                    branch_name, repo_name=repo_name
                )
            found = True
        marked.append(record)
    if not found:
        marked.append(
            {
                "alias": repo_name.split("/", 1)[-1],
                "repo_full_name": repo_name,
                "source_branch": "main",
                "target_branch": branch_name,
                "branch_action": "unknown",
                "commit_sha": (
                    get_branch_sha(branch_name, repo_name=repo_name)
                    if refresh_commit_sha
                    else None
                ),
                "modified_files": modified_files,
                "new_files": new_files,
            }
        )
    return marked



def run_pipeline_auto(payload: dict, tracer=None, exp3_provider_boundary=None) -> dict:
    # MCP path requires network access — fall back to scripted path in offline mode
    offline = os.getenv("RAE_OFFLINE") == "1"
    use_mcp = os.getenv("USE_MCP_GITHUB", "true").lower() == "true"
    if "architecture_mode" in payload:
        from exp3.architecture import SINGLE_AGENT, validate_explicit_e3_request
        from exp3.production_provider import ProductionProviderBoundaryUnavailable

        if validate_explicit_e3_request(payload) != SINGLE_AGENT:
            raise ProductionProviderBoundaryUnavailable(
                "manager_star cannot enter the single-agent pipeline"
            )
        if exp3_provider_boundary is None:
            raise ProductionProviderBoundaryUnavailable(
                "explicit E3 M0 requires the production provider boundary"
            )
        if offline or not use_mcp:
            raise ProductionProviderBoundaryUnavailable(
                "explicit E3 M0 forbids the scripted provider path"
            )
    elif exp3_provider_boundary is not None:
        from exp3.production_provider import ProductionProviderBoundaryError

        raise ProductionProviderBoundaryError(
            "an E3 provider boundary cannot be attached to a legacy request"
        )
    repositories = [
        repo for repo in payload.get("repositories") or [] if isinstance(repo, dict)
    ]

    if len(repositories) > 1 and (offline or not use_mcp):
        raise RuntimeError(
            "Multi-repository editing requires USE_MCP_GITHUB=true and an online "
            "runtime; the scripted pipeline supports only one repository."
        )

    # Discovery (a request naming no file) needs the MCP agent's list/read tools.
    # Offline runs are exempt: they read the bundled demo file regardless of path.
    source_path = (payload.get("strategy") or {}).get("source_path") or (
        payload.get("strategy") or {}
    ).get("path")
    if not source_path and not use_mcp and not offline:
        raise RuntimeError(
            "Resource discovery (a request naming no file) requires "
            "USE_MCP_GITHUB=true; the online scripted pipeline needs an explicit "
            "resource_path."
        )

    if use_mcp and not offline:
        log.info("[pipeline] USE_MCP_GITHUB=true — using MCP-driven pipeline")
        from pipeline_mcp import run_pipeline_mcp

        return run_pipeline_mcp(
            payload,
            tracer=tracer,
            exp3_provider_boundary=exp3_provider_boundary,
        )
    else:
        if offline:
            log.info(
                "[pipeline] RAE_OFFLINE=1 — forcing scripted pipeline (MCP requires network)"
            )
        else:
            log.info("[pipeline] USE_MCP_GITHUB=false — using scripted pipeline")
        return run_pipeline(payload, tracer=tracer)


if __name__ == "__main__":
    # for local testing - simulate what Airflow would inject
    logging.basicConfig(level=logging.INFO)  # so log.info shows when run directly
    test_payload = {
        "run_id": "ALPHA-101-20001",
        "issue_key": "ALPHA-101",
        "command": "backtest",
        "args": {
            "strategy": "add RSI filter so we only take long signals when RSI is below 70"
        },
        "strategy": {
            "repo_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop",
            "ref": "proxy/litellm-client",
            "path": "rae_runtime/proxy/strategy.py",
        },
        "result_path": "/outputs/result.json",
    }

    result = run_pipeline_auto(test_payload)
    print(f"\nResult: {json.dumps(result, indent=2)}")
