# Jira Comment Poller

This folder contains an Airflow DAG that polls Jira Cloud for recently updated
comments. It replaces the previous FastAPI webhook receiver and local webhook
server while retaining reusable ADF formatting and Jira write-back code for the
end of the downstream quant workflow.

## Why Polling Replaced Webhooks

The webhook implementation required a continuously running, publicly reachable
FastAPI service plus webhook authentication, routing, and deployment. The
polling model keeps scheduling, retries, logging, and operational ownership in
Airflow, where the downstream quant workflow already runs.

The trade-off is latency and duplicate handling. This DAG runs every 10 minutes
and looks back 30 minutes, so downstream triggering must be idempotent before it
is enabled. The overlap is intentional: it reduces the chance of missing a Jira
update when one polling run is delayed or fails.

## How The DAG Works

[`dags/jira_comment_poller.py`](dags/jira_comment_poller.py) performs the
following steps:

1. Runs every 10 minutes.
2. Searches for issues in `JIRA_PROJECT_KEY` updated during the last 30 minutes.
3. Requests each issue's summary, type, update timestamp, and comments.
4. Filters comments using each comment's `updated` timestamp.
5. Groups matching comments by Jira issue key.
6. Logs one proposed downstream DAG payload per issue.

The downstream trigger is intentionally commented out until the shared Airflow
deployment, authentication, and duplicate-processing strategy are agreed.

## End-To-End Workflow

The intended ownership is:

```text
Jira comments
-> jira_comment_poller DAG
-> downstream quant/LiteLLM DAG
-> Docker-based strategy and backtest loop
-> normalize the final result
-> build Jira ADF
-> post the ADF comment back to the originating Jira issue
```

The final two operations can run in the Docker orchestration step or in a
dedicated final Airflow task. A dedicated Airflow task is usually easier to
retry independently when Jira is temporarily unavailable, while keeping the
Docker image focused on the quant workload.

The reusable integration code remains here:

- `dags/adf_utils/builder.py` converts normalized workflow results to Jira ADF.
- `jira_client/client.py` posts an ADF document to a Jira issue without any
  FastAPI dependency.

The downstream team can import these modules directly if this repository is
available in its runtime, or package/copy them into the downstream image.

## Jira Search Endpoint

The DAG uses:

```text
POST /rest/api/3/search/jql
```

Jira Cloud's enhanced JQL search API accepts the JQL, requested fields, and
result limit in a JSON body. The older `GET /rest/api/3/search` endpoint is not
used because Jira Cloud has deprecated that search path and the POST body avoids
putting a potentially long JQL expression in the URL.

The request body is equivalent to:

```json
{
  "jql": "project = ALPHA AND updated >= -30m ORDER BY updated ASC",
  "maxResults": 100,
  "fields": ["summary", "issuetype", "updated", "comment"]
}
```

## Comment Filtering And Grouping

An issue may have been updated for reasons unrelated to comments. The DAG
therefore checks `comment.updated` for every returned comment and retains only
comments updated within the 30-minute UTC lookback window.

All retained comments for the same issue are collected into one configuration
payload. This means a polling run produces at most one proposed downstream DAG
run per Jira issue key, even when several comments on that issue changed.

Workflow comments are read from Jira's Atlassian Document Format and normalized
before command extraction. Users can write `/quant` requests as plain text or
inside common Jira rich-text structures such as multi-line paragraphs, bullet or
numbered lists, tables, mentions, links, formatted text, and code blocks. The
command marker must still be `/quant`, followed by the request body. The
`start_date` and `end_date` fields are optional overrides; omitting them inherits
the backtest window from the version-controlled `strategy.request`:

```text
/quant
Backtest the momentum strategy
strategy_type: backtest
start_date: 2024-01-01
end_date: 2024-12-31
options:
  max_iterations: 3
```

`strategy_type` is **required** and is never inferred from the request text.
It selects the workflow:

| `strategy_type` | Runs the backtest engine? | Typical use |
|---|---|---|
| `backtest` | yes | tune a strategy `.request` and backtest it |
| `refactor` | no | edit code and commit it |
| `ingestion` | no | prepare/mount data |
| `analysis` | no | inspect and report back |
| `other` | no | anything else |

Only `backtest` reaches the engine. Omitting `strategy_type`, or passing a value
outside the table, is a validation error posted back to the ticket — the request
text is deliberately not consulted, so an objective that merely mentions the word
"backtest" can never route a refactor to the engine.

Non-backtest requests should name the file they operate on with `resource_path`
(repo-relative, no leading `/` or `..`); without it the run falls back to the
demo strategy file:

```text
/quant
Tidy up the retry handling in the GitHub client
strategy_type: refactor
resource_path: rae_runtime/proxy/github_client.py
repo: bankingscience/BSLAgenticQuantDevLoop
```

When the requested deliverable differs from the starting context, set
`target_path`. It defaults to `resource_path` for existing commands. Both paths
must be repository-relative and inside `allowed_directories`:

```text
/quant
Create a root README from the existing resource notes and repository evidence
strategy_type: other
resource_path: atp-handlers/src/main/resources/readme
target_path: README.md
allowed_directories: .
repo: ATPDataHandlersRepo
```

### File scope

`resource_path` names the file the run starts from; `target_path` names the
required deliverable and defaults to the source path. A general request is not
limited to it: if the task needs changes in other files the agent may commit
those too, and every file it changed is reported back in
`generated_artifacts.modified_files`. A `backtest` request is the exception —
its `.request` target must change, because that exact file is what the engine
runs, so committing something else and backtesting an untouched baseline would
report metrics for a strategy nobody asked for.

`allowed_directories` bounds where the run may read and write:

```text
/quant
Tidy up the retry handling in the GitHub client
strategy_type: refactor
resource_path: rae_runtime/proxy/github_client.py
allowed_directories: rae_runtime/proxy strategies
```

This is enforced by the GitHub MCP server, not by the prompt: paths are checked
and normalised server-side on every read, list, and commit, so an agent that
strays — or Jira content that talks it into straying — is blocked rather than
merely asked not to. Traversal (`..`) and absolute paths are always rejected,
even when no `allowed_directories` is given. Omitting `allowed_directories`
leaves the run unrestricted within the repository.

A `resource_path` or `target_path` outside `allowed_directories` is a contradiction and is
rejected up front, rather than surfacing later as the agent being blocked from
the file it was told to edit.

For multi-repository edits, use `allowed_directories_map` when repositories need
different scopes. A mapped scope overrides the global value; unlisted selected
repositories inherit `allowed_directories`:

```text
allowed_directories: shared
allowed_directories_map: BSLAgenticQuantDevLoop=rae_runtime|jira-chatops-gateway; ATPDataHandlersRepo=.
```

An edited non-backtest run succeeds only when the reviewer accepts the committed
artifacts. Empty, unreadable, placeholder-only, trivial README, missing-target,
or rejected review results return `QUALITY_VALIDATION_FAILED`; the branch and
final commit SHA remain in the Jira report for repair.

Generated content is validated before the GitHub write. The agent can repair
deterministic failures without placing invalid content in branch history, and
committed artifacts are still read back and validated before acceptance.

### Read-only requests

`zero_code_modifications: true` (alias `read_only: true`) declares that the run
must not change anything. No branch is created and nothing is committed:

```text
/quant
Does the GitHub client retry safely, and where would backoff go?
strategy_type: analysis
resource_path: rae_runtime/proxy/github_client.py
read_only: true
```

What happens next depends on the workflow:

- `backtest` — submits the existing `.request` baseline to the engine as-is,
  without rewriting the strategy first. This is the declared form of the
  direct-backtest canary.
- everything else — reads the target file and answers the ticket. The findings
  come back in the Jira comment's **Evaluation** section with
  `recommended_action: review`, since nothing was scored and nothing changed.

A request with no `repo` defaults to
`bankingscience/BSLAgenticQuantDevLoop`. Use `read_only: true` when the run must
not edit or commit repository content.

### Iteration

Every request runs the same bounded `edit -> evaluate -> repeat` loop; only the
evaluator differs. A `backtest` run is scored by the engine against
`target_criteria`. A general run has no metrics, so it is scored by a review
agent that reads the committed file and judges it against the ticket, returning
the same `evaluation`/`recommended_action`. The loop repeats while the verdict is
`iterate`, bounded by `max_iterations`, `timeout_seconds`, and the token budget.

That review verdict is a model's opinion, not a measurement: it is labelled
advisory in the Jira comment, and reports `confidence: 0.0` with empty
`criteria_results` because nothing in it is numerically scorable. If the reviewer
cannot be reached or returns something unusable, the run degrades to
`recommended_action: review` — it never guesses a verdict, and never fails a run
whose edit already succeeded.

`allow_iteration` (alias `iterate`) controls the loop. Its default depends on the
workflow, because the two evaluators are not equally trustworthy:

| `strategy_type` | Default | Why |
|---|---|---|
| `backtest` | `true` | the engine returns measured metrics, so another pass has a real signal to improve against |
| everything else | `false` | the review verdict is a model's opinion, not a measurement — not a basis for spending more passes unasked |

So a general request runs once by default and still reports its review verdict to
Jira. Opt in when you want it to keep going:

```text
/quant
Tidy up the retry handling in the GitHub client
strategy_type: refactor
resource_path: rae_runtime/proxy/github_client.py
allow_iteration: true
```

Conversely `allow_iteration: false` pins a backtest to a single pass.
`allow_iteration: false` overrides `max_iterations` and also disables failure
retries, so a failed pass is returned rather than retried.

### Repository selection

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

`BSLAgenticQuantDevLoop` defaults to `main`; all ATP repositories default to
`develop`. A branch map overrides the source branch, while every selected
repository gets the same safe ticket target by default:

```text
/quant
Update coordinated connector and handler code
strategy_type: refactor
resource_path: rae_runtime/proxy/contracts.py
branch_map: BSLAgenticQuantDevLoop=main, ATPConnectorsRepo=feature/connectors, ATPDataHandlersRepo=develop
```

Coordinated non-backtest edits may select several repositories. The MCP runtime
inspects and may commit in each repository, while `resource_path` and
`target_path` remain primary-repository fields:

```text
/quant
Update the shared data contract and its consuming handler
strategy_type: refactor
repos: BSLAgenticQuantDevLoop, ATPConnectorsRepo, ATPDataHandlersRepo
resource_path: rae_runtime/proxy/contracts.py
allowed_directories: .
allowed_directories_map: BSLAgenticQuantDevLoop=rae_runtime; ATPConnectorsRepo=.; ATPDataHandlersRepo=.
allow_iteration: true
model: qwen3-coder
max_iterations: 10
max_failed_iterations: 2
max_agent_turns: 45
max_commits_per_run: 20
timeout_seconds: 10800
max_token_budget_per_run: 1000000
cpu_vcpus: 2
memory_mb: 4096
gpu_count: 0
execution_timeout_seconds: 10800
pids_limit: 256
yarn_queue: root.default
```

Multi-repository ATP backtests are rejected before credentials or a sandbox are
started. These ATP repositories are code repositories, not the six compiled
ATRADE sifting components. The `atrade_sifting_*` values in an engine request
come from its selected baseline unless a direct low-level `submit_backtest`
caller supplies an explicit override; repository selection never rewrites them.

Available models for `model`: `nova-micro`, `nova-pro`, `test-model`,
`gpt-oss`, `qwen3-coder`

The `params` option is passed through as a raw engine parameter string. The
gateway does not validate the individual `KEY=value` entries.

Before the Docker workflow starts, the command is validated against the runtime
request contract. Invalid commands are rejected without launching the sandbox,
and the Jira issue receives a validation failure comment with the fields to fix.
Repository references must match the approved code-owned alias catalog and
point at the approved `bankingscience` GitHub organisation. Target branches must
be ticket-scoped quant branches. Execution limits such as `max_iterations`,
`timeout_seconds`, `max_failed_iterations`, `max_agent_turns`,
`max_commits_per_run`, and `max_token_budget_per_run` are bounded by the runtime
schema. The optional commit cap accepts `1`-`50`, applies to one agent/edit pass,
and retains the runtime `MAX_COMMITS_PER_RUN` setting (default `5`) when omitted;
each iteration receives a fresh allowance. Runtime resource options are also
bounded: `cpu_vcpus` (`0.25`-`8`), `memory_mb`
(`512`-`16384`), `gpu_count` (`0` only), `execution_timeout_seconds`
(`60`-`10800`), and `pids_limit` (`64`-`1024`). `yarn_queue` may select a
per-run YARN queue. Omitting resource options uses safe defaults documented in
`docs/runtime_payload_contract.md`.

Experiment 2 adds `rag_enabled` (boolean, default `false`) and `rag_top_k`
(`1`-`10`, default `5`). C0 does not open an index. C1 retrieves exactly once
from a frozen, SHA-256-verified local index and fails closed on missing or
drifting data. The current local implementation sends a bounded evidence block
only to scripted/MCP coding prompts: 4,000 characters per memory and 12,000
characters for the complete block. Review and read-only analysis prompts never
receive it, and telemetry contains only hashes, scores, ranks, and memory IDs.
Prompt-enabled C1 is Docker-only because YARN persists request JSON to HDFS. See
`experiments/exp2/README.md` for the paired run template and freeze procedure.

## Airflow Configuration

Production credentials belong in Airflow Connections:

```text
jira_cloud          Required. Jira Cloud host/login/API token
litellm_default     Required. LiteLLM token in the password field
github_default      Optional. Global GitHub token for DAG/package operations and explicitly mapped user-scoped workflows
per-user GitHub PAT connections  Required for user-scoped /quant repo editing/backtesting
```

Non-sensitive deployment config belongs in Airflow Variables:

```text
JIRA_PROJECT_KEY        Required. Project key to include in the JQL query
JIRA_CONNECTION_ID      Optional. Defaults to jira_cloud
JIRA_RAG_INDEX_PATH     Required only for rag_enabled=true. Read-only frozen index path
JIRA_RAG_INDEX_SHA256   Required only for rag_enabled=true. Approved exact file digest
LITELLM_CONNECTION_ID   Optional. Defaults to litellm_default
GITHUB_CONNECTION_ID    Optional. Leave unset for pure backtesting runs
QUANT_GITHUB_USER_ACCESS_MATRIX  Required for user-scoped /quant repo editing/backtesting
SANDBOX_IMAGE           Optional. Defaults to the stable sandbox image
DOCKER_URL              Optional. Remote Docker daemon URL when needed
SANDBOX_EXECUTION_MODE  Optional. Defaults to docker; set to yarn for YARN
USE_REAL_BACKTESTER     Optional. Defaults to false; set true for real engine
BACKTEST_STORAGE_KIND   Optional. shared_fs (default) or hdfs (YARN only)
SIMULATION_REQUESTS_DIR Required when real mode is true. Host/shared path or YARN HDFS URI; Bialobog uses hdfs:///quant-sandbox/simulation-requests
SIMULATION_RESULTS_DIR  Required when real mode is true. Host/shared path or YARN HDFS URI; Bialobog uses hdfs:///quant-sandbox/simulation-results
BACKTEST_RESULT_TIMEOUT_SECONDS Optional. Real result polling timeout
SANDBOX_HDFS_NAMENODE_URI  Required for YARN. Explicit HDFS URI
SANDBOX_HDFS_RUN_ROOT      Required for YARN. HDFS run directory root
SANDBOX_YARN_QUEUE         Required for YARN. Queue name
SANDBOX_YARN_MASTER_MEMORY_MB     Optional for YARN. Defaults to 512
SANDBOX_YARN_CONTAINER_MEMORY_MB  Optional for YARN. Defaults to 4096
SANDBOX_YARN_TIMEOUT_SECONDS      Optional for YARN. Defaults to 2100
SANDBOX_CONTAINER_TIMEOUT_SECONDS Optional for Docker. Defaults to 1800
SANDBOX_YARN_CLEANUP_HDFS         Optional for YARN. Defaults to false
HADOOP_HOME                       Optional for YARN. Defaults to /opt/hadoop
HADOOP_CONF_DIR                   Optional for YARN. Defaults to /opt/hadoop/etc/hadoop
WORKFLOW_TASK_TIMEOUT_SECONDS     Optional. Defaults to 11100
WORKFLOW_RECOVERY_STATE_DIR       Optional. Defaults to /tmp/jira_quant_workflow_recovery
WORKFLOW_RETRY_MAX_ATTEMPTS       Optional. Defaults to 3
WORKFLOW_RETRY_INITIAL_DELAY_SECONDS  Optional. Defaults to 60
WORKFLOW_RETRY_MAX_DELAY_SECONDS      Optional. Defaults to 300
WORKFLOW_RETRY_BACKOFF_MULTIPLIER     Optional. Defaults to 2
WORKFLOW_RETRY_JITTER_RATIO           Optional. Defaults to 0.2
WORKFLOW_ENABLE_DOCKER_FALLBACK       Optional. Defaults to false
```

Local development can use `.env` as a fallback when Airflow metadata is not
available. Do not commit a populated `.env` file or credentials.

In real backtester mode, `shared_fs` request/result paths must be persistent
host-local or shared filesystem directories visible to the YARN worker or local
Docker host. For YARN HDFS storage, set `BACKTEST_STORAGE_KIND=hdfs` and provide
`hdfs://` request/result URIs. If execution uses local Docker directly, or YARN
falls back to Docker, HDFS settings are replaced with the conventional local
watcher paths `~/simulation-requests` and `~/simulation-results`. These local
directories must already exist. The runner bind-mounts host paths into the
sandbox at `/workspace/backtester/simulation-requests` and
`/workspace/backtester/simulation-results`. Requests are staged atomically as
`<workflow_id>.request` and results are polled under `<workflow_id>/`.

Because Bialobog's vendor watcher cannot read HDFS directly, production YARN
mode also runs `scripts/hdfs_simulation_service_bridge.py` from the Bialobog
crontab. It delivers HDFS requests atomically to the watcher's local inbox and
publishes stable completed directories or rejection files back to HDFS. See
`docs/airflow_ui_configuration.md` for canonical paths, disk safety controls,
heartbeat state, phased cleanup enablement, and the read-only deployment check.

`WORKFLOW_RECOVERY_STATE_DIR` should point to shared durable storage in
production when Airflow retries may run on different workers. The runner uses
that state to skip completed sandbox execution, retry Jira writeback, and avoid
duplicate bot comments for the same Jira command.

When `SANDBOX_EXECUTION_MODE=yarn`, `WORKFLOW_ENABLE_DOCKER_FALLBACK=true`
allows a single workflow run to fall back to local Docker after a YARN/HDFS
infrastructure failure. It does not mutate Airflow Variables or change future
runs.

## Local Test

Install `requests` and `pytest` from `requirements.txt`. Install Airflow plus
the standard and HTTP providers using the same versions and constraints as the
target Airflow environment; the repository deliberately does not pin the
Airflow runtime.

Export the variables from `.env.example`, place or symlink the DAG into the
local Airflow DAG folder, then run:

```bash
airflow tasks test jira_comment_poller poll_jira_comments 2026-06-09
```

This calls Jira but only logs proposed downstream runs because triggering is
disabled.

Run the isolated unit tests without Jira network access:

```bash
pytest
```

Example finalization code for the downstream workflow:

```python
from jira_quant_common import format_result_comment_adf, post_jira_adf_comment

post_jira_adf_comment(
    final_result["execution_summary"]["ticket_id"],
    format_result_comment_adf(final_result),
)
```

## Delivering DAGs From GitHub

Airflow does not automatically load the newest DAG from GitHub by default. Git
can be the source of truth, but the deployment mechanism depends on the Airflow
platform:

- CI/CD can validate the DAG and copy it to the scheduler's DAG folder or the
  object-storage bucket used by a managed Airflow service.
- Kubernetes deployments can use Git-sync to continuously mirror a branch or
  commit into the DAG folder.
- A managed Airflow provider may offer its own repository or deployment
  integration.

Prefer deploying an immutable commit through CI/CD for production. Automatically
tracking the head of a branch is convenient, but a broken commit can immediately
break DAG parsing. The CI pipeline should at least run unit tests and an Airflow
DAG import check before deployment.

## Runtime Request/Response Contract

Runtime request/response contract documentation is available in
`docs/runtime_payload_contract.md`.

RAE writes the structured response to the request's `output_paths.result_path`,
normally `/workspace/output/result.json`. IW reads that mounted result file
first and falls back to the final stdout JSON line only when `result.json` is
not readable.

`output_paths.progress_events_path` is optional. When present, it points to a
mounted JSONL side-channel such as `/workspace/output/progress_events.jsonl`.
IW reads live progress only from that JSONL file; progress events are never read
from stdout because the final non-empty stdout line is reserved for the final
result JSON fallback used by Airflow/XCom.

The final Jira workflow report is generated from the Runtime Response Payload
Contract in `result.json` and enriched with optional progress events plus the
SCRUM-18 artifact ingestion result. Safe referenced artifacts under
`/workspace/output` are uploaded as Jira attachments before the report is
posted, and the report includes uploaded, missing, and skipped artifact
ingestion sections.

A non-zero container exit code with a valid structured JSON response is a
handled RAE failure and should carry failure details in `diagnostics`. A non-zero
container exit code with no readable structured JSON response is a hard
infrastructure failure. Exit codes are intentionally simple: `0` means success
and non-zero means failure.

Optional `telemetry` fields such as `execution_time_seconds`,
`container_exit_code`, `retry_count`, `model_usage`, `stage_timings`, and
`audit_trace` are for debugging, audit, and monitoring. RAE does not need to
provide every telemetry field on every run.
