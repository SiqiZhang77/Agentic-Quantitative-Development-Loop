from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from airflow import DAG
from airflow.providers.standard.operators.python import PythonOperator

from jira_quant_common import (
    extract_plain_text_from_adf,
    is_automated_result_comment,
    jira_request,
    normalize_jira_attachments,
    require_airflow_variable,
    sanitize_ticket_text,
)


DOWNSTREAM_DAG_ID = os.environ.get("DOWNSTREAM_DAG_ID", "quant_loop_mini")
MAX_HISTORY_COMMENTS = 20
MAX_SUMMARY_CHARS = 500
MAX_DESCRIPTION_CHARS = 12_000
MAX_COMMENT_CHARS = 4_000


def parse_jira_datetime(value: str) -> datetime:
    """Parse the timestamp format returned for Jira comments."""

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")


def poll_jira_comments() -> dict[str, dict[str, Any]]:
    """Find recently updated comments and group them into one event per issue."""

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    jira_project_key = require_airflow_variable("JIRA_PROJECT_KEY")

    payload = {
        "jql": (
            f"project = {jira_project_key} "
            "AND updated >= -30m ORDER BY updated ASC"
        ),
        "maxResults": 100,
        "fields": [
            "summary",
            "description",
            "status",
            "issuetype",
            "updated",
            "comment",
            "attachment",
        ],
    }

    response = jira_request(
        "POST",
        "/rest/api/3/search/jql",
        json=payload,
        headers={"Accept": "application/json"},
        timeout=30,
    )

    print(f"Jira search status code: {response.status_code}")
    response.raise_for_status()

    issues = response.json().get("issues", [])
    grouped_events: dict[str, dict[str, Any]] = {}

    for issue in issues:
        issue_key = issue["key"]
        fields = issue["fields"]
        comments = fields.get("comment", {}).get("comments", [])
        normalized_comments = []
        recent_comment_ids: set[str] = set()

        for comment in comments:
            comment_updated = parse_jira_datetime(comment["updated"])
            author = comment.get("author") or {}
            text = sanitize_ticket_text(
                extract_plain_text_from_adf(comment.get("body")), MAX_COMMENT_CHARS
            )
            if not text or is_automated_result_comment(text):
                continue
            comment_id = str(comment["id"])
            normalized_comments.append(
                {
                    "comment_id": comment_id,
                    "author": author.get("displayName"),
                    "author_display_name": author.get("displayName"),
                    "author_email": author.get("emailAddress"),
                    "author_account_id": author.get("accountId"),
                    "created": comment.get("created"),
                    "updated": comment.get("updated"),
                    "text": text,
                }
            )
            if comment_updated >= cutoff:
                recent_comment_ids.add(comment_id)

        triggering_comments = [
            comment
            for comment in normalized_comments
            if comment["comment_id"] in recent_comment_ids
        ]
        triggering_comments.sort(key=lambda item: item["updated"])
        triggering_comments = triggering_comments[-MAX_HISTORY_COMMENTS:]
        triggering_ids = {
            comment["comment_id"] for comment in triggering_comments
        }
        non_triggering_comments = [
            comment
            for comment in normalized_comments
            if comment["comment_id"] not in triggering_ids
        ]
        history_slots = MAX_HISTORY_COMMENTS - len(triggering_comments)
        history_comments = (
            non_triggering_comments[-history_slots:] if history_slots else []
        ) + triggering_comments
        history_comments.sort(key=lambda item: item.get("created") or item["updated"])

        if triggering_comments:
            grouped_events[issue_key] = {
                "ticket": {
                    "key": issue_key,
                    "id": issue["id"],
                    "summary": sanitize_ticket_text(
                        fields.get("summary"), MAX_SUMMARY_CHARS
                    ),
                    "description": sanitize_ticket_text(
                        extract_plain_text_from_adf(fields.get("description")),
                        MAX_DESCRIPTION_CHARS,
                    ),
                    "status": fields.get("status", {}).get("name"),
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

    print(f"Found {len(grouped_events)} ticket-level comment update groups")

    for issue_key, conf in grouped_events.items():
        print(f"Would trigger DAG {DOWNSTREAM_DAG_ID} for {issue_key}")
        print(
            f"Prepared {len(conf['request']['comments'])} bounded history events "
            f"({len(conf['request']['triggering_comments'])} triggering)"
        )

        # Enable this only after authentication and idempotency are defined for
        # the shared Airflow deployment.
        #
        # airflow_api_url = (
        #     f"http://localhost:8008/api/v1/dags/{DOWNSTREAM_DAG_ID}/dagRuns"
        # )
        # trigger_response = requests.post(
        #     airflow_api_url,
        #     json={"conf": conf},
        #     headers={"Content-Type": "application/json"},
        #     timeout=30,
        # )
        # print(f"Airflow trigger status: {trigger_response.status_code}")
        # print(trigger_response.text[:1000])
        # trigger_response.raise_for_status()

    return grouped_events


default_args = {
    "owner": "rshah",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=5),
}


with DAG(
    dag_id="jira_comment_poller",
    default_args=default_args,
    description="Poll Jira comments and group recent updates by ticket",
    start_date=datetime(2026, 6, 1),
    schedule=timedelta(minutes=10),
    catchup=False,
    tags=["jira", "comments", "quant-loop"],
) as dag:
    poll_comments = PythonOperator(
        task_id="poll_jira_comments",
        python_callable=poll_jira_comments,
    )
