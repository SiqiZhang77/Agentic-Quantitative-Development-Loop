# BSLAgenticQuantDevLoop

Automated agentic workflow for turning Jira ChatOps requests into isolated,
LLM-assisted quantitative strategy backtests.

The repository is split into two main pieces:

- `jira-chatops-gateway/`: Airflow DAGs and Jira integration code that poll Jira
  comments, group requests by ticket, launch sandbox runs, and post final Jira
  ADF comments.
- `rae_runtime/`: the hardened runtime sandbox, MCP backtest shim, and proxy
  code used by the quant execution loop.

## Workflow

```text
Jira ticket comment
-> Airflow Jira polling/orchestration DAG
-> docker_sandbox_runner DAG
-> hardened RAE Docker container
-> optional GitHub branch/edit/commit
-> evaluate: MCP backtest request (strategy_type=backtest)
             or review agent      (every other strategy_type)
-> repeat while the evaluation says "iterate" (bounded; see allow_iteration)
-> structured runtime result JSON
-> Jira ADF result comment
```

`strategy_type` selects the workflow. Only `backtest` reaches the engine; the
other types are general requests that edit and commit code without a backtest.

## Jira Command Examples

Example Jira comments that trigger the workflow:

```text
/quant Backtest the momentum strategy strategy_type=backtest start_date=2024-01-01 end_date=2024-12-31 repo=bankingscience/BSLAgenticQuantDevLoop
```

A non-backtest request — `strategy_type` selects the workflow and `resource_path`
names the initial context. `target_path` optionally names a different required
deliverable and otherwise defaults to `resource_path`:

```text
/quant Tidy up the retry handling in the GitHub client strategy_type=refactor resource_path=rae_runtime/proxy/github_client.py
```

Documentation and other miscellaneous repository edits use `strategy_type: other`:

```text
/quant
Create a root README using the existing resource notes and verified repository files
strategy_type: other
repo: ATPDataHandlersRepo
resource_path: atp-handlers/src/main/resources/readme
target_path: README.md
allowed_directories: .
```

When no repository is specified, the workflow defaults to
`bankingscience/BSLAgenticQuantDevLoop`.

Explicit selection accepts the catalog name, canonical full name, or GitHub URL
(case-insensitive after trimming whitespace):

```text
/quant
Refactor the connector retry policy
strategy_type: refactor
resource_path: src/connectors/retry.py
repo: ATPConnectorsRepo
branch: quant/SCRUM-123
```

`BSLAgenticQuantDevLoop` defaults to source branch `main`; all ATP repositories
default to `develop`. A branch map overrides those defaults, while every
repository uses the same safe ticket branch by default:

```text
/quant
Run coordinated repository checks
strategy_type: analysis
read_only: true
branch_map: BSLAgenticQuantDevLoop=main, ATPConnectorsRepo=feature/connectors, ATPDataHandlersRepo=develop
```

The exact names are `BSLAgenticQuantDevLoop`, `ATPConnectorsRepo`,
`ATPDataHandlersRepo`, `ATPSiftingAnalyticsRepo`, and
`ATPSiftingPreTradeRepo`. Explicit targets must be `quant/<ticket>` or a
descendant; the workflow never writes directly to `main` or `develop`.

`strategy_type` is required and is never inferred from the request text. Only
`backtest` runs the engine; `ingestion`, `refactor`, `analysis`, and `other` are
general requests that edit/commit code without a backtest. Omitting it, or using
an unknown value, is a validation error posted back to the ticket. See
[`jira-chatops-gateway/README.md`](jira-chatops-gateway/README.md) for the full
table.

Available models for the `model` option:
`nova-micro`, `nova-pro`, `test-model`, `gpt-oss`, `qwen3-coder`

The `params` option is passed through as a raw engine parameter string. The
gateway does not validate the individual `KEY=value` entries.

The current Airflow orchestration path is:

- `jira-chatops-gateway/dags/jira_quant_docker_orchestrator.py` polls Jira and
  triggers one `docker_sandbox_runner` run per grouped ticket request.
- `jira-chatops-gateway/dags/docker_sandbox_runner.py` runs the sandbox
  container, parses its final JSON output, formats the result, and writes back
  to Jira.

`jira-chatops-gateway/dags/jira_comment_poller.py` is retained as the earlier
standalone polling DAG and reference implementation.

## Repository Layout

```text
.
+-- jira-chatops-gateway/
|   +-- dags/                  # Airflow DAGs and shared DAG utilities
|   |   +-- adf_utils/         # Jira ADF result formatting used by DAGs
|   +-- jira_client/           # Jira Cloud comment write-back client
|   +-- schemas/               # Runtime request/response JSON schemas
|   +-- examples/              # Example runtime payloads
|   +-- docs/                  # Runtime payload contract
+-- rae_runtime/
    +-- sandbox/               # Hardened run-once Docker container entrypoint
    +-- mcp/                   # Local MCP server and mock backtester
    +-- proxy/                 # GitHub, LiteLLM, strategy, and budget helpers
    +-- docker-compose.yml     # Local sandbox compose entrypoint
```

## Runtime Contract

The handoff between Airflow and the RAE container is documented in
`jira-chatops-gateway/docs/runtime_payload_contract.md`.

Important contract files:

- `jira-chatops-gateway/schemas/runtime_request.schema.json`
- `jira-chatops-gateway/schemas/runtime_response.schema.json`
- `jira-chatops-gateway/examples/runtime_request_example.json`
- `jira-chatops-gateway/examples/runtime_response_success_example.json`
- `jira-chatops-gateway/examples/runtime_response_failure_example.json`

The sandbox writes one final structured JSON result to the request's
`output_paths.result_path`, normally `/workspace/output/result.json`. Airflow
reads that mounted result file first and falls back to the final stdout JSON
line only when the file is not readable. Optional response `telemetry` is for
debugging, audit, and monitoring; detailed failure reasons belong in
`diagnostics`.

## Airflow Configuration

Detailed Airflow UI setup is documented in
[`jira-chatops-gateway/docs/airflow_ui_configuration.md`](jira-chatops-gateway/docs/airflow_ui_configuration.md).

Production secrets should be stored as Airflow Connections:

```text
jira_cloud          Jira Cloud host/login/API token
litellm_default     LiteLLM token in the password field
github_default      Optional GitHub username/token for code-writing runs
```

Non-sensitive runtime settings should be stored as Airflow Variables:

```text
JIRA_PROJECT_KEY        Jira project key to poll
JIRA_CONNECTION_ID      Optional; defaults to jira_cloud
LITELLM_CONNECTION_ID   Optional; defaults to litellm_default
LITELLM_BASE_URL        Optional; fallback when the connection has no host/base_url
LITELLM_MODEL           Optional; defaults to nova-micro
GITHUB_CONNECTION_ID    Optional; leave unset for code-free runs
GHCR_CONNECTION_ID      Optional; separate GHCR image pull credentials
SANDBOX_IMAGE           Optional; sandbox image to run
DOCKER_URL              Optional; remote Docker daemon URL
SANDBOX_EXECUTION_MODE  Optional; defaults to docker, set to yarn for YARN
USE_REAL_BACKTESTER     Optional; set true for the real watched engine
BACKTEST_STORAGE_KIND   Optional; set hdfs for the Bialobog YARN bridge
SIMULATION_REQUESTS_DIR Required in real mode; Bialobog uses hdfs:///quant-sandbox/simulation-requests
SIMULATION_RESULTS_DIR  Required in real mode; Bialobog uses hdfs:///quant-sandbox/simulation-results
WORKFLOW_ENABLE_DOCKER_FALLBACK  Optional; defaults to false
WORKFLOW_RECOVERY_STATE_DIR      Optional; defaults to /tmp/jira_quant_workflow_recovery
WORKFLOW_RETRY_MAX_ATTEMPTS      Optional; defaults to 3
WORKFLOW_RETRY_INITIAL_DELAY_SECONDS  Optional; defaults to 60
WORKFLOW_RETRY_MAX_DELAY_SECONDS      Optional; defaults to 300
WORKFLOW_RETRY_BACKOFF_MULTIPLIER     Optional; defaults to 2
WORKFLOW_RETRY_JITTER_RATIO           Optional; defaults to 0.2
SANDBOX_HDFS_NAMENODE_URI  Required for YARN; explicit HDFS URI
SANDBOX_HDFS_RUN_ROOT      Required for YARN; HDFS run directory root
SANDBOX_YARN_QUEUE         Required for YARN; queue name
SANDBOX_YARN_MASTER_MEMORY_MB     Optional for YARN; defaults to 512
SANDBOX_YARN_CONTAINER_MEMORY_MB  Optional for YARN; defaults to 4096
SANDBOX_YARN_TIMEOUT_SECONDS      Optional for YARN; defaults to 2100
SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS  Optional for YARN; defaults to 60
SANDBOX_YARN_CLEANUP_HDFS         Optional for YARN; defaults to false
HADOOP_HOME                       Optional for YARN; defaults to /opt/hadoop
HADOOP_CONF_DIR                   Optional for YARN; defaults to /opt/hadoop/etc/hadoop
```

Local development may use `.env` fallbacks where supported. Do not commit
populated `.env` files or credentials.

For manual cluster DAG sync and sandbox image promotion steps, see
[`jira-chatops-gateway/docs/local_cron_dag_sync.md`](jira-chatops-gateway/docs/local_cron_dag_sync.md).

## Local Development

Install and run the Jira/Airflow unit tests:

```bash
cd jira-chatops-gateway
python -m pip install -r requirements.txt
pytest
```

Run the RAE contract unit tests (no network/Docker needed):

​```bash
cd rae_runtime
PYTHONPATH=proxy:sandbox python3 -m pytest sandbox/tests/ -q
​```

Run the sandbox locally. Default input is a JSON request on **stdin**, validated against IW's runtime_request.schema.json:

​```bash
cd rae_runtime
RAE_OFFLINE=1 PYTHONPATH=proxy:sandbox python3 sandbox/run.py \
  < sandbox/tests/fixtures/runtime_request_example.json
​```

** The contracts in `rae_runtime/sandbox/schemas/` MUST be in sync with those in `jira-chatops-gateway/schemas/`.

Run the sandbox locally from `rae_runtime` with mocked LLM behavior and a real
GitHub token:

```bash
cd rae_runtime
export GITHUB_TOKEN="github_pat_..."
export API_KEY="dummy"
export MOCK_RUNTIME_DIR=/tmp/mock_runtime
export BACKTEST_REQUESTS_DIR=/tmp/mock_runtime/backtest-requests
export MCP_SERVER_PATH=mcp/server.py

rm -rf /tmp/mock_runtime && mkdir -p /tmp/rae/output

PYTHONPATH=proxy:sandbox python3 sandbox/run.py \
  < sandbox/tests/fixtures/runtime_request_example.json
```

Build and run the hardened sandbox image:

```bash
cd rae_runtime
docker build -f sandbox/Dockerfile -t rae-local .

mkdir -p /tmp/rae/output && chmod 777 /tmp/rae/output

docker run -i --rm \
  --user 1000:1000 --read-only \
  --tmpfs /tmp --tmpfs /workspace:uid=1000,gid=1000,mode=0750 \
  -v /tmp/rae/output:/workspace/output \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --memory 512m \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" -e API_KEY="dummy" \
  rae-local < sandbox/tests/fixtures/runtime_request_example.json

cat /tmp/rae/output/result.json
```

For an offline smoke test with no network or real secrets:

```bash
docker run -i --rm --network none \
  --user 1000:1000 --read-only \
  --tmpfs /tmp --tmpfs /workspace:uid=1000,gid=1000,mode=0750 \
  -v /tmp/rae/output:/workspace/output \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --memory 512m \
  -e RAE_OFFLINE=1 \
  rae-local < sandbox/tests/fixtures/runtime_request_example.json
``` 

## Documentation

- `jira-chatops-gateway/README.md`: Jira polling, Airflow ownership, ADF
  write-back, and deployment notes.
- `jira-chatops-gateway/docs/runtime_payload_contract.md`: Team IW/RAE runtime
  request and response contract.
- `rae_runtime/sandbox/README.md`: sandbox inputs, outputs, hardening,
  DockerOperator settings, and dependency upgrade process.
