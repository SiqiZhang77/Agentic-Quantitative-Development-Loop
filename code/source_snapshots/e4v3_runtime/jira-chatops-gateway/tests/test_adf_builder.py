from __future__ import annotations

import json
from pathlib import Path

from adf_utils.builder import (
    build_backtest_result_adf,
    build_runtime_workflow_report_adf,
    build_text_adf_comment,
    build_workflow_progress_adf,
)


GATEWAY_ROOT = Path(__file__).parents[1]


def _paragraph_texts(adf: dict) -> list[str]:
    return [
        paragraph["content"][0]["text"]
        for paragraph in adf["content"]
    ]


def _all_text(adf_node: object) -> list[str]:
    if isinstance(adf_node, dict):
        if adf_node.get("type") == "text":
            return [str(adf_node.get("text", ""))]

        texts: list[str] = []
        for child in adf_node.get("content", []):
            texts.extend(_all_text(child))
        return texts

    if isinstance(adf_node, list):
        texts = []
        for child in adf_node:
            texts.extend(_all_text(child))
        return texts

    return []


def _joined_text(adf: dict) -> str:
    return "\n".join(_all_text(adf))


def _load_example(filename: str) -> dict:
    return json.loads(
        (GATEWAY_ROOT / "examples" / filename).read_text(encoding="utf-8")
    )


def test_build_text_adf_comment() -> None:
    adf = build_text_adf_comment("Workflow failed.")

    assert adf == {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Workflow failed."}],
            }
        ],
    }


def test_runtime_report_displays_unknown_ticket_without_fake_issue_key() -> None:
    result = _load_example("runtime_response_failure_example.json")
    result["execution_summary"]["ticket_id"] = "unknown"

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Ticket ID: unknown" in text
    assert "RAE-0" not in text


def test_build_backtest_result_adf() -> None:
    adf = build_backtest_result_adf(
        {
            "issue_key": "ALPHA-101",
            "ticker": "AAPL",
            "strategy": "momentum",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "metrics": {
                "sharpe_ratio": 1.42,
                "max_drawdown": "-8.7%",
                "total_return": "18.4%",
            },
            "summary": "Momentum remained profitable.",
        }
    )

    assert adf["type"] == "doc"
    assert adf["version"] == 1
    assert _paragraph_texts(adf) == [
        "Backtest succeeded",
        "Issue: ALPHA-101",
        "Command: backtest",
        "Summary:",
        "Momentum remained profitable.",
        "Ticker: AAPL",
        "Strategy: momentum",
        "Period: 2024-01-01 to 2024-12-31",
        "Metrics:",
        "Sharpe Ratio: 1.42",
        "Max Drawdown: -8.7%",
        "Total Return: 18.4%",
    ]


def test_build_runtime_workflow_report_adf_combines_final_progress_and_artifacts() -> None:
    result = _load_example("runtime_response_success_example.json")
    result["workflow_id"] = "SCRUM-46_2_abc123"
    result["artifact_ingestion"] = {
        "uploaded": [
            {
                "filename": "run.log",
                "size_bytes": 128,
                "jira_attachment_id": "att-1",
                "jira_attachment_url": "https://jira/attachment/att-1",
            }
        ],
        "missing": [
            {
                "path": "/workspace/output/artifacts/equity_curve.csv",
                "reason": "file does not exist",
            }
        ],
        "skipped": [],
    }
    progress_events = [
        {
            "schema_version": "1.0",
            "run_id": result["run_id"],
            "ticket_id": "SCRUM-46",
            "stage": "backtest",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:39:00Z",
            "iteration": 2,
            "agent": "backtest_agent",
            "tool_call": "run_backtest",
            "message": "Backtest running",
        },
        {
            "schema_version": "1.0",
            "run_id": result["run_id"],
            "ticket_id": "SCRUM-46",
            "stage": "backtest",
            "status": "SUCCESS",
            "timestamp": "2026-06-18T10:40:00Z",
            "iteration": 2,
            "agent": "backtest_agent",
            "tool_call": "run_backtest",
            "message": "Backtest completed",
        },
    ]

    text = _joined_text(build_runtime_workflow_report_adf(result, progress_events))

    assert "[quant-loop-bot]" in text
    assert "Automated quant workflow report: successful completion" in text
    assert "Run ID: run_SCRUM-46_20260618_001" in text
    assert "Workflow ID: SCRUM-46_2_abc123" in text
    assert "Ticket ID: SCRUM-46" in text
    assert "Request Type: backtest" in text
    assert "Final Status: succeeded" in text
    assert "Stage Progress" in text
    assert "backtest: SUCCESS" in text
    assert "Performance Metrics" in text
    assert "Sharpe Ratio: 1.2" in text
    assert "Generated Artifacts" in text
    assert "Modified Files: src/strategies/momentum.py" in text
    assert "Ingested Artifacts" in text
    assert "Uploaded: run.log (128 bytes, attachment att-1" in text
    assert "Missing: /workspace/output/artifacts/equity_curve.csv" in text


def _report_with_evaluation(evaluation: dict, recommended_action: str) -> str:
    result = {
        "run_id": "run_SCRUM-9_1",
        "schema_version": "1.0",
        "execution_summary": {
            "ticket_id": "SCRUM-9",
            "status": "succeeded",
            "start_time": "2026-06-18T10:31:00Z",
            "end_time": "2026-06-18T10:45:00Z",
            "iteration_traces": [],
        },
        "performance_metrics": None,
        "generated_artifacts": {
            "modified_files": ["proxy/github_client.py"],
            "new_files": [],
            "backtest_plots_path": None,
        },
        "diagnostics": {
            "error_code": None,
            "error_message": None,
            "raw_log_reference": None,
        },
        "evaluation": evaluation,
        "recommended_action": recommended_action,
    }
    return _joined_text(build_runtime_workflow_report_adf(result))


def test_report_shows_the_review_verdict_for_a_run_without_metrics() -> None:
    # A general request has no metrics, so the verdict is the entire result. It
    # used to be written to result.json and never surfaced on the ticket.
    text = _report_with_evaluation(
        {
            "met_criteria": False,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "Automated review (advisory): retry backoff is still missing.",
        },
        "iterate",
    )

    assert "Evaluation" in text
    assert "Recommended Action: iterate" in text
    assert "Met Criteria: false" in text
    assert "retry backoff is still missing" in text


def test_report_shows_backtest_criteria_scoring() -> None:
    text = _report_with_evaluation(
        {
            "met_criteria": False,
            "confidence": 1.0,
            "criteria_results": [
                {
                    "criterion": "min_sharpe_ratio",
                    "metric": "sharpe_ratio",
                    "threshold": 1.5,
                    "actual": 0.9,
                    "passed": False,
                }
            ],
            "unrecognised_criteria": ["min_sortino"],
            "summary": "1/1 criteria failed.",
        },
        "iterate",
    )

    assert "Min Sharpe Ratio: fail (actual 0.9, threshold 1.5)" in text
    assert "Unrecognised Criteria: min_sortino" in text


def test_report_reports_an_unscored_run_as_undetermined_rather_than_failed() -> None:
    text = _report_with_evaluation(
        {
            "met_criteria": None,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "The reviewer could not be reached.",
        },
        "review",
    )

    assert "Met Criteria: not determined" in text
    assert "needs a human read" in text


def test_quality_failure_reports_revision_reason_and_preserved_branch() -> None:
    result = {
        "run_id": "run_SCRUM-115_2",
        "execution_summary": {
            "ticket_id": "SCRUM-115",
            "status": "failed",
            "start_time": "2026-07-18T10:00:00Z",
            "end_time": "2026-07-18T10:02:00Z",
            "iteration_traces": [],
        },
        "performance_metrics": None,
        "generated_artifacts": {
            "modified_files": [],
            "new_files": ["README.md"],
            "branch_name": "quant/SCRUM-115",
            "repository_branches": [
                {
                    "alias": "ATPDataHandlersRepo",
                    "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                    "source_branch": "develop",
                    "target_branch": "quant/SCRUM-115",
                    "branch_action": "reused",
                    "commit_sha": "1234567890abcdef",
                    "modified_files": [],
                    "new_files": ["README.md"],
                }
            ],
        },
        "diagnostics": {
            "error_code": "QUALITY_VALIDATION_FAILED",
            "error_message": "README.md contains placeholder-only content.",
            "raw_log_reference": None,
        },
        "evaluation": {
            "met_criteria": False,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "README.md contains placeholder-only content.",
        },
        "recommended_action": "iterate",
    }

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Automated quant workflow report: revision required" in text
    assert "QUALITY_VALIDATION_FAILED" in text
    assert "README.md contains placeholder-only content" in text
    assert "bankingscience/ATPDataHandlersRepo" in text
    assert "quant/SCRUM-115" in text
    assert "1234567890abcdef" in text
    assert "branch is preserved for repair" in text


def test_next_action_follows_the_recommendation_not_just_the_status() -> None:
    accepted = _report_with_evaluation(
        {
            "met_criteria": True,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "ok",
        },
        "accept",
    )

    assert "merge or close the ticket" in accepted


def test_report_without_an_evaluation_omits_the_section() -> None:
    result = _load_example("runtime_response_success_example.json")
    result.pop("evaluation", None)
    result.pop("recommended_action", None)

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Evaluation" not in text


def test_build_runtime_workflow_report_adf_includes_repository_branches() -> None:
    result = _load_example("runtime_response_success_example.json")
    result["generated_artifacts"]["repository_branches"] = [
        {
            "alias": "ATPConnectorsRepo",
            "repo_full_name": "bankingscience/ATPConnectorsRepo",
            "source_branch": "develop",
            "target_branch": "quant/SCRUM-46",
            "branch_action": "created",
            "commit_sha": "abcdef1234567890",
            "modified_files": ["src/connector.py"],
            "new_files": [],
        }
    ]

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Repository Branches" in text
    assert "ATPConnectorsRepo | repo bankingscience/ATPConnectorsRepo" in text
    assert "source develop" in text
    assert "target quant/SCRUM-46" in text
    assert "commit abcdef1234567890" in text


def test_build_runtime_workflow_report_adf_failed_null_metrics_and_diagnostics() -> None:
    result = _load_example("runtime_response_failure_example.json")

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Automated quant workflow report: failed completion" in text
    assert "Request Type: backtest" in text
    assert "Final Status: failed" in text
    assert "Performance Metrics" not in text
    assert "Diagnostics" in text
    assert "Error Code: STRATEGY_COMPILE_ERROR" in text
    assert "Error Message: Strategy file failed to compile" in text
    assert "Inspect diagnostics" in text


def test_build_runtime_workflow_report_adf_timeout_response() -> None:
    result = _load_example("runtime_response_timeout_example.json")

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Automated quant workflow report: timeout completion" in text
    assert "Final Status: timeout" in text
    assert "Error Code: TIMEOUT_REACHED" in text
    assert "Inspect timeout diagnostics" in text


def test_build_runtime_workflow_report_adf_non_backtest_request_omits_metrics() -> None:
    result = _load_example("runtime_response_success_non_backtest_example.json")

    text = _joined_text(build_runtime_workflow_report_adf(result))

    assert "Automated quant workflow report: successful completion" in text
    assert "Request Type: refactor" in text
    assert "Final Status: succeeded" in text
    assert "Generated Artifacts" in text
    assert "Modified Files: src/data/loader.py" in text
    # No backtest ran for this request type: no Performance Metrics section, and
    # no Sharpe/drawdown/return noise anywhere in the report.
    assert "Performance Metrics" not in text
    assert "Sharpe" not in text


def test_build_workflow_progress_adf_non_backtest_request() -> None:
    # Paired request for runtime_response_success_non_backtest_example.json: same
    # run_id/ticket_id, but as the in-flight runtime_request shape. The in-flight
    # report reads execution_objectives.strategy_type directly since it runs
    # before result.json exists.
    runtime_request = _load_example("runtime_request_non_backtest_example.json")

    text = _joined_text(
        build_workflow_progress_adf(
            runtime_request=runtime_request,
            progress_events=[
                {
                    "schema_version": "1.0",
                    "run_id": runtime_request["run_id"],
                    "ticket_id": "SCRUM-52",
                    "stage": "fetch",
                    "status": "RUNNING",
                    "timestamp": "2026-06-18T13:57:00Z",
                    "message": "Fetching repository",
                }
            ],
        )
    )

    assert "Ticket ID: SCRUM-52" in text
    assert "Request Type: refactor" in text
    assert "fetch: RUNNING" in text


def test_build_workflow_progress_adf_partial_without_final_result() -> None:
    runtime_request = _load_example("runtime_request_example.json")
    runtime_request["workflow_id"] = "SCRUM-46_2_abc123"
    progress_events = [
        {
            "schema_version": "1.0",
            "run_id": runtime_request["run_id"],
            "ticket_id": "SCRUM-46",
            "stage": "fetch",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:10Z",
            "message": "Fetching repository",
        }
    ]

    text = _joined_text(
        build_workflow_progress_adf(
            runtime_request=runtime_request,
            progress_events=progress_events,
            diagnostics={
                "execution_mode": "YARN",
                "exit_code": 1,
                "result_path": "/workspace/output/nested/result.json",
            },
            state="partial",
        )
    )

    assert "Automated quant workflow report: partial progress" in text
    assert "Workflow ID: SCRUM-46_2_abc123" in text
    assert "Request Type: backtest" in text
    assert "Milestone: partial" in text
    assert "Final Status: not available" in text
    assert "fetch: RUNNING" in text
    assert "Diagnostics" in text
    assert "Execution Mode: YARN" in text
    assert "Exit Code: 1" in text
    assert "Result Path: /workspace/output/nested/result.json" in text
    assert "Final result.json is not available yet" in text


def test_build_workflow_progress_adf_failed_milestone() -> None:
    runtime_request = _load_example("runtime_request_example.json")
    runtime_request["workflow_id"] = "SCRUM-46_2_abc123"

    text = _joined_text(
        build_workflow_progress_adf(
            runtime_request=runtime_request,
            diagnostics={
                "dag_run_id": "manual__1",
                "error_type": "RuntimeError",
            },
            state="failed",
        )
    )

    assert "Automated quant workflow report: failed workflow" in text
    assert "Workflow ID: SCRUM-46_2_abc123" in text
    assert "Request Type: backtest" in text
    assert "Milestone: failed" in text
    assert "Final Status: failed" in text
    assert "Dag Run Id: manual__1" in text
    assert "Error Type: RuntimeError" in text
    assert "Inspect diagnostics and attached logs" in text
