from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def candidate(monkeypatch):
    class FakeOutput:
        pass

    class FakeDAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeTask:
        def __init__(self, function):
            self.function = function

        def __call__(self, *args, **kwargs):
            return FakeOutput()

    def fake_task(**_kwargs):
        def decorator(function):
            return FakeTask(function)

        return decorator

    airflow = types.ModuleType("airflow")
    airflow_sdk = types.ModuleType("airflow.sdk")
    airflow_sdk.DAG = FakeDAG
    airflow_sdk.get_current_context = Mock()
    airflow_sdk.task = fake_task
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", airflow_sdk)

    sandbox_runner = types.ModuleType("docker_sandbox_runner")
    sandbox_runner.run_and_writeback = Mock(return_value={"status": "succeeded"})
    monkeypatch.setitem(sys.modules, "docker_sandbox_runner", sandbox_runner)

    common = types.ModuleType("jira_quant_common")

    @contextmanager
    def namespace(_prefix):
        yield

    common.airflow_variable_namespace = namespace
    common.extract_plain_text_from_adf = lambda body: str(body or "")
    common.get_airflow_variable = lambda name, default=None: default
    common.is_automated_result_comment = lambda text: "[quant-loop-bot]" in text
    common.jira_request = Mock()
    common.normalize_jira_attachments = lambda value: value if isinstance(value, list) else []
    common.sanitize_ticket_text = lambda value, limit: str(value or "")[:limit]
    monkeypatch.setitem(sys.modules, "jira_quant_common", common)

    dag_path = Path(__file__).parents[1] / "dags" / "jira_exp2_si_runner.py"
    spec = importlib.util.spec_from_file_location("jira_exp2_si_runner", dag_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_candidate_marker_is_exact_and_canonical(candidate) -> None:
    assert candidate.canonicalize_candidate_command(
        "/quant-exp2\nRefactor safely\nrag_enabled: true"
    ) == "/quant\nRefactor safely\nrag_enabled: true"
    assert candidate.canonicalize_candidate_command("/quant regular") is None
    assert candidate.canonicalize_candidate_command("/quant-exp20 wrong") is None


def test_comment_fetch_is_paginated(candidate, monkeypatch) -> None:
    responses = iter(
        [
            FakeResponse(
                {"startAt": 0, "total": 3, "comments": [{"id": "1"}, {"id": "2"}]}
            ),
            FakeResponse(
                {"startAt": 2, "total": 3, "comments": [{"id": "3"}]}
            ),
        ]
    )
    request = Mock(side_effect=lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(candidate, "jira_request", request)

    assert [item["id"] for item in candidate.fetch_all_issue_comments("SCRUM-201")] == [
        "1",
        "2",
        "3",
    ]
    assert request.call_count == 2
    assert request.call_args_list[1].kwargs["params"]["startAt"] == 2


def test_build_payload_fetches_only_selected_candidate_comment(
    candidate,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        candidate,
        "get_airflow_variable",
        lambda name, default=None: "SCRUM" if name == "ALLOWED_PROJECT_KEY" else default,
    )
    issue = FakeResponse(
        {
            "id": "10201",
            "fields": {
                "summary": "Paired task",
                "description": "Same task body",
                "status": {"name": "To Do"},
                "issuetype": {"name": "Task"},
                "updated": "2026-08-18T12:00:00.000+0000",
                "attachment": [],
            },
        }
    )
    monkeypatch.setattr(candidate, "jira_request", Mock(return_value=issue))
    monkeypatch.setattr(
        candidate,
        "fetch_all_issue_comments",
        lambda _key: [
            {
                "id": "11",
                "created": "2026-08-18T11:00:00.000+0000",
                "updated": "2026-08-18T11:00:00.000+0000",
                "author": {"displayName": "Student", "accountId": "abc"},
                "body": "/quant production command",
            },
            {
                "id": "12",
                "created": "2026-08-18T12:00:00.000+0000",
                "updated": "2026-08-18T12:00:00.000+0000",
                "author": {"displayName": "Student", "accountId": "abc"},
                "body": "/quant-exp2\nRefactor safely\nrag_enabled: false",
            },
        ],
    )

    payload, comment_id = candidate.build_candidate_payload(
        "SCRUM-201",
        requested_comment_id="12",
    )

    assert comment_id == "12"
    assert payload["request"]["triggering_comments"][0]["text"].startswith("/quant\n")
    assert payload["request"]["triggering_comments"][0]["comment_id"] == "12"
    assert payload["ticket"]["key"] == "SCRUM-201"


def test_candidate_configuration_requires_digest_and_docker(candidate, monkeypatch) -> None:
    values = {
        "EXP2_SI_ALLOWED_PROJECT_KEY": "SCRUM",
        "EXP2_SI_SANDBOX_EXECUTION_MODE": "docker",
        "EXP2_SI_SANDBOX_IMAGE": "ghcr.io/org/repo/sandbox@sha256:" + "a" * 64,
        "EXP2_SI_JIRA_RAG_INDEX_PATH": "/opt/airflow3/private-rag/index.json",
        "EXP2_SI_JIRA_RAG_INDEX_SHA256": "b" * 64,
        "EXP2_SI_WORKFLOW_RECOVERY_STATE_DIR": "/opt/airflow3/private-runs/state",
        "EXP2_SI_USE_REAL_BACKTESTER": "false",
    }
    monkeypatch.setattr(
        candidate,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )
    assert candidate.validate_candidate_configuration()["SANDBOX_EXECUTION_MODE"] == "docker"

    values["EXP2_SI_SANDBOX_IMAGE"] = "ghcr.io/org/repo/sandbox:latest"
    with pytest.raises(ValueError, match="immutable"):
        candidate.validate_candidate_configuration()


def test_airflow_task_does_not_return_verbose_result(candidate, monkeypatch) -> None:
    dag_run = types.SimpleNamespace(
        conf={"issue_key": "SCRUM-184", "comment_id": "14207"},
        run_id="manual__c1",
    )
    monkeypatch.setattr(candidate, "get_current_context", lambda: {"dag_run": dag_run})
    run_issue = Mock(return_value={"status": "succeeded", "large": "x" * 100_000})
    monkeypatch.setattr(candidate, "run_exp2_issue", run_issue)

    returned = candidate.run_exp2_candidate_and_writeback.function()

    assert returned is None
    run_issue.assert_called_once_with(dag_run.conf, dag_run.run_id)
