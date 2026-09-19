"""Builders for Jira Atlassian Document Format comments."""

from typing import Any, Dict, Iterable, List, Sequence


BOT_MARKER = "[quant-loop-bot]"
LIVE_ONLY_PROGRESS_STATUSES = {"PENDING", "RUNNING"}
PROGRESS_EVENT_STATUS_TO_ITERATION_TRACE_STATUS = {
    "SUCCESS": "succeeded",
    "FAILED": "failed",
    "SKIPPED": "skipped",
}
PROGRESS_EVENT_STATUSES = (
    LIVE_ONLY_PROGRESS_STATUSES
    | set(PROGRESS_EVENT_STATUS_TO_ITERATION_TRACE_STATUS)
)


def text_node(text: str) -> Dict[str, Any]:
    """Build a Jira ADF text node."""

    return {
        "type": "text",
        "text": text
    }


def paragraph(text: str) -> Dict[str, Any]:
    """Build a Jira ADF paragraph containing plain text."""

    return {
        "type": "paragraph",
        "content": [
            text_node(text)
        ]
    }


def heading(text: str, level: int = 3) -> Dict[str, Any]:
    """Build a Jira ADF heading containing plain text."""

    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [
            text_node(text)
        ]
    }


def bullet_list(items: Iterable[str]) -> Dict[str, Any]:
    """Build a Jira ADF bullet list from plain text items."""

    return {
        "type": "bulletList",
        "content": [
            {
                "type": "listItem",
                "content": [
                    paragraph(item)
                ],
            }
            for item in items
        ],
    }


def build_text_adf_comment(text: str) -> Dict[str, Any]:
    """
    Build a simple Jira ADF document with one text paragraph.

    This is used for invalid or ignored command responses where a concise
    message is enough.
    """
    return {
        "type": "doc",
        "version": 1,
        "content": [
            paragraph(text)
        ]
    }


def build_runtime_workflow_report_adf(
    result: Dict[str, Any],
    progress_events: Sequence[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Build a structured workflow report from the runtime response contract."""

    execution_summary = _dict_value(result.get("execution_summary"))
    final_status = str(
        execution_summary.get("status") or result.get("status") or "unknown"
    ).lower()
    report_events = list(progress_events or result.get("_progress_events") or [])
    diagnostics = _dict_value(result.get("diagnostics"))
    error_code = diagnostics.get("error_code")

    header_lines: List[Dict[str, Any]] = [
        paragraph(f"Run ID: {_display_value(result.get('run_id'))}"),
    ]
    if result.get("workflow_id"):
        header_lines.append(
            paragraph(f"Workflow ID: {_display_value(result.get('workflow_id'))}")
        )
    header_lines.append(
        paragraph(
            "Ticket ID: "
            f"{_display_value(execution_summary.get('ticket_id') or result.get('issue_key'))}"
        )
    )
    if execution_summary.get("request_type"):
        header_lines.append(
            paragraph(f"Request Type: {_display_value(execution_summary.get('request_type'))}")
        )
    header_lines.extend(
        [
            paragraph(f"Final Status: {final_status}"),
            paragraph(f"Start Time: {_display_value(execution_summary.get('start_time'))}"),
            paragraph(f"End Time: {_display_value(execution_summary.get('end_time'))}"),
        ]
    )

    content: List[Dict[str, Any]] = [
        paragraph(BOT_MARKER),
        heading(
            f"Automated quant workflow report: {_final_status_title(final_status, error_code)}",
            2,
        ),
        *header_lines,
    ]

    _append_bullet_section(
        content,
        "Stage Progress",
        _progress_event_lines(report_events),
    )
    _append_bullet_section(
        content,
        "Iteration Summary",
        _iteration_trace_lines(execution_summary.get("iteration_traces") or []),
    )

    if (
        "performance_metrics" in result
        and result.get("performance_metrics") is not None
    ):
        _append_bullet_section(
            content,
            "Performance Metrics",
            _metrics_lines(_dict_value(result.get("performance_metrics"))),
        )

    _append_bullet_section(
        content,
        "Evaluation",
        _evaluation_lines(
            _dict_value(result.get("evaluation")),
            result.get("recommended_action"),
        ),
    )

    artifacts = result.get("generated_artifacts") or result.get("artifacts")
    if isinstance(artifacts, dict):
        _append_bullet_section(
            content,
            "Generated Artifacts",
            _artifact_lines(artifacts),
        )
        _append_bullet_section(
            content,
            "Repository Branches",
            _repository_branch_lines(artifacts.get("repository_branches") or []),
        )

    if diagnostics:
        _append_bullet_section(content, "Diagnostics", _diagnostic_lines(diagnostics))

    artifact_ingestion = _dict_value(result.get("artifact_ingestion"))
    if artifact_ingestion:
        _append_bullet_section(
            content,
            "Ingested Artifacts",
            _artifact_ingestion_lines(artifact_ingestion),
        )

    content.append(heading("Next Action", 3))
    content.append(
        paragraph(
            _next_action(final_status, result.get("recommended_action"), error_code)
        )
    )

    return {
        "type": "doc",
        "version": 1,
        "content": content
    }


def build_workflow_progress_adf(
    *,
    runtime_request: Dict[str, Any] | None = None,
    progress_events: Sequence[Dict[str, Any]] | None = None,
    diagnostics: Dict[str, Any] | None = None,
    state: str = "running",
) -> Dict[str, Any]:
    """Build a progress-only ADF report when final result.json is unavailable."""

    request = runtime_request or {}
    jira_metadata = _dict_value(request.get("jira_metadata"))
    execution_objectives = _dict_value(request.get("execution_objectives"))
    events = list(progress_events or [])
    first_event = events[0] if events else {}
    normalized_state = state.lower().strip() or "running"
    final_status = (
        normalized_state
        if normalized_state in {"failed", "timeout"}
        else "not available"
    )

    header_lines: List[Dict[str, Any]] = [
        paragraph(
            f"Run ID: {_display_value(request.get('run_id') or first_event.get('run_id'))}"
        ),
    ]
    if request.get("workflow_id"):
        header_lines.append(
            paragraph(f"Workflow ID: {_display_value(request.get('workflow_id'))}")
        )
    header_lines.append(
        paragraph(
            "Ticket ID: "
            f"{_display_value(jira_metadata.get('ticket_id') or first_event.get('ticket_id'))}"
        )
    )
    if execution_objectives.get("strategy_type"):
        header_lines.append(
            paragraph(f"Request Type: {_display_value(execution_objectives.get('strategy_type'))}")
        )
    header_lines.extend(
        [
            paragraph(f"Milestone: {normalized_state}"),
            paragraph(f"Final Status: {final_status}"),
        ]
    )

    content: List[Dict[str, Any]] = [
        paragraph(BOT_MARKER),
        heading(
            f"Automated quant workflow report: {_progress_state_title(normalized_state)}",
            2,
        ),
        *header_lines,
    ]
    _append_bullet_section(content, "Stage Progress", _progress_event_lines(events))
    diagnostic_values = _dict_value(diagnostics)
    if diagnostic_values:
        _append_bullet_section(
            content,
            "Diagnostics",
            _diagnostic_lines(diagnostic_values),
        )
    content.append(heading("Next Action", 3))
    content.append(paragraph(_progress_next_action(normalized_state)))

    return {
        "type": "doc",
        "version": 1,
        "content": content
    }


def build_backtest_result_adf(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert a normalized workflow/backtest result into Jira-compatible ADF JSON.

    Supports both:
    1. The old local mock workflow format, with ticker/strategy/start/end.
    2. The Luc/Umar output JSON format, with status/summary/metrics/artifacts.
    """
    metrics = result.get("metrics") or {}
    artifacts = result.get("artifacts") or {}

    status = str(result.get("status", "succeeded")).lower()
    command = result.get("command", "backtest")
    issue_key = result.get("issue_key", "UNKNOWN")
    summary = result.get("summary", "No summary provided.")

    if status == "failed":
        title = f"{command.capitalize()} failed"
    elif status == "succeeded":
        title = f"{command.capitalize()} succeeded"
    else:
        title = f"{command.capitalize()} status: {status}"

    content: List[Dict[str, Any]] = [
        paragraph(title),
        paragraph(f"Issue: {issue_key}"),
        paragraph(f"Command: {command}"),
        paragraph("Summary:"),
        paragraph(summary),
    ]

    # Optional old-format fields. Only show them if they exist.
    if result.get("ticker"):
        content.append(paragraph(f"Ticker: {result['ticker']}"))

    if result.get("strategy"):
        content.append(paragraph(f"Strategy: {result['strategy']}"))

    if result.get("start") or result.get("end"):
        start = result.get("start", "not_provided")
        end = result.get("end", "not_provided")
        content.append(paragraph(f"Period: {start} to {end}"))

    if metrics:
        content.append(paragraph("Metrics:"))

        if "sharpe_ratio" in metrics:
            content.append(paragraph(f"Sharpe Ratio: {metrics['sharpe_ratio']}"))

        if "max_drawdown" in metrics:
            content.append(paragraph(f"Max Drawdown: {metrics['max_drawdown']}"))

        if "total_return" in metrics:
            content.append(paragraph(f"Total Return: {metrics['total_return']}"))

        known_metrics = {"sharpe_ratio", "max_drawdown", "total_return"}
        for key, value in metrics.items():
            if key not in known_metrics:
                label = key.replace("_", " ").title()
                content.append(paragraph(f"{label}: {value}"))

    if artifacts:
        content.append(paragraph("Artifacts:"))

        if "feature_branch" in artifacts:
            content.append(paragraph(f"Feature Branch: {artifacts['feature_branch']}"))

        if "changed_file" in artifacts:
            content.append(paragraph(f"Changed File: {artifacts['changed_file']}"))

        known_artifacts = {"feature_branch", "changed_file"}
        for key, value in artifacts.items():
            if key not in known_artifacts:
                label = key.replace("_", " ").title()
                content.append(paragraph(f"{label}: {value}"))

    return {
        "type": "doc",
        "version": 1,
        "content": content
    }


def _append_bullet_section(
    content: List[Dict[str, Any]],
    title: str,
    lines: Sequence[str],
) -> None:
    if not lines:
        return

    content.append(heading(title, 3))
    content.append(bullet_list(lines))


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _display_value(value: Any) -> str:
    if value is None:
        return "not provided"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _label(key: str) -> str:
    return key.replace("_", " ").title()


def _list_value(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) if value else "none"
    return str(value)


def _final_status_title(status: str, error_code: Any = None) -> str:
    if error_code == "QUALITY_VALIDATION_FAILED":
        return "revision required"
    if status == "succeeded":
        return "successful completion"
    if status == "failed":
        return "failed completion"
    if status == "timeout":
        return "timeout completion"
    return "workflow completion"


def _progress_state_title(state: str) -> str:
    if state in {"queued", "accepted"}:
        return "queued / accepted request"
    if state == "partial":
        return "partial progress"
    if state == "failed":
        return "failed workflow"
    if state == "timeout":
        return "timeout workflow"
    return "running workflow"


def _progress_event_lines(events: Sequence[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue

        stage = _display_value(event.get("stage"))
        status = _display_value(event.get("status"))
        timestamp = event.get("timestamp")
        message = event.get("message")
        parts = [f"{stage}: {status}"]

        if timestamp:
            parts.append(f"at {timestamp}")
        if event.get("iteration") is not None:
            parts.append(f"iteration {event['iteration']}")
        if event.get("agent"):
            parts.append(f"agent {event['agent']}")
        if event.get("tool_call"):
            parts.append(f"tool {event['tool_call']}")

        line = " | ".join(parts)
        if message:
            line = f"{line} - {message}"
        lines.append(line)

    return lines


def _iteration_trace_lines(traces: Sequence[Any]) -> List[str]:
    lines: List[str] = []
    for trace in traces:
        if not isinstance(trace, dict):
            continue

        prefix = (
            f"Iteration {trace.get('iteration')}: "
            f"{_display_value(trace.get('status'))}"
        )
        details = (
            f"agent {_display_value(trace.get('agent'))} | "
            f"tool {_display_value(trace.get('tool_call'))}"
        )
        if trace.get("timestamp"):
            details = f"{details} | at {trace['timestamp']}"
        line = f"{prefix} | {details}"
        if trace.get("message"):
            line = f"{line} - {trace['message']}"
        lines.append(line)

    return lines


def _metrics_lines(metrics: Dict[str, Any]) -> List[str]:
    ordered_keys = [
        "total_return",
        "sharpe_ratio",
        "max_drawdown",
        "alpha",
        "beta",
        "time_series_data_path",
    ]
    lines = [
        f"{_label(key)}: {_display_value(metrics.get(key))}"
        for key in ordered_keys
        if key in metrics
    ]
    lines.extend(
        f"{_label(key)}: {_display_value(value)}"
        for key, value in metrics.items()
        if key not in ordered_keys
    )
    return lines


def _evaluation_lines(
    evaluation: Dict[str, Any],
    recommended_action: Any,
) -> List[str]:
    """Render the run's verdict: whether it met the ticket's bar, and why.

    For a general (non-backtest) request this is the whole result — there are no
    metrics — so without it the ticket only learns that something ran. For a
    backtest it carries the target_criteria scoring.
    """
    if not evaluation and not recommended_action:
        return []

    lines: List[str] = []
    if recommended_action:
        lines.append(f"Recommended Action: {_display_value(recommended_action)}")

    met_criteria = evaluation.get("met_criteria")
    lines.append(
        "Met Criteria: "
        + ("not determined" if met_criteria is None else _display_value(met_criteria))
    )

    summary = evaluation.get("summary")
    if summary:
        lines.append(f"Summary: {_display_value(summary)}")

    # Per-criterion detail is empty for a general run: a prose verdict has no
    # numeric thresholds to report.
    for result in evaluation.get("criteria_results") or []:
        if not isinstance(result, dict):
            continue
        passed = result.get("passed")
        verdict = "not scored" if passed is None else ("pass" if passed else "fail")
        lines.append(
            f"{_label(str(result.get('criterion', 'criterion')))}: {verdict} "
            f"(actual {_display_value(result.get('actual'))}, "
            f"threshold {_display_value(result.get('threshold'))})"
        )

    unrecognised = evaluation.get("unrecognised_criteria") or []
    if unrecognised:
        lines.append(f"Unrecognised Criteria: {_list_value(list(unrecognised))}")

    return lines


def _artifact_lines(artifacts: Dict[str, Any]) -> List[str]:
    ordered_keys = [
        "modified_files",
        "new_files",
        "backtest_plots_path",
        "branch_name",
    ]
    lines = [
        f"{_label(key)}: {_list_value(artifacts.get(key))}"
        for key in ordered_keys
        if key in artifacts
    ]
    lines.extend(
        f"{_label(key)}: {_list_value(value)}"
        for key, value in artifacts.items()
        if key not in ordered_keys
    )
    return lines or ["No generated artifacts recorded."]


def _repository_branch_lines(branches: Sequence[Any]) -> List[str]:
    lines: List[str] = []
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        parts = [
            f"{_display_value(branch.get('alias'))}",
            f"repo {_display_value(branch.get('repo_full_name'))}",
            f"source {_display_value(branch.get('source_branch'))}",
            f"target {_display_value(branch.get('target_branch'))}",
        ]
        if branch.get("branch_action") is not None:
            parts.append(f"action {_display_value(branch.get('branch_action'))}")
        if branch.get("commit_sha") is not None:
            parts.append(f"commit {_display_value(branch.get('commit_sha'))}")
        modified = branch.get("modified_files")
        if modified:
            parts.append(f"modified {_list_value(modified)}")
        new_files = branch.get("new_files")
        if new_files:
            parts.append(f"new {_list_value(new_files)}")
        lines.append(" | ".join(parts))
    return lines


def _diagnostic_lines(diagnostics: Dict[str, Any]) -> List[str]:
    ordered_keys = ["error_code", "error_message", "raw_log_reference"]
    lines = [
        f"{_label(key)}: {_display_value(diagnostics.get(key))}"
        for key in ordered_keys
        if key in diagnostics
    ]
    lines.extend(
        f"{_label(key)}: {_display_value(value)}"
        for key, value in diagnostics.items()
        if key not in ordered_keys
    )
    return lines or ["No diagnostics recorded."]


def _artifact_ingestion_lines(artifact_ingestion: Dict[str, Any]) -> List[str]:
    uploaded = artifact_ingestion.get("uploaded") or []
    missing = artifact_ingestion.get("missing") or []
    skipped = artifact_ingestion.get("skipped") or []
    lines: List[str] = []

    if uploaded:
        for item in uploaded:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename") or item.get("source_path") or "artifact"
            details = []
            if item.get("size_bytes") is not None:
                details.append(f"{item['size_bytes']} bytes")
            attachment_id = item.get("jira_attachment_id") or item.get("id")
            if attachment_id:
                details.append(f"attachment {attachment_id}")
            url = item.get("jira_attachment_url") or item.get("content")
            if url:
                details.append(str(url))
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"Uploaded: {filename}{suffix}")
    else:
        lines.append("Uploaded: none")

    for label, items in (("Missing", missing), ("Skipped", skipped)):
        if not items:
            lines.append(f"{label}: none")
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("source_path") or "artifact"
            reason = item.get("reason")
            suffix = f" ({reason})" if reason else ""
            lines.append(f"{label}: {path}{suffix}")

    return lines


_RECOMMENDED_ACTION_NEXT_STEPS = {
    "accept": (
        "The run met the ticket's criteria. Review the branch output, then merge or "
        "close the ticket."
    ),
    "iterate": (
        "The run did not meet the ticket's criteria and stopped. Read the evaluation "
        "summary above, then rerun /quant with a refined request."
    ),
    "review": (
        "The run completed but could not be scored, so it needs a human read. Review "
        "the evaluation summary and branch output before deciding."
    ),
    "escalate": (
        "The run did not complete. Inspect diagnostics and attached logs before "
        "rerunning /quant."
    ),
}


def _next_action(
    status: str,
    recommended_action: Any = None,
    error_code: Any = None,
) -> str:
    # The evaluator's recommendation is more specific than the status alone, so
    # prefer it for a completed run.
    if status == "succeeded" and recommended_action in _RECOMMENDED_ACTION_NEXT_STEPS:
        return _RECOMMENDED_ACTION_NEXT_STEPS[str(recommended_action)]
    if status == "succeeded":
        return (
            "Review generated artifacts, attachments, and branch output before "
            "merging or closing the ticket."
        )
    if status == "failed" and error_code == "QUALITY_VALIDATION_FAILED":
        return (
            "The generated branch is preserved for repair. Review the validation "
            "reason and branch files, refine the request if needed, then rerun /quant."
        )
    if status == "failed":
        return "Inspect diagnostics, attached logs, and raw logs, then rerun /quant."
    if status == "timeout":
        return (
            "Inspect timeout diagnostics and attached logs, reduce scope or increase "
            "timeout, then rerun /quant."
        )
    return "Review the workflow output before deciding the next ticket action."


def _progress_next_action(state: str) -> str:
    if state in {"queued", "accepted"}:
        return "The request has been accepted; wait for the workflow to start."
    if state == "failed":
        return "Inspect diagnostics and attached logs, then rerun /quant."
    if state == "timeout":
        return (
            "Inspect timeout diagnostics and attached logs, reduce scope or increase "
            "timeout, then rerun /quant."
        )
    if state == "partial":
        return (
            "Final result.json is not available yet; inspect captured progress and "
            "runtime logs before retrying."
        )
    return "Workflow is running; wait for the authoritative final result.json report."
