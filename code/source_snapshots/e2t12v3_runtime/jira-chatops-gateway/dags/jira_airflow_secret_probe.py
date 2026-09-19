"""Manual DAG for checking Airflow Connection and Variable wiring."""

from __future__ import annotations

import os
from datetime import datetime

try:
    from airflow.sdk import BaseHook, DAG, Variable, task
except ImportError:
    from airflow.hooks.base import BaseHook
    from airflow.sdk import DAG, Variable, task


CONNECTION_IDS = [
    "jira_cloud",
    "litellm_default",
    "github_default",
]

CONFIG_KEYS = [
    "JIRA_PROJECT_KEY",
    "JIRA_CONNECTION_ID",
    "JIRA_RAG_INDEX_PATH",
    "JIRA_RAG_INDEX_SHA256",
    "LITELLM_CONNECTION_ID",
    "LITELLM_BASE_URL",
    "LITELLM_MODEL",
    "GITHUB_CONNECTION_ID",
    "QUANT_GITHUB_USER_ACCESS_MATRIX",
    "DAG_SYNC_GITHUB_CONNECTION_ID",
    "GHCR_CONNECTION_ID",
    "SANDBOX_IMAGE",
    "DOCKER_URL",
    "SANDBOX_CONTAINER_TIMEOUT_SECONDS",
    "SANDBOX_EXECUTION_MODE",
    "USE_REAL_BACKTESTER",
    "BACKTEST_STORAGE_KIND",
    "SIMULATION_REQUESTS_DIR",
    "SIMULATION_RESULTS_DIR",
    "BACKTEST_RESULT_TIMEOUT_SECONDS",
    "WORKFLOW_ENABLE_DOCKER_FALLBACK",
    "WORKFLOW_TASK_TIMEOUT_SECONDS",
    "WORKFLOW_RECOVERY_STATE_DIR",
    "WORKFLOW_RETRY_MAX_ATTEMPTS",
    "WORKFLOW_RETRY_INITIAL_DELAY_SECONDS",
    "WORKFLOW_RETRY_MAX_DELAY_SECONDS",
    "WORKFLOW_RETRY_BACKOFF_MULTIPLIER",
    "WORKFLOW_RETRY_JITTER_RATIO",
    "SANDBOX_HDFS_NAMENODE_URI",
    "SANDBOX_HDFS_RUN_ROOT",
    "SANDBOX_YARN_QUEUE",
    "SANDBOX_YARN_MASTER_MEMORY_MB",
    "SANDBOX_YARN_CONTAINER_MEMORY_MB",
    "SANDBOX_YARN_TIMEOUT_SECONDS",
    "SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS",
    "SANDBOX_YARN_CLEANUP_HDFS",
    "SANDBOX_INPUT_HDFS_ALLOWED_ROOTS",
    "SANDBOX_INPUT_MAX_FILE_BYTES",
    "SANDBOX_INPUT_MAX_TOTAL_BYTES",
    "HADOOP_HOME",
    "HADOOP_CONF_DIR",
]


def mask_value(value: str | None) -> str:
    if value is None:
        return "<missing>"

    if value == "":
        return "<empty>"

    if len(value) <= 8:
        return f"<set length={len(value)}>"

    return f"{value[:4]}...{value[-4:]} length={len(value)}"


def get_variable(name: str) -> str | None:
    return Variable.get(name, None)


def print_connection_probe(conn_id: str) -> None:
    print(f"{conn_id}:")
    try:
        conn = BaseHook.get_connection(conn_id)
    except Exception as exc:
        print(f"  Connection: <missing> ({type(exc).__name__})")
        return

    print(f"  Host:     {mask_value(conn.host)}")
    print(f"  Login:    {mask_value(conn.login)}")
    print(f"  Password: {mask_value(conn.password)}")
    print(f"  Extra:    {'<set>' if conn.extra else '<missing>'}")


@task(task_id="print_secret_probe")
def print_secret_probe() -> None:
    print("Airflow connection/config probe")
    print("Expected credential Connections:")
    print("- jira_cloud")
    print("- litellm_default")
    print("- github_default for global GitHub operations and explicitly mapped users")
    print("- per-user GitHub PAT Connections named by QUANT_GITHUB_USER_ACCESS_MATRIX")
    print("")

    for conn_id in CONNECTION_IDS:
        print_connection_probe(conn_id)

    print("")
    print("Expected non-sensitive Variables:")
    for name in CONFIG_KEYS:
        variable_value = get_variable(name)
        env_value = os.environ.get(name)

        print(f"{name}:")
        print(f"  Airflow Variable: {mask_value(variable_value)}")
        print(f"  Worker env var:   {mask_value(env_value)}")
        print(f"  Effective value:  {mask_value(variable_value or env_value)}")


with DAG(
    dag_id="jira_airflow_secret_probe",
    description="Manually print masked Airflow Variable and env secret status",
    start_date=datetime(2026, 6, 1),
    schedule=None,
    catchup=False,
    tags=["jira", "debug", "secrets"],
) as dag:
    print_secret_probe()
