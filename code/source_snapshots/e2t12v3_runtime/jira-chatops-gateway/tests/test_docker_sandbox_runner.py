from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def docker_sandbox_runner(monkeypatch, tmp_path):
    dag_dir = Path(__file__).parents[1] / "dags"
    monkeypatch.syspath_prepend(str(dag_dir))
    monkeypatch.setenv("WORKFLOW_RECOVERY_STATE_DIR", str(tmp_path / "workflow-state"))
    monkeypatch.setenv("AIRFLOW_DAG_GIT_COMMIT", "test-source-revision")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_USERNAME", raising=False)

    class FakeOutput:
        def __rshift__(self, other):
            self.downstream = other
            return other

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

    def fake_task(**task_kwargs):
        def decorator(function):
            return FakeTask(function, task_kwargs)

        return decorator

    class FakeVariable:
        @staticmethod
        def get(name, default=None):
            return default

    class FakeBaseHook:
        @staticmethod
        def get_connection(conn_id):
            raise KeyError(conn_id)

    airflow = types.ModuleType("airflow")
    airflow_sdk = types.ModuleType("airflow.sdk")
    airflow_sdk.DAG = FakeDAG
    airflow_sdk.BaseHook = FakeBaseHook
    airflow_sdk.get_current_context = Mock()
    airflow_sdk.task = fake_task
    airflow_sdk.Variable = FakeVariable
    http_hook = types.ModuleType("airflow.providers.http.hooks.http")
    http_hook.HttpHook = Mock()

    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.sdk", airflow_sdk)
    monkeypatch.setitem(sys.modules, "airflow.providers.http.hooks.http", http_hook)

    dag_path = Path(__file__).parents[1] / "dags" / "docker_sandbox_runner.py"
    spec = importlib.util.spec_from_file_location("docker_sandbox_runner", dag_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setitem(
        module.validate_jira_command_payload.__globals__,
        "validate_repository_access",
        lambda repositories, errors: None,
    )
    monkeypatch.setattr(module, "get_optional_ghcr_credentials", Mock(return_value={}))
    monkeypatch.setattr(
        module,
        "resolve_sandbox_github_credentials",
        Mock(
            return_value=(
                {
                    "GITHUB_USERNAME": "octocat",
                    "GITHUB_TOKEN": "github-secret",
                },
                {
                    "github_username": "octocat",
                    "github_connection_id": "github_user_octocat",
                    "requested_repositories": [
                        "bankingscience/BSLAgenticQuantDevLoop",
                    ],
                    "token_present": True,
                    "token_length": len("github-secret"),
                },
            )
        ),
    )
    return module


def test_run_command_streaming_streams_and_captures_output(
    docker_sandbox_runner,
    capsys,
) -> None:
    completed = docker_sandbox_runner.run_command_streaming(
        [
            sys.executable,
            "-c",
            "import sys; print('live stdout'); print('live stderr', file=sys.stderr)",
        ],
        env={**os.environ},
        input_text="",
        timeout=10,
    )

    assert completed.returncode == 0
    assert "live stdout" in completed.stdout
    assert "live stderr" in completed.stdout
    output = capsys.readouterr().out
    assert "live stdout" in output
    assert "live stderr" in output


def grouped_payload() -> dict:
    return {
        "ticket": {
            "key": "SCRUM-5",
            "id": "10005",
            "summary": "Run a backtest",
            "description": "Meeting notes and acceptance criteria.",
        },
        "request": {
            "event_type": "jira_comment_update",
            "comments": [
                {
                    "comment_id": "1",
                    "updated": "2026-06-11T10:00:00.000+0000",
                    "text": (
                        "/quant First request "
                        "start_date=2024-01-01 end_date=2024-12-31"
                    ),
                },
                {
                    "comment_id": "2",
                    "updated": "2026-06-11T10:05:00.000+0000",
                    "text": (
                        "/quant Latest request strategy_type=backtest ticker=AAPL "
                        "repo=bankingscience/"
                        "BSLAgenticQuantDevLoop start_date=2024-01-01 "
                        "end_date=2024-12-31 max_iterations=3 "
                        "max_failed_iterations=2"
                    ),
                },
            ],
        },
    }


def runtime_success_response() -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "run_SCRUM-5_001",
        "execution_summary": {
            "ticket_id": "SCRUM-5",
            "status": "succeeded",
            "start_time": "2026-06-18T10:31:00Z",
            "end_time": "2026-06-18T10:45:00Z",
            "iteration_traces": [
                {
                    "iteration": 1,
                    "agent": "planner_agent",
                    "tool_call": "parse_execution_objectives",
                    "status": "succeeded",
                    "message": "Parsed objective.",
                    "timestamp": "2026-06-18T10:32:00Z",
                }
            ],
        },
        "performance_metrics": {
            "total_return": 0.45,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.08,
            "alpha": 0.03,
            "beta": 1.1,
            "time_series_data_path": "/workspace/output/artifacts/equity_curve.csv",
        },
        "generated_artifacts": {
            "modified_files": ["src/strategies/momentum.py"],
            "new_files": ["reports/backtest_report.html"],
            "backtest_plots_path": "/workspace/output/artifacts/equity_curve.png",
            "branch_name": "quant/SCRUM-5",
        },
        "diagnostics": {
            "error_code": None,
            "error_message": None,
            "raw_log_reference": "/workspace/output/artifacts/run.log",
        },
    }


def runtime_failure_response() -> dict:
    result = runtime_success_response()
    result["run_id"] = "run_SCRUM-5_002"
    result["execution_summary"] = {
        "ticket_id": "SCRUM-5",
        "status": "failed",
        "start_time": "2026-06-18T11:00:00Z",
        "end_time": "2026-06-18T11:05:00Z",
        "iteration_traces": [
            {
                "iteration": 1,
                "agent": "backtest_agent",
                "tool_call": "run_backtest",
                "status": "failed",
                "message": "Backtest failed.",
                "timestamp": "2026-06-18T11:04:00Z",
            }
        ],
    }
    result["performance_metrics"] = None
    result["generated_artifacts"] = {
        "modified_files": [],
        "new_files": [],
        "backtest_plots_path": None,
        "branch_name": None,
    }
    result["diagnostics"] = {
        "error_code": "RUNTIME_ERROR",
        "error_message": "KeyError: 'choices'",
        "raw_log_reference": "/workspace/output/artifacts/run.log",
    }
    return result


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
def grouped_payload_with_model(model: str = "nova-pro") -> dict:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest model:"
        f"{model} ticker=AAPL repo=bankingscience/BSLAgenticQuantDevLoop "
        "start_date=2024-01-01 end_date=2024-12-31 max_iterations=3"
    )
    return payload


def retrieval_context(top_k: int = 3) -> dict:
    return {
        "enabled": True,
        "status": "ok",
        "query": "Summary: Refactor Jira history",
        "query_sha256": "1" * 64,
        "retriever_name": "jira_lexical_bm25",
        "retriever_version": "1.1.0",
        "corpus_id": "e2-corpus-v1",
        "corpus_sha256": "2" * 64,
        "index_id": "e2-index-v1",
        "index_sha256": "3" * 64,
        "exclusion_list_id": "e2-exclusions-v1",
        "exclusion_list_sha256": "4" * 64,
        "cutoff_at": "2026-06-01T00:00:00+00:00",
        "requested_top_k": top_k,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 10,
        "memories": [
            {
                "memory_id": "MEM-01",
                "source_ticket_id": "SCRUM-4",
                "source_type": "comment",
                "source_id": "comment:10001",
                "source_timestamp": "2026-05-01T00:00:00+00:00",
                "rank": 1,
                "score": 2.5,
                "text": "History must stay chronological and capped.",
            }
        ],
    }


def test_extract_runner_input_selects_latest_quant_comment(docker_sandbox_runner) -> None:
    assert docker_sandbox_runner.extract_runner_input(grouped_payload()) == {
        "issue_key": "SCRUM-5",
        "request_text": (
            "Latest request strategy_type=backtest ticker=AAPL "
            "repo=bankingscience/"
            "BSLAgenticQuantDevLoop start_date=2024-01-01 "
            "end_date=2024-12-31 max_iterations=3 "
            "max_failed_iterations=2"
        ),
    }


def test_extract_runner_input_rejects_missing_quant_comment(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][0]["text"] = "plain comment"
    payload["request"]["comments"][1]["text"] = "also plain"

    with pytest.raises(ValueError, match="no valid /quant request"):
        docker_sandbox_runner.extract_runner_input(payload)


def test_validate_jira_command_builds_runtime_request(docker_sandbox_runner) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    runtime_request = validated.runtime_request

    assert runtime_request["schema_version"] == "1.0"
    assert runtime_request["jira_metadata"]["ticket_id"] == "SCRUM-5"
    assert runtime_request["jira_metadata"]["summary"] == "Run a backtest"
    assert runtime_request["jira_metadata"]["description"] == (
        "Meeting notes and acceptance criteria."
    )
    assert runtime_request["jira_metadata"]["triggering_comment"]["comment_id"] == "2"
    assert [event["text"] for event in runtime_request["jira_metadata"]["events_history"]] == [
        grouped_payload()["request"]["comments"][0]["text"],
        grouped_payload()["request"]["comments"][1]["text"],
    ]
    assert runtime_request["repository_details"] == [
        {
            "alias": "BSLAgenticQuantDevLoop",
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "clone_url": (
                "https://github.com/bankingscience/"
                "BSLAgenticQuantDevLoop.git"
            ),
            "source_branch": "main",
            "target_branch": "quant/SCRUM-5",
            "runtime_role": "default",
            "allowed_directories": ["."],
        }
    ]
    assert runtime_request["execution_objectives"]["strategy_type"] == "backtest"
    assert runtime_request["execution_objectives"]["stock_type"] == "AAPL"
    assert runtime_request["execution_objectives"]["target_date_range"] == {
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    }
    assert runtime_request["iteration_controls"]["max_iterations"] == 3
    assert runtime_request["iteration_controls"]["max_failed_iterations"] == 2
    assert runtime_request["resource_requirements"] == {
        "cpu_vcpus": 2.0,
        "memory_mb": 4096,
        "gpu_count": 0,
        "execution_timeout_seconds": 2100,
        "pids_limit": 256,
    }
    assert runtime_request["output_paths"]["progress_events_path"] == (
        "/workspace/output/progress_events.jsonl"
    )


def test_validate_jira_command_builds_hdfs_input_dataset(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse the Experiment 2 dataset strategy_type=analysis "
        "read_only=true "
        "input_hdfs_uri=hdfs:///user/masteruser/quant-experiment-data/"
        "SCRUM-195/experiment_2_input.csv"
    )

    runtime_request = docker_sandbox_runner.validate_jira_command_payload(
        payload
    ).runtime_request

    assert runtime_request["input_datasets"] == [
        {
            "dataset_id": "experiment_2_input",
            "source_kind": "hdfs",
            "original_filename": "experiment_2_input.csv",
            "source_uri": (
                "hdfs:///user/masteruser/quant-experiment-data/"
                "SCRUM-195/experiment_2_input.csv"
            ),
            "container_path": (
                "/workspace/input/datasets/experiment_2_input.csv"
            ),
            "format": "csv",
            "read_only": True,
        }
    ]


def test_validate_jira_command_accepts_custom_hdfs_input_mount_path(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse data strategy_type=analysis "
        "input_hdfs_uri=hdfs:///approved/source/data.parquet "
        "input_mount_path=/workspace/input/datasets/fund/data.parquet "
        "input_format=parquet"
    )

    dataset = docker_sandbox_runner.validate_jira_command_payload(
        payload
    ).runtime_request["input_datasets"][0]

    assert dataset["container_path"] == (
        "/workspace/input/datasets/fund/data.parquet"
    )
    assert dataset["format"] == "parquet"


@pytest.mark.parametrize(
    "option_text, expected_message",
    [
        (
            "input_hdfs_uri=file:///tmp/data.csv",
            "input_hdfs_uri must be an absolute hdfs:// URI",
        ),
        (
            "input_hdfs_uri=hdfs:///approved/data.csv "
            "input_mount_path=/workspace/output/data.csv",
            "input_mount_path must name a file below",
        ),
        (
            "input_hdfs_uri=hdfs:///approved/data.csv input_format=parquet",
            "does not match the file extension",
        ),
    ],
)
def test_validate_jira_command_rejects_unsafe_hdfs_dataset_options(
    docker_sandbox_runner,
    option_text,
    expected_message,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Analyse data strategy_type=analysis {option_text}"
    )

    with pytest.raises(ValueError, match=expected_message):
        docker_sandbox_runner.validate_jira_command_payload(payload)


def test_validate_jira_command_uses_workflow_timeout_as_execution_default(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "start_date=2024-01-01 end_date=2024-12-31 "
        "timeout=10800"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["iteration_controls"]["timeout_seconds"] == 10800
    assert (
        validated.runtime_request["resource_requirements"][
            "execution_timeout_seconds"
        ]
        == 10800
    )


def test_validate_jira_command_defaults_to_current_repo(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest ticker=AAPL start_date=2024-01-01 "
        "end_date=2024-12-31"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["repository_details"][0]["alias"] == (
        "BSLAgenticQuantDevLoop"
    )
    assert validated.runtime_request["repository_details"][0]["repo_full_name"] == (
        "bankingscience/BSLAgenticQuantDevLoop"
    )


def test_validate_jira_command_builds_five_repo_branch_map(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Run coordinated repository work strategy_type=refactor\n"
        "branch_map: BSLAgenticQuantDevLoop=feature/gateway, "
        "ATPConnectorsRepo=feature/connectors, "
        "ATPDataHandlersRepo=feature/handlers, "
        "ATPSiftingAnalyticsRepo=feature/analytics, "
        "ATPSiftingPreTradeRepo=feature/pretrade"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    repos = validated.runtime_request["repository_details"]

    assert [repo["alias"] for repo in repos] == [
        "BSLAgenticQuantDevLoop",
        "ATPConnectorsRepo",
        "ATPDataHandlersRepo",
        "ATPSiftingAnalyticsRepo",
        "ATPSiftingPreTradeRepo",
    ]
    assert repos[0]["source_branch"] == "feature/gateway"
    assert repos[3]["repo_full_name"] == "bankingscience/ATPSiftingAnalyticsRepo"
    assert repos[3]["source_branch"] == "feature/analytics"
    assert {repo["target_branch"] for repo in repos} == {"quant/SCRUM-5"}
    assert all("backtest_request_key" not in repo for repo in repos)


def test_validate_jira_command_uses_catalog_source_branch_defaults(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Run repository checks strategy_type=analysis "
        "repos=BSLAgenticQuantDevLoop,ATPConnectorsRepo,ATPDataHandlersRepo,"
        "ATPSiftingAnalyticsRepo,ATPSiftingPreTradeRepo"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    repos = validated.runtime_request["repository_details"]

    assert [repo["source_branch"] for repo in repos] == [
        "main",
        "develop",
        "develop",
        "develop",
        "develop",
    ]


def test_validate_jira_command_normalizes_alias_casing_and_whitespace(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Run repository work strategy_type=refactor "
        "repos=\" atpconnectorsrepo , HTTPS://GITHUB.COM/BANKINGSCIENCE/ATPDATAHANDLERSREPO.git \""
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert [repo["alias"] for repo in validated.runtime_request["repository_details"]] == [
        "ATPConnectorsRepo",
        "ATPDataHandlersRepo",
    ]


def test_validate_jira_command_rejects_unknown_alias(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest repos=unknown_repo "
        "start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "Unknown repository alias 'unknown_repo'" in "\n".join(exc_info.value.errors)
    assert "ATPConnectorsRepo" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_rejects_conflicting_duplicate_alias(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "start_date=2024-01-01 end_date=2024-12-31\n"
        "branch_map: ATPConnectorsRepo=feature/a, atpconnectorsrepo=feature/b"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "has conflicting source branches" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_allows_same_target_branch_in_different_repositories(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=refactor "
        "repos=ATPConnectorsRepo,ATPDataHandlersRepo "
        "target_branch_map=ATPConnectorsRepo=quant/SCRUM-5,"
        "ATPDataHandlersRepo=quant/SCRUM-5"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert [
        repo["target_branch"]
        for repo in validated.runtime_request["repository_details"]
    ] == ["quant/SCRUM-5", "quant/SCRUM-5"]


def test_multi_repository_atp_backtest_is_rejected_before_execution(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Run coordinated backtest strategy_type=backtest "
        "repos=ATPConnectorsRepo,ATPDataHandlersRepo "
        "start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    errors = "\n".join(exc_info.value.errors)
    assert "Multi-repository backtests are not supported" in errors
    assert "not ATRADE engine components" in errors


def test_validate_jira_command_rejects_conflicting_targets_for_same_repository(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest repo=ATPConnectorsRepo "
        "target_branch_map=ATPConnectorsRepo=quant/SCRUM-5/a,"
        "atpconnectorsrepo=quant/SCRUM-5/b "
        "start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "has conflicting target branches" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_rejects_target_outside_ticket_namespace(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "repo=ATPConnectorsRepo target_branch=develop "
        "start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "must be the current ticket quant branch 'quant/SCRUM-5'" in "\n".join(
        exc_info.value.errors
    )

def test_validate_jira_command_parses_inline_model_colon_option(
    docker_sandbox_runner,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(
        grouped_payload_with_model("nova-pro")
    )
    parameters = validated.runtime_request["execution_objectives"][
        "parsed_task_parameters"
    ]

    assert parameters["model"] == "nova-pro"
    assert parameters["objective"] == "Latest request"


def test_validate_jira_command_normalizes_rag_options(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " rag_enabled=true rag_top_k=3"
    )

    runtime_request = docker_sandbox_runner.validate_jira_command_payload(
        payload
    ).runtime_request
    parameters = runtime_request["execution_objectives"][
        "parsed_task_parameters"
    ]

    assert parameters["rag_enabled"] is True
    assert parameters["rag_top_k"] == 3
    assert "retrieval_context" not in runtime_request


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ("rag_enabled=perhaps", "rag_enabled must be true or false"),
        ("rag_top_k=0", "rag_top_k must be between 1 and 10"),
        ("rag_top_k=11", "rag_top_k must be between 1 and 10"),
        ("rag_top_k=many", "rag_top_k must be an integer"),
    ],
)
def test_validate_jira_command_rejects_invalid_rag_options(
    docker_sandbox_runner,
    options,
    message,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += f" {options}"

    with pytest.raises(
        docker_sandbox_runner.JiraCommandValidationError,
        match=message,
    ):
        docker_sandbox_runner.validate_jira_command_payload(payload)


def test_validate_jira_command_passes_schema_valid_retrieval_context(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " rag_enabled=true rag_top_k=3"
    )
    payload["retrieval_context"] = retrieval_context(3)

    runtime_request = docker_sandbox_runner.validate_jira_command_payload(
        payload
    ).runtime_request

    assert runtime_request["retrieval_context"] == payload["retrieval_context"]


def test_validate_jira_command_rejects_context_for_c0(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += " rag_enabled=false"
    payload["retrieval_context"] = retrieval_context(5)

    with pytest.raises(
        docker_sandbox_runner.JiraCommandValidationError,
        match="retrieval_context is forbidden",
    ):
        docker_sandbox_runner.validate_jira_command_payload(payload)


def test_validate_jira_command_rejects_context_top_k_mismatch(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " rag_enabled=true rag_top_k=3"
    )
    payload["retrieval_context"] = retrieval_context(5)

    with pytest.raises(
        docker_sandbox_runner.JiraCommandValidationError,
        match="requested_top_k must match rag_top_k",
    ):
        docker_sandbox_runner.validate_jira_command_payload(payload)


def test_prepare_rag_execution_payload_does_not_call_retriever_for_c0(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    payload = grouped_payload()
    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    retrieve = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "retrieve_jira_memory", retrieve)

    execution_payload, execution_validated = (
        docker_sandbox_runner.prepare_rag_execution_payload(payload, validated)
    )

    assert execution_payload is payload
    assert execution_validated is validated
    retrieve.assert_not_called()


def test_prepare_rag_execution_payload_calls_retriever_once_for_c1(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " rag_enabled=true rag_top_k=3"
    )
    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    retrieve = Mock(return_value=retrieval_context(3))
    monkeypatch.setattr(docker_sandbox_runner, "retrieve_jira_memory", retrieve)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: {
            "JIRA_RAG_INDEX_PATH": "/approved/e2-index.json",
            "JIRA_RAG_INDEX_SHA256": "3" * 64,
        }.get(name, default),
    )

    execution_payload, execution_validated = (
        docker_sandbox_runner.prepare_rag_execution_payload(payload, validated)
    )

    retrieve.assert_called_once_with(
        validated.runtime_request,
        index_path="/approved/e2-index.json",
        expected_index_sha256="3" * 64,
        top_k=3,
    )
    assert "retrieval_context" not in payload
    assert execution_payload["retrieval_context"] == retrieval_context(3)
    assert execution_validated.runtime_request["retrieval_context"] == (
        retrieval_context(3)
    )


def test_prepare_rag_execution_payload_rejects_yarn_before_retrieval(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " rag_enabled=true rag_top_k=3"
    )
    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    retrieve = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "retrieve_jira_memory", retrieve)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: (
            "yarn" if name == "SANDBOX_EXECUTION_MODE" else default
        ),
    )

    with pytest.raises(ValueError, match="requires Docker execution"):
        docker_sandbox_runner.prepare_rag_execution_payload(payload, validated)

    retrieve.assert_not_called()


def test_validate_jira_command_preserves_raw_params_option(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant\n"
        "Backtest the momentum strategy\n"
        "strategy_type: backtest\n"
        "start_date: 2024-01-01\n"
        "end_date: 2024-12-31\n"
        "params: NFREQ=4, NPORT=50, UNKNOWN_ENGINE_KEY=enabled"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    parameters = validated.runtime_request["execution_objectives"][
        "parsed_task_parameters"
    ]

    assert parameters["objective"] == "Backtest the momentum strategy"
    assert parameters["params"] == "NFREQ=4, NPORT=50, UNKNOWN_ENGINE_KEY=enabled"


def test_validate_jira_command_keeps_urls_in_objective(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Review https://github.com/bankingscience/BSLAgenticQuantDevLoop "
        "strategy_type=backtest start_date=2024-01-01 end_date=2024-12-31"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    parameters = validated.runtime_request["execution_objectives"][
        "parsed_task_parameters"
    ]

    assert parameters["objective"] == (
        "Review https://github.com/bankingscience/BSLAgenticQuantDevLoop"
    )


def test_validate_jira_command_rejects_invalid_model_option(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload_with_model("nova-pro;rm")

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "model must be a non-empty model id" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_accepts_missing_dates_and_inherits_window(
    docker_sandbox_runner,
) -> None:
    # Dates are optional now: a /quant command with no start_date/end_date is
    # accepted and yields an empty target_date_range, so the run inherits the
    # strategy's own backtest window (strategy.request) instead of being rejected.
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["execution_objectives"]["target_date_range"] == {}


def test_validate_jira_command_rejects_unapproved_repository(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request repo=https://github.com/evil/quant.git "
        "start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "approved bankingscience GitHub organisation" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_command_rejects_unrelated_feature_branch(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request repo=bankingscience/BSLAgenticQuantDevLoop "
        "branch=quant/SCRUM-123 start_date=2024-01-01 end_date=2024-12-31"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "quant/SCRUM-5" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_rejects_execution_limit_violation(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "start_date=2024-01-01 end_date=2024-12-31 "
        "max_iterations=20"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "max_iterations must be between 1 and 10" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_command_builds_custom_resource_requirements(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "start_date=2024-01-01 end_date=2024-12-31 "
        "cpu=1.5 memory_mb=2048 gpu=0 runtime_timeout=900 "
        "pids_limit=128 queue=research"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["resource_requirements"] == {
        "cpu_vcpus": 1.5,
        "memory_mb": 2048,
        "gpu_count": 0,
        "execution_timeout_seconds": 900,
        "pids_limit": 128,
        "yarn_queue": "research",
    }


def test_backtest_requests_iterate_by_default(docker_sandbox_runner) -> None:
    # The engine returns measured metrics, so another pass has a real signal.
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())

    assert validated.runtime_request["iteration_controls"]["allow_iteration"] is True


@pytest.mark.parametrize(
    "strategy_type", ["refactor", "ingestion", "analysis", "other"]
)
def test_general_requests_are_single_pass_by_default(
    docker_sandbox_runner,
    strategy_type,
) -> None:
    # A general run is scored by an advisory prose review, not a measurement, so
    # it does not spend extra passes on that basis unless asked to.
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Latest request strategy_type={strategy_type}"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["iteration_controls"]["allow_iteration"] is False


def test_general_requests_can_opt_in_to_iteration(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=refactor allow_iteration=true"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["iteration_controls"]["allow_iteration"] is True


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("allow_iteration=false", False),
        ("allow_iteration=no", False),
        ("allow_iteration=0", False),
        ("iterate=false", False),
        ("allow_iteration=true", True),
        ("iterate=yes", True),
    ],
)
def test_iteration_can_be_turned_off_from_the_ticket(
    docker_sandbox_runner,
    option,
    expected,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Latest request strategy_type=backtest {option}"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["iteration_controls"]["allow_iteration"] is expected


def test_non_boolean_allow_iteration_is_rejected(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest allow_iteration=sometimes"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "allow_iteration must be true or false" in "\n".join(exc_info.value.errors)


def test_requests_modify_code_by_default(docker_sandbox_runner) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())

    assert validated.runtime_request["execution_objectives"][
        "zero_code_modifications"
    ] is False


@pytest.mark.parametrize(
    "option",
    ["zero_code_modifications=true", "read_only=true", "read_only=yes"],
)
def test_a_ticket_can_declare_a_read_only_run(docker_sandbox_runner, option) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Does the client retry safely strategy_type=analysis {option}"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["execution_objectives"][
        "zero_code_modifications"
    ] is True


def test_non_boolean_read_only_is_rejected(docker_sandbox_runner) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=analysis read_only=maybe"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "zero_code_modifications must be true or false" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_command_requires_explicit_strategy_type(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = "/quant Latest request"

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "strategy_type is required" in "\n".join(exc_info.value.errors)


def test_validate_jira_command_rejects_unknown_strategy_type(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=teleport"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "strategy_type 'teleport' is not supported" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_command_never_infers_strategy_type_from_objective(
    docker_sandbox_runner,
) -> None:
    """The objective mentioning "backtest" must not route the run to the engine.

    Regression: the old inference matched the word anywhere in the prose, so
    "refactor the backtest harness" silently ran a real backtest.
    """
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Refactor the backtest harness strategy_type=refactor"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["execution_objectives"]["strategy_type"] == (
        "refactor"
    )


def test_validate_jira_command_passes_resource_path_to_runtime_request(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Tidy the helpers strategy_type=refactor "
        "resource_path=rae_runtime/proxy/github_client.py"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["execution_objectives"]["resource_path"] == (
        "rae_runtime/proxy/github_client.py"
    )
    assert validated.runtime_request["execution_objectives"]["target_path"] == (
        "rae_runtime/proxy/github_client.py"
    )


def test_validate_jira_command_passes_distinct_target_path_to_runtime_request(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Create repository documentation strategy_type=other "
        "resource_path=docs/source-notes.txt target_path=README.md "
        "allowed_directories=."
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    objectives = validated.runtime_request["execution_objectives"]
    assert objectives["resource_path"] == "docs/source-notes.txt"
    assert objectives["target_path"] == "README.md"


def test_validate_jira_command_accepts_bounded_agent_turn_override(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Create documentation strategy_type=other "
        "resource_path=docs/source.txt target_path=README.md max_agent_turns=45"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["iteration_controls"]["max_agent_turns"] == 45


def test_validate_jira_command_rejects_agent_turn_override_outside_safe_bounds(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Create documentation strategy_type=other max_agent_turns=61"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "max_agent_turns must be between 10 and 60" in "\n".join(exc_info.value.errors)


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("max_commits_per_run=20", 20),
        ("max_commits=12", 12),
    ],
)
def test_validate_jira_command_accepts_bounded_commit_cap(
    docker_sandbox_runner,
    option: str,
    expected: int,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update several files strategy_type=refactor " + option
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert (
        validated.runtime_request["iteration_controls"]["max_commits_per_run"]
        == expected
    )


@pytest.mark.parametrize("value", ["0", "51", "many"])
def test_validate_jira_command_rejects_invalid_commit_cap(
    docker_sandbox_runner,
    value: str,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update several files strategy_type=refactor "
        f"max_commits_per_run={value}"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    error = "\n".join(exc_info.value.errors)
    if value == "many":
        assert "max_commits_per_run must be an integer" in error
    else:
        assert "max_commits_per_run must be between 1 and 50" in error


def test_validate_jira_command_omits_commit_cap_when_not_requested(
    docker_sandbox_runner,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())

    assert "max_commits_per_run" not in validated.runtime_request["iteration_controls"]


def test_validate_jira_command_omits_resource_path_when_not_requested(
    docker_sandbox_runner,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())

    assert "resource_path" not in validated.runtime_request["execution_objectives"]


def test_resource_path_outside_allowed_directories_is_rejected(
    docker_sandbox_runner,
) -> None:
    # The MCP server enforces allowed_directories at runtime, so this would
    # otherwise surface as the agent being blocked from the file it was told to edit.
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Tidy the client strategy_type=refactor "
        "repo=bankingscience/BSLAgenticQuantDevLoop "
        "resource_path=rae_runtime/mcp/server.py "
        "allowed_directories=rae_runtime/proxy"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    errors = "\n".join(exc_info.value.errors)
    assert "is outside allowed_directories" in errors


def test_target_path_outside_allowed_directories_is_rejected(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Create documentation strategy_type=other "
        "resource_path=docs/source.md target_path=README.md "
        "allowed_directories=docs"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "target_path 'README.md' is outside allowed_directories" in "\n".join(
        exc_info.value.errors
    )


def test_resource_path_inside_allowed_directories_is_accepted(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Tidy the client strategy_type=refactor "
        "repo=bankingscience/BSLAgenticQuantDevLoop "
        "resource_path=rae_runtime/proxy/github_client.py "
        "allowed_directories=rae_runtime/proxy"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.runtime_request["repository_details"][0][
        "allowed_directories"
    ] == ["rae_runtime/proxy"]


def test_allowed_directories_without_a_resource_path_is_accepted(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Tidy things strategy_type=refactor "
        "repo=bankingscience/BSLAgenticQuantDevLoop "
        "allowed_directories=rae_runtime/proxy"
    )

    docker_sandbox_runner.validate_jira_command_payload(payload)


def test_allowed_directories_map_overrides_global_scope_per_repository(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update both components strategy_type=refactor "
        "repos=BSLAgenticQuantDevLoop,ATPDataHandlersRepo "
        "allowed_directories=shared\n"
        "allowed_directories_map: bslagenticquantdevloop=rae_runtime|jira-chatops-gateway; "
        "https://github.com/bankingscience/ATPDataHandlersRepo=."
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)
    repos = validated.runtime_request["repository_details"]

    assert repos[0]["allowed_directories"] == [
        "rae_runtime",
        "jira-chatops-gateway",
    ]
    assert repos[1]["allowed_directories"] == ["."]


def test_allowed_directories_map_uses_global_scope_for_unlisted_repository(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update both components strategy_type=refactor "
        "repos=BSLAgenticQuantDevLoop,ATPDataHandlersRepo "
        "allowed_directories=shared\n"
        "allowed_directories_map: BSLAgenticQuantDevLoop=rae_runtime"
    )

    repos = docker_sandbox_runner.validate_jira_command_payload(payload).runtime_request[
        "repository_details"
    ]

    assert repos[0]["allowed_directories"] == ["rae_runtime"]
    assert repos[1]["allowed_directories"] == ["shared"]


def test_allowed_directories_map_rejects_unknown_repository(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update components strategy_type=refactor\n"
        "allowed_directories_map: unknown=src"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "Unknown repository alias 'unknown'" in "\n".join(exc_info.value.errors)


def test_allowed_directories_map_rejects_conflicting_alias_values(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Update components strategy_type=refactor "
        "repo=ATPDataHandlersRepo\n"
        "allowed_directories_map: ATPDataHandlersRepo=src; atpdatahandlersrepo=docs"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "has conflicting values" in "\n".join(exc_info.value.errors)


@pytest.mark.parametrize(
    "resource_path",
    ["/etc/passwd", "../../etc/passwd", "strategies/../../secrets.py"],
)
def test_validate_jira_command_rejects_unsafe_resource_path(
    docker_sandbox_runner,
    resource_path,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Tidy the helpers strategy_type=refactor resource_path={resource_path}"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "resource_path must be a repository-relative path" in "\n".join(
        exc_info.value.errors
    )


@pytest.mark.parametrize(
    "target_path",
    ["/README.md", "../../README.md", "docs/../../README.md"],
)
def test_validate_jira_command_rejects_unsafe_target_path(
    docker_sandbox_runner,
    target_path,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        f"/quant Write docs strategy_type=other target_path={target_path}"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "target_path must be a repository-relative path" in "\n".join(
        exc_info.value.errors
    )


@pytest.mark.parametrize(
    ("option", "expected_error"),
    [
        ("cpu_vcpus=12", "cpu_vcpus must be between 0.25 and 8.0"),
        ("cpu_vcpus=nan", "cpu_vcpus must be a finite number"),
        ("memory_mb=128", "memory_mb must be between 512 and 16384"),
        ("gpu_count=1", "gpu_count is not supported"),
        ("pids_limit=10", "pids_limit must be between 64 and 1024"),
        ("yarn_queue=bad/queue", "yarn_queue must start with a letter or number"),
    ],
)
def test_validate_jira_command_rejects_invalid_resource_requirements(
    docker_sandbox_runner,
    option,
    expected_error,
) -> None:
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] = (
        "/quant Latest request strategy_type=backtest "
        "start_date=2024-01-01 end_date=2024-12-31 "
        f"{option}"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert expected_error in "\n".join(exc_info.value.errors)


def test_resolve_user_scoped_github_credentials_authorized_repo(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    get_credentials = Mock(
        return_value={"GITHUB_USERNAME": "old-user", "GITHUB_TOKEN": "user-token"}
    )
    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        get_credentials,
    )

    credentials, metadata = docker_sandbox_runner.resolve_user_scoped_github_credentials(
        {
            "emailAddress": "student@example.com",
            "displayName": "Student One",
            "accountId": "jira-1",
        },
        [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
        matrix=[
            {
                "jira_email": "student@example.com",
                "github_username": "student-gh",
                "github_connection_id": "github_student",
                "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                "permission_level": "write",
            }
        ],
    )

    get_credentials.assert_called_once_with("github_student")
    assert credentials == {
        "GITHUB_USERNAME": "student-gh",
        "GITHUB_TOKEN": "user-token",
    }
    assert metadata["github_username"] == "student-gh"
    assert metadata["github_connection_id"] == "github_student"
    assert metadata["token_present"] is True
    assert "user-token" not in json.dumps(metadata)


def test_resolve_user_scoped_github_credentials_rejects_unauthorized_repo(
    docker_sandbox_runner,
) -> None:
    with pytest.raises(
        ValueError,
        match="Jira user is not authorised for requested repository",
    ):
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/OtherRepo"}],
            matrix=[
                {
                    "jira_email": "student@example.com",
                    "github_username": "student-gh",
                    "github_connection_id": "github_student",
                    "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                    "permission_level": "write",
                }
            ],
        )


def test_resolve_user_scoped_github_credentials_rejects_unknown_jira_user(
    docker_sandbox_runner,
) -> None:
    with pytest.raises(
        ValueError,
        match="No GitHub credential mapping configured for Jira user",
    ):
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "unknown@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[],
        )


def test_resolve_user_scoped_github_credentials_requires_github_username(
    docker_sandbox_runner,
) -> None:
    with pytest.raises(
        ValueError,
        match="GitHub username is not configured for Jira user",
    ):
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[
                {
                    "jira_email": "student@example.com",
                    "github_connection_id": "github_student",
                    "authorized_repositories": [
                        "bankingscience/BSLAgenticQuantDevLoop"
                    ],
                    "permission_level": "write",
                }
            ],
        )


def test_resolve_user_scoped_github_credentials_rejects_missing_secret(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        Mock(side_effect=ValueError("raw token backend failure with token-secret")),
    )

    with pytest.raises(ValueError) as exc_info:
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[
                {
                    "jira_email": "student@example.com",
                    "github_username": "student-gh",
                    "github_connection_id": "github_student",
                    "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                    "permission_level": "write",
                }
            ],
        )

    assert str(exc_info.value) == "GitHub token secret is not configured"


def test_resolve_user_scoped_github_credentials_supports_shared_display_mapping(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        Mock(return_value={"GITHUB_TOKEN": "shared-token"}),
    )

    credentials, _ = docker_sandbox_runner.resolve_user_scoped_github_credentials(
        {"displayName": "Student Shared"},
        [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
        matrix=[
            {
                "jira_display_aliases": ["Student Shared"],
                "github_username": "shared-gh",
                "github_connection_id": "github_shared",
                "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                "permission_level": "write",
            }
        ],
    )

    assert credentials["GITHUB_USERNAME"] == "shared-gh"
    assert credentials["GITHUB_TOKEN"] == "shared-token"


def test_resolve_user_scoped_github_credentials_allows_rah9742_global_connection(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    get_credentials = Mock(return_value={"GITHUB_TOKEN": "rah-token"})
    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        get_credentials,
    )

    credentials, _ = docker_sandbox_runner.resolve_user_scoped_github_credentials(
        {"emailAddress": "rahil.shah.25@ucl.ac.uk"},
        [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
        matrix=[
            {
                "jira_email": "rahil.shah.25@ucl.ac.uk",
                "github_username": "Rah9742",
                "github_connection_id": "github_default",
                "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                "permission_level": "write",
            }
        ],
    )

    get_credentials.assert_called_once_with("github_default")
    assert credentials["GITHUB_USERNAME"] == "Rah9742"
    assert credentials["GITHUB_TOKEN"] == "rah-token"


def test_access_matrix_ignores_legacy_folder_scopes(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    load_matrix = docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__[
        "load_github_user_access_matrix"
    ]
    monkeypatch.setitem(
        load_matrix.__globals__,
        "get_airflow_variable",
        Mock(
            return_value=json.dumps(
                {
                    "users": [
                        {
                            "jira_email": "student@example.com",
                            "github_username": "student-gh",
                            "github_connection_id": "github_student",
                            "authorized_repositories": [
                                "bankingscience/BSLAgenticQuantDevLoop"
                            ],
                            "allowed_paths": ["rae_runtime"],
                            "permission_level": "write",
                        }
                    ]
                }
            )
        ),
    )

    assert "allowed_paths" not in load_matrix()[0]

    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        Mock(return_value={"GITHUB_TOKEN": "user-token"}),
    )
    credentials, _ = docker_sandbox_runner.resolve_user_scoped_github_credentials(
        {"emailAddress": "student@example.com"},
        [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
        matrix=[
        {
            "jira_email": "student@example.com",
            "github_username": "student-gh",
            "github_connection_id": "github_student",
            "authorized_repositories": [
                "bankingscience/BSLAgenticQuantDevLoop",
                "bankingscience/ATPConnectorsRepo",
            ],
            "allowed_paths": ["jira-chatops-gateway"],
            "permission_level": "write",
        }
        ],
    )

    assert credentials["GITHUB_USERNAME"] == "student-gh"


@pytest.mark.parametrize("permission_level", [None, "admin", ""])
def test_resolve_user_scoped_credentials_rejects_unknown_permission(
    docker_sandbox_runner,
    permission_level,
) -> None:
    with pytest.raises(ValueError, match="permission_level"):
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[
                {
                    "jira_email": "student@example.com",
                    "github_username": "student-gh",
                    "github_connection_id": "github_student",
                    "authorized_repositories": [
                        "bankingscience/BSLAgenticQuantDevLoop"
                    ],
                    "permission_level": permission_level,
                }
            ],
        )


def test_read_permission_requires_explicit_read_only_workflow(
    docker_sandbox_runner,
) -> None:
    row = {
        "jira_email": "student@example.com",
        "github_username": "student-gh",
        "github_connection_id": "github_student",
        "authorized_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
        "permission_level": "read",
    }

    with pytest.raises(ValueError, match="read-only GitHub permission"):
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[row],
        )


def test_read_permission_allows_explicit_read_only_workflow(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        docker_sandbox_runner.resolve_user_scoped_github_credentials.__globals__,
        "get_optional_github_credentials",
        Mock(return_value={"GITHUB_TOKEN": "read-token"}),
    )

    credentials, metadata = (
        docker_sandbox_runner.resolve_user_scoped_github_credentials(
            {"emailAddress": "student@example.com"},
            [{"repo_full_name": "bankingscience/BSLAgenticQuantDevLoop"}],
            matrix=[
                {
                    "jira_email": "student@example.com",
                    "github_username": "student-gh",
                    "github_connection_id": "github_student",
                    "authorized_repositories": [
                        "bankingscience/BSLAgenticQuantDevLoop"
                    ],
                    "permission_level": "read",
                }
            ],
            explicitly_read_only=True,
        )
    )

    assert credentials["GITHUB_TOKEN"] == "read-token"
    assert metadata["permission_level"] == "read"


def test_verify_user_github_access_checks_identity_and_each_source_branch(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    verify = docker_sandbox_runner.verify_user_github_access
    get = Mock(
        side_effect=[
            Mock(status_code=200, json=Mock(return_value={"login": "student-gh"})),
            Mock(status_code=200),
            Mock(status_code=200),
        ]
    )
    monkeypatch.setitem(verify.__globals__, "requests", Mock(get=get))

    verify(
        {"GITHUB_TOKEN": "user-token"},
        "Student-GH",
        [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "main",
            },
            {
                "repo_full_name": "bankingscience/ATPConnectorsRepo",
                "source_branch": "feature/with/slash",
            },
        ],
    )

    assert get.call_count == 3
    assert get.call_args_list[2].args[0].endswith("feature%2Fwith%2Fslash")


def test_verify_user_github_access_rejects_identity_mismatch(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    verify = docker_sandbox_runner.verify_user_github_access
    monkeypatch.setitem(
        verify.__globals__,
        "requests",
        Mock(
            get=Mock(
                return_value=Mock(
                    status_code=200,
                    json=Mock(return_value={"login": "different-user"}),
                )
            )
        ),
    )

    with pytest.raises(ValueError, match="identity does not match"):
        verify({"GITHUB_TOKEN": "secret-value"}, "student-gh", [])


def test_verify_user_github_access_rejects_missing_source_branch(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    verify = docker_sandbox_runner.verify_user_github_access
    monkeypatch.setitem(
        verify.__globals__,
        "requests",
        Mock(
            get=Mock(
                side_effect=[
                    Mock(
                        status_code=200,
                        json=Mock(return_value={"login": "student-gh"}),
                    ),
                    Mock(status_code=404),
                ]
            )
        ),
    )

    with pytest.raises(ValueError) as exc_info:
        verify(
            {"GITHUB_TOKEN": "secret-value"},
            "student-gh",
            [
                {
                    "repo_full_name": "bankingscience/ATPConnectorsRepo",
                    "source_branch": "missing",
                }
            ],
        )

    assert "ATPConnectorsRepo@missing" in str(exc_info.value)
    assert "secret-value" not in str(exc_info.value)


def test_resolve_litellm_config_uses_requested_model(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    validated = docker_sandbox_runner.validate_jira_command_payload(
        grouped_payload_with_model("nova-pro")
    )

    assert docker_sandbox_runner.resolve_litellm_config_for_request(
        validated.runtime_request
    ) == {
        "api_key": "litellm-secret",
        "base_url": "http://weles.cs.ucl.ac.uk:4000",
        "model": "nova-pro",
    }


def test_resolve_litellm_config_falls_back_to_airflow_model(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())

    assert docker_sandbox_runner.resolve_litellm_config_for_request(
        validated.runtime_request
    )["model"] == "nova-micro"


def test_explicit_openai_provider_config_and_environment_are_secret_scoped(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_openai_config",
        Mock(
            return_value={
                "api_key": "openai-secret",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5.6-terra",
            }
        ),
    )
    company = Mock(side_effect=AssertionError("company secret path was consulted"))
    monkeypatch.setattr(docker_sandbox_runner, "get_litellm_config", company)
    runtime_request = {
        "execution_objectives": {
            "parsed_task_parameters": {
                "provider_mode": "openai",
                "provider_base_url": "https://api.openai.com/v1",
                "provider_identity_sha256": (
                    "f5712a84b16c220abf9b470e0a5df839"
                    "b9cdf3f2be4386989c2e4b18f9086cdc"
                ),
                "model": "gpt-5.6-terra",
            }
        }
    }

    config = docker_sandbox_runner.resolve_provider_config_for_request(
        runtime_request
    )
    environment = docker_sandbox_runner.provider_environment(config)

    assert environment == {
        "LLM_PROVIDER": "openai",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-5.6-terra",
    }
    assert not set(environment).intersection(
        {
            "API_KEY",
            "LITELLM_API_KEY",
            "LITELLM_PROXY_API_KEY",
            "LITELLM_BASE_URL",
            "LITELLM_MODEL",
            "MODEL",
            "MODEL_NAME",
        }
    )
    company.assert_not_called()


def test_gateway_openai_config_never_falls_back_to_company_secret(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "dedicated-openai-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1/")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("LITELLM_API_KEY", "wrong-company-secret")
    monkeypatch.setenv("API_KEY", "wrong-legacy-secret")
    function_globals = docker_sandbox_runner.get_openai_config.__globals__
    monkeypatch.setitem(function_globals, "load_local_env", lambda: None)
    monkeypatch.setitem(
        function_globals,
        "get_openai_connection_id",
        lambda: "openai_default",
    )
    monkeypatch.setitem(
        function_globals,
        "get_airflow_connection",
        Mock(side_effect=KeyError("no test connection")),
    )
    monkeypatch.setitem(
        function_globals,
        "get_airflow_variable",
        lambda name, default=None: os.environ.get(name, default),
    )

    assert docker_sandbox_runner.get_openai_config() == {
        "api_key": "dedicated-openai-secret",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-5.6-terra",
    }


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("provider_base_url", "https://example.com/v1", "provider_base_url"),
        ("model", "gpt-5.6-sol", "model"),
        ("provider_identity_sha256", "0" * 64, "identity hash"),
    ],
)
def test_explicit_openai_provider_identity_mismatch_fails_before_container(
    docker_sandbox_runner,
    monkeypatch,
    field,
    value,
    error,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_openai_config",
        Mock(
            return_value={
                "api_key": "openai-secret",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5.6-terra",
            }
        ),
    )
    parameters = {
        "provider_mode": "openai",
        "provider_base_url": "https://api.openai.com/v1",
        "provider_identity_sha256": (
            "f5712a84b16c220abf9b470e0a5df839"
            "b9cdf3f2be4386989c2e4b18f9086cdc"
        ),
        "model": "gpt-5.6-terra",
    }
    parameters[field] = value

    with pytest.raises(ValueError, match=error):
        docker_sandbox_runner.resolve_provider_config_for_request(
            {"execution_objectives": {"parsed_task_parameters": parameters}}
        )


def test_explicit_company_environment_excludes_openai_secret_aliases(
    docker_sandbox_runner,
) -> None:
    environment = docker_sandbox_runner.provider_environment(
        {
            "provider_mode": "company_litellm",
            "api_key": "company-secret",
            "base_url": "http://weles.cs.ucl.ac.uk:4000",
            "model": "qwen3-coder",
        }
    )

    assert environment == {
        "LLM_PROVIDER": "company_litellm",
        "LITELLM_API_KEY": "company-secret",
        "LITELLM_BASE_URL": "http://weles.cs.ucl.ac.uk:4000",
        "LITELLM_MODEL": "qwen3-coder",
    }
    assert "OPENAI_API_KEY" not in environment


def test_local_docker_openai_path_scrubs_inherited_company_secrets(
    docker_sandbox_runner,
    monkeypatch,
    capsys,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    validated.runtime_request["execution_objectives"]["parsed_task_parameters"].update(
        {
            "provider_mode": "openai",
            "provider_base_url": "https://api.openai.com/v1",
            "provider_identity_sha256": (
                "f5712a84b16c220abf9b470e0a5df839"
                "b9cdf3f2be4386989c2e4b18f9086cdc"
            ),
            "model": "gpt-5.6-terra",
            "rag_enabled": False,
        }
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "validate_jira_command_payload",
        Mock(return_value=validated),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_openai_config",
        Mock(
            return_value={
                "api_key": "openai-secret",
                "base_url": "https://api.openai.com/v1",
                "model": "gpt-5.6-terra",
            }
        ),
    )
    company = Mock(side_effect=AssertionError("company secret path was consulted"))
    monkeypatch.setattr(docker_sandbox_runner, "get_litellm_config", company)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    for name in (
        "LITELLM_API_KEY",
        "LITELLM_PROXY_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "API_KEY",
        "MODEL",
        "MODEL_NAME",
    ):
        monkeypatch.setenv(name, "inherited-company-secret")

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text(
            json.dumps({"status": "succeeded"}),
            encoding="utf-8",
        )
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    result = docker_sandbox_runner.run_container_local_docker(grouped_payload())

    assert result["status"] == "succeeded"
    command = stream.call_args.args[0]
    environment = stream.call_args.kwargs["env"]
    assert environment["LLM_PROVIDER"] == "openai"
    assert environment["OPENAI_API_KEY"] == "openai-secret"
    assert environment["OPENAI_BASE_URL"] == "https://api.openai.com/v1"
    assert environment["OPENAI_MODEL"] == "gpt-5.6-terra"
    for name in (
        "LITELLM_API_KEY",
        "LITELLM_PROXY_API_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "API_KEY",
        "OPENAI_API_BASE",
        "MODEL",
        "MODEL_NAME",
    ):
        assert name not in environment
        assert name not in command
    for name in (
        "LLM_PROVIDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
    ):
        assert name in command
    output = capsys.readouterr().out
    assert "openai-secret" not in output
    assert "inherited-company-secret" not in output
    company.assert_not_called()


def test_run_container_uses_hardened_docker_cli_and_reads_result_file(
    docker_sandbox_runner,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setenv("RAE_LEGACY_INPUT", "1")
    monkeypatch.setenv("TICKET", "OLD-1")
    monkeypatch.setenv("REQUEST", "old request")
    monkeypatch.setenv("REQUEST_PATH", "/old/request.json")
    monkeypatch.setenv("RESULT_PATH", "/old/result.json")
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(return_value={"api_key": "litellm-secret", "base_url": "http://weles.cs.ucl.ac.uk:4000", "model": "nova-micro"}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    post_jira_attachments = Mock(return_value=[])
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        input_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/input" in item
        )
        input_dir = Path(
            next(
                part.removeprefix("source=")
                for part in input_mount.split(",")
                if part.startswith("source=")
            )
        )
        request_payload = json.loads(
            (input_dir / "request.json").read_text(encoding="utf-8")
        )
        request_stdin = json.loads(kwargs["input_text"])
        assert request_stdin["schema_version"] == "1.0"
        assert request_stdin["jira_metadata"]["ticket_id"] == "SCRUM-5"
        assert request_payload["schema_version"] == "1.0"
        assert request_payload["jira_metadata"]["ticket_id"] == "SCRUM-5"
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text(
            json.dumps({"status": "succeeded"}),
            encoding="utf-8",
        )
        print("starting")
        print("warning")
        return Mock(returncode=0, stdout="starting\nwarning\n", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    result = docker_sandbox_runner.run_container(grouped_payload_with_model())

    assert result["status"] == "succeeded"
    assert result["issue_key"] == "SCRUM-5"
    assert result["artifact_ingestion"] == {"uploaded": [], "missing": [], "skipped": []}
    post_jira_attachments.assert_called_once_with("SCRUM-5", [])
    call = stream.call_args
    command = call.args[0]
    _, docker_user, workspace_tmpfs = docker_sandbox_runner.current_docker_user_spec()
    assert command[:23] == [
        "docker",
        "run",
        "-i",
        "--pull",
        "always",
        "--rm",
        "--cpus",
        "2.0",
        "--memory",
        "4096m",
        "--pids-limit",
        "256",
        "--read-only",
        "--user",
        docker_user,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=1g,mode=1777",
        "--tmpfs",
        workspace_tmpfs,
    ]
    assert any("target=/workspace/input" in item for item in command)
    assert any("target=/workspace/output" in item for item in command)
    for legacy_name in (
        "RAE_LEGACY_INPUT",
        "REQUEST",
        "REQUEST_PATH",
        "RESULT_PATH",
        "TICKET",
    ):
        assert legacy_name not in command
        assert legacy_name not in call.kwargs["env"]
    assert call.kwargs["env"]["GITHUB_USERNAME"] == "octocat"
    assert call.kwargs["env"]["GITHUB_TOKEN"] == "github-secret"
    assert call.kwargs["env"]["LITELLM_API_KEY"] == "litellm-secret"
    assert call.kwargs["env"]["LITELLM_BASE_URL"] == "http://weles.cs.ucl.ac.uk:4000"
    assert call.kwargs["env"]["LITELLM_MODEL"] == "nova-pro"
    assert call.kwargs["env"]["API_KEY"] == "litellm-secret"
    assert call.kwargs["env"]["OPENAI_API_KEY"] == "litellm-secret"
    assert call.kwargs["env"]["OPENAI_BASE_URL"] == "http://weles.cs.ucl.ac.uk:4000"
    assert call.kwargs["env"]["OPENAI_API_BASE"] == "http://weles.cs.ucl.ac.uk:4000"
    assert call.kwargs["env"]["MODEL"] == "nova-pro"
    assert call.kwargs["env"]["MODEL_NAME"] == "nova-pro"
    assert call.kwargs["timeout"] == 1800
    assert "-e" in command
    assert "LITELLM_BASE_URL" in command
    assert "LITELLM_MODEL" in command
    assert "API_KEY" in command
    assert "OPENAI_API_KEY" in command
    assert "OPENAI_BASE_URL" in command
    assert "OPENAI_API_BASE" in command
    assert "MODEL" in command
    assert "MODEL_NAME" in command
    assert "GITHUB_USERNAME" in command
    assert "GITHUB_TOKEN" in command
    assert "DOCKER_HOST" not in call.kwargs["env"]
    assert call.kwargs["timeout"] == 1800
    output = capsys.readouterr().out
    assert "starting" in output
    assert "warning" in output


def test_run_container_expands_unconfigured_docker_timeout_for_explicit_request(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(return_value={"api_key": "litellm-secret", "base_url": "http://weles.cs.ucl.ac.uk:4000", "model": "nova-micro"}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_sandbox_github_credentials",
        Mock(return_value=({}, {})),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_image_pull_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: default,
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text(
            json.dumps({"status": "succeeded"}),
            encoding="utf-8",
        )
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)
    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += " timeout=10800"

    docker_sandbox_runner.run_container(payload)

    assert stream.call_args.kwargs["timeout"] == 10800


def test_run_container_uses_user_token_not_global_env(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_USERNAME", "global-user")
    monkeypatch.setenv("GITHUB_TOKEN", "global-token")
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_sandbox_github_credentials",
        Mock(
            return_value=(
                {"GITHUB_USERNAME": "user-gh", "GITHUB_TOKEN": "user-token"},
                {
                    "github_username": "user-gh",
                    "github_connection_id": "github_user",
                    "requested_repositories": ["bankingscience/BSLAgenticQuantDevLoop"],
                    "token_present": True,
                    "token_length": len("user-token"),
                },
            )
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_image_pull_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text("{}", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    docker_sandbox_runner.run_container(grouped_payload())

    assert stream.call_args.kwargs["env"]["GITHUB_USERNAME"] == "user-gh"
    assert stream.call_args.kwargs["env"]["GITHUB_TOKEN"] == "user-token"


def test_run_container_uses_configured_remote_docker_url(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(return_value={"api_key": "litellm-secret", "base_url": "http://weles.cs.ucl.ac.uk:4000", "model": "nova-micro"}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: (
            "tcp://docker.example:2375" if name == "DOCKER_URL" else default
        ),
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text("{}", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    docker_sandbox_runner.run_container(grouped_payload())

    assert stream.call_args.kwargs["env"]["DOCKER_HOST"] == (
        "tcp://docker.example:2375"
    )


def test_current_docker_user_spec_matches_airflow_process_user(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(docker_sandbox_runner.os, "getuid", lambda: 1234)
    monkeypatch.setattr(docker_sandbox_runner.os, "getgid", lambda: 5678)

    uid, user_spec, workspace_tmpfs = docker_sandbox_runner.current_docker_user_spec()

    assert uid == "1234"
    assert user_spec == "1234:5678"
    assert workspace_tmpfs == (
        "/workspace:rw,nosuid,size=4g,uid=1234,gid=5678,mode=0770"
    )


def test_run_container_logs_into_ghcr_with_connection_credentials(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_ghcr_credentials",
        Mock(
            return_value={
                "GHCR_USERNAME": "octocat",
                "GHCR_TOKEN": "ghcr-secret",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text("{}", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    run = Mock(return_value=Mock(returncode=0, stdout="login ok\n", stderr=""))
    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner.subprocess, "run", run)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    docker_sandbox_runner.run_container(grouped_payload())

    assert run.call_args.args[0] == [
        "docker",
        "login",
        "ghcr.io",
        "-u",
        "octocat",
        "--password-stdin",
    ]
    assert run.call_args.kwargs["input"] == "ghcr-secret"
    assert run.call_args.kwargs["env"]["DOCKER_CONFIG"].endswith("/.docker")
    assert stream.call_args.args[0][:3] == ["docker", "run", "-i"]
    assert stream.call_args.kwargs["env"]["GHCR_USERNAME"] == "octocat"
    assert stream.call_args.kwargs["env"]["GHCR_TOKEN"] == "ghcr-secret"


def test_resolve_image_pull_credentials_falls_back_to_global_github(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_ghcr_credentials",
        Mock(return_value={}),
    )
    get_global_github = Mock(
        return_value={
            "GITHUB_USERNAME": "global-gh",
            "GITHUB_TOKEN": "global-token",
        }
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        get_global_github,
    )

    credentials = docker_sandbox_runner.resolve_image_pull_credentials("github_default")

    get_global_github.assert_called_once_with("github_default")
    assert credentials == {
        "GHCR_USERNAME": "global-gh",
        "GHCR_TOKEN": "global-token",
    }


def test_run_container_fails_before_docker_when_user_token_missing(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(return_value={"api_key": "litellm-secret", "base_url": "http://weles.cs.ucl.ac.uk:4000", "model": "nova-micro"}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_sandbox_github_credentials",
        Mock(side_effect=ValueError("GitHub token secret is not configured")),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )

    stream = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    with pytest.raises(ValueError, match="GitHub token secret is not configured"):
        docker_sandbox_runner.run_container(grouped_payload())

    stream.assert_not_called()


def test_run_container_passes_real_backtester_env_to_docker(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    airflow_variables = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": str(requests),
        "SIMULATION_RESULTS_DIR": str(results),
        "BACKTEST_RESULT_TIMEOUT_SECONDS": "1200",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: airflow_variables.get(name, default),
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text("{}", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    docker_sandbox_runner.run_container(grouped_payload())

    command = stream.call_args.args[0]
    env = stream.call_args.kwargs["env"]
    for name in docker_sandbox_runner.BACKTESTER_ENV_VARS:
        assert name in command
    assert env["USE_REAL_BACKTESTER"] == "true"
    assert env["SIMULATION_REQUESTS_HOST_DIR"] == str(requests)
    assert env["SIMULATION_RESULTS_HOST_DIR"] == str(results)
    assert env["SIMULATION_REQUESTS_DIR"] == "/workspace/backtester/simulation-requests"
    assert env["SIMULATION_RESULTS_DIR"] == "/workspace/backtester/simulation-results"
    assert env["BACKTEST_RESULT_TIMEOUT_SECONDS"] == "1200"
    assert (
        f"type=bind,source={requests},target=/workspace/backtester/simulation-requests"
        in command
    )
    assert (
        f"type=bind,source={results},target=/workspace/backtester/simulation-results"
        in command
    )
    assert not any("deployed-jobs" in str(part) for part in command)


def test_run_container_expands_home_relative_backtester_mounts_locally(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(return_value=[]),
    )
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    airflow_variables = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": "~/simulation-requests",
        "SIMULATION_RESULTS_DIR": "~/simulation-results",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: airflow_variables.get(name, default),
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        (output_dir / "result.json").write_text("{}", encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    stream = Mock(side_effect=fake_stream)
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    docker_sandbox_runner.run_container(grouped_payload())

    command = stream.call_args.args[0]
    env = stream.call_args.kwargs["env"]
    assert env["SIMULATION_REQUESTS_HOST_DIR"] == "~/simulation-requests"
    assert env["SIMULATION_RESULTS_HOST_DIR"] == "~/simulation-results"
    assert env["SIMULATION_REQUESTS_DIR"] == "/workspace/backtester/simulation-requests"
    assert env["SIMULATION_RESULTS_DIR"] == "/workspace/backtester/simulation-results"
    assert (
        f"type=bind,source={requests},target=/workspace/backtester/simulation-requests"
        in command
    )
    assert (
        f"type=bind,source={results},target=/workspace/backtester/simulation-results"
        in command
    )


def test_resolve_runtime_config_uses_airflow_variable_defaults(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    get_airflow_variable = Mock(
        side_effect=lambda name, default=None: {
            "SANDBOX_IMAGE": "airflow-image",
            "DOCKER_URL": "tcp://docker.example:2375",
            "SANDBOX_CONTAINER_TIMEOUT_SECONDS": "2400",
        }.get(name, default)
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        get_airflow_variable,
    )

    config = docker_sandbox_runner.resolve_runtime_config(
        {
            "sandbox_image": "payload-image",
            "docker_url": "tcp://payload:2375",
            "github_connection_id": "payload-github",
            "runtime": {"github_connection_id": "runtime-github"},
        }
    )

    assert config == {
        "sandbox_image": "airflow-image",
        "docker_url": "tcp://docker.example:2375",
        "github_connection_id": "runtime-github",
        "container_timeout_seconds": 2400,
        "container_timeout_seconds_configured": True,
    }
    assert get_airflow_variable.call_args_list == [
        (("SANDBOX_CONTAINER_TIMEOUT_SECONDS",),),
        (("SANDBOX_IMAGE", docker_sandbox_runner.DEFAULT_SANDBOX_IMAGE),),
        (("DOCKER_URL",),),
    ]


def test_resolve_runtime_config_defaults_github_connection_from_airflow(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    get_airflow_variable = Mock(
        side_effect=lambda name, default=None: {
            "SANDBOX_IMAGE": "airflow-image",
            "DOCKER_URL": None,
            "GITHUB_CONNECTION_ID": "github_default",
        }.get(name, default)
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        get_airflow_variable,
    )

    config = docker_sandbox_runner.resolve_runtime_config({})

    assert config["github_connection_id"] == "github_default"


def test_resolve_backtester_environment_defaults_to_mock(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: default,
    )

    assert docker_sandbox_runner.resolve_backtester_environment() == {
        "USE_REAL_BACKTESTER": "false"
    }


def test_resolve_backtester_environment_requires_explicit_real_dirs(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: "true" if name == "USE_REAL_BACKTESTER" else default,
    )

    with pytest.raises(ValueError, match="SIMULATION_REQUESTS_DIR must be configured"):
        docker_sandbox_runner.resolve_backtester_environment()


def test_resolve_backtester_environment_enables_real_with_absolute_dirs(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    values = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": str(requests),
        "SIMULATION_RESULTS_DIR": str(results),
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    assert docker_sandbox_runner.resolve_backtester_environment() == {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "shared_fs",
        "SIMULATION_REQUESTS_HOST_DIR": str(requests),
        "SIMULATION_RESULTS_HOST_DIR": str(results),
        "SIMULATION_REQUESTS_DIR": "/workspace/backtester/simulation-requests",
        "SIMULATION_RESULTS_DIR": "/workspace/backtester/simulation-results",
    }


def test_resolve_backtester_environment_accepts_current_user_home_dirs(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": "~/simulation-requests",
        "SIMULATION_RESULTS_DIR": "~/simulation-results",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    assert docker_sandbox_runner.resolve_backtester_environment() == {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "shared_fs",
        "SIMULATION_REQUESTS_HOST_DIR": "~/simulation-requests",
        "SIMULATION_RESULTS_HOST_DIR": "~/simulation-results",
        "SIMULATION_REQUESTS_DIR": "/workspace/backtester/simulation-requests",
        "SIMULATION_RESULTS_DIR": "/workspace/backtester/simulation-results",
    }


def test_resolve_backtester_environment_keeps_hdfs_dirs_for_yarn(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "hdfs",
        "SIMULATION_REQUESTS_DIR": "hdfs:///quant-sandbox/simulation-requests/",
        "SIMULATION_RESULTS_DIR": "hdfs:///quant-sandbox/simulation-results/",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    assert docker_sandbox_runner.resolve_backtester_environment(
        execution_mode="yarn"
    ) == {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "hdfs",
        "SIMULATION_REQUESTS_HDFS_DIR": "hdfs:///quant-sandbox/simulation-requests",
        "SIMULATION_RESULTS_HDFS_DIR": "hdfs:///quant-sandbox/simulation-results",
        "SIMULATION_REQUESTS_DIR": "/workspace/backtester/simulation-requests",
        "SIMULATION_RESULTS_DIR": "/workspace/backtester/simulation-results",
    }


def test_resolve_backtester_environment_uses_local_dirs_for_docker_with_hdfs_config(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "hdfs",
        "SIMULATION_REQUESTS_DIR": "hdfs:///quant-sandbox/simulation-requests",
        "SIMULATION_RESULTS_DIR": "hdfs:///quant-sandbox/simulation-results",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    assert docker_sandbox_runner.resolve_backtester_environment(
        execution_mode="docker"
    ) == {
        "USE_REAL_BACKTESTER": "true",
        "BACKTEST_STORAGE_KIND": "shared_fs",
        "SIMULATION_REQUESTS_HOST_DIR": "~/simulation-requests",
        "SIMULATION_RESULTS_HOST_DIR": "~/simulation-results",
        "SIMULATION_REQUESTS_DIR": "/workspace/backtester/simulation-requests",
        "SIMULATION_RESULTS_DIR": "/workspace/backtester/simulation-results",
    }


def test_resolve_backtester_environment_rejects_other_user_home_dirs(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": "~other/simulation-requests",
        "SIMULATION_RESULTS_DIR": "~/simulation-results",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    with pytest.raises(ValueError, match="~user"):
        docker_sandbox_runner.resolve_backtester_environment()


def test_resolve_backtester_environment_rejects_relative_dirs(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": "simulation-requests",
        "SIMULATION_RESULTS_DIR": "~/simulation-results",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    with pytest.raises(ValueError, match="absolute path or ~/ path"):
        docker_sandbox_runner.resolve_backtester_environment()


def test_resolve_backtester_environment_rejects_invalid_boolean(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: "maybe" if name == "USE_REAL_BACKTESTER" else default,
    )

    with pytest.raises(ValueError, match="USE_REAL_BACKTESTER must be a boolean"):
        docker_sandbox_runner.resolve_backtester_environment()


def test_resolve_source_revision_prefers_deployment_environment(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIRFLOW_DAG_GIT_COMMIT", "abc123")

    assert docker_sandbox_runner.resolve_source_revision() == "abc123"


def test_resolve_source_revision_reads_sync_marker(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    for name in docker_sandbox_runner.SOURCE_REVISION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    marker = tmp_path / docker_sandbox_runner.SOURCE_REVISION_MARKER
    marker.write_text("def456\n", encoding="utf-8")
    monkeypatch.setattr(
        docker_sandbox_runner,
        "__file__",
        str(tmp_path / "docker_sandbox_runner.py"),
    )

    assert docker_sandbox_runner.resolve_source_revision() == "def456"


def test_selected_execution_mode_defaults_to_docker_and_ignores_payload(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    get_airflow_variable = Mock(return_value="docker")
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        get_airflow_variable,
    )

    assert (
        docker_sandbox_runner.selected_execution_mode(
            {"sandbox_execution_mode": "yarn"}
        )
        == "docker"
    )
    get_airflow_variable.assert_called_once_with("SANDBOX_EXECUTION_MODE", "docker")


def test_resolve_workflow_retry_policy_parses_defaults_and_overrides(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    policy = docker_sandbox_runner.resolve_workflow_retry_policy()
    assert policy.max_attempts == 3
    assert policy.initial_delay_seconds == 60
    assert policy.max_delay_seconds == 300
    assert policy.backoff_multiplier == 2
    assert policy.jitter_ratio == 0.2

    values = {
        "WORKFLOW_RETRY_MAX_ATTEMPTS": "4",
        "WORKFLOW_RETRY_INITIAL_DELAY_SECONDS": "1.5",
        "WORKFLOW_RETRY_MAX_DELAY_SECONDS": "9",
        "WORKFLOW_RETRY_BACKOFF_MULTIPLIER": "3",
        "WORKFLOW_RETRY_JITTER_RATIO": "0",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    policy = docker_sandbox_runner.resolve_workflow_retry_policy()

    assert policy.max_attempts == 4
    assert policy.initial_delay_seconds == 1.5
    assert policy.max_delay_seconds == 9
    assert policy.backoff_multiplier == 3
    assert policy.jitter_ratio == 0
    assert docker_sandbox_runner.workflow_retry_delay(1, policy) == 1.5
    assert docker_sandbox_runner.workflow_retry_delay(3, policy) == 9


def test_resolve_workflow_retry_policy_rejects_invalid_values(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: (
            "0" if name == "WORKFLOW_RETRY_MAX_ATTEMPTS" else default
        ),
    )

    with pytest.raises(ValueError, match="WORKFLOW_RETRY_MAX_ATTEMPTS"):
        docker_sandbox_runner.resolve_workflow_retry_policy()


def test_retry_classification_distinguishes_transient_and_terminal_errors(
    docker_sandbox_runner,
) -> None:
    assert docker_sandbox_runner.is_retryable_exception(
        RuntimeError("Sandbox timed out after 30 minutes")
    )
    assert docker_sandbox_runner.is_retryable_exception(
        RuntimeError("Sandbox exited with code 125 and produced no readable structured result")
    )
    assert not docker_sandbox_runner.is_retryable_exception(
        docker_sandbox_runner.JiraCommandValidationError(
            "Jira command validation failed",
            ["bad input"],
        )
    )

    response = docker_sandbox_runner.requests.Response()
    response.status_code = 429
    http_error = docker_sandbox_runner.requests.HTTPError("rate limited")
    http_error.response = response
    assert docker_sandbox_runner.is_retryable_exception(http_error)

    response.status_code = 400
    assert not docker_sandbox_runner.is_retryable_exception(http_error)


def test_resolve_yarn_config_rejects_invalid_numeric_and_boolean_values(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    values = {
        "HADOOP_HOME": "/opt/hadoop",
        "HADOOP_CONF_DIR": "/opt/hadoop/etc/hadoop",
        "SANDBOX_HDFS_NAMENODE_URI": "hdfs://namenode:8020",
        "SANDBOX_HDFS_RUN_ROOT": "/user/airflow/runs",
        "SANDBOX_YARN_QUEUE": "root.default",
        "SANDBOX_YARN_MASTER_MEMORY_MB": "not-an-int",
        "SANDBOX_YARN_CONTAINER_MEMORY_MB": "4096",
        "SANDBOX_YARN_TIMEOUT_SECONDS": "2100",
        "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS": "300",
        "SANDBOX_YARN_CLEANUP_HDFS": "false",
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: values.get(name, default),
    )

    with pytest.raises(ValueError, match="SANDBOX_YARN_MASTER_MEMORY_MB must be an integer"):
        docker_sandbox_runner.resolve_yarn_config()

    values["SANDBOX_YARN_MASTER_MEMORY_MB"] = "512"
    values["SANDBOX_YARN_CLEANUP_HDFS"] = "maybe"
    with pytest.raises(ValueError, match="SANDBOX_YARN_CLEANUP_HDFS must be a boolean"):
        docker_sandbox_runner.resolve_yarn_config()


def yarn_config(cleanup_hdfs: bool = False) -> dict:
    return {
        "hadoop_home": "/opt/hadoop",
        "hadoop_conf_dir": "/opt/hadoop/etc/hadoop",
        "hdfs_namenode_uri": "hdfs://namenode:8020",
        "hdfs_run_root": "/user/airflow/runs",
        "yarn_queue": "root.default",
        "master_memory_mb": 512,
        "container_memory_mb": 4096,
        "timeout_seconds": 2100,
        "hdfs_safe_mode_wait_seconds": 0,
        "cleanup_hdfs": cleanup_hdfs,
    }


def test_materialize_hdfs_input_dataset_downloads_and_hashes_file(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    source_bytes = b"date,fund_return\n2024-01-31,0.01\n"
    runtime_request = {
        "input_datasets": [
            {
                "dataset_id": "experiment_2_input",
                "source_kind": "hdfs",
                "original_filename": "experiment_2_input.csv",
                "source_uri": (
                    "hdfs:///user/masteruser/quant-experiment-data/"
                    "SCRUM-195/experiment_2_input.csv"
                ),
                "container_path": (
                    "/workspace/input/datasets/experiment_2_input.csv"
                ),
                "format": "csv",
                "read_only": True,
            }
        ]
    }
    config = {
        **yarn_config(),
        "allowed_roots": [
            "hdfs://namenode:8020/user/masteruser/quant-experiment-data"
        ],
        "max_file_bytes": 1024,
        "max_total_bytes": 2048,
    }

    def fake_hadoop(command, _config, **_kwargs):
        if command[1:4] == ["dfs", "-test", "-f"]:
            return Mock(returncode=0, stdout="", stderr="")
        if command[1:4] == ["dfs", "-stat", "%b"]:
            return Mock(returncode=0, stdout=f"{len(source_bytes)}\n", stderr="")
        assert command[1:4] == ["dfs", "-get", "-f"]
        Path(command[-1]).write_bytes(source_bytes)
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docker_sandbox_runner, "run_hadoop_command", fake_hadoop)
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    updated = docker_sandbox_runner.materialize_hdfs_input_datasets(
        runtime_request,
        input_dir,
        config,
    )

    dataset = updated["input_datasets"][0]
    local_file = input_dir / "datasets" / "experiment_2_input.csv"
    assert local_file.read_bytes() == source_bytes
    assert dataset["source_uri"] == (
        "hdfs://namenode:8020/user/masteruser/quant-experiment-data/"
        "SCRUM-195/experiment_2_input.csv"
    )
    assert dataset["size_bytes"] == len(source_bytes)
    assert dataset["sha256"] == docker_sandbox_runner.hashlib.sha256(
        source_bytes
    ).hexdigest()


def test_materialize_hdfs_input_dataset_rejects_oversized_file_before_download(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    request = {
        "input_datasets": [
            {
                "dataset_id": "large",
                "source_kind": "hdfs",
                "original_filename": "large.csv",
                "source_uri": "hdfs:///approved/large.csv",
                "container_path": "/workspace/input/datasets/large.csv",
                "format": "csv",
                "read_only": True,
            }
        ]
    }
    config = {
        **yarn_config(),
        "allowed_roots": ["hdfs://namenode:8020/approved"],
        "max_file_bytes": 4,
        "max_total_bytes": 8,
    }
    commands = []

    def fake_hadoop(command, _config, **_kwargs):
        commands.append(command)
        if command[1:4] == ["dfs", "-test", "-f"]:
            return Mock(returncode=0, stdout="", stderr="")
        if command[1:4] == ["dfs", "-stat", "%b"]:
            return Mock(returncode=0, stdout="5\n", stderr="")
        raise AssertionError("oversized input must not be downloaded")

    monkeypatch.setattr(docker_sandbox_runner, "run_hadoop_command", fake_hadoop)
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    with pytest.raises(ValueError, match="above SANDBOX_INPUT_MAX_FILE_BYTES"):
        docker_sandbox_runner.materialize_hdfs_input_datasets(
            request,
            input_dir,
            config,
        )

    assert [command[1:4] for command in commands] == [
        ["dfs", "-test", "-f"],
        ["dfs", "-stat", "%b"],
    ]


def test_materialize_hdfs_input_dataset_rejects_total_limit_before_download(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    request = {
        "input_datasets": [
            {
                "dataset_id": "first",
                "source_kind": "hdfs",
                "original_filename": "first.csv",
                "source_uri": "hdfs:///approved/first.csv",
                "container_path": "/workspace/input/datasets/first.csv",
                "format": "csv",
                "read_only": True,
            },
            {
                "dataset_id": "second",
                "source_kind": "hdfs",
                "original_filename": "second.csv",
                "source_uri": "hdfs:///approved/second.csv",
                "container_path": "/workspace/input/datasets/second.csv",
                "format": "csv",
                "read_only": True,
            },
        ]
    }
    config = {
        **yarn_config(),
        "allowed_roots": ["hdfs://namenode:8020/approved"],
        "max_file_bytes": 8,
        "max_total_bytes": 8,
    }
    stat_sizes = {"first.csv": 8, "second.csv": 1}
    get_calls = 0

    def fake_hadoop(command, _config, **_kwargs):
        nonlocal get_calls
        if command[1:4] == ["dfs", "-test", "-f"]:
            return Mock(returncode=0, stdout="", stderr="")
        if command[1:4] == ["dfs", "-stat", "%b"]:
            return Mock(
                returncode=0,
                stdout=f"{stat_sizes[Path(command[-1]).name]}\n",
                stderr="",
            )
        if command[1:4] == ["dfs", "-get", "-f"]:
            get_calls += 1
            Path(command[-1]).write_bytes(b"12345678")
            return Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(docker_sandbox_runner, "run_hadoop_command", fake_hadoop)
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    with pytest.raises(ValueError, match="Total HDFS input size"):
        docker_sandbox_runner.materialize_hdfs_input_datasets(
            request,
            input_dir,
            config,
        )

    assert get_calls == 1


def test_materialize_hdfs_input_dataset_rejects_unapproved_root(
    docker_sandbox_runner,
    tmp_path,
) -> None:
    request = {
        "input_datasets": [
            {
                "dataset_id": "data",
                "source_kind": "hdfs",
                "original_filename": "data.csv",
                "source_uri": "hdfs:///secret/data.csv",
                "container_path": "/workspace/input/datasets/data.csv",
                "format": "csv",
                "read_only": True,
            }
        ]
    }
    config = {
        **yarn_config(),
        "allowed_roots": ["hdfs://namenode:8020/approved"],
        "max_file_bytes": 1024,
        "max_total_bytes": 2048,
    }
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    with pytest.raises(ValueError, match="outside the approved roots"):
        docker_sandbox_runner.materialize_hdfs_input_datasets(
            request,
            input_dir,
            config,
        )


def test_build_yarn_shell_command_fetches_and_verifies_input_dataset(
    docker_sandbox_runner,
) -> None:
    command = docker_sandbox_runner.build_yarn_shell_command(
        "hdfs://namenode:8020/runs/request.json",
        "hdfs://namenode:8020/runs/output",
        "hdfs://namenode:8020/runs/debug.txt",
        "hdfs://namenode:8020/runs/env.sh",
        "sandbox:stable",
        yarn_config(),
        {
            "cpu_vcpus": 1.0,
            "memory_mb": 2048,
            "pids_limit": 128,
        },
        input_datasets=[
            {
                "dataset_id": "experiment_2_input",
                "container_path": (
                    "/workspace/input/datasets/experiment_2_input.csv"
                ),
                "run_staged_hdfs_uri": (
                    "hdfs://namenode:8020/runs/input/datasets/"
                    "experiment_2_input.csv"
                ),
                "size_bytes": 37,
                "sha256": "a" * 64,
            }
        ],
    )

    assert (
        'dfs -get -f hdfs://namenode:8020/runs/input/datasets/'
        'experiment_2_input.csv "$WORKDIR/input/datasets/'
        'experiment_2_input.csv"'
    ) in command
    assert "ACTUAL_SIZE=$(wc -c" in command
    assert "ACTUAL_SHA=$(sha256sum" in command
    assert "INPUT_DATASET_VERIFIED=experiment_2_input" in command


def test_build_yarn_shell_command_supports_attachment_and_dataset_together(
    docker_sandbox_runner,
) -> None:
    command = docker_sandbox_runner.build_yarn_shell_command(
        "hdfs://namenode:8020/runs/request.json",
        "hdfs://namenode:8020/runs/output",
        "hdfs://namenode:8020/runs/debug.txt",
        "hdfs://namenode:8020/runs/env.sh",
        "sandbox:stable",
        yarn_config(),
        {
            "cpu_vcpus": 1.0,
            "memory_mb": 2048,
            "pids_limit": 128,
        },
        "hdfs://namenode:8020/runs/input/attachments/SCRUM-195/template.request",
        "attachments/SCRUM-195/template.request",
        input_datasets=[
            {
                "dataset_id": "experiment_2_input",
                "container_path": (
                    "/workspace/input/datasets/experiment_2_input.csv"
                ),
                "run_staged_hdfs_uri": (
                    "hdfs://namenode:8020/runs/input/datasets/"
                    "experiment_2_input.csv"
                ),
                "size_bytes": 37,
                "sha256": "a" * 64,
            }
        ],
    )

    assert (
        "dfs -get -f "
        "hdfs://namenode:8020/runs/input/attachments/SCRUM-195/"
        "template.request "
        '"$WORKDIR/input/attachments/SCRUM-195/template.request"'
    ) in command
    assert "REQUEST_ATTACHMENT=attachments/SCRUM-195/template.request" in command
    assert (
        "dfs -get -f hdfs://namenode:8020/runs/input/datasets/"
        'experiment_2_input.csv "$WORKDIR/input/datasets/'
        'experiment_2_input.csv"'
    ) in command
    assert "INPUT_DATASET_VERIFIED=experiment_2_input" in command


def test_workspace_input_relative_path_accepts_supported_input_namespaces(
    docker_sandbox_runner,
) -> None:
    assert docker_sandbox_runner.workspace_input_relative_path(
        "/workspace/input/attachments/SCRUM-195/template.request"
    ).as_posix() == "attachments/SCRUM-195/template.request"
    assert docker_sandbox_runner.workspace_input_relative_path(
        "/workspace/input/datasets/experiment_2_input.csv"
    ).as_posix() == "datasets/experiment_2_input.csv"

    with pytest.raises(ValueError, match="attachments/ or /workspace/input/datasets"):
        docker_sandbox_runner.workspace_input_relative_path(
            "/workspace/input/other/unapproved.csv"
        )


def test_build_yarn_shell_command_applies_nested_docker_limits(
    docker_sandbox_runner,
) -> None:
    command = docker_sandbox_runner.build_yarn_shell_command(
        "hdfs://namenode:8020/runs/request.json",
        "hdfs://namenode:8020/runs/output",
        "hdfs://namenode:8020/runs/debug.txt",
        "hdfs://namenode:8020/runs/env.sh",
        "sandbox:stable",
        yarn_config(),
        {
            "cpu_vcpus": 1.5,
            "memory_mb": 2048,
            "pids_limit": 128,
        },
    )

    assert "--cpus 1.5" in command
    assert "--memory 2048m" in command
    assert "--pids-limit 128" in command
    assert "-e USE_REAL_BACKTESTER" in command
    assert "-e SIMULATION_REQUESTS_DIR" in command
    assert "-e SIMULATION_RESULTS_DIR" in command
    assert "BACKTEST_STORAGE_KIND_UNSUPPORTED" in command
    assert "resolve_backtester_worker_home()" in command
    assert "BACKTESTER_WORKER_HOME=$(resolve_backtester_worker_home)" in command
    assert "BACKTESTER_WORKER_HOME=$BACKTESTER_WORKER_HOME" in command
    assert "expand_backtester_host_dir()" in command
    assert "*/~/*)" in command
    assert "SIMULATION_REQUESTS_HOST_DIR_INVALID" in command
    assert "SIMULATION_RESULTS_HOST_DIR_INVALID" in command
    assert "source=$SIMULATION_REQUESTS_HOST_DIR,target=$SIMULATION_REQUESTS_DIR" in command
    assert "source=$SIMULATION_RESULTS_HOST_DIR,target=$SIMULATION_RESULTS_DIR" in command
    assert 'hdfs" dfs -ls "$SIMULATION_RESULTS_HDFS_DIR"' in command
    assert 'hdfs" dfs -get "$REMOTE_RESULT_PATH" "$TMP_RESULT_PATH"' in command
    assert 'mv "$TMP_RESULT_PATH" "$LOCAL_RESULT_PATH"' in command
    assert "-type d" not in command
    assert "SIMULATION_DEPLOYED_JOBS" not in command


def test_run_hadoop_command_retries_while_hdfs_is_in_safe_mode(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    config = yarn_config()
    config["hdfs_safe_mode_wait_seconds"] = 30
    failed = Mock(
        returncode=1,
        stdout="",
        stderr=(
            "mkdir: Cannot create directory /user/airflow/runs. "
            "Name node is in safe mode."
        ),
    )
    succeeded = Mock(returncode=0, stdout="", stderr="")
    run = Mock(side_effect=[failed, succeeded])
    sleep = Mock()
    monkeypatch.setattr(docker_sandbox_runner.subprocess, "run", run)
    monkeypatch.setattr(
        docker_sandbox_runner.time,
        "monotonic",
        Mock(side_effect=[0, 1, 1]),
    )
    monkeypatch.setattr(docker_sandbox_runner.time, "sleep", sleep)

    result = docker_sandbox_runner.run_hadoop_command(
        ["/opt/hadoop/bin/hdfs", "dfs", "-mkdir", "-p", "/user/airflow/runs"],
        config,
    )

    assert result is succeeded
    assert run.call_count == 2
    sleep.assert_called_once_with(10)


def install_yarn_mocks(
    docker_sandbox_runner,
    monkeypatch,
    *,
    subprocess_returncode: int,
    cleanup_hdfs: bool = False,
):
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_yarn_config",
        Mock(return_value=yarn_config(cleanup_hdfs=cleanup_hdfs)),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://litellm.example",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(
            return_value={
                "GITHUB_USERNAME": "octocat",
                "GITHUB_TOKEN": "github-secret",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_ghcr_credentials",
        Mock(
            return_value={
                "GHCR_USERNAME": "octocat",
                "GHCR_TOKEN": "ghcr-secret",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "find_distributed_shell_jar",
        Mock(return_value="/opt/hadoop/share/hadoop/yarn/distributedshell.jar"),
    )
    monkeypatch.setattr(docker_sandbox_runner, "hdfs_exists", Mock(return_value=True))
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: (
            "airflow-image" if name == "SANDBOX_IMAGE" else default
        ),
    )
    run_hadoop_command = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_hadoop_command",
        run_hadoop_command,
    )

    def copy_hdfs_output_to_local(hdfs_output_dir, output_dir, config):
        artifacts_dir = output_dir / "artifacts"
        artifacts_dir.mkdir()
        (artifacts_dir / "equity_curve.csv").write_text("date,value\n", encoding="utf-8")
        result_status = "failed" if subprocess_returncode else "succeeded"
        (output_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": result_status,
                    "performance_metrics": {
                        "time_series_data_path": (
                            "/workspace/output/artifacts/equity_curve.csv"
                        )
                    },
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        docker_sandbox_runner,
        "copy_hdfs_output_to_local",
        Mock(side_effect=copy_hdfs_output_to_local),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        Mock(
            return_value=[
                {
                    "id": "att-1",
                    "filename": "SCRUM-5__equity_curve.csv",
                    "size": 11,
                    "content": "https://jira/attachment/att-1",
                }
            ]
        ),
    )
    run = Mock(
        return_value=Mock(
            returncode=subprocess_returncode,
            stdout="yarn stdout",
            stderr="yarn stderr",
        )
    )
    monkeypatch.setattr(docker_sandbox_runner.subprocess, "run", run)
    return run, run_hadoop_command


def test_run_container_dispatches_to_yarn_when_configured(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: "yarn" if name == "SANDBOX_EXECUTION_MODE" else default,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_current_context",
        Mock(return_value={"dag_run": Mock(run_id="manual__1")}),
    )
    run_container_via_yarn = Mock(return_value={"status": "succeeded"})
    run_container_local_docker = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_via_yarn",
        run_container_via_yarn,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_local_docker",
        run_container_local_docker,
    )

    assert docker_sandbox_runner.run_container(grouped_payload()) == {
        "status": "succeeded"
    }
    run_container_via_yarn.assert_called_once_with(grouped_payload(), "manual__1")
    run_container_local_docker.assert_not_called()


def test_run_container_falls_back_to_docker_for_yarn_hdfs_failure_when_enabled(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: {
            "SANDBOX_EXECUTION_MODE": "yarn",
            "WORKFLOW_ENABLE_DOCKER_FALLBACK": "true",
        }.get(name, default),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_current_context",
        Mock(return_value={"dag_run": Mock(run_id="manual__1")}),
    )
    yarn_error = docker_sandbox_runner.HadoopCommandError(
        ["/opt/hadoop/bin/hdfs", "dfs", "-mkdir", "-p", "/runs"],
        "",
        "Name node is in safe mode",
        returncode=1,
    )
    run_container_via_yarn = Mock(side_effect=yarn_error)
    run_container_local_docker = Mock(
        return_value={"status": "succeeded", "issue_key": "SCRUM-5"}
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_via_yarn",
        run_container_via_yarn,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_local_docker",
        run_container_local_docker,
    )

    result = docker_sandbox_runner.run_container(grouped_payload())

    assert result["status"] == "succeeded"
    assert result["execution_fallback"] == {
        "enabled": True,
        "primary_execution_mode": "yarn",
        "fallback_execution_mode": "docker",
        "fallback_reason": docker_sandbox_runner.exception_message(yarn_error),
    }
    run_container_via_yarn.assert_called_once_with(grouped_payload(), "manual__1")
    run_container_local_docker.assert_called_once_with(grouped_payload())


def test_run_container_does_not_fallback_to_docker_when_disabled(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: {
            "SANDBOX_EXECUTION_MODE": "yarn",
            "WORKFLOW_ENABLE_DOCKER_FALLBACK": "false",
        }.get(name, default),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_current_context",
        Mock(return_value={"dag_run": Mock(run_id="manual__1")}),
    )
    yarn_error = docker_sandbox_runner.HadoopCommandError(
        ["/opt/hadoop/bin/hdfs", "dfs", "-mkdir", "-p", "/runs"],
        "",
        "Name node is in safe mode",
        returncode=1,
    )
    run_container_via_yarn = Mock(side_effect=yarn_error)
    run_container_local_docker = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_via_yarn",
        run_container_via_yarn,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container_local_docker",
        run_container_local_docker,
    )

    with pytest.raises(docker_sandbox_runner.HadoopCommandError):
        docker_sandbox_runner.run_container(grouped_payload())

    run_container_local_docker.assert_not_called()


def test_yarn_command_does_not_include_secret_values_and_ingests_artifacts(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    worker_env = {}
    monkeypatch.setattr(
        docker_sandbox_runner,
        "write_worker_env_file",
        Mock(side_effect=lambda env: worker_env.update(env) or "/tmp/worker-env.sh"),
    )
    run, run_hadoop_command = install_yarn_mocks(
        docker_sandbox_runner,
        monkeypatch,
        subprocess_returncode=0,
        cleanup_hdfs=True,
    )

    payload = grouped_payload()
    payload["request"]["comments"][1]["text"] += (
        " cpu_vcpus=1.5 memory_mb=2048 execution_timeout_seconds=900 "
        "pids_limit=128 yarn_queue=research"
    )
    result = docker_sandbox_runner.run_container_via_yarn(
        payload,
        "manual__2026-06-24T16:38:27",
    )

    yarn_command = run.call_args.args[0]
    command_text = " ".join(yarn_command)
    assert "litellm-secret" not in command_text
    assert "github-secret" not in command_text
    assert "ghcr-secret" not in command_text
    assert "Latest request" not in command_text
    assert "-file" not in yarn_command
    assert "SANDBOX_ENV_FILE=" not in command_text
    assert "SANDBOX_ENV_HDFS_PATH=" in command_text
    assert yarn_command[yarn_command.index("-container_memory") + 1] == "2048"
    assert yarn_command[yarn_command.index("-container_vcores") + 1] == "2"
    assert yarn_command[yarn_command.index("-queue") + 1] == "research"
    assert run.call_args.kwargs["timeout"] == 900
    assert result["artifact_ingestion"]["uploaded"] == [
        {
            "source_path": "/workspace/output/artifacts/equity_curve.csv",
            "filename": "SCRUM-5__equity_curve.csv",
            "size_bytes": 11,
            "jira_attachment_id": "att-1",
            "jira_attachment_url": "https://jira/attachment/att-1",
        }
    ]
    assert worker_env["GITHUB_USERNAME"] == "octocat"
    assert worker_env["GITHUB_TOKEN"] == "github-secret"
    assert worker_env["GHCR_USERNAME"] == "octocat"
    assert worker_env["GHCR_TOKEN"] == "ghcr-secret"
    hadoop_commands = [call.args[0] for call in run_hadoop_command.call_args_list]
    assert any(
        "-chmod" in command and any("worker-env.sh" in part for part in command)
        for command in hadoop_commands
    )
    assert any(
        "-rm" in command and any("worker-env.sh" in part for part in command)
        for command in hadoop_commands
    )
    assert any(
        "-rm" in command and "/user/airflow/runs" in " ".join(command)
        for command in hadoop_commands
    )


def test_yarn_worker_passes_real_backtester_env_to_nested_docker(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    worker_env = {}
    monkeypatch.setattr(
        docker_sandbox_runner,
        "write_worker_env_file",
        Mock(side_effect=lambda env: worker_env.update(env) or "/tmp/worker-env.sh"),
    )
    install_yarn_mocks(
        docker_sandbox_runner,
        monkeypatch,
        subprocess_returncode=0,
    )
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    airflow_variables = {
        "SANDBOX_IMAGE": "airflow-image",
        "USE_REAL_BACKTESTER": "true",
        "SIMULATION_REQUESTS_DIR": str(requests),
        "SIMULATION_RESULTS_DIR": str(results),
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_airflow_variable",
        lambda name, default=None: airflow_variables.get(name, default),
    )

    docker_sandbox_runner.run_container_via_yarn(
        grouped_payload(),
        "manual__2026-06-24T16:38:27",
    )

    assert worker_env["USE_REAL_BACKTESTER"] == "true"
    assert worker_env["BACKTEST_STORAGE_KIND"] == "shared_fs"
    assert worker_env["SIMULATION_REQUESTS_HOST_DIR"] == str(requests)
    assert worker_env["SIMULATION_RESULTS_HOST_DIR"] == str(results)
    assert worker_env["SIMULATION_REQUESTS_DIR"] == "/workspace/backtester/simulation-requests"
    assert worker_env["SIMULATION_RESULTS_DIR"] == "/workspace/backtester/simulation-results"
    assert "SIMULATION_DEPLOYED_JOBS_HOST_DIR" not in worker_env
    assert "SIMULATION_DEPLOYED_JOBS_DIR" not in worker_env


def test_yarn_nonzero_exit_with_result_returns_failed_result(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    install_yarn_mocks(
        docker_sandbox_runner,
        monkeypatch,
        subprocess_returncode=1,
    )

    result = docker_sandbox_runner.run_container_via_yarn(
        grouped_payload(),
        "manual__2026-06-24T16:38:27",
    )

    assert result["status"] == "failed"
    assert result["issue_key"] == "SCRUM-5"
    assert result["artifact_ingestion"]["uploaded"][0]["filename"] == (
        "SCRUM-5__equity_curve.csv"
    )


def test_yarn_reads_configured_result_path_and_progress_events(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    runtime_request = {
        "schema_version": "1.0",
        "run_id": "run-SCRUM-5-yarn-custom",
        "jira_metadata": {"ticket_id": "SCRUM-5"},
        "output_paths": {
            "result_path": "/workspace/output/nested/result.json",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "validate_jira_command_payload",
        Mock(
            return_value=types.SimpleNamespace(
                issue_key="SCRUM-5",
                request_text="Latest request ticker=AAPL",
                runtime_request=runtime_request,
            )
        ),
    )
    install_yarn_mocks(
        docker_sandbox_runner,
        monkeypatch,
        subprocess_returncode=0,
    )

    def copy_hdfs_output_to_local(hdfs_output_dir, output_dir, config):
        nested_dir = output_dir / "nested"
        artifacts_dir = output_dir / "artifacts"
        nested_dir.mkdir()
        artifacts_dir.mkdir()
        result = runtime_success_response()
        result["performance_metrics"]["time_series_data_path"] = (
            "/workspace/output/artifacts/equity_curve.csv"
        )
        (artifacts_dir / "equity_curve.csv").write_text(
            "date,value\n",
            encoding="utf-8",
        )
        (nested_dir / "result.json").write_text(
            json.dumps(result),
            encoding="utf-8",
        )
        (output_dir / "progress_events.jsonl").write_text(
            (
                '{"schema_version":"1.0","run_id":"run-SCRUM-5-yarn-custom",'
                '"ticket_id":"SCRUM-5","stage":"backtest","status":"RUNNING",'
                '"timestamp":"2026-06-18T10:31:00Z"}\n'
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        docker_sandbox_runner,
        "copy_hdfs_output_to_local",
        Mock(side_effect=copy_hdfs_output_to_local),
    )

    result = docker_sandbox_runner.run_container_via_yarn(
        grouped_payload(),
        "manual__2026-06-24T16:38:27",
    )

    hdfs_exists_paths = [
        call.args[0]
        for call in docker_sandbox_runner.hdfs_exists.call_args_list
    ]
    assert any(path.endswith("/output/nested/result.json") for path in hdfs_exists_paths)
    assert result["execution_summary"]["status"] == "succeeded"
    assert result["_progress_events"][0]["status"] == "RUNNING"
    assert result["artifact_ingestion"]["uploaded"][0]["filename"] == (
        "SCRUM-5__equity_curve.csv"
    )


def test_ingest_mounted_artifacts_uploads_referenced_files(
    docker_sandbox_runner,
    tmp_path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "output"
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "equity_curve.csv").write_text("date,value\n", encoding="utf-8")
    (artifacts_dir / "plot.png").write_bytes(b"png")
    (artifacts_dir / "run.log").write_text("runtime log", encoding="utf-8")
    result = {
        "run_id": "run_SCRUM-5_20260624T160000",
        "performance_metrics": {
            "time_series_data_path": "/workspace/output/artifacts/equity_curve.csv"
        },
        "generated_artifacts": {
            "backtest_plots_path": "/workspace/output/artifacts/plot.png"
        },
        "diagnostics": {
            "raw_log_reference": "/workspace/output/artifacts/run.log"
        },
    }
    post_jira_attachments = Mock(
        return_value=[
            {
                "id": "1",
                "filename": "run_SCRUM-5_20260624T160000__equity_curve.csv",
                "size": 11,
            },
            {
                "id": "2",
                "filename": "run_SCRUM-5_20260624T160000__plot.png",
                "size": 3,
            },
            {
                "id": "3",
                "filename": "run_SCRUM-5_20260624T160000__run.log",
                "size": 11,
            },
        ]
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )

    ingestion = docker_sandbox_runner.ingest_mounted_artifacts(
        "SCRUM-5",
        output_dir,
        result,
        [],
    )

    assert [path.name for path in post_jira_attachments.call_args.args[1]] == [
        "run_SCRUM-5_20260624T160000__equity_curve.csv",
        "run_SCRUM-5_20260624T160000__plot.png",
        "run_SCRUM-5_20260624T160000__run.log",
    ]
    assert ingestion["uploaded"] == [
        {
            "source_path": "/workspace/output/artifacts/equity_curve.csv",
            "filename": "run_SCRUM-5_20260624T160000__equity_curve.csv",
            "size_bytes": 11,
            "jira_attachment_id": "1",
            "jira_attachment_url": None,
        },
        {
            "source_path": "/workspace/output/artifacts/plot.png",
            "filename": "run_SCRUM-5_20260624T160000__plot.png",
            "size_bytes": 3,
            "jira_attachment_id": "2",
            "jira_attachment_url": None,
        },
        {
            "source_path": "/workspace/output/artifacts/run.log",
            "filename": "run_SCRUM-5_20260624T160000__run.log",
            "size_bytes": 11,
            "jira_attachment_id": "3",
            "jira_attachment_url": None,
        },
    ]
    assert ingestion["missing"] == []
    assert ingestion["skipped"] == []


def test_is_successful_result_accepts_lowercase_and_legacy_uppercase(
    docker_sandbox_runner,
) -> None:
    assert docker_sandbox_runner.is_successful_result({"status": "succeeded"})
    assert docker_sandbox_runner.is_successful_result({"status": "SUCCESS"})
    assert docker_sandbox_runner.is_successful_result(
        {"execution_summary": {"status": "succeeded"}}
    )
    assert docker_sandbox_runner.is_successful_result(
        {"execution_summary": {"status": "SUCCESS"}}
    )
    assert not docker_sandbox_runner.is_successful_result({"status": "failed"})


def test_ingest_mounted_artifacts_uploads_the_executed_request(
    docker_sandbox_runner,
    tmp_path,
    monkeypatch,
) -> None:
    """The .request the engine ran must survive the run.

    It is written into the mounted output directory, which the orchestrator
    deletes once the run is ingested. If it is not uploaded here, the exact
    configuration behind the reported metrics is unrecoverable.
    """
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "quant-SCRUM-5.generated.request").write_text(
        "NPORT = 5\n", encoding="utf-8"
    )
    result = {
        "run_id": "run_SCRUM-5_20260624T160000",
        "generated_artifacts": {
            "executed_request_path": "/workspace/output/quant-SCRUM-5.generated.request"
        },
    }
    post_jira_attachments = Mock(
        return_value=[
            {
                "id": "1",
                "filename": "run_SCRUM-5_20260624T160000__quant-SCRUM-5.generated.request",
                "size": 10,
            }
        ]
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )

    ingestion = docker_sandbox_runner.ingest_mounted_artifacts(
        "SCRUM-5",
        output_dir,
        result,
        [],
    )

    assert [path.name for path in post_jira_attachments.call_args.args[1]] == [
        "run_SCRUM-5_20260624T160000__quant-SCRUM-5.generated.request",
    ]
    assert ingestion["uploaded"][0]["source_path"] == (
        "/workspace/output/quant-SCRUM-5.generated.request"
    )


def test_ingest_mounted_artifacts_records_unsafe_missing_and_oversized_paths(
    docker_sandbox_runner,
    tmp_path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "output"
    artifacts_dir = output_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "large.log").write_text("too large", encoding="utf-8")
    post_jira_attachments = Mock(return_value=[])
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )
    monkeypatch.setattr(docker_sandbox_runner, "ARTIFACT_MAX_BYTES", 1)
    result = {
        "performance_metrics": {
            "time_series_data_path": "/workspace/output/../secret.csv"
        },
        "generated_artifacts": {
            "backtest_plots_path": "/etc/passwd"
        },
        "diagnostics": {
            "raw_log_reference": "/workspace/output/artifacts/missing.log"
        },
    }

    ingestion = docker_sandbox_runner.ingest_mounted_artifacts(
        "SCRUM-5",
        output_dir,
        result,
        [
            "/workspace/output/artifacts",
            "/workspace/output/artifacts/large.log",
        ],
    )

    post_jira_attachments.assert_called_once_with("SCRUM-5", [])
    assert ingestion["missing"] == [
        {
            "path": "/workspace/output/artifacts/missing.log",
            "reason": "file does not exist",
        }
    ]
    assert ingestion["skipped"] == [
        {
            "path": "/workspace/output/../secret.csv",
            "reason": "parent traversal is not allowed",
        },
        {
            "path": "/etc/passwd",
            "reason": "outside /workspace/output",
        },
        {
            "path": "/workspace/output/artifacts",
            "reason": "not a regular file",
        },
        {
            "path": "/workspace/output/artifacts/large.log",
            "reason": "file exceeds 1 byte limit",
            "size_bytes": 9,
        },
    ]


def test_run_container_returns_structured_result_from_nonzero_exit_stdout(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(return_value={"api_key": "litellm-secret", "base_url": "http://weles.cs.ucl.ac.uk:4000", "model": "nova-micro"}),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )
    post_jira_attachments = Mock(return_value=[])
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )
    stream = Mock(
        return_value=Mock(
            returncode=1,
            stdout=(
                "progress\n"
                '{"status":"failed","issue_key":"SCRUM-5",'
                '"summary":"KeyError: choices"}\n'
            ),
            stderr="warn: could not write result\n",
        )
    )
    monkeypatch.setattr(docker_sandbox_runner, "run_command_streaming", stream)

    result = docker_sandbox_runner.run_container(grouped_payload())
    assert result["status"] == "failed"
    assert result["issue_key"] == "SCRUM-5"
    assert result["summary"] == "KeyError: choices"
    post_jira_attachments.assert_called_once_with("SCRUM-5", [])
    assert result["artifact_ingestion"] == {"uploaded": [], "missing": [], "skipped": []}


def test_read_progress_events_file_skips_when_path_omitted(
    docker_sandbox_runner,
    tmp_path,
) -> None:
    assert docker_sandbox_runner.read_progress_events_file(tmp_path, None) == []


def test_read_progress_events_file_fail_soft_for_missing_empty_and_bad_jsonl(
    docker_sandbox_runner,
    tmp_path,
    capsys,
) -> None:
    progress_file = tmp_path / "progress_events.jsonl"
    progress_file.write_text(
        "\n".join(
            [
                (
                    '{"schema_version":"1.0","run_id":"run-001",'
                    '"ticket_id":"SCRUM-5","stage":"fetch","status":"RUNNING",'
                    '"timestamp":"2026-06-18T10:31:00Z"}'
                ),
                "not-json",
                (
                    '{"schema_version":"1.0","run_id":"run-001",'
                    '"ticket_id":"SCRUM-5","stage":"fetch","status":"DONE",'
                    '"timestamp":"2026-06-18T10:32:00Z"}'
                ),
                "[]",
            ]
        ),
        encoding="utf-8",
    )

    events = docker_sandbox_runner.read_progress_events_file(
        tmp_path,
        "/workspace/output/progress_events.jsonl",
    )

    assert events == [
        {
            "schema_version": "1.0",
            "run_id": "run-001",
            "ticket_id": "SCRUM-5",
            "stage": "fetch",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
        }
    ]
    output = capsys.readouterr().out
    assert "Warning: ignoring malformed progress event" in output
    assert "Warning: ignoring unsupported progress event" in output
    assert "Warning: ignoring non-object progress event" in output

    assert docker_sandbox_runner.read_progress_events_file(
        tmp_path,
        "/workspace/output/missing_progress_events.jsonl",
    ) == []
    assert "Warning: progress events file is missing" in capsys.readouterr().out

    (tmp_path / "empty_progress_events.jsonl").write_text("", encoding="utf-8")
    assert docker_sandbox_runner.read_progress_events_file(
        tmp_path,
        "/workspace/output/empty_progress_events.jsonl",
    ) == []
    assert "Warning: progress events file is empty" in capsys.readouterr().out


def test_read_progress_events_file_rejects_schema_incompatible_events(
    docker_sandbox_runner,
    tmp_path,
    capsys,
) -> None:
    progress_file = tmp_path / "progress_events.jsonl"
    invalid_events = [
        {
            "schema_version": "1.0",
            "run_id": "run-001",
            "ticket_id": "SCRUM-5",
            "stage": "deploy",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
        },
        {
            "schema_version": "1.0",
            "run_id": "run-001",
            "ticket_id": "scrum-5",
            "stage": "fetch",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
        },
        {
            "schema_version": "1.0",
            "run_id": "run-001",
            "ticket_id": "SCRUM-5",
            "stage": "fetch",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
            "iteration": 0,
        },
        {
            "schema_version": "1.0",
            "run_id": "run-001",
            "ticket_id": "SCRUM-5",
            "stage": "fetch",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
            "message": "x" * 501,
        },
    ]
    progress_file.write_text(
        "\n".join(json.dumps(event) for event in invalid_events),
        encoding="utf-8",
    )

    assert docker_sandbox_runner.read_progress_events_file(
        tmp_path,
        "/workspace/output/progress_events.jsonl",
    ) == []
    assert capsys.readouterr().out.count(
        "Warning: ignoring unsupported progress event"
    ) == len(invalid_events)


def test_run_container_reads_configured_result_path_progress_and_keeps_artifacts(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    runtime_request = {
        "schema_version": "1.0",
        "run_id": "run-SCRUM-5-custom",
        "jira_metadata": {"ticket_id": "SCRUM-5"},
        "output_paths": {
            "result_path": "/workspace/output/nested/result.json",
            "artifact_dir": "/workspace/output/artifacts",
            "progress_events_path": "/workspace/output/progress_events.jsonl",
        },
    }
    monkeypatch.setattr(
        docker_sandbox_runner,
        "validate_jira_command_payload",
        Mock(
            return_value=types.SimpleNamespace(
                issue_key="SCRUM-5",
                runtime_request=runtime_request,
            )
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_litellm_config",
        Mock(
            return_value={
                "api_key": "litellm-secret",
                "base_url": "http://weles.cs.ucl.ac.uk:4000",
                "model": "nova-micro",
            }
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "get_optional_github_credentials",
        Mock(return_value={}),
    )

    def fake_post_attachments(issue_key, file_paths):
        assert issue_key == "SCRUM-5"
        return [
            {
                "id": f"att-{index}",
                "filename": path.name,
                "size": path.stat().st_size,
                "content": f"https://jira/attachment/att-{index}",
            }
            for index, path in enumerate(file_paths, start=1)
        ]

    post_jira_attachments = Mock(side_effect=fake_post_attachments)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments,
    )

    def fake_stream(command, **kwargs):
        output_mount = next(
            item
            for item in command
            if item.startswith("type=bind,") and "target=/workspace/output" in item
        )
        output_dir = Path(
            next(
                part.removeprefix("source=")
                for part in output_mount.split(",")
                if part.startswith("source=")
            )
        )
        nested_dir = output_dir / "nested"
        artifact_dir = output_dir / "artifacts"
        nested_dir.mkdir()
        artifact_dir.mkdir()
        result = runtime_success_response()
        result["performance_metrics"]["time_series_data_path"] = None
        result["generated_artifacts"]["backtest_plots_path"] = None
        (artifact_dir / "run.log").write_text("runtime log", encoding="utf-8")
        (nested_dir / "result.json").write_text(
            json.dumps(result),
            encoding="utf-8",
        )
        (output_dir / "progress_events.jsonl").write_text(
            (
                '{"schema_version":"1.0","run_id":"run-SCRUM-5-custom",'
                '"ticket_id":"SCRUM-5","stage":"backtest","status":"RUNNING",'
                '"timestamp":"2026-06-18T10:31:00Z"}\n'
            ),
            encoding="utf-8",
        )
        return Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_command_streaming",
        Mock(side_effect=fake_stream),
    )

    result = docker_sandbox_runner.run_container(grouped_payload())

    assert result["execution_summary"]["status"] == "succeeded"
    assert result["_progress_events"][0]["status"] == "RUNNING"
    assert result["artifact_ingestion"]["uploaded"][0]["jira_attachment_id"] == "att-1"
    assert post_jira_attachments.call_args.args[0] == "SCRUM-5"


def test_attach_airflow_log_uploads_run_scoped_task_log(
    docker_sandbox_runner,
    tmp_path,
    monkeypatch,
) -> None:
    task_instance = Mock(
        id="ti-1",
        log_filepath=None,
        dag_id="docker_sandbox_runner",
        task_id="attach_failure_airflow_log",
        run_id="manual__2026-06-24T16:38:27.015240+00:00",
        try_number=1,
        map_index=-1,
    )
    context = {
        "task_instance": task_instance,
        "dag_run": Mock(run_id="manual__2026-06-24T16:38:27.015240+00:00"),
    }
    monkeypatch.setenv("AIRFLOW_LOG_BASE_PATH", str(tmp_path))
    upstream_log_dir = (
        tmp_path
        / "dag_id=docker_sandbox_runner"
        / "run_id=manual__2026-06-24T16:38:27.015240+00:00"
        / "task_id=run_sandbox_and_writeback"
    )
    upstream_log_dir.mkdir(parents=True)
    upstream_log_path = upstream_log_dir / "attempt=1.log"
    upstream_log_path.write_text("final upstream airflow log", encoding="utf-8")
    upload_name = (
        "docker_sandbox_runner_"
        "2026-06-24T16:38:27.015240_"
        "run_sandbox_and_writeback_"
        "attempt=1.log"
    )

    def post_jira_attachments(issue_key, file_paths):
        assert issue_key == "SCRUM-5"
        assert file_paths[0].name == upload_name
        assert file_paths[0].read_text(encoding="utf-8") == "final upstream airflow log"
        return [
            {
                "id": "att-1",
                "filename": file_paths[0].name,
                "size": file_paths[0].stat().st_size,
                "content": "https://jira/attachment/att-1",
            }
        ]

    post_jira_attachments_mock = Mock(side_effect=post_jira_attachments)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_attachments",
        post_jira_attachments_mock,
    )

    ingestion = docker_sandbox_runner.attach_airflow_log(
        "SCRUM-5",
        context,
    )

    upload_path = post_jira_attachments_mock.call_args.args[1][0]
    assert upload_path.name == upload_name
    assert ingestion == {
        "uploaded": [
            {
                "source_path": str(upstream_log_path),
                "filename": upload_name,
                "size_bytes": len("final upstream airflow log"),
                "jira_attachment_id": "att-1",
                "jira_attachment_url": "https://jira/attachment/att-1",
            }
        ],
        "missing": [],
        "skipped": [],
    }


def test_current_airflow_log_path_uses_cluster_log_layout(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIRFLOW_LOG_BASE_PATH", "/opt/airflow3/logs_masteruser")
    task_instance = Mock(
        log_filepath=None,
        dag_id="docker_sandbox_runner",
        task_id="run_sandbox_and_writeback",
        run_id="manual__2026-06-24T16:04:11.760456+00:00",
        try_number=1,
        map_index=-1,
    )
    context = {
        "task_instance": task_instance,
        "dag_run": Mock(
            dag_id="docker_sandbox_runner",
            run_id="manual__2026-06-24T16:04:11.760456+00:00",
        ),
    }

    assert docker_sandbox_runner.current_airflow_log_path(context) == Path(
        "/opt/airflow3/logs_masteruser/"
        "dag_id=docker_sandbox_runner/"
        "run_id=manual__2026-06-24T16:04:11.760456+00:00/"
        "task_id=run_sandbox_and_writeback/"
        "attempt=1.log"
    )


def test_attach_failure_log_if_needed_skips_success_and_uploads_failure(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    context = {"task_instance": Mock(), "dag_run": Mock()}
    attach_airflow_log = Mock(return_value={"uploaded": [{"filename": "attempt=1.log"}]})
    monkeypatch.setattr(docker_sandbox_runner, "attach_airflow_log", attach_airflow_log)

    assert docker_sandbox_runner.attach_failure_log_if_needed(
        grouped_payload(),
        context,
        {"status": "succeeded"},
    ) == {"uploaded": [], "missing": [], "skipped": []}
    attach_airflow_log.assert_not_called()

    assert docker_sandbox_runner.attach_failure_log_if_needed(
        grouped_payload(),
        context,
        {"status": "failed"},
    ) == {"uploaded": [{"filename": "attempt=1.log"}]}
    attach_airflow_log.assert_called_once_with("SCRUM-5", context)


def test_run_and_writeback_posts_success_result(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    result = runtime_success_response()
    result["artifact_ingestion"] = {"uploaded": [], "missing": [], "skipped": []}
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", Mock(return_value=result))
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    assert docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1") == result

    assert post_jira_adf_comment.call_count == 2
    running_text = _joined_text(post_jira_adf_comment.call_args_list[0].args[1])
    assert "Automated quant workflow report: running workflow" in running_text
    assert "Milestone: running" in running_text
    assert post_jira_adf_comment.call_args_list[-1].args[0] == "SCRUM-5"
    posted_text = _joined_text(post_jira_adf_comment.call_args_list[-1].args[1])
    assert "Automated quant workflow report: successful completion" in posted_text
    assert "Workflow ID: " in posted_text
    assert "Final Status: succeeded" in posted_text
    assert "Ingested Artifacts" in posted_text


def test_run_and_writeback_progress_milestone_failure_does_not_block_final(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    result = runtime_success_response()
    result["artifact_ingestion"] = {"uploaded": [], "missing": [], "skipped": []}
    post_jira_adf_comment = Mock(
        side_effect=[
            docker_sandbox_runner.requests.ConnectionError("progress jira outage"),
            None,
        ]
    )
    run_container = Mock(return_value=result)
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    assert docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1") == result

    workflow_id = result["workflow_id"]
    state = docker_sandbox_runner.load_workflow_state(workflow_id)
    run_container.assert_called_once()
    assert post_jira_adf_comment.call_count == 2
    assert state["stages"]["jira_progress_running"]["status"] == "failed"
    assert state["stages"]["jira_writeback"]["status"] == "succeeded"


def test_post_jira_attachments_sends_multipart_with_required_header(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    jira_quant_common = sys.modules["jira_quant_common"]
    file_path = tmp_path / "artifact.log"
    file_path.write_text("artifact body", encoding="utf-8")
    response = Mock(status_code=200, text="[]")
    response.json.return_value = [
        {
            "id": "10001",
            "filename": "artifact.log",
            "size": 13,
            "content": "https://jira/attachment/10001",
        }
    ]
    jira_request = Mock(return_value=response)
    monkeypatch.setattr(jira_quant_common, "jira_request", jira_request)

    uploaded = jira_quant_common.post_jira_attachments("SCRUM-5", [file_path])

    call = jira_request.call_args
    assert call.args == ("POST", "/rest/api/3/issue/SCRUM-5/attachments")
    assert call.kwargs["headers"] == {
        "Accept": "application/json",
        "X-Atlassian-Token": "no-check",
    }
    assert call.kwargs["files"][0][0] == "file"
    assert call.kwargs["files"][0][1][0] == "artifact.log"
    assert call.kwargs["timeout"] == 60
    response.raise_for_status.assert_called_once_with()
    assert uploaded == [
        {
            "id": "10001",
            "filename": "artifact.log",
            "size": 13,
            "content": "https://jira/attachment/10001",
        }
    ]


def test_format_result_comment_uses_runtime_response_contract(
    docker_sandbox_runner,
) -> None:
    result = {
        "schema_version": "1.0",
        "run_id": "run_SCRUM-9_20260624T2031576130530",
        "execution_summary": {
            "ticket_id": "SCRUM-9",
            "status": "succeeded",
            "iteration_traces": [
                {
                    "iteration": 5,
                    "agent": "backtest_agent",
                    "tool_call": "run_backtest_via_mcp",
                    "status": "succeeded",
                    "message": "Backtest completed via MCP.",
                }
            ],
        },
        "performance_metrics": {
            "total_return": 0.45,
            "sharpe_ratio": 1.2,
            "max_drawdown": -0.08,
        },
        "generated_artifacts": {
            "modified_files": ["rae_runtime/proxy/strategy.py"],
            "new_files": [],
            "backtest_plots_path": None,
        },
        "diagnostics": {
            "error_code": None,
            "error_message": None,
        },
        "artifact_ingestion": {
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
        },
    }

    comment = docker_sandbox_runner.format_result_comment(result)

    assert "Status: succeeded" in comment
    assert "Command: run_SCRUM-9_20260624T2031576130530" in comment
    assert "Summary: Backtest completed via MCP." in comment
    assert "- Sharpe ratio: 1.2" in comment
    assert "- Max drawdown: -0.08" in comment
    assert "- Total return: 0.45" in comment
    assert "- Modified files: rae_runtime/proxy/strategy.py" in comment
    assert "- New files: None" in comment
    assert "Ingested artifacts:" in comment
    assert (
        "- Uploaded: run.log (128 bytes, attachment att-1, "
        "https://jira/attachment/att-1)"
    ) in comment
    assert (
        "- Missing: /workspace/output/artifacts/equity_curve.csv "
        "(file does not exist)"
    ) in comment
    assert "Status: None" not in comment
    assert "Summary: None" not in comment


def test_format_result_comment_omits_metrics_for_non_backtest_request(
    docker_sandbox_runner,
) -> None:
    result = {
        "schema_version": "1.0",
        "run_id": "run_SCRUM-10_20260624T2031576130530",
        "execution_summary": {
            "ticket_id": "SCRUM-10",
            "status": "succeeded",
            "request_type": "refactor",
            "iteration_traces": [
                {
                    "iteration": 1,
                    "agent": "code_agent",
                    "tool_call": "edit_file",
                    "status": "succeeded",
                    "message": "Refactored the data loader.",
                }
            ],
        },
        "performance_metrics": None,
        "generated_artifacts": {
            "modified_files": ["src/data/loader.py"],
            "new_files": [],
            "backtest_plots_path": None,
        },
        "diagnostics": {
            "error_code": None,
            "error_message": None,
        },
    }

    comment = docker_sandbox_runner.format_result_comment(result)

    assert "Status: succeeded" in comment
    assert "Request Type: refactor" in comment
    assert "Summary: Refactored the data loader." in comment
    assert "- Modified files: src/data/loader.py" in comment
    assert "Metrics:" not in comment
    assert "Sharpe ratio" not in comment
    assert "Backtest plot" not in comment


def test_run_and_writeback_posts_failed_result_with_null_metrics(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    result = runtime_failure_response()
    result["artifact_ingestion"] = {"uploaded": [], "missing": [], "skipped": []}
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", Mock(return_value=result))

    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    assert docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1") == result

    assert post_jira_adf_comment.call_count == 2
    posted_text = _joined_text(post_jira_adf_comment.call_args_list[-1].args[1])
    assert "Final Status: failed" in posted_text
    assert "Performance Metrics" not in posted_text
    assert "Error Message: KeyError: 'choices'" in posted_text


def test_run_and_writeback_posts_failure_result(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    post_jira_adf_comment = Mock()
    post_jira_comment = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container",
        Mock(side_effect=RuntimeError("container failed")),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="container failed"):
        docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    assert post_jira_adf_comment.call_count == 2
    running_text = _joined_text(post_jira_adf_comment.call_args_list[0].args[1])
    assert "Milestone: running" in running_text
    failed_text = _joined_text(post_jira_adf_comment.call_args_list[-1].args[1])
    assert "Automated quant workflow report: failed workflow" in failed_text
    assert "Milestone: failed" in failed_text
    assert "Final Status: failed" in failed_text
    assert "Error Type: RuntimeError" in failed_text
    assert "Dag Run Id: manual__1" in failed_text
    post_jira_comment.assert_not_called()


def test_run_and_writeback_posts_partial_adf_when_structured_result_unavailable(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    progress_events = [
        {
            "schema_version": "1.0",
            "run_id": validated.runtime_request["run_id"],
            "ticket_id": "SCRUM-5",
            "stage": "backtest",
            "status": "RUNNING",
            "timestamp": "2026-06-18T10:31:00Z",
            "message": "Backtest running",
        }
    ]
    unavailable = docker_sandbox_runner.SandboxStructuredResultUnavailable(
        issue_key="SCRUM-5",
        runtime_request=validated.runtime_request,
        progress_events=progress_events,
        returncode=0,
        stdout="",
        stderr="",
        result_path="/workspace/output/result.json",
        execution_mode="Docker",
    )
    post_jira_adf_comment = Mock()
    post_jira_comment = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_workflow_retry_policy",
        Mock(
            return_value=docker_sandbox_runner.WorkflowRetryPolicy(
                1,
                0,
                0,
                2,
                0,
            )
        ),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_container",
        Mock(side_effect=unavailable),
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    with pytest.raises(docker_sandbox_runner.WorkflowRetryExhausted):
        docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    post_jira_comment.assert_not_called()
    assert post_jira_adf_comment.call_count == 2
    running_text = _joined_text(post_jira_adf_comment.call_args_list[0].args[1])
    assert "Milestone: running" in running_text
    posted_text = _joined_text(post_jira_adf_comment.call_args_list[-1].args[1])
    workflow_id = docker_sandbox_runner.build_workflow_id(
        "SCRUM-5",
        validated.comment_id,
        validated.request_text,
    )
    assert "Automated quant workflow report: partial progress" in posted_text
    assert f"Workflow ID: {workflow_id}" in posted_text
    assert "Final Status: not available" in posted_text
    assert "backtest: RUNNING" in posted_text
    assert "Diagnostics" in posted_text
    assert "Execution Mode: Docker" in posted_text
    assert "Exit Code: 0" in posted_text
    assert "Result Path: /workspace/output/result.json" in posted_text


def test_run_and_writeback_retries_transient_sandbox_failure_then_succeeds(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_workflow_retry_policy",
        Mock(
            return_value=docker_sandbox_runner.WorkflowRetryPolicy(
                2,
                0,
                0,
                2,
                0,
            )
        ),
    )
    run_container = Mock(
        side_effect=[
            RuntimeError("Sandbox timed out after 30 minutes"),
            {"status": "succeeded", "issue_key": "SCRUM-5", "summary": "ok"},
        ]
    )
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )
    monkeypatch.setattr(docker_sandbox_runner.time, "sleep", Mock())

    result = docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    workflow_id = result["workflow_id"]
    state = docker_sandbox_runner.load_workflow_state(workflow_id)
    assert result["status"] == "succeeded"
    assert run_container.call_count == 2
    assert post_jira_adf_comment.call_count == 2
    assert state["status"] == "succeeded"
    assert state["stages"]["sandbox"]["attempts"] == 2
    assert state["stages"]["jira_progress_running"]["status"] == "succeeded"
    assert state["stages"]["jira_writeback"]["status"] == "succeeded"


def test_run_and_writeback_retries_transient_jira_writeback_failure(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_workflow_retry_policy",
        Mock(
            return_value=docker_sandbox_runner.WorkflowRetryPolicy(
                2,
                0,
                0,
                2,
                0,
            )
        ),
    )
    run_container = Mock(
        return_value={"status": "succeeded", "issue_key": "SCRUM-5", "summary": "ok"}
    )
    post_jira_adf_comment = Mock(
        side_effect=[
            None,
            docker_sandbox_runner.requests.ConnectionError("temporary jira outage"),
            None,
        ]
    )
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )
    monkeypatch.setattr(docker_sandbox_runner.time, "sleep", Mock())

    result = docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    state = docker_sandbox_runner.load_workflow_state(result["workflow_id"])
    run_container.assert_called_once()
    assert post_jira_adf_comment.call_count == 3
    assert state["status"] == "succeeded"
    assert state["stages"]["sandbox"]["attempts"] == 1
    assert state["stages"]["jira_progress_running"]["status"] == "succeeded"
    assert state["stages"]["jira_writeback"]["attempts"] == 2
    assert state["stages"]["jira_writeback"]["status"] == "succeeded"


def test_run_and_writeback_marks_failed_and_posts_once_after_retry_exhaustion(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_workflow_retry_policy",
        Mock(
            return_value=docker_sandbox_runner.WorkflowRetryPolicy(
                2,
                0,
                0,
                2,
                0,
            )
        ),
    )
    run_container = Mock(side_effect=RuntimeError("Sandbox timed out after 30 minutes"))
    post_jira_adf_comment = Mock()
    post_jira_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )
    monkeypatch.setattr(docker_sandbox_runner.time, "sleep", Mock())

    with pytest.raises(docker_sandbox_runner.WorkflowRetryExhausted):
        docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    workflow_id = docker_sandbox_runner.build_workflow_id(
        "SCRUM-5",
        validated.comment_id,
        validated.request_text,
    )
    state = docker_sandbox_runner.load_workflow_state(workflow_id)
    assert run_container.call_count == 2
    assert post_jira_adf_comment.call_count == 2
    running_text = _joined_text(post_jira_adf_comment.call_args_list[0].args[1])
    assert "Milestone: running" in running_text
    failed_text = _joined_text(post_jira_adf_comment.call_args_list[-1].args[1])
    assert "Milestone: failed" in failed_text
    assert f"Workflow ID: {workflow_id}" in failed_text
    assert post_jira_comment.call_count == 0
    assert state["status"] == "failed"
    assert state["stages"]["jira_progress_running"]["status"] == "succeeded"
    assert state["stages"]["sandbox"]["status"] == "failed"
    assert state["stages"]["sandbox"]["attempts"] == 2
    assert state["stages"]["failure_writeback"]["status"] == "succeeded"


def test_run_and_writeback_resumes_after_completed_sandbox_stage(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    workflow_id = docker_sandbox_runner.build_workflow_id(
        validated.issue_key,
        validated.comment_id,
        validated.request_text,
    )
    saved_result = {
        "status": "succeeded",
        "issue_key": "SCRUM-5",
        "summary": "saved",
        "workflow_id": workflow_id,
    }
    docker_sandbox_runner.save_workflow_state(
        workflow_id,
        {
            "status": "running",
            "stages": {
                "sandbox": {
                    "status": "succeeded",
                    "attempts": 1,
                    "result": saved_result,
                }
            },
            "final_result": saved_result,
        },
    )
    run_container = Mock()
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "has_existing_workflow_comment",
        Mock(return_value=False),
    )

    result = docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__2")

    run_container.assert_not_called()
    post_jira_adf_comment.assert_called_once()
    assert result == saved_result
    state = docker_sandbox_runner.load_workflow_state(workflow_id)
    assert state["status"] == "succeeded"
    assert state["stages"]["jira_writeback"]["status"] == "succeeded"


def test_run_and_writeback_returns_terminal_saved_result_without_reposting(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    validated = docker_sandbox_runner.validate_jira_command_payload(grouped_payload())
    workflow_id = docker_sandbox_runner.build_workflow_id(
        validated.issue_key,
        validated.comment_id,
        validated.request_text,
    )
    saved_result = {
        "status": "succeeded",
        "issue_key": "SCRUM-5",
        "summary": "already done",
        "workflow_id": workflow_id,
    }
    docker_sandbox_runner.save_workflow_state(
        workflow_id,
        {
            "status": "succeeded",
            "final_result": saved_result,
            "stages": {
                "sandbox": {"status": "succeeded", "attempts": 1, "result": saved_result},
                "jira_writeback": {"status": "succeeded", "attempts": 1},
            },
        },
    )
    run_container = Mock()
    post_jira_comment = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)

    result = docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__2")

    assert result == saved_result
    run_container.assert_not_called()
    post_jira_comment.assert_not_called()


def test_post_result_comment_once_skips_existing_workflow_comment(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    workflow_id = "SCRUM-5_2_abc123"
    monkeypatch.setattr(
        docker_sandbox_runner,
        "fetch_existing_jira_comments",
        Mock(
            return_value=[
                {
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "[quant-loop-bot]\n"
                                            f"Workflow ID: {workflow_id}"
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                }
            ]
        ),
    )
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )

    docker_sandbox_runner.post_result_comment_once(
        "SCRUM-5",
        {"status": "succeeded", "workflow_id": workflow_id},
        workflow_id,
    )

    post_jira_adf_comment.assert_not_called()


def test_post_result_comment_once_posts_when_workflow_comment_is_missing(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    workflow_id = "SCRUM-5_2_abc123"
    monkeypatch.setattr(
        docker_sandbox_runner,
        "fetch_existing_jira_comments",
        Mock(return_value=[]),
    )
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )

    docker_sandbox_runner.post_result_comment_once(
        "SCRUM-5",
        {"status": "succeeded", "workflow_id": workflow_id},
        workflow_id,
    )

    post_jira_adf_comment.assert_called_once()
    posted_text = _joined_text(post_jira_adf_comment.call_args.args[1])
    assert f"Workflow ID: {workflow_id}" in posted_text


def test_post_result_comment_once_ignores_existing_partial_progress_comment(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    workflow_id = "SCRUM-5_2_abc123"
    monkeypatch.setattr(
        docker_sandbox_runner,
        "fetch_existing_jira_comments",
        Mock(
            return_value=[
                {
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "[quant-loop-bot]\n"
                                            f"Workflow ID: {workflow_id}\n"
                                            "Final Status: not available"
                                        ),
                                    }
                                ],
                            }
                        ],
                    }
                }
            ]
        ),
    )
    post_jira_adf_comment = Mock()
    monkeypatch.setattr(
        docker_sandbox_runner,
        "post_jira_adf_comment",
        post_jira_adf_comment,
    )

    docker_sandbox_runner.post_result_comment_once(
        "SCRUM-5",
        {"status": "succeeded", "workflow_id": workflow_id},
        workflow_id,
    )

    post_jira_adf_comment.assert_called_once()


def test_run_and_writeback_handles_validation_failure_without_container(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    payload = grouped_payload()
    # An unsupported option is a clean validation failure now that dates are optional.
    payload["request"]["comments"][1]["text"] = "/quant Latest request badoption=xyz"
    post_jira_comment = Mock()
    run_container = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)

    result = docker_sandbox_runner.run_and_writeback(payload, "manual__1")

    assert result["status"] == "validation_failed"
    assert result["issue_key"] == "SCRUM-5"
    assert "Unsupported /quant option" in "\n".join(result["validation_errors"])
    run_container.assert_not_called()
    post_jira_comment.assert_called_once()
    assert post_jira_comment.call_args.args[0] == "SCRUM-5"
    posted_comment = post_jira_comment.call_args.args[1]
    assert "Status: validation_failed" in posted_comment
    assert "Unsupported /quant option" in posted_comment
    assert "Optional date overrides" in posted_comment
    assert "/quant Backtest the strategy strategy_type=backtest" in posted_comment
    assert "Required command shape" not in posted_comment
    assert "manual__1" in posted_comment


def test_run_and_writeback_handles_user_github_validation_failure_without_container(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    post_jira_comment = Mock()
    run_container = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "post_jira_comment", post_jira_comment)
    monkeypatch.setattr(docker_sandbox_runner, "run_container", run_container)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_sandbox_github_credentials",
        Mock(side_effect=ValueError("No GitHub credential mapping configured for Jira user")),
    )

    result = docker_sandbox_runner.run_and_writeback(grouped_payload(), "manual__1")

    assert result["status"] == "validation_failed"
    assert result["issue_key"] == "SCRUM-5"
    assert result["validation_errors"] == [
        "No GitHub credential mapping configured for Jira user"
    ]
    run_container.assert_not_called()
    post_jira_comment.assert_called_once()
    posted_comment = post_jira_comment.call_args.args[1]
    assert "No GitHub credential mapping configured for Jira user" in posted_comment
    assert "github-secret" not in posted_comment


def test_validate_jira_dataset_attachment_builds_input_manifest(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {
            "id": "20001",
            "filename": "experiment_2_input.csv",
            "size": 31,
            "mime_type": "text/csv",
            "created": "2026-07-20T09:00:00.000+0000",
        }
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse the attached Experiment 2 data "
        "strategy_type=analysis zero_code_modifications=true "
        "input_attachment=experiment_2_input.csv"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.dataset_attachment["id"] == "20001"
    dataset = validated.runtime_request["input_datasets"][0]
    assert dataset == {
        "dataset_id": "experiment_2_input",
        "source_kind": "jira_attachment",
        "original_filename": "experiment_2_input.csv",
        "source_uri": "jira-attachment://20001",
        "container_path": "/workspace/input/datasets/experiment_2_input.csv",
        "format": "csv",
        "read_only": True,
        "jira_attachment_id": "20001",
        "jira_mime_type": "text/csv",
        "jira_created": "2026-07-20T09:00:00.000+0000",
        "size_bytes": 31,
    }


def test_validate_jira_dataset_attachment_uses_latest_duplicate_id(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "returns.csv", "size": 10},
        {"id": "20009", "filename": "returns.csv", "size": 12},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse returns strategy_type=analysis "
        "input_attachment=returns.csv"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.dataset_attachment["id"] == "20009"
    assert (
        validated.runtime_request["input_datasets"][0]["source_uri"]
        == "jira-attachment://20009"
    )


def test_validate_jira_dataset_attachment_requires_explicit_selection(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "equity_curve.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse the attached dataset strategy_type=analysis"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.dataset_attachment is None
    assert "input_datasets" not in validated.runtime_request


def test_validate_hdfs_dataset_does_not_auto_select_jira_attachment(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "equity_curve.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse the Experiment 2 dataset strategy_type=analysis "
        "input_hdfs_uri=hdfs:///approved/experiment_2_input.csv"
    )

    validated = docker_sandbox_runner.validate_jira_command_payload(payload)

    assert validated.dataset_attachment is None
    dataset = validated.runtime_request["input_datasets"][0]
    assert dataset["source_kind"] == "hdfs"
    assert dataset["source_uri"] == "hdfs:///approved/experiment_2_input.csv"


def test_validate_jira_dataset_attachment_rejects_spaces_with_clear_error(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "experiment 2 input.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        '/quant Analyse returns strategy_type=analysis '
        'input_attachment="experiment 2 input.csv"'
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "without spaces or path components" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_dataset_attachment_rejects_inline_option_without_value(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "experiment_2_input.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse the attached dataset strategy_type=analysis "
        "input_attachment: experiment_2_input.csv"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "input_attachment requires a filename" in "\n".join(
        exc_info.value.errors
    )


def test_validate_jira_dataset_attachment_accepts_own_line_option(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "experiment_2_input.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant\n"
        "Analyse the attached dataset\n"
        "strategy_type: analysis\n"
        "input_attachment: experiment_2_input.csv"
    )

    dataset = docker_sandbox_runner.validate_jira_command_payload(
        payload
    ).runtime_request["input_datasets"][0]

    assert dataset["source_kind"] == "jira_attachment"
    assert dataset["source_uri"] == "jira-attachment://20001"


def test_validate_jira_dataset_attachment_rejects_hdfs_and_attachment_together(
    docker_sandbox_runner,
) -> None:
    payload = grouped_payload()
    payload["request"]["attachments"] = [
        {"id": "20001", "filename": "returns.csv", "size": 10},
    ]
    payload["request"]["comments"][1]["text"] = (
        "/quant Analyse returns strategy_type=analysis "
        "input_attachment=returns.csv "
        "input_hdfs_uri=hdfs:///approved/returns.csv"
    )

    with pytest.raises(docker_sandbox_runner.JiraCommandValidationError) as exc_info:
        docker_sandbox_runner.validate_jira_command_payload(payload)

    assert "Use either input_hdfs_uri or input_attachment" in "\n".join(
        exc_info.value.errors
    )


def test_jira_only_dataset_materialization_does_not_resolve_hdfs_config(
    docker_sandbox_runner,
    monkeypatch,
) -> None:
    runtime_request = {
        "input_datasets": [
            {
                "source_kind": "jira_attachment",
                "source_uri": "jira-attachment://20001",
            }
        ]
    }
    size_config = {"max_file_bytes": 1024, "max_total_bytes": 2048}
    resolve_size = Mock(return_value=size_config)
    resolve_hdfs = Mock(side_effect=AssertionError("HDFS config must not be used"))
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_input_dataset_size_config",
        resolve_size,
    )
    monkeypatch.setattr(
        docker_sandbox_runner,
        "resolve_hdfs_input_config",
        resolve_hdfs,
    )

    config = docker_sandbox_runner.resolve_input_dataset_materialization_config(
        runtime_request
    )

    assert config == size_config
    resolve_size.assert_called_once_with()
    resolve_hdfs.assert_not_called()


def test_materialize_jira_dataset_attachment_downloads_and_verifies(
    docker_sandbox_runner,
    monkeypatch,
    tmp_path,
) -> None:
    content = b"date,return\n2024-01-31,0.01\n"

    class FakeResponse:
        headers = {"Content-Length": str(len(content))}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield content[:10]
            yield content[10:]

        def close(self):
            self.closed = True

    jira_request = Mock(return_value=FakeResponse())
    run_hadoop_command = Mock()
    monkeypatch.setattr(docker_sandbox_runner, "jira_request", jira_request)
    monkeypatch.setattr(
        docker_sandbox_runner,
        "run_hadoop_command",
        run_hadoop_command,
    )

    runtime_request = {
        "input_datasets": [
            {
                "dataset_id": "returns",
                "source_kind": "jira_attachment",
                "original_filename": "returns.csv",
                "source_uri": "jira-attachment://20001",
                "jira_attachment_id": "20001",
                "container_path": "/workspace/input/datasets/returns.csv",
                "format": "csv",
                "read_only": True,
                "size_bytes": len(content),
            }
        ]
    }
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    config = {
        "hadoop_home": "/opt/hadoop",
        "hdfs_namenode_uri": "hdfs://namenode:8020",
        "allowed_roots": [],
        "max_file_bytes": 1024,
        "max_total_bytes": 2048,
    }

    updated = docker_sandbox_runner.materialize_hdfs_input_datasets(
        runtime_request,
        input_dir,
        config,
    )

    local_file = input_dir / "datasets" / "returns.csv"
    assert local_file.read_bytes() == content
    dataset = updated["input_datasets"][0]
    assert dataset["size_bytes"] == len(content)
    assert dataset["sha256"] == docker_sandbox_runner.sha256_file(local_file)
    assert dataset["source_uri"] == "jira-attachment://20001"
    assert run_hadoop_command.call_count == 0
    jira_request.assert_called_once_with(
        "GET",
        "/rest/api/3/attachment/content/20001",
        headers={"Accept": "*/*"},
        stream=True,
        allow_redirects=True,
        timeout=60,
    )


def _request_attachment_validated(size: int | None) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        request_attachment={
            "id": "11417",
            "filename": "backtest.request",
            "size": size,
        },
        runtime_request={
            "input_paths": {
                "request_template_path": (
                    "/workspace/input/attachments/SCRUM-104/backtest.request"
                ),
            }
        },
    )


def test_stage_jira_request_attachment_downloads_and_verifies(
    docker_sandbox_runner, monkeypatch, tmp_path
) -> None:
    content = b"strategy: momentum\nlookback: 20\n"

    class FakeResponse:
        headers = {"Content-Length": str(len(content))}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield content[:8]
            yield content[8:]

        def close(self):
            self.closed = True

    jira_request = Mock(return_value=FakeResponse())
    monkeypatch.setattr(docker_sandbox_runner, "jira_request", jira_request)

    input_dir = tmp_path / "input"
    input_dir.mkdir()

    destination = docker_sandbox_runner.stage_jira_request_attachment(
        _request_attachment_validated(len(content)),
        input_dir,
    )

    assert destination == input_dir / "attachments" / "SCRUM-104" / "backtest.request"
    assert destination.read_bytes() == content
    jira_request.assert_called_once_with(
        "GET",
        "/rest/api/3/attachment/content/11417",
        headers={"Accept": "*/*"},
        stream=True,
        allow_redirects=True,
        timeout=60,
    )


def test_stage_jira_request_attachment_rejects_size_mismatch(
    docker_sandbox_runner, monkeypatch, tmp_path
) -> None:
    content = b"strategy: momentum\n"

    class FakeResponse:
        headers: dict[str, str] = {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield content

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        docker_sandbox_runner,
        "jira_request",
        Mock(return_value=FakeResponse()),
    )

    input_dir = tmp_path / "input"
    input_dir.mkdir()

    with pytest.raises(ValueError, match="size does not match Jira metadata"):
        docker_sandbox_runner.stage_jira_request_attachment(
            _request_attachment_validated(len(content) + 5),
            input_dir,
        )

    assert not (input_dir / "attachments" / "SCRUM-104" / "backtest.request").exists()
