from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def poller(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net/")
    monkeypatch.setenv("JIRA_EMAIL", "airflow@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    monkeypatch.setenv("JIRA_PROJECT_KEY", "ALPHA")
    monkeypatch.setenv("DOWNSTREAM_DAG_ID", "quant_loop_mini")

    class FakeDAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakePythonOperator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeVariable:
        @staticmethod
        def get(name, default=None):
            return default

    class FakeBaseHook:
        @staticmethod
        def get_connection(conn_id):
            raise KeyError(conn_id)

    airflow = types.ModuleType("airflow")
    airflow.DAG = FakeDAG
    airflow_sdk = types.ModuleType("airflow.sdk")
    airflow_sdk.BaseHook = FakeBaseHook
    airflow_sdk.Variable = FakeVariable
    python_operator = types.ModuleType(
        "airflow.providers.standard.operators.python"
    )
    python_operator.PythonOperator = FakePythonOperator
    http_hook = types.ModuleType("airflow.providers.http.hooks.http")
    http_hook.HttpHook = Mock()

    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", airflow_sdk)
    monkeypatch.setitem(sys.modules, "airflow.providers.http.hooks.http", http_hook)
    monkeypatch.setitem(
        sys.modules,
        "airflow.providers.standard.operators.python",
        python_operator,
    )

    dag_path = (
        Path(__file__).parents[1] / "dags" / "jira_comment_poller.py"
    )
    spec = importlib.util.spec_from_file_location("jira_comment_poller", dag_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_jira_datetime(poller) -> None:
    parsed = poller.parse_jira_datetime("2026-06-09T12:34:56.789+0000")

    assert parsed == datetime(2026, 6, 9, 12, 34, 56, 789000, timezone.utc)


def test_poll_filters_comments_and_groups_by_issue(
    poller, monkeypatch, capsys
) -> None:
    response = Mock()
    response.status_code = 200
    response.text = '{"issues": [...]}'
    response.json.return_value = {
        "issues": [
            {
                "id": "10001",
                "key": "ALPHA-1",
                "fields": {
                    "summary": "Backtest strategy",
                    "description": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [
                            {"type": "text", "text": "Use meeting notes. password=hunter2"}
                        ]}],
                    },
                    "status": {"name": "To Do"},
                    "issuetype": {"name": "Task"},
                    "updated": "2026-06-09T12:00:00.000+0000",
                    "comment": {
                        "comments": [
                            {
                                "id": "recent",
                                "author": {
                                    "displayName": "Rahil",
                                    "emailAddress": "rahil@example.com",
                                    "accountId": "account-1",
                                },
                                "created": "2026-06-09T11:50:00.000+0000",
                                "updated": "2026-06-09T11:55:00.000+0000",
                                "body": {
                                    "type": "doc",
                                    "content": [{"type": "paragraph", "content": [
                                        {"type": "text", "text": "/quant Run it start_date=2024-01-01 end_date=2024-12-31"}
                                    ]}],
                                },
                            },
                            {
                                "id": "old",
                                "updated": "2026-06-09T10:00:00.000+0000",
                            },
                        ]
                    },
                },
            }
        ]
    }
    jira_request = Mock(return_value=response)
    monkeypatch.setattr(poller, "jira_request", jira_request)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(poller, "datetime", FixedDatetime)

    grouped = poller.poll_jira_comments()

    jira_request.assert_called_once()
    call = jira_request.call_args
    assert call.args[:2] == ("POST", "/rest/api/3/search/jql")
    assert call.kwargs["json"]["jql"] == (
        "project = ALPHA AND updated >= -30m ORDER BY updated ASC"
    )
    assert call.kwargs["timeout"] == 30
    assert call.kwargs["json"]["fields"] == [
        "summary",
        "description",
        "status",
        "issuetype",
        "updated",
        "comment",
        "attachment",
    ]
    response.raise_for_status.assert_called_once_with()

    output = capsys.readouterr().out
    assert "Found 1 ticket-level comment update groups" in output
    assert "Would trigger DAG quant_loop_mini for ALPHA-1" in output
    assert "Prepared 1 bounded history events (1 triggering)" in output
    assert "hunter2" not in output
    ticket = grouped["ALPHA-1"]["ticket"]
    assert ticket["summary"] == "Backtest strategy"
    assert ticket["description"] == "Use meeting notes. password=[REDACTED]"
    assert ticket["status"] == "To Do"
    assert grouped["ALPHA-1"]["request"]["triggering_comments"][0]["text"].startswith(
        "/quant Run it"
    )
    requester = grouped["ALPHA-1"]["request"]["triggering_comments"][0]
    assert requester["author_display_name"] == "Rahil"
    assert requester["author_email"] == "rahil@example.com"
    assert requester["author_account_id"] == "account-1"


def test_poll_raises_for_jira_http_error(poller, monkeypatch) -> None:
    response = Mock()
    response.status_code = 401
    response.text = "Unauthorized"
    response.raise_for_status.side_effect = requests.HTTPError("401")
    monkeypatch.setattr(poller, "jira_request", Mock(return_value=response))

    with pytest.raises(requests.HTTPError, match="401"):
        poller.poll_jira_comments()
