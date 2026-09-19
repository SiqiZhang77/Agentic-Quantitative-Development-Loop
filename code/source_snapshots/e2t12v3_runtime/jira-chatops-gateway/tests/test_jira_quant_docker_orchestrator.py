from __future__ import annotations

from datetime import datetime, timezone, timedelta
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def orchestrator(monkeypatch, request):
    monkeypatch.setenv("JIRA_PROJECT_KEY", "SCRUM")
    airflow_variables = getattr(request, "param", {})
    dag_dir = Path(__file__).parents[1] / "dags"
    monkeypatch.syspath_prepend(str(dag_dir))
    monkeypatch.delitem(sys.modules, "jira_quant_common", raising=False)

    class FakeOutput:
        pass

    class FakeMappedOperator:
        pass

    class FakeDAG:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    class FakeTask:
        def __init__(self, function, task_kwargs):
            self.function = function
            self.task_kwargs = task_kwargs

        def __call__(self, *args, **kwargs):
            return FakeOutput()

        def expand(self, **kwargs):
            return FakeMappedOperator()

    def fake_task(**task_kwargs):
        def decorator(function):
            return FakeTask(function, task_kwargs)

        return decorator

    class FakeTriggerDagRunOperator:
        @classmethod
        def partial(cls, **kwargs):
            return cls()

        def expand(self, **kwargs):
            return FakeMappedOperator()

    class FakeVariable:
        @staticmethod
        def get(name, default=None):
            return airflow_variables.get(name, default)

    class FakeBaseHook:
        @staticmethod
        def get_connection(conn_id):
            raise KeyError(conn_id)

    airflow = types.ModuleType("airflow")
    airflow_sdk = types.ModuleType("airflow.sdk")
    airflow_sdk.BaseHook = FakeBaseHook
    airflow_sdk.DAG = FakeDAG
    airflow_sdk.task = fake_task
    airflow_sdk.Variable = FakeVariable
    trigger_dagrun = types.ModuleType(
        "airflow.providers.standard.operators.trigger_dagrun"
    )
    trigger_dagrun.TriggerDagRunOperator = FakeTriggerDagRunOperator
    http_hook = types.ModuleType("airflow.providers.http.hooks.http")
    http_hook.HttpHook = Mock()

    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", airflow_sdk)
    monkeypatch.setitem(sys.modules, "airflow.providers.http.hooks.http", http_hook)
    monkeypatch.setitem(
        sys.modules,
        "airflow.providers.standard.operators.trigger_dagrun",
        trigger_dagrun,
    )

    dag_path = (
        Path(__file__).parents[1]
        / "dags"
        / "jira_quant_docker_orchestrator.py"
    )
    spec = importlib.util.spec_from_file_location(
        "jira_quant_docker_orchestrator",
        dag_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_non_secret_jira_project_key_comes_from_airflow_config(
    orchestrator,
) -> None:
    assert orchestrator.require_airflow_variable("JIRA_PROJECT_KEY") == "SCRUM"


def test_airflow_variable_takes_precedence(
    orchestrator, monkeypatch
) -> None:
    monkeypatch.setenv("SANDBOX_IMAGE", "local-image")
    variable_get = Mock(return_value="airflow-image")
    monkeypatch.setattr(
        orchestrator.get_airflow_variable.__globals__["Variable"],
        "get",
        variable_get,
    )

    assert orchestrator.get_airflow_variable("SANDBOX_IMAGE") == "airflow-image"
    variable_get.assert_called_once_with("SANDBOX_IMAGE", "local-image")


def test_airflow_variable_falls_back_to_worker_environment(
    orchestrator, monkeypatch
) -> None:
    monkeypatch.setenv("SANDBOX_IMAGE", "local-image")

    assert orchestrator.get_airflow_variable("SANDBOX_IMAGE") == "local-image"


@pytest.mark.parametrize(
    "orchestrator",
    [
        {
            "SANDBOX_IMAGE": "shared-image",
            "EXP2_SI_SANDBOX_IMAGE": "candidate-image",
        }
    ],
    indirect=True,
)
def test_airflow_variable_namespace_is_scoped_and_falls_back(orchestrator) -> None:
    common = sys.modules["jira_quant_common"]

    assert orchestrator.get_airflow_variable("SANDBOX_IMAGE") == "shared-image"
    with common.airflow_variable_namespace("EXP2_SI_"):
        assert orchestrator.get_airflow_variable("SANDBOX_IMAGE") == "candidate-image"
        assert orchestrator.get_airflow_variable("UNSET", "fallback") == "fallback"
    assert orchestrator.get_airflow_variable("SANDBOX_IMAGE") == "shared-image"


def test_parse_final_json_stdout_uses_last_non_empty_line(orchestrator) -> None:
    stdout = (
        "backtest engine starting...\n"
        "computing metrics...\n"
        "\n"
        '{"status":"succeeded","issue_key":"SCRUM-5"}\n'
    )

    assert orchestrator.parse_final_json_stdout(stdout) == {
        "status": "succeeded",
        "issue_key": "SCRUM-5",
    }


def test_parse_final_json_stdout_rejects_invalid_last_line(orchestrator) -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        orchestrator.parse_final_json_stdout('{"status":"succeeded"}\nnot-json')


@pytest.mark.parametrize(
    "text",
    [
        "[quant-loop-bot]\nAutomated quant loop result",
        "Automated quant loop result\n\nStatus: succeeded",
    ],
)
def test_automated_result_comments_are_identified(orchestrator, text) -> None:
    assert orchestrator.is_automated_result_comment(text) is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/quant Backtest the momentum strategy", "Backtest the momentum strategy"),
        ("/quant\nBacktest the momentum strategy", "Backtest the momentum strategy"),
        ("Backtest the momentum strategy", None),
        ("/quant", None),
        ("/quantum Backtest the momentum strategy", None),
    ],
)
def test_extract_quant_request(orchestrator, text, expected) -> None:
    assert orchestrator.extract_quant_request(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        '{"api_key":"super-secret-value"}',
        "token: super-secret-value",
        "Authorization: Bearer super-secret-value",
        "Bearer super-secret-value",
    ],
)
def test_ticket_sanitizer_redacts_additional_secret_shapes(
    orchestrator, text
) -> None:
    sanitized = orchestrator.sanitize_ticket_text(text, 4_000)

    assert "super-secret-value" not in sanitized
    assert "[REDACTED]" in sanitized


def test_extract_plain_text_from_adf_preserves_nested_command_blocks(
    orchestrator,
) -> None:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "attrs": {"text": "@Siqi"}},
                    {"type": "text", "text": " please run:"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "/quant"},
                ],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Backtest the momentum strategy",
                                        "marks": [{"type": "strong"}],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Use repo "},
                                    {
                                        "type": "inlineCard",
                                        "attrs": {
                                            "url": "https://github.com/acme/quant"
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                ],
            },
        ],
    }

    assert orchestrator.extract_plain_text_from_adf(adf) == (
        "@Siqi please run:\n"
        "/quant\n"
        "Backtest the momentum strategy\n"
        "Use repo https://github.com/acme/quant"
    )


def test_extract_plain_text_from_adf_handles_tables_and_code_blocks(
    orchestrator,
) -> None:
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {"type": "text", "text": "/quant"}
                                        ],
                                    }
                                ],
                            },
                            {
                                "type": "tableCell",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Backtest EURUSD breakout",
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "text"},
                "content": [
                    {
                        "type": "text",
                        "text": "repo: acme/quant\noptions:\n  max_iterations: 3",
                    }
                ],
            },
        ],
    }

    plain_text = orchestrator.extract_plain_text_from_adf(adf)

    assert plain_text == (
        "/quant\n"
        "Backtest EURUSD breakout\n"
        "repo: acme/quant\n"
        "options:\n"
        "  max_iterations: 3"
    )
    assert orchestrator.extract_quant_request(plain_text) == (
        "Backtest EURUSD breakout\n"
        "repo: acme/quant\n"
        "options:\n"
        "  max_iterations: 3"
    )


def test_fetch_recent_comments_excludes_bot_writebacks(
    orchestrator, monkeypatch
) -> None:
    def adf(text):
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }

    response = Mock(status_code=200, text='{"issues":[]}')
    response.json.return_value = {
        "issues": [
            {
                "id": "10005",
                "key": "SCRUM-5",
                "fields": {
                    "summary": "Run a backtest",
                    "description": adf("Keep the existing history contract."),
                    "issuetype": {"name": "Task"},
                    "status": {"name": "In Progress"},
                    "updated": "2999-06-12T10:02:00.000+0000",
                    "comment": {
                        "comments": [
                            {
                                "id": "1",
                                "created": "2999-06-12T10:00:00.000+0000",
                                "updated": "2999-06-12T10:00:00.000+0000",
                                "body": adf(
                                    "/quant Backtest the momentum strategy"
                                ),
                            },
                            {
                                "id": "2",
                                "created": "2999-06-12T10:01:00.000+0000",
                                "updated": "2999-06-12T10:01:00.000+0000",
                                "body": adf(
                                    "[quant-loop-bot]\n"
                                    "Automated quant loop result"
                                ),
                            },
                            {
                                "id": "3",
                                "created": "2999-06-12T10:02:00.000+0000",
                                "updated": "2999-06-12T10:02:00.000+0000",
                                "body": adf("Automated quant loop result"),
                            },
                            {
                                "id": "4",
                                "created": "2999-06-12T10:03:00.000+0000",
                                "updated": "2999-06-12T10:03:00.000+0000",
                                "body": adf("A normal Jira discussion comment"),
                            },
                        ]
                    },
                },
            }
        ]
    }
    jira_request = Mock(return_value=response)
    monkeypatch.setattr(orchestrator, "jira_request", jira_request)

    grouped_events = orchestrator.fetch_recent_comment_groups()

    assert jira_request.call_args.args[:2] == ("POST", "/rest/api/3/search/jql")
    assert "project = SCRUM" in jira_request.call_args.kwargs["json"]["jql"]
    assert "description" in jira_request.call_args.kwargs["json"]["fields"]
    assert "status" in jira_request.call_args.kwargs["json"]["fields"]
    ticket = grouped_events["SCRUM-5"]["ticket"]
    assert ticket["description"] == "Keep the existing history contract."
    assert ticket["status"] == "In Progress"
    comments = grouped_events["SCRUM-5"]["request"]["comments"]
    assert [comment["text"] for comment in comments] == [
        "/quant Backtest the momentum strategy",
        "A normal Jira discussion comment",
    ]
    triggering = grouped_events["SCRUM-5"]["request"]["triggering_comments"]
    assert [comment["request_text"] for comment in triggering] == [
        "Backtest the momentum strategy"
    ]


def test_fetch_recent_comments_uses_five_minute_jql_and_cutoff(
    orchestrator, monkeypatch
) -> None:
    response = Mock(status_code=200, text='{"issues":[]}')
    response.json.return_value = {
        "issues": [
            {
                "id": "10020",
                "key": "SCRUM-20",
                "fields": {
                    "summary": "Check lookback",
                    "issuetype": {"name": "Task"},
                    "updated": "2026-06-09T12:05:00.000+0000",
                    "comment": {
                        "comments": [
                            {
                                "id": "recent",
                                "created": "2026-06-09T11:59:00.000+0000",
                                "updated": "2026-06-09T11:59:00.000+0000",
                                "body": {"text": "/quant recent request"},
                            },
                            {
                                "id": "old",
                                "created": "2026-06-09T11:54:00.000+0000",
                                "updated": "2026-06-09T11:54:00.000+0000",
                                "body": {"text": "/quant old request"},
                            },
                        ]
                    },
                },
            }
        ]
    }

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 9, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(orchestrator, "datetime", FixedDatetime)
    monkeypatch.setattr(
        orchestrator,
        "extract_plain_text_from_adf",
        lambda body: body["text"],
    )
    monkeypatch.setattr(orchestrator, "jira_request", Mock(return_value=response))

    grouped_events = orchestrator.fetch_recent_comment_groups()

    assert [
        comment["comment_id"]
        for comment in grouped_events["SCRUM-20"]["request"][
            "triggering_comments"
        ]
    ] == ["recent"]
    assert [
        comment["comment_id"]
        for comment in grouped_events["SCRUM-20"]["request"]["comments"]
    ] == ["old", "recent"]
    assert (
        "updated >= -5m"
        in orchestrator.jira_request.call_args.kwargs["json"]["jql"]
    )


def test_active_orchestrator_defaults_to_five_minute_poll_schedule(orchestrator) -> None:
    assert orchestrator.POLL_INTERVAL_MINUTES == 5
    assert orchestrator.dag.kwargs["schedule"] == timedelta(minutes=5)


@pytest.mark.parametrize(
    "orchestrator",
    [{"POLL_INTERVAL_MINUTES": "1"}],
    indirect=True,
)
def test_active_orchestrator_uses_configured_poll_schedule(orchestrator) -> None:
    assert orchestrator.POLL_INTERVAL_MINUTES == 1
    assert orchestrator.dag.kwargs["schedule"] == timedelta(minutes=1)


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_poll_interval_must_be_a_positive_integer(
    orchestrator, monkeypatch, value
) -> None:
    monkeypatch.setattr(
        orchestrator,
        "get_airflow_variable",
        Mock(return_value=value),
    )

    with pytest.raises(
        ValueError,
        match="POLL_INTERVAL_MINUTES must be a positive integer",
    ):
        orchestrator.get_poll_interval_minutes()


def test_fetch_recent_comments_extracts_multiline_adf_command(
    orchestrator, monkeypatch
) -> None:
    response = Mock(status_code=200, text='{"issues":[]}')
    response.json.return_value = {
        "issues": [
            {
                "id": "10015",
                "key": "SCRUM-15",
                "fields": {
                    "summary": "Run a rich Jira command",
                    "issuetype": {"name": "Task"},
                    "updated": "2999-06-12T10:02:00.000+0000",
                    "comment": {
                        "comments": [
                            {
                                "id": "rich-adf",
                                "created": "2999-06-12T10:00:00.000+0000",
                                "updated": "2999-06-12T10:00:00.000+0000",
                                "body": {
                                    "type": "doc",
                                    "version": 1,
                                    "content": [
                                        {
                                            "type": "paragraph",
                                            "content": [
                                                {
                                                    "type": "text",
                                                    "text": "/quant",
                                                }
                                            ],
                                        },
                                        {
                                            "type": "orderedList",
                                            "content": [
                                                {
                                                    "type": "listItem",
                                                    "content": [
                                                        {
                                                            "type": "paragraph",
                                                            "content": [
                                                                {
                                                                    "type": "text",
                                                                    "text": (
                                                                        "Backtest "
                                                                        "momentum"
                                                                    ),
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                },
                                                {
                                                    "type": "listItem",
                                                    "content": [
                                                        {
                                                            "type": "paragraph",
                                                            "content": [
                                                                {
                                                                    "type": "text",
                                                                    "text": (
                                                                        "Report "
                                                                        "Sharpe "
                                                                        "and max "
                                                                        "drawdown"
                                                                    ),
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                },
                                            ],
                                        },
                                    ],
                                },
                            }
                        ]
                    },
                },
            }
        ]
    }
    monkeypatch.setattr(orchestrator, "jira_request", Mock(return_value=response))

    grouped_events = orchestrator.fetch_recent_comment_groups()

    comments = grouped_events["SCRUM-15"]["request"]["comments"]
    assert comments[0]["text"] == (
        "/quant\nBacktest momentum\nReport Sharpe and max drawdown"
    )
    triggering = grouped_events["SCRUM-15"]["request"]["triggering_comments"]
    assert triggering[0]["request_text"] == (
        "Backtest momentum\nReport Sharpe and max drawdown"
    )


def test_fetch_recent_comments_skips_only_ticket_with_incomplete_embedded_page(
    orchestrator, monkeypatch
) -> None:
    response = Mock(status_code=200, text='{"issues":[]}')
    response.json.return_value = {
        "issues": [
            {
                "id": "10030",
                "key": "SCRUM-30",
                "fields": {
                    "summary": "Paged comments",
                    "issuetype": {"name": "Task"},
                    "updated": "2999-06-12T10:02:00.000+0000",
                    "comment": {"total": 21, "comments": []},
                },
            }
        ]
    }
    monkeypatch.setattr(orchestrator, "jira_request", Mock(return_value=response))

    assert orchestrator.fetch_recent_comment_groups() == {}


def test_poll_prepares_plain_request_and_separate_metadata(
    orchestrator, monkeypatch
) -> None:
    grouped_event = {
        "ticket": {
            "key": "SCRUM-5",
            "id": "10005",
            "summary": "Run a backtest",
        },
        "request": {
            "event_type": "jira_comment_update",
            "comments": [
                {
                    "updated": "2026-06-11T10:00:00.000+0000",
                    "text": "/quant Backtest the momentum strategy",
                    "request_text": "Backtest the momentum strategy",
                }
            ],
        },
    }
    monkeypatch.setattr(
        orchestrator,
        "fetch_recent_comment_groups",
        Mock(return_value={"SCRUM-5": grouped_event}),
    )

    container_inputs = orchestrator.poll_and_prepare_container_inputs.function()

    assert container_inputs == [grouped_event]
