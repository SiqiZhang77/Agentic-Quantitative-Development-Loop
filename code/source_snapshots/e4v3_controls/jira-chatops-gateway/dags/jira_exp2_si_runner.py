"""Manually launch an isolated Experiment 2 C0/C1 run from one Jira ticket.

This DAG is deliberately unscheduled.  It recognises ``/quant-exp2`` rather
than the production ``/quant`` marker, fetches the selected Jira issue through
the existing read/write connection, canonicalises that marker to ``/quant`` in
memory, and then executes the feature-branch runner under the ``EXP2_SI_``
Airflow Variable namespace.

The deployed loader imports this file from the private ``exp2_si_runtime``
package.  The fallback imports keep the source directly testable in the normal
repository layout.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from airflow.sdk import DAG, get_current_context, task

try:
    from . import docker_sandbox_runner as sandbox_runner
    from .jira_quant_common import (
        airflow_variable_namespace,
        extract_plain_text_from_adf,
        get_airflow_variable,
        is_automated_result_comment,
        jira_request,
        normalize_jira_attachments,
        sanitize_ticket_text,
    )
except ImportError:
    import docker_sandbox_runner as sandbox_runner
    from jira_quant_common import (
        airflow_variable_namespace,
        extract_plain_text_from_adf,
        get_airflow_variable,
        is_automated_result_comment,
        jira_request,
        normalize_jira_attachments,
        sanitize_ticket_text,
    )


DAG_ID = "jira_exp2_si_runner"
VARIABLE_NAMESPACE = "EXP2_SI_"
CANDIDATE_COMMAND_MARKER = "/quant-exp2"
PRODUCTION_COMMAND_MARKER = "/quant"
MAX_HISTORY_COMMENTS = 20
MAX_SUMMARY_CHARS = 500
MAX_DESCRIPTION_CHARS = 12_000
MAX_COMMENT_CHARS = 4_000
MAX_COMMENT_PAGES = 100
_ISSUE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9]+)-[1-9][0-9]*$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def exact_candidate_variable(name: str) -> str:
    """Return one explicitly configured candidate override.

    This lookup runs before the namespace is active, so it cannot silently fall
    back to a production value with the same unprefixed name.
    """

    full_name = f"{VARIABLE_NAMESPACE}{name}"
    value = get_airflow_variable(full_name)
    if value is None or not str(value).strip():
        raise ValueError(f"Missing required candidate Airflow Variable {full_name}")
    return str(value).strip()


def validate_candidate_configuration() -> dict[str, str]:
    """Fail closed before Jira access if candidate isolation is incomplete."""

    values = {
        name: exact_candidate_variable(name)
        for name in (
            "ALLOWED_PROJECT_KEY",
            "SANDBOX_EXECUTION_MODE",
            "SANDBOX_IMAGE",
            "JIRA_RAG_INDEX_PATH",
            "JIRA_RAG_INDEX_SHA256",
            "WORKFLOW_RECOVERY_STATE_DIR",
            "USE_REAL_BACKTESTER",
        )
    }

    if values["SANDBOX_EXECUTION_MODE"].lower() != "docker":
        raise ValueError("EXP2_SI_SANDBOX_EXECUTION_MODE must be docker")
    if "@sha256:" not in values["SANDBOX_IMAGE"]:
        raise ValueError(
            "EXP2_SI_SANDBOX_IMAGE must use an immutable @sha256: image digest"
        )
    index_path = Path(values["JIRA_RAG_INDEX_PATH"])
    if not index_path.is_absolute():
        raise ValueError("EXP2_SI_JIRA_RAG_INDEX_PATH must be an absolute path")
    if not _SHA256_RE.fullmatch(values["JIRA_RAG_INDEX_SHA256"]):
        raise ValueError(
            "EXP2_SI_JIRA_RAG_INDEX_SHA256 must be 64 lowercase hexadecimal characters"
        )
    state_path = Path(values["WORKFLOW_RECOVERY_STATE_DIR"])
    if not state_path.is_absolute():
        raise ValueError(
            "EXP2_SI_WORKFLOW_RECOVERY_STATE_DIR must be an absolute path"
        )
    if values["USE_REAL_BACKTESTER"].lower() != "false":
        raise ValueError(
            "EXP2_SI_USE_REAL_BACKTESTER must be false for the coding-task C0/C1 run"
        )
    if not re.fullmatch(r"[A-Z][A-Z0-9]+", values["ALLOWED_PROJECT_KEY"]):
        raise ValueError("EXP2_SI_ALLOWED_PROJECT_KEY is not a valid Jira project key")
    return values


def canonicalize_candidate_command(text: str) -> str | None:
    """Convert an exact ``/quant-exp2`` command into the production contract."""

    stripped = str(text or "").strip()
    parts = stripped.split(maxsplit=1)
    if len(parts) != 2 or parts[0] != CANDIDATE_COMMAND_MARKER:
        return None
    body = parts[1].strip()
    return f"{PRODUCTION_COMMAND_MARKER}\n{body}" if body else None


def fetch_all_issue_comments(issue_key: str) -> list[dict[str, Any]]:
    """Fetch every visible comment page for a single explicitly selected issue."""

    comments: list[dict[str, Any]] = []
    start_at = 0
    encoded_key = quote(issue_key, safe="")

    for _ in range(MAX_COMMENT_PAGES):
        response = jira_request(
            "GET",
            f"/rest/api/3/issue/{encoded_key}/comment",
            headers={"Accept": "application/json"},
            params={"startAt": start_at, "maxResults": 100},
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        page = body.get("comments") or []
        if not isinstance(page, list):
            raise ValueError("Jira comment response did not contain a list")
        comments.extend(item for item in page if isinstance(item, dict))

        total = body.get("total")
        if body.get("isLast") is True:
            return comments
        if not page:
            if isinstance(total, int) and start_at < total:
                raise ValueError("Jira comment pagination stopped before total comments")
            return comments
        next_start = int(body.get("startAt", start_at)) + len(page)
        if next_start <= start_at:
            raise ValueError("Jira comment pagination made no progress")
        if isinstance(total, int) and next_start >= total:
            return comments
        start_at = next_start

    raise ValueError("Jira comment pagination exceeded the safety page limit")


def build_candidate_payload(
    issue_key: str,
    *,
    requested_comment_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Fetch one issue and build the existing grouped Jira runner payload."""

    match = _ISSUE_KEY_RE.fullmatch(str(issue_key or "").strip().upper())
    if match is None:
        raise ValueError("dag_run.conf.issue_key must look like SCRUM-123")
    normalized_key = match.group(0)
    allowed_project = str(get_airflow_variable("ALLOWED_PROJECT_KEY") or "").strip()
    if match.group(1) != allowed_project:
        raise ValueError(
            f"Candidate runner accepts only {allowed_project}-* issues, got {normalized_key}"
        )

    encoded_key = quote(normalized_key, safe="")
    issue_response = jira_request(
        "GET",
        f"/rest/api/3/issue/{encoded_key}",
        headers={"Accept": "application/json"},
        params={
            "fields": "summary,description,status,issuetype,updated,attachment",
        },
        timeout=30,
    )
    issue_response.raise_for_status()
    issue = issue_response.json()
    fields = issue.get("fields") or {}
    if not isinstance(fields, dict):
        raise ValueError("Jira issue response did not contain fields")

    normalized_comments: list[dict[str, Any]] = []
    candidate_comments: list[dict[str, Any]] = []
    requested_id = str(requested_comment_id or "").strip()

    for raw_comment in fetch_all_issue_comments(normalized_key):
        author = raw_comment.get("author") or {}
        updated = raw_comment.get("updated") or raw_comment.get("created")
        if not updated:
            continue
        text = sanitize_ticket_text(
            extract_plain_text_from_adf(raw_comment.get("body")).strip(),
            MAX_COMMENT_CHARS,
        )
        if not text or is_automated_result_comment(text):
            continue
        comment_id = str(raw_comment.get("id") or "")
        common = {
            "comment_id": comment_id,
            "author": str(author.get("displayName") or "Unknown"),
            "created": raw_comment.get("created") or updated,
            "updated": updated,
            "text": text,
        }
        normalized_comments.append(common)
        canonical_text = canonicalize_candidate_command(text)
        if canonical_text is None:
            continue
        if requested_id and comment_id != requested_id:
            continue
        candidate_comments.append(
            {
                **common,
                "text": canonical_text,
                "author_display_name": str(author.get("displayName") or "Unknown"),
                "author_email": str(author.get("emailAddress") or ""),
                "author_account_id": str(author.get("accountId") or ""),
            }
        )

    if not candidate_comments:
        qualifier = f" with id {requested_id}" if requested_id else ""
        raise ValueError(
            f"No valid {CANDIDATE_COMMAND_MARKER} comment{qualifier} was found on {normalized_key}"
        )
    selected = max(
        candidate_comments,
        key=lambda item: (str(item["updated"]), str(item["comment_id"])),
    )

    history = sorted(
        normalized_comments,
        key=lambda item: (str(item["updated"]), str(item["comment_id"])),
    )[-MAX_HISTORY_COMMENTS:]
    selected_in_history = False
    for item in history:
        if item["comment_id"] == selected["comment_id"]:
            item["text"] = selected["text"]
            selected_in_history = True
    if not selected_in_history:
        history = (history + [{key: selected[key] for key in (
            "comment_id", "author", "created", "updated", "text"
        )}])[-MAX_HISTORY_COMMENTS:]
        history.sort(key=lambda item: (str(item["updated"]), str(item["comment_id"])))

    description = fields.get("description")
    if description is None:
        description = ""
    elif not isinstance(description, str):
        description = extract_plain_text_from_adf(description)
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}

    payload = {
        "ticket": {
            "key": normalized_key,
            "id": str(issue.get("id") or ""),
            "summary": sanitize_ticket_text(
                fields.get("summary") or normalized_key,
                MAX_SUMMARY_CHARS,
            ),
            "description": sanitize_ticket_text(description, MAX_DESCRIPTION_CHARS),
            "status": str(status.get("name") or "Unknown"),
            "issue_type": str(issue_type.get("name") or "Unknown"),
            "updated": fields.get("updated"),
        },
        "request": {
            "event_type": "jira_comment_update",
            "comments": history,
            "triggering_comments": [selected],
            "attachments": normalize_jira_attachments(fields.get("attachment")),
        },
    }
    return payload, str(selected["comment_id"])


def run_exp2_issue(conf: dict[str, Any], dag_run_id: str) -> dict[str, Any]:
    """Validate the isolated deployment, fetch Jira, then execute/write back."""

    if not isinstance(conf, dict):
        raise ValueError("jira_exp2_si_runner dag_run.conf must be a JSON object")
    unknown = sorted(set(conf) - {"issue_key", "comment_id"})
    if unknown:
        raise ValueError(
            "Unsupported candidate dag_run.conf field(s): " + ", ".join(unknown)
        )
    validate_candidate_configuration()
    with airflow_variable_namespace(VARIABLE_NAMESPACE):
        payload, selected_comment_id = build_candidate_payload(
            str(conf.get("issue_key") or ""),
            requested_comment_id=(
                str(conf["comment_id"]) if conf.get("comment_id") is not None else None
            ),
        )
        print(
            "Prepared isolated Experiment 2 Jira request "
            f"issue={payload['ticket']['key']} comment_id={selected_comment_id}"
        )
        return sandbox_runner.run_and_writeback(payload, dag_run_id)


@task(
    task_id="run_exp2_candidate_and_writeback",
    execution_timeout=timedelta(hours=3),
    retries=0,
    do_xcom_push=False,
)
def run_exp2_candidate_and_writeback() -> None:
    """Execute the self-contained candidate run without persisting its result.

    ``run_exp2_issue`` already writes the authoritative terminal report to Jira
    and the recovery-state directory.  Returning that verbose result from an
    Airflow task makes TaskFlow try to serialize and persist the entire object
    as XCom after the workflow has succeeded.  This isolated DAG has no
    downstream consumer, so retaining another copy in the metadata database is
    unnecessary and can turn a successful run into a post-execution failure.
    """

    context = get_current_context()
    dag_run = context["dag_run"]
    run_exp2_issue(dag_run.conf or {}, dag_run.run_id)


with DAG(
    dag_id=DAG_ID,
    default_args={"owner": "siqi", "depends_on_past": False, "retries": 0},
    description="Isolated manual C0/C1 Jira RAG runner for Siqi Experiment 2",
    start_date=datetime(2026, 8, 18),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["jira", "exp2", "siqi", "rag", "candidate", "manual"],
) as dag:
    run_exp2_candidate_and_writeback()
