"""Poll Jira requests and trigger one docker_sandbox_runner DAG run per ticket."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.sdk import DAG, task

from jira_quant_common import (
    COMMAND_MARKER,
    extract_plain_text_from_adf,
    extract_quant_request,
    get_airflow_variable,
    is_automated_result_comment,
    jira_request,
    normalize_jira_attachments,
    parse_final_json_stdout,
    require_airflow_variable,
    sanitize_ticket_text,
)

DEFAULT_POLL_INTERVAL_MINUTES = 5
MAX_SUMMARY_CHARS = 500
MAX_DESCRIPTION_CHARS = 12_000
MAX_COMMENT_CHARS = 4_000
MAX_HISTORY_COMMENTS = 20


def get_poll_interval_minutes() -> int:
    raw_value = get_airflow_variable(
        "POLL_INTERVAL_MINUTES",
        str(DEFAULT_POLL_INTERVAL_MINUTES),
    )
    try:
        interval_minutes = int(raw_value or "")
    except ValueError as exc:
        raise ValueError(
            "Airflow Variable POLL_INTERVAL_MINUTES must be a positive integer"
        ) from exc

    if interval_minutes <= 0:
        raise ValueError(
            "Airflow Variable POLL_INTERVAL_MINUTES must be a positive integer"
        )

    return interval_minutes


POLL_INTERVAL_MINUTES = get_poll_interval_minutes()
SANDBOX_RUNNER_DAG_ID = "docker_sandbox_runner"


def parse_jira_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def fetch_recent_comment_groups() -> dict[str, dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=POLL_INTERVAL_MINUTES)
    jira_project_key = require_airflow_variable("JIRA_PROJECT_KEY")

    response = jira_request(
        "POST",
        "/rest/api/3/search/jql",
        headers={"Accept": "application/json"},
        json={
            "jql": (
                f"project = {jira_project_key} "
                f"AND updated >= -{POLL_INTERVAL_MINUTES}m "
                "ORDER BY updated ASC"
            ),
            "maxResults": 100,
            "fields": [
                "summary",
                "description",
                "issuetype",
                "status",
                "updated",
                "comment",
                "attachment",
            ],
        },
        timeout=30,
    )

    print(f"Jira search status: {response.status_code}")
    response.raise_for_status()

    grouped_events: dict[str, dict[str, Any]] = {}

    for issue in response.json().get("issues", []):
        issue_key = issue["key"]
        fields = issue["fields"]
        history_comments = []
        triggering_comments = []

        comment_page = fields.get("comment") or {}
        embedded_comments = comment_page.get("comments") or []
        total_comments = comment_page.get("total")
        if isinstance(total_comments, int) and total_comments > len(embedded_comments):
            print(
                f"Skipping {issue_key}: Jira returned an incomplete comment page; "
                "configure explicit comment pagination before processing this ticket"
            )
            continue

        for comment in embedded_comments:
            author = comment.get("author") or {}
            comment_timestamp = comment.get("updated") or comment.get("created")
            if not comment_timestamp:
                continue

            plain_text = sanitize_ticket_text(
                extract_plain_text_from_adf(comment.get("body")).strip(),
                MAX_COMMENT_CHARS,
            )
            if not plain_text or is_automated_result_comment(plain_text):
                continue

            common_comment = {
                "comment_id": str(comment.get("id") or ""),
                "author": author.get("displayName"),
                "created": comment.get("created"),
                "updated": comment.get("updated") or comment_timestamp,
                "text": plain_text,
            }
            history_comments.append(common_comment)

            if parse_jira_datetime(comment_timestamp) < cutoff:
                continue
            request_text = extract_quant_request(plain_text)
            if request_text is None:
                continue

            triggering_comments.append(
                {
                    **common_comment,
                    "author_display_name": author.get("displayName"),
                    "author_email": author.get("emailAddress"),
                    "author_account_id": author.get("accountId"),
                    "request_text": request_text,
                }
            )

        if triggering_comments:
            history_comments.sort(
                key=lambda item: parse_jira_datetime(item["updated"])
            )
            history_comments = history_comments[-MAX_HISTORY_COMMENTS:]
            triggering_comments.sort(
                key=lambda item: parse_jira_datetime(item["updated"])
            )
            triggering_comments = triggering_comments[-MAX_HISTORY_COMMENTS:]
            description = fields.get("description")
            if description is None:
                description = ""
            elif not isinstance(description, str):
                description = extract_plain_text_from_adf(description)
            status = fields.get("status")
            if isinstance(status, dict):
                status = status.get("name")
            grouped_events[issue_key] = {
                "ticket": {
                    "key": issue_key,
                    "id": issue["id"],
                    "summary": sanitize_ticket_text(
                        fields.get("summary"), MAX_SUMMARY_CHARS
                    ),
                    "description": sanitize_ticket_text(
                        description, MAX_DESCRIPTION_CHARS
                    ),
                    "status": status or "Unknown",
                    "issue_type": fields.get("issuetype", {}).get("name"),
                    "updated": fields.get("updated"),
                },
                "request": {
                    "event_type": "jira_comment_update",
                    "comments": history_comments,
                    "triggering_comments": triggering_comments,
                    "attachments": normalize_jira_attachments(
                        fields.get("attachment")
                    ),
                },
            }

    return grouped_events


def select_request_text(grouped_event: dict[str, Any]) -> str:
    comments = grouped_event["request"].get("triggering_comments") or grouped_event[
        "request"
    ]["comments"]

    latest_comment = max(
        comments,
        key=lambda comment: parse_jira_datetime(comment["updated"]),
    )

    return latest_comment["request_text"]


@task(task_id="poll_and_prepare_container_inputs")
def poll_and_prepare_container_inputs() -> list[dict[str, Any]]:
    """Poll Jira and return one grouped payload per ticket.

    This parent DAG owns Jira polling and grouping only. The docker_sandbox_runner DAG
    owns sandbox/container execution and Jira result writeback.
    """

    grouped_events = fetch_recent_comment_groups()
    print(f"Found {len(grouped_events)} ticket-level groups")

    container_inputs: list[dict[str, Any]] = []

    for issue_key, grouped_event in grouped_events.items():
        request_text = select_request_text(grouped_event)

        print(f"\nPrepared docker_sandbox_runner trigger payload for {issue_key}")
        print(
            "Retained bounded current-ticket context: "
            f"request_chars={len(request_text)}, "
            f"history_comments={len(grouped_event['request'].get('comments') or [])}, "
            f"attachments={len(grouped_event['request'].get('attachments') or [])}"
        )

        container_inputs.append(grouped_event)

    return container_inputs


default_args = {
    "owner": "rshah",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="jira_quant_docker_orchestrator",
    default_args=default_args,
    description="Poll Jira and trigger the dedicated docker_sandbox_runner DAG",
    start_date=datetime(2026, 6, 1),
    schedule=timedelta(minutes=POLL_INTERVAL_MINUTES),
    catchup=False,
    max_active_runs=1,
    tags=["jira", "docker", "quant-loop"],
) as dag:
    TriggerDagRunOperator.partial(
        task_id="trigger_docker_sandbox_runner",
        trigger_dag_id=SANDBOX_RUNNER_DAG_ID,
        wait_for_completion=False,
    ).expand(
        conf=poll_and_prepare_container_inputs(),
    )
