"""RAE-only. Maps the agent pipeline + MCP backtest outputs onto IW's
runtime_response.schema.json. Never redefines the shape (contract.py validates it).
Defensive: normalises legacy percent-string metrics and drops non-conformant paths,
so it produces a schema-valid object even if upstream tools are mid-migration.
"""

from __future__ import annotations
import copy
import re
import sys
from trace import now_iso
from datetime import datetime
from pathlib import Path

# outcome key -> (status, error_code).  status/error_code are IW's enums.
_OUTCOME = {
    "success": ("succeeded", None),
    "timeout": ("timeout", "TIMEOUT_REACHED"),
    "compile": ("failed", "STRATEGY_COMPILE_ERROR"),
    "rate_limit": ("failed", "RATE_LIMIT_EXCEEDED"),
    "bad_payload": ("failed", "PAYLOAD_VALIDATION_ERROR"),
    "quality": ("failed", "QUALITY_VALIDATION_FAILED"),
    "agent_turn_limit": ("failed", "AGENT_TURN_LIMIT_EXCEEDED"),
    "runtime": ("failed", "RUNTIME_ERROR"),
    "unknown": ("failed", "UNKNOWN_ERROR"),
}

_TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
_OUTPUT_RE = re.compile(r"^/workspace/output/(?!.*\.\.)[A-Za-z0-9._/-]+$")
_REL_RE = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]+$")
FALLBACK_TICKET = "unknown"


def _num(v):
    """Backtester may emit floats (0.45) or legacy percent strings ('18.4%'). -> float."""
    if v is None or isinstance(v, (int, float)):
        return float(v) if v is not None else None
    s = str(v).strip()
    pct = s.endswith("%")
    try:
        f = float(s.rstrip("%").strip())
    except ValueError:
        return None
    return f / 100.0 if pct else f


def _output_path(p):
    if isinstance(p, str) and _OUTPUT_RE.match(p):
        return p
    if p:
        print(f"warn: dropping non-conformant output path: {p!r}", file=sys.stderr)
    return None


def _existing_output_path(p):
    path = _output_path(p)
    return path if path and Path(path).is_file() else None


def _rel_paths(paths):
    out = []
    for p in paths or []:
        if isinstance(p, str) and _REL_RE.match(p):
            out.append(p)
        elif p:
            print(f"warn: dropping non-relative file path: {p!r}", file=sys.stderr)
    return sorted(set(out))  # uniqueItems


def _repository_branches(items):
    out = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        record = {
            "alias": str(item.get("alias") or "current"),
            "repo_full_name": str(item.get("repo_full_name") or ""),
            "source_branch": str(item.get("source_branch") or "main"),
            "target_branch": str(item.get("target_branch") or item.get("branch_name") or ""),
        }
        if not record["repo_full_name"] or not record["target_branch"]:
            continue
        action = item.get("branch_action") or item.get("action")
        if action in {"created", "reused", "unknown", "skipped", "failed"}:
            record["branch_action"] = action
        sha = item.get("commit_sha")
        if isinstance(sha, str) and re.fullmatch(r"[A-Fa-f0-9]{7,40}", sha):
            record["commit_sha"] = sha
        elif sha is None:
            record["commit_sha"] = None
        record["modified_files"] = _rel_paths(item.get("modified_files"))
        record["new_files"] = _rel_paths(item.get("new_files"))
        record["existing_files"] = _rel_paths(item.get("existing_files"))
        out.append(record)
    return out


def _metrics(backtest, ts_path):
    m = (backtest or {}).get("metrics")
    if m is None:
        return None
    return {
        "total_return": _num(m.get("total_return")),
        "sharpe_ratio": _num(m.get("sharpe_ratio", m.get("sharpe"))),
        "max_drawdown": _num(m.get("max_drawdown")),
        "alpha": _num(m.get("alpha")),  # no MCP producer yet -> null (interim)
        "beta": _num(m.get("beta")),  # no MCP producer yet -> null (interim)
        "time_series_data_path": ts_path,
    }


def _retrieval_audit(context, delivery=None):
    """Return experiment provenance without copying query or Jira evidence text."""

    if not isinstance(context, dict) or context.get("enabled") is not True:
        return {
            "enabled": False,
            "delivery_mode": "disabled",
            "prompt_injected": False,
        }
    memories = []
    for item in context.get("memories") or []:
        if not isinstance(item, dict):
            continue
        memories.append(
            {
                "memory_id": str(item.get("memory_id") or ""),
                "rank": item.get("rank"),
                "score": item.get("score"),
            }
        )
    audit = {
        "enabled": True,
        "delivery_mode": "shadow",
        "prompt_injected": False,
        "status": context.get("status"),
        "query_sha256": context.get("query_sha256"),
        "retriever_name": context.get("retriever_name"),
        "retriever_version": context.get("retriever_version"),
        "corpus_id": context.get("corpus_id"),
        "corpus_sha256": context.get("corpus_sha256"),
        "index_id": context.get("index_id"),
        "index_sha256": context.get("index_sha256"),
        "exclusion_list_id": context.get("exclusion_list_id"),
        "exclusion_list_sha256": context.get("exclusion_list_sha256"),
        "cutoff_at": context.get("cutoff_at"),
        "requested_top_k": context.get("requested_top_k"),
        "max_memories_per_source_ticket": context.get(
            "max_memories_per_source_ticket"
        ),
        "candidate_count": context.get("candidate_count"),
        "memories": memories,
    }
    retrieved_ids = {item["memory_id"] for item in memories if item["memory_id"]}
    if isinstance(delivery, dict):
        evidence_hash = delivery.get("evidence_text_sha256")
        template_hash = delivery.get("prompt_template_sha256")
        injected_ids = delivery.get("injected_memory_ids")
        hashes_valid = all(
            isinstance(value, str) and re.fullmatch(r"[A-Fa-f0-9]{64}", value)
            for value in (evidence_hash, template_hash)
        )
        ids_valid = (
            isinstance(injected_ids, list)
            and len(injected_ids) == len(set(injected_ids))
            and all(
                isinstance(value, str) and value in retrieved_ids
                for value in injected_ids
            )
            and (not memories or bool(injected_ids))
        )
        if (
            delivery.get("delivery_mode") == "generator_prompt"
            and delivery.get("prompt_injected") is True
            and hashes_valid
            and ids_valid
        ):
            audit.update(
                {
                    "delivery_mode": "generator_prompt",
                    "prompt_injected": True,
                    "evidence_text_sha256": evidence_hash,
                    "prompt_template_sha256": template_hash,
                    "injected_memory_ids": injected_ids,
                }
            )
    return audit


def _legacy_model_provider(usage, experiment_telemetry) -> str:
    """Map the versioned provider identity into the legacy usage label.

    Old non-experiment callers have no explicit identity and historically used
    the LiteLLM label, so retain that default for M0 compatibility. Explicit
    OpenAI runs must never be reported as LiteLLM.
    """

    explicit_mode = usage.get("provider_mode") if isinstance(usage, dict) else None
    identity = (
        experiment_telemetry.get("provider_identity")
        if isinstance(experiment_telemetry, dict)
        else None
    )
    mode = explicit_mode or (
        identity.get("provider_mode") if isinstance(identity, dict) else None
    )
    return "openai" if mode == "openai" else "litellm"


def build_response(
    *,
    run_id,
    ticket_id,
    start_time,
    end_time,
    outcome,
    traces,
    backtest=None,
    pipeline_out=None,
    error_message=None,
    log_ref=None,
    stage_timings=None,
    command=None,
    input_datasets=None,
    retrieval_context=None,
    retrieval_delivery=None,
    experiment_telemetry=None,
) -> dict:
    status, code = _OUTCOME.get(outcome, _OUTCOME["unknown"])
    ts_path = _output_path(
        (backtest or {}).get("time_series_data_path")
        or (backtest or {}).get("equity_curve")
    )
    plots = _output_path((backtest or {}).get("backtest_plots_path"))
    # The exact .request submitted to the engine. Referenced here so the
    # orchestrator uploads it before deleting the temporary output mount —
    # otherwise the config behind these metrics is unrecoverable after the run.
    executed_request = _output_path((backtest or {}).get("executed_request_path"))
    arts = (pipeline_out or {}).get("artifacts", {}) or {}
    no_code_changes = (
        (pipeline_out or {}).get("status") == "no_changes"
        or arts.get("no_code_changes") is True
    )
    modified = arts.get("modified_files")
    if no_code_changes:
        modified = []
    elif modified is None and arts.get("changed_file"):
        modified = [arts["changed_file"]]
    new_files = [] if no_code_changes else arts.get("new_files")

    usage = (pipeline_out or {}).get("usage") or {}
    retry_safe = ((pipeline_out or {}).get("diagnostics") or {}).get("retry_safe")
    repository_branches = _repository_branches(arts.get("repository_branches"))
    branch_name = arts.get("feature_branch") or arts.get("branch_name")
    if repository_branches and not branch_name:
        branch_name = repository_branches[0].get("target_branch")

    model_usage = []
    if usage.get("model"):
        model_usage.append(
            {
                "model_name": usage["model"],
                "provider": _legacy_model_provider(usage, experiment_telemetry),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        )
    experiment_shared = (
        experiment_telemetry.get("shared_budget")
        if isinstance(experiment_telemetry, dict)
        else None
    )
    if not model_usage and isinstance(experiment_shared, dict):
        total_tokens = int(experiment_shared.get("total_tokens") or 0)
        if total_tokens > 0:
            model_usage.append(
                {
                    "model_name": experiment_telemetry.get("model_alias"),
                    "provider": _legacy_model_provider({}, experiment_telemetry),
                    "prompt_tokens": int(
                        experiment_shared.get("prompt_tokens") or 0
                    ),
                    "completion_tokens": int(
                        experiment_shared.get("completion_tokens") or 0
                    ),
                    "total_tokens": total_tokens,
                }
            )
    retry_count = int(usage.get("calls") or 0)
    if retry_count == 0 and isinstance(experiment_shared, dict):
        retry_count = int(experiment_shared.get("calls") or 0)

    exec_seconds = None
    try:
        t0 = datetime.fromisoformat(start_time)
        t1 = datetime.fromisoformat(end_time)
        exec_seconds = round((t1 - t0).total_seconds(), 3)
    except Exception:
        pass  # Not critical

    analysis_results = (backtest or {}).get("analysis_results")
    if not isinstance(analysis_results, dict):
        analysis_results = None
    evaluation = (backtest or {}).get("evaluation") or {
        "met_criteria": None,
        "confidence": 0.0,
        "criteria_results": [],
        "unrecognised_criteria": [],
        "summary": "No evaluation available for this run.",
    }
    recommended_action = (backtest or {}).get("recommended_action") or (
        "review" if status == "succeeded" else "escalate"
    )
    execution_summary = {
        "ticket_id": (
            ticket_id
            if isinstance(ticket_id, str) and _TICKET_RE.fullmatch(ticket_id)
            else FALLBACK_TICKET
        ),
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "iteration_traces": traces or [],
    }
    if no_code_changes:
        execution_summary["zero_code_modifications"] = True
    if isinstance(command, str) and command.strip():
        # Lets Jira write-back (and any downstream consumer) tell a request that
        # never runs a backtest apart from a backtest request whose performance_metrics
        # is null because it failed before producing metrics.
        execution_summary["request_type"] = command.strip()

    response = {
        "schema_version": "1.0",
        "run_id": run_id or "unknown",
        "execution_summary": execution_summary,
        "performance_metrics": _metrics(backtest, ts_path),
        "generated_artifacts": {
            "modified_files": _rel_paths(modified),
            "new_files": _rel_paths(new_files),
            "backtest_plots_path": plots,
            "executed_request_path": executed_request,
            "branch_name": branch_name if isinstance(branch_name, str) else None,
            **({"repository_branches": repository_branches} if repository_branches else {}),
        },
        "diagnostics": {
            "error_code": code,
            "error_message": error_message,
            "raw_log_reference": _existing_output_path(log_ref),
        },
        "telemetry": {
            "execution_time_seconds": exec_seconds,
            "container_exit_code": 0 if status == "succeeded" else 1,
            "retry_count": retry_count,
            "model_usage": model_usage,
            "stage_timings": stage_timings or [],
            "audit_trace": None,
            "retry_safe": retry_safe,
            "retrieval": _retrieval_audit(
                retrieval_context,
                retrieval_delivery,
            ),
        },
        "evaluation": evaluation,
        "recommended_action": recommended_action,
    }
    if input_datasets:
        response["input_datasets"] = [dict(item) for item in input_datasets]
    if analysis_results is not None:
        response["analysis_results"] = analysis_results
    if experiment_telemetry is not None:
        response["telemetry"]["experiment3"] = copy.deepcopy(
            experiment_telemetry
        )
    return response

def minimal_failed_response(
    run_id, ticket_id, message, *, outcome="unknown", log_ref=None
) -> dict:
    """Last-resort valid response when the normal build can't run / failed validation."""
    n = now_iso()
    return build_response(
        run_id=run_id,
        ticket_id=ticket_id,
        start_time=n,
        end_time=n,
        outcome=outcome,
        traces=[],
        error_message=(message or "unknown error")[:1000],
        log_ref=log_ref,
    )
