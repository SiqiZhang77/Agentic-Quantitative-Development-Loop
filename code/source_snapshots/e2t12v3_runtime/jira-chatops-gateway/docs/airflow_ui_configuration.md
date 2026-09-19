# Airflow UI Configuration

This document lists the Airflow Connections and Variables needed by the Jira
ChatOps and quant sandbox DAGs.

Use Airflow Connections for secrets. Use Airflow Variables only for
non-sensitive runtime configuration.

## Connections

Create these from **Admin > Connections** in the Airflow UI.

| Connection ID | Required | Purpose | Fields |
| --- | --- | --- | --- |
| `jira_cloud` | Yes, unless `JIRA_CONNECTION_ID` points elsewhere | Jira Cloud API access for polling, validation, and Jira write-back | `Host`: Jira API base URL; for scoped tokens use `https://api.atlassian.com/ex/jira/{cloudId}`; `Login`: Jira service-account email; `Password`: Jira API token |
| `litellm_default` | Yes, unless `LITELLM_CONNECTION_ID` points elsewhere | LiteLLM/OpenAI-compatible API access for the sandbox agent | `Host`: LiteLLM base URL; `Password`: API key; optional `Extra`: `{"model": "nova-micro"}` |
| `github_default` | Optional | Global GitHub credentials for general DAG GitHub operations, DAG sync, package access fallback, and explicitly mapped user-scoped workflows | `Login`: GitHub username; `Password`: GitHub token |
| per-user GitHub PAT connections | Required for user-scoped `/quant` repository editing/backtesting | One connection per Jira user that can run repo-scoped workflows | `Login`: GitHub username; `Password`: that user's GitHub PAT |
| custom GHCR connection | Optional | Pull private sandbox images from GitHub Container Registry when GHCR credentials differ from GitHub credentials | `Login`: GHCR username; `Password`: GHCR token; set `GHCR_CONNECTION_ID` to this connection ID |

Connection notes:

- Jira requires the `apache-airflow-providers-http` provider when running
  through the Airflow Jira connection path.
- Scoped Jira API tokens must use Atlassian's
  `https://api.atlassian.com/ex/jira/{cloudId}` gateway rather than the
  site-specific `https://example.atlassian.net` URL. Resolve the Cloud ID from
  `https://example.atlassian.net/_edge/tenant_info`.
- The account stored in the `jira_cloud` Login field is the visible author of
  polling-triggered Jira write-back, including `[quant-loop-bot]` comments and
  uploaded attachments.
- LiteLLM may also read `api_key`, `token`, `base_url`, and `model` from
  connection `Extra`, but prefer `Password` for the key and `Host` for the base
  URL.
- GitHub and GHCR tokens must be stored in the connection `Password` field.
- Do not put tokens in Airflow Variables.

## Required Variables

Create these from **Admin > Variables** in the Airflow UI.

| Variable | Required When | Example | Notes |
| --- | --- | --- | --- |
| `JIRA_PROJECT_KEY` | Always | `SCRUM` | Jira project key to poll and process. |
| `JIRA_RAG_INDEX_PATH` | A `/quant` request uses `rag_enabled=true` | `/srv/quant-rag/e2-jira-index-v1.json` | Absolute path to the approved frozen Jira-memory index, mounted read-only and identically on every runner worker. C0 does not read this variable. |
| `JIRA_RAG_INDEX_SHA256` | A `/quant` request uses `rag_enabled=true` | `64 lowercase hex characters` | Independently recorded SHA-256 of the exact frozen index file. A mismatch fails C1 closed rather than silently falling back to C0. |
| `SANDBOX_YARN_QUEUE` | `SANDBOX_EXECUTION_MODE=yarn` | `default` | YARN queue used by the DistributedShell submission. |
| `SANDBOX_HDFS_NAMENODE_URI` | `SANDBOX_EXECUTION_MODE=yarn` | `hdfs://namenode:8020` | Explicit HDFS NameNode URI used by workers. |
| `SANDBOX_HDFS_RUN_ROOT` | `SANDBOX_EXECUTION_MODE=yarn` | `/user/airflow/quant-runs` | HDFS root where per-run request, output, debug, and env files are written. |
| `SANDBOX_INPUT_HDFS_ALLOWED_ROOTS` | Any `/quant` request uses `input_hdfs_uri` | `["hdfs:///user/masteruser/quant-experiment-data/"]` | JSON array or comma-separated allow-list. Input files outside these roots are rejected before execution. |

## Optional Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `POLL_INTERVAL_MINUTES` | `5` | Positive integer controlling both the `jira_quant_docker_orchestrator` schedule and its Jira comment lookback window. Set to `1` on Bialobog for one-minute polling. |
| `JIRA_CONNECTION_ID` | `jira_cloud` | Override the Jira connection ID. |
| `LITELLM_CONNECTION_ID` | `litellm_default` | Override the LiteLLM connection ID. |
| `LITELLM_BASE_URL` | none | Fallback LiteLLM base URL when the connection does not provide one. |
| `LITELLM_MODEL` | `nova-micro` | Default model. A validated Jira command can override this for a run. |
| `GITHUB_CONNECTION_ID` | unset | Global GitHub credential for non-user-specific DAG operations. Do not rely on this as a fallback for user-scoped `/quant` repo editing. |
| `QUANT_GITHUB_USER_ACCESS_MATRIX` | unset | Non-secret JSON matrix mapping Jira users to GitHub usernames, authorised repositories, and per-user GitHub Connection IDs. Required for user-scoped `/quant` repo editing/backtesting. |
| `DAG_SYNC_GITHUB_CONNECTION_ID` | `GITHUB_CONNECTION_ID`, then `github_default` | Optional override for the GitHub Connection used by `sync_airflow_dags_from_git.sh`. |
| `GHCR_CONNECTION_ID` | unset | Uses a separate GHCR pull credential. If unset, image pulls may fall back to the global GitHub credential, never the user-scoped sandbox PAT. |
| `SANDBOX_IMAGE` | `ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:stable` | Sandbox image run by Docker or by YARN workers. |
| `DOCKER_URL` | unset | Remote Docker daemon URL. When set, it becomes `DOCKER_HOST` for local Docker execution. |
| `SANDBOX_CONTAINER_TIMEOUT_SECONDS` | `1800` | Local Docker sandbox subprocess timeout cap. When unset, an explicit Jira `timeout_seconds` can raise the per-run cap up to the requested execution timeout; when set, this variable remains a hard cap. |
| `SANDBOX_EXECUTION_MODE` | `docker` | Set to `docker` or `yarn`. |
| `USE_REAL_BACKTESTER` | `false` | Set to `true` to make the sandbox submit to the real watched backtester directories instead of the local mock runtime. |
| `BACKTEST_STORAGE_KIND` | `shared_fs` | Real backtester storage type: `shared_fs`, or `hdfs` for YARN execution. Local Docker always uses `shared_fs`. |
| `SIMULATION_REQUESTS_DIR` | required when `USE_REAL_BACKTESTER=true` | Host/shared filesystem path for `shared_fs`, or an HDFS request URI when YARN uses `hdfs`. On Bialobog use `hdfs:///quant-sandbox/simulation-requests`. Local Docker falls back to `~/simulation-requests` when HDFS is configured. |
| `SIMULATION_RESULTS_DIR` | required when `USE_REAL_BACKTESTER=true` | Host/shared filesystem path for `shared_fs`, or an HDFS results URI when YARN uses `hdfs`. On Bialobog use `hdfs:///quant-sandbox/simulation-results`. Local Docker falls back to `~/simulation-results` when HDFS is configured. |
| `BACKTEST_RESULT_TIMEOUT_SECONDS` | unset | Optional timeout for sandbox polling of real backtester results. If unset, the runtime request timeout or runtime default is used. |
| `WORKFLOW_ENABLE_DOCKER_FALLBACK` | `false` | When `true`, selected YARN infrastructure failures can fall back to local Docker execution. |
| `WORKFLOW_TASK_TIMEOUT_SECONDS` | `11100` | Airflow task timeout for the sandbox-and-writeback stage. |
| `WORKFLOW_RECOVERY_STATE_DIR` | `/tmp/jira_quant_workflow_recovery` | Directory for workflow retry/idempotency state files. |
| `WORKFLOW_RETRY_MAX_ATTEMPTS` | `3` | Maximum Jira/progress/recovery retry attempts. |
| `WORKFLOW_RETRY_INITIAL_DELAY_SECONDS` | `60` | Initial retry delay. |
| `WORKFLOW_RETRY_MAX_DELAY_SECONDS` | `300` | Maximum retry delay. |
| `WORKFLOW_RETRY_BACKOFF_MULTIPLIER` | `2` | Retry delay multiplier. |
| `WORKFLOW_RETRY_JITTER_RATIO` | `0.2` | Random jitter ratio applied to retry delays. |
| `HADOOP_HOME` | `/opt/hadoop` | Hadoop installation path available to the Airflow worker. |
| `HADOOP_CONF_DIR` | `/opt/hadoop/etc/hadoop` | Hadoop config directory available to the Airflow worker. |
| `SANDBOX_YARN_MASTER_MEMORY_MB` | `512` | YARN DistributedShell application master memory. |
| `SANDBOX_YARN_CONTAINER_MEMORY_MB` | `4096` | YARN worker container memory. |
| `SANDBOX_YARN_TIMEOUT_SECONDS` | `2100` | YARN run timeout. |
| `SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS` | `60` | Retry window when HDFS reports safe mode. |
| `SANDBOX_YARN_CLEANUP_HDFS` | `false` | Remove temporary HDFS files after the run. |
| `SANDBOX_INPUT_MAX_FILE_BYTES` | `104857600` | Maximum size of one HDFS input file (100 MiB). |
| `SANDBOX_INPUT_MAX_TOTAL_BYTES` | `262144000` | Maximum combined HDFS input size per run (250 MiB). |

## Isolated Experiment 2 candidate Variables

The feature-branch-only `jira_exp2_si_runner` runs inside the `EXP2_SI_`
Variable namespace. A namespaced value overrides the corresponding shared value
only for that candidate task; missing non-critical settings deliberately fall
back to shared Connections and configuration. The candidate requires all seven
rows below and fails before Jira access if one is absent or unsafe.

| Variable | Required value |
| --- | --- |
| `EXP2_SI_ALLOWED_PROJECT_KEY` | `SCRUM` |
| `EXP2_SI_SANDBOX_EXECUTION_MODE` | `docker` |
| `EXP2_SI_SANDBOX_IMAGE` | GHCR image pinned with `@sha256:<digest>` |
| `EXP2_SI_JIRA_RAG_INDEX_PATH` | Absolute protected path to the frozen E2 index |
| `EXP2_SI_JIRA_RAG_INDEX_SHA256` | Approved 64-character lowercase index digest |
| `EXP2_SI_WORKFLOW_RECOVERY_STATE_DIR` | Absolute experiment-only state directory |
| `EXP2_SI_USE_REAL_BACKTESTER` | `false` for the coding-task C0/C1 experiment |

These settings do not replace or require edits to the unprefixed production
Variables. See `experiments/exp2/ISOLATED_DEPLOYMENT.md` for the full deployment
and paired-run procedure.

Real backtester request/result paths are persistent storage, not container
scratch space. YARN can use HDFS URIs when `BACKTEST_STORAGE_KIND=hdfs`. With
`shared_fs`, configure `~/simulation-requests` and `~/simulation-results` when
the watcher follows each cluster user's home directory; the worker expands `~`
from its own `$HOME`, validates the expanded host directories, and mounts them
into the sandbox at
`/workspace/backtester/simulation-requests` and
`/workspace/backtester/simulation-results`. Requests are staged atomically as
`<workflow_id>.request` for the existing cluster watcher. Results are polled as
`<workflow_id>/`, where the workflow id is the sanitized per-run/per-iteration
backtest job name. When local Docker is selected or used as the YARN fallback
while HDFS is configured, it instead mounts `~/simulation-requests` and
`~/simulation-results`; both directories must exist on the Docker host. Docker
`--rm` only removes the sandbox container, not those mounted directories.

RAG C1 runtime requests contain bounded retrieved Jira evidence and therefore
must use Docker execution. The gateway rejects `rag_enabled=true` with YARN
before retrieval, so raw memory cannot be staged below `SANDBOX_HDFS_RUN_ROOT`.
The 4,000-character per-memory and 12,000-character total prompt limits apply in
Docker, and only text-free memory IDs and hashes are returned in telemetry.

### Bialobog HDFS bridge

The simulation-service watcher only observes local directories under
`/home/masteruser`. In YARN mode, run
`scripts/hdfs_simulation_service_bridge.py` once per minute on Bialobog to copy
requests from HDFS into the local watcher inbox and publish terminal local
results back to HDFS.

HDFS URI slash count is significant:

- `hdfs://host/path` selects the named HDFS authority `host`.
- `hdfs:///path` selects `path` on the cluster configured by `fs.defaultFS`.

`quant-sandbox` is an HDFS directory, not a DNS hostname. The canonical values
for this deployment are:

```text
BACKTEST_STORAGE_KIND=hdfs
SIMULATION_REQUESTS_DIR=hdfs:///quant-sandbox/simulation-requests
SIMULATION_RESULTS_DIR=hdfs:///quant-sandbox/simulation-results
```

The bridge's non-secret cron environment is:

```text
HDFS_SIMULATION_REQUESTS_DIR=hdfs:///quant-sandbox/simulation-requests
HDFS_SIMULATION_RESULTS_DIR=hdfs:///quant-sandbox/simulation-results
LOCAL_SIMULATION_REQUESTS_DIR=/home/masteruser/simulation-requests
LOCAL_SIMULATION_RESULTS_DIR=/home/masteruser/simulation-results
HDFS_BRIDGE_STATE_DIR=/home/masteruser/.quant-hdfs-bridge
HDFS_BRIDGE_RETENTION_DAYS=1
HDFS_BRIDGE_MIN_FREE_GB=5
HDFS_BRIDGE_RESUME_FREE_GB=7.5
HDFS_BRIDGE_STUCK_SECONDS=10800
HDFS_BRIDGE_CLEANUP_ENABLED=true
HDFS_BRIDGE_HDFS_RETENTION_DAYS=30
HDFS_BRIDGE_HDFS_CLEANUP_MODE=report
```

Local cleanup applies only to bridge-published results whose terminal artifact
still matches HDFS. Results are normally retained locally for one day. If free
space falls below 5 GiB, the bridge removes the oldest verified local results
regardless of age until 7.5 GiB is free; intake remains paused if safe cleanup
cannot recover enough space.

Start HDFS retention in `report` mode. It reports only bridge-managed results
older than 30 days whose stored terminal size and checksum still match HDFS.
After reviewing those candidates, change the mode to `delete`. `disabled`
neither reports nor deletes candidates. Unmanaged and legacy results without
verification metadata are never deleted automatically.

Run the read-only deployment probe before enabling cron:

```bash
/opt/airflow3/venv/bin/python \
  /opt/airflow3/scripts/hdfs_simulation_service_bridge.py --check
```

Operational state is written under `/home/masteruser/.quant-hdfs-bridge`.
`heartbeat.json` reports health, free space, intake pause state, cleanup modes
and counters, per-state job counts, and the last sanitized error. New request
delivery pauses below 5 GiB free and resumes at 7.5 GiB; publication of existing
terminal results continues.

After the read-only check and canary pass, use this local-cleanup-enabled,
HDFS-report-only cron entry:

```cron
* * * * * HADOOP_HOME=/opt/hadoop-3.4.1 HADOOP_CONF_DIR=/opt/hadoop-3.4.1/etc/hadoop HDFS_SIMULATION_REQUESTS_DIR=hdfs:///quant-sandbox/simulation-requests HDFS_SIMULATION_RESULTS_DIR=hdfs:///quant-sandbox/simulation-results LOCAL_SIMULATION_REQUESTS_DIR=/home/masteruser/simulation-requests LOCAL_SIMULATION_RESULTS_DIR=/home/masteruser/simulation-results HDFS_BRIDGE_STATE_DIR=/home/masteruser/.quant-hdfs-bridge HDFS_BRIDGE_RETENTION_DAYS=1 HDFS_BRIDGE_MIN_FREE_GB=5 HDFS_BRIDGE_RESUME_FREE_GB=7.5 HDFS_BRIDGE_STUCK_SECONDS=10800 HDFS_BRIDGE_CLEANUP_ENABLED=true HDFS_BRIDGE_HDFS_RETENTION_DAYS=30 HDFS_BRIDGE_HDFS_CLEANUP_MODE=report /opt/airflow3/venv/bin/python /opt/airflow3/scripts/hdfs_simulation_service_bridge.py >> /opt/airflow3/logs/hdfs-simulation-service-bridge.cron.log 2>&1
```

Before replacing the existing bridge or crontab, save timestamped copies of
both. Preserve `/home/masteruser/.quant-hdfs-bridge` during deployment because
the hardened bridge reads the existing request markers. Roll back by restoring
the saved script and crontab and reverting the two `SIMULATION_*_DIR` Airflow
Variables. Do not change HDFS cleanup from `report` to `delete` until candidates
have been reviewed. A uniquely named canary must be consumed and its terminal
result verified in HDFS before local cleanup is enabled.

## Per-User GitHub Credentials

User-scoped `/quant` workflows that edit or backtest a repository must resolve a
GitHub PAT for the Jira requester before the sandbox starts. The sandbox
receives only that user's `GITHUB_USERNAME` and `GITHUB_TOKEN`. It does not fall
back to `github_default` unless that requester's matrix row explicitly names
that connection.

Store each PAT as a separate Airflow Connection. Store only non-secret mapping
metadata in `QUANT_GITHUB_USER_ACCESS_MATRIX`.

Example matrix shape:

```json
{
  "users": [
    {
      "full_name": "Example Maintainer",
      "jira_email": "maintainer@example.edu",
      "github_username": "example-maintainer",
      "github_connection_id": "github_pat_example_maintainer",
      "authorized_repositories": [
        "bankingscience/BSLAgenticQuantDevLoop",
        "bankingscience/ATPConnectorsRepo"
      ],
      "permission_level": "write"
    },
    {
      "full_name": "Example Student",
      "jira_email": "student@example.edu",
      "jira_account_id": "optional-jira-account-id",
      "jira_display_aliases": ["Student Shared Account"],
      "github_username": "student-gh",
      "github_connection_id": "github_pat_example_student",
      "authorized_repositories": [
        "bankingscience/BSLAgenticQuantDevLoop"
      ],
      "permission_level": "read"
    }
  ]
}
```

For user-scoped runs, `github_username` and `permission_level` are required.
`permission_level: write` permits editing, backtesting, and explicitly read-only
workflows. `permission_level: read` permits only commands that explicitly set
`read_only: true` or `zero_code_modifications: true`. Missing and unknown
permission values are rejected before launch.

Before Docker or YARN starts, the gateway uses the selected PAT to authenticate
`/user`, verifies that its login matches `github_username`, and checks access to
every selected repository and source branch. The runtime repeats identity
verification as defense in depth. A missing Connection password fails safely
with `GitHub token secret is not configured`.

The Bialobog requester configuration uses a classic GitHub PAT created under
**Settings > Developer settings > Personal access tokens > Tokens (classic)**,
named `QuantDevLoop`, with the expiry set for as long as possible. Select the
following scopes exactly: `repo`, `repo:status`, `repo_deployment`,
`public_repo`, `repo:invite`, `security_events`, `workflow`, `write:packages`,
and `read:packages`. The submitted token is stored in the cluster's encrypted
vault behind the requester's dedicated Airflow Connection and associated with
their Jira account. Do not store PAT values in this Variable, Jira, logs,
runtime payloads, or documentation.

The access matrix and gateway/runtime validation continue to enforce the
narrower repository, permission-level, branch, operation, and path scope.

The exact selectable repositories and default source branches are:

| Repository | Default source branch |
| --- | --- |
| `bankingscience/BSLAgenticQuantDevLoop` | `main` |
| `bankingscience/ATPConnectorsRepo` | `develop` |
| `bankingscience/ATPDataHandlersRepo` | `develop` |
| `bankingscience/ATPSiftingAnalyticsRepo` | `develop` |
| `bankingscience/ATPSiftingPreTradeRepo` | `develop` |

`branch_map` may override a source branch. Target branches default to
`quant/<Jira-ticket-id>` in every repository and may only use that branch or a
descendant. The workflow never writes directly to `main` or `develop`.

Multi-repository selection supports coordinated code edits only. The listed ATP
repositories are not ATRADE engine components, so multi-repository ATP
backtests are rejected before launch and repository branches are never mapped
to `atrade_sifting_*` engine properties.

To add a Jira user:

- Create or update an Airflow Connection whose password is that user's GitHub
  PAT.
- Add a matrix row with the Jira email when available. If Jira Cloud omits
  email addresses, configure `jira_account_id` or an explicit display alias.
- List every repository the user may target in `authorized_repositories`. A
  user authorised for a repository may read and edit every path in it.

### Finding `jira_account_id`

Jira Cloud may omit a comment author's email address, so an email-only matrix
row will not match that requester. To obtain the stable account ID, have the
user add a comment to a Jira issue, then read the comment through the Jira REST
API using the existing `jira_cloud` Connection. The required value is the
comment author's `author.accountId` from:

```text
GET /rest/api/3/issue/{issueKey}/comment
```

Add that value as `jira_account_id` in the user's access-matrix row. Do not
place Jira credentials or GitHub PATs in the Variable, Jira comments, logs, or
documentation.

Logs may include Jira identity, GitHub username, requested repositories,
connection ID, and token presence/length. Logs, Jira comments, runtime payloads,
and test fixtures must never include token values.

Complex repository-grounded edits may set `max_agent_turns` in the Jira command
to a bounded value between 10 and 60. This limits one agent pass only;
`max_iterations` controls successful edit/review passes and
`max_failed_iterations` controls retryable failed attempts. The overall timeout
and token budget are still enforced for the full workflow.

## Validation

Run the manual DAG `jira_airflow_secret_probe` after changing Airflow UI
configuration. It prints masked connection and variable status without exposing
secret values.

Expected baseline:

- `jira_cloud` and `litellm_default` exist and have host/login/password values
  appropriate to their use.
- `JIRA_PROJECT_KEY` is set.
- `github_default` is present when global DAG GitHub operations, explicitly
  mapped user-scoped execution, or private image fallback credentials are needed.
- `QUANT_GITHUB_USER_ACCESS_MATRIX` is set before user-scoped `/quant` repo
  editing/backtesting is enabled.
- YARN-only variables may be missing when `SANDBOX_EXECUTION_MODE` is `docker`.

## Local Development

Local development can use environment variables or
`jira-chatops-gateway/.env` where the DAG helpers support fallbacks. This is for
local runs only. Do not commit populated `.env` files or credentials.
