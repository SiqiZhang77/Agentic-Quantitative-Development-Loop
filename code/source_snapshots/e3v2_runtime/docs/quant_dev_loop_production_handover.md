---
title: "Quant Dev Loop: Production Handover"
subtitle: "Deployment and operations guide for the production-cluster owning team"
date: "12 August 2026"
lang: en-GB
geometry: "a4paper,margin=20mm"
fontsize: 10pt
colorlinks: true
toc: true
toc-depth: 2
numbersections: true
header-includes:
  - |
    \usepackage{tikz}
    \usetikzlibrary{arrows.meta,positioning,fit,shapes.geometric}
  - |
    \definecolor{flowblue}{HTML}{1F4E79}
    \definecolor{flowteal}{HTML}{0F766E}
---

# Purpose and status

This is the production handover for the Quant Dev Loop: a Jira ChatOps workflow
that turns a structured `/quant` request into a bounded, auditable quantitative
development task or strategy backtest. It is written for the engineers and
operators taking responsibility for deployment, access control, monitoring, and
incident response on the production cluster.

## Handover document set

This handover is the overview and ownership record. Use its two operational
companions to deploy and run the system without student assistance:

- [Architecture and Operational SOP](quant_dev_loop_architecture_and_operational_sop.md):
  complete component catalogue, service dependencies, request sequence,
  startup/shutdown, recovery, rollback, troubleshooting scenarios, and two
  worked SOPs.
- [Deployment and Configuration Guide](quant_dev_loop_deployment_and_configuration_guide.md):
  infrastructure prerequisites, Airflow service-account preparation, fresh
  installation, Connections, Variables, Docker/YARN/HDFS, bridge setup, and the
  service permissions matrix.

## Documented project requirement and intended approach

The implemented-system requirement is to provide an operational route from a
Jira ticket comment to either:

1. a controlled quantitative strategy backtest; or
2. a controlled repository task such as a refactor, analysis, ingestion task,
   or documentation change.

The system must return a structured, traceable result to the originating Jira
ticket while preventing an LLM from freely choosing repositories, branches,
paths, execution duration, or external write capability. The original early
PoC established ticket-to-result transport with mocks; the current codebase extends
that into a bounded edit/evaluate loop, user-scoped GitHub access, optional
real-backtester paths, and structured audit/diagnostic contracts.

The planned approach is deliberately split across two responsibilities:

- **Gateway / orchestration (Airflow):** poll and validate Jira requests,
  resolve the requester and deployment configuration, stage approved inputs,
  launch one isolated runtime, collect its result, and post the Jira report.
- **Runtime (RAE):** validate the request again, use the allowed GitHub and
  backtest MCP capabilities, emit progress plus one final response, and exit.

The principal design rule is that requirements supplied in Jira are untrusted
content. Prompt text helps an agent understand the requested task; it does not
grant repository, branch, filesystem, HDFS, credential, or tool access.

# High-level flow

**Source -> method -> destination.** The normal route is Jira request data into
Airflow orchestration, then a run-scoped sandbox. The sandbox may read/write a
ticket branch and, for `backtest`, stage a request to the real engine. The final
contract returns to Airflow and is rendered as a Jira ADF comment.

```{=latex}
\begin{figure}[htbp]
\centering
\begin{tikzpicture}[
  box/.style={draw, rounded corners=2pt, align=center, minimum height=13mm,
              text width=27mm, font=\scriptsize},
  orchestration/.style={box, draw=flowblue, fill=blue!7},
  runtime/.style={box, draw=flowteal, fill=teal!8},
  arrow/.style={-{Stealth[length=2.1mm]}, thick, draw=flowblue},
  iteration/.style={-{Stealth[length=2.0mm]}, thick, dashed, draw=flowteal}
]
  \node[orchestration] (request) at (-72mm,0) {\textbf{1. Jira request}\\/quant comment\\and selected input};
  \node[orchestration] (airflow) at (-36mm,0) {\textbf{2. Airflow validation}\\identity, schema\\and scope};
  \node[runtime] (sandbox) at (0,0) {\textbf{3. Governed sandbox}\\fresh RAE runtime\\per ticket run};
  \node[orchestration] (result) at (36mm,0) {\textbf{4. Structured result}\\metrics, artefacts\\and diagnostics};
  \node[orchestration] (writeback) at (72mm,0) {\textbf{5. Jira write-back}\\ADF report\\and attachments};
  \node[runtime] (github) at (-18mm,-29mm) {\textbf{Controlled GitHub work}\\approved repository\\and ticket branch};
  \node[runtime] (evaluation) at (18mm,-29mm) {\textbf{MCP validation}\\backtest engine\\or review agent};
  \draw[arrow] (request) -- (airflow);
  \draw[arrow] (airflow) -- (sandbox);
  \draw[arrow] (sandbox) -- (result);
  \draw[arrow] (result) -- (writeback);
  \draw[arrow] (sandbox.south) -- (github.north);
  \draw[arrow] (github) -- (evaluation);
  \draw[arrow] (evaluation.north) -- (result.south);
  \draw[iteration] (evaluation.south) to[out=-90,in=-90,looseness=1.25]
    node[midway,below,font=\scriptsize\itshape,text=flowteal]{bounded iteration when enabled} (github.south);
\end{tikzpicture}
\caption{Quant Dev Loop request, governed execution, bounded validation, and Jira reporting flow.}
\end{figure}
```

## Run sequence

1. A Jira comment containing `/quant` is found by
   `jira_quant_docker_orchestrator`. It groups valid command content by issue.
2. The gateway validates the command before starting a container: command type,
   repository alias, requester authorisation, branch, path scope, numeric
   bounds, attachment selection, and the runtime request schema.
3. Airflow creates a globally unique `run_id`, stages the runtime request and
   any approved input as read-only, then launches one fresh Docker or YARN
   sandbox run.
4. RAE validates the request schema again. It can perform a GitHub operation
   only with the requester's resolved credential and server-enforced scope.
5. A `backtest` may submit a uniquely named `.request` to the configured
   backtester route. Other strategy types do not invoke the backtest engine.
6. RAE atomically writes the final response JSON; the last non-empty stdout
   line is a fallback only. Optional progress is JSONL, never stdout.
7. Airflow reads the final response, uploads safe output artifacts to Jira, and
   posts an ADF report to the originating issue. Retry/recovery state suppresses
   duplicate completion comments.

# Architecture and data destinations

For the Bialobog local-watcher integration, the deployment uses the shared
`masteruser` Unix account. Host paths later in this section therefore use
`/home/masteruser/...` (or `~` when it resolves for that account).

## Where data goes

### Request and input lineage

- **Jira request and requester identity:** Jira remains the initiating record;
  Airflow retains the correlated task context. Never put credentials in issue
  text or comments.
- **Named Jira dataset attachment:** the gateway verifies attachment identity,
  byte count, and SHA-256, then mounts the file read-only below
  `/workspace/input/datasets/`. In YARN it first copies the verified input into
  a unique run directory.
- **Approved HDFS input dataset:** only a file below
  `SANDBOX_INPUT_HDFS_ALLOWED_ROOTS` may be staged read-only into the sandbox.
  One Jira command selects one external input dataset.
- **Runtime request:** moves from Airflow to the sandbox through the defined
  stdin/mounted-input contract. It contains objectives and paths, never tokens.

### Runtime output and write-back

- **Progress:** optional JSONL appears as
  `/workspace/output/progress_events.jsonl`. It is not a replacement for the
  final result, and it must not be written to stdout.
- **Final result:** `/workspace/output/result.json` is the canonical,
  atomically-written response. The final stdout JSON is an XCom fallback only.
- **Generated artifacts:** safe files under `/workspace/output` may be uploaded
  to Jira. Airflow records uploaded, missing, or skipped ingestion; local
  scratch paths are not retained after container exit unless uploaded.
- **GitHub changes:** land only on `quant/<Jira-key>` or an allowed descendant
  branch. The final report names the actual branch, commit, and changed files.

### Engine, recovery, and bridge state

- **Real-engine request:** a uniquely named `<job>.request` enters the approved
  HDFS requests root in YARN mode; the bridge transfers it atomically to the
  Bialobog watcher inbox. Docker instead uses host-visible
  `~/simulation-requests`.
- **Real-engine result:** the bridge publishes stable terminal directories or
  rejection files into the approved HDFS results root. Docker instead polls
  `~/simulation-results`.
- **Workflow recovery:** `WORKFLOW_RECOVERY_STATE_DIR` must be shared durable
  storage when retrying across different Airflow workers.
- **Bridge health:** the Bialobog bridge records heartbeat, disk state, cleanup
  counters, job states, and a sanitised last error under
  `/home/masteruser/.quant-hdfs-bridge/heartbeat.json`.

## HDFS route and safe directory boundary

The canonical default-filesystem URIs are:

```text
hdfs:///quant-sandbox/simulation-requests
hdfs:///quant-sandbox/simulation-results
```

Three slashes are intentional: `quant-sandbox` is an HDFS directory, not a
host name. The Bialobog watcher cannot consume HDFS directly. The production
route is therefore:

```text
YARN sandbox -> HDFS request -> HDFS bridge -> /home/masteruser/simulation-requests
-> simulation-service watcher/backtester -> /home/masteruser/simulation-results
-> HDFS bridge -> HDFS terminal result -> YARN/Airflow
```

Only the following directory classes should be writable by this workflow, and
each must be confirmed by the platform owner before go-live:

- run-isolated temporary files under `SANDBOX_HDFS_RUN_ROOT`;
- HDFS request staging below the approved simulation-requests URI;
- HDFS result publication below the approved simulation-results URI;
- Bialobog bridge state under `/home/masteruser/.quant-hdfs-bridge`;
- watcher-facing local request and result directories listed in the bridge
  configuration; and
- sandbox `/workspace/output` for final result and artifacts.

Do not grant broad HDFS home-directory write access merely to make a run work.
Input data is explicitly read-only and must originate from
`SANDBOX_INPUT_HDFS_ALLOWED_ROOTS`.

# Access controls and operational risks

## GitHub access and write controls

User-scoped repository work requires `QUANT_GITHUB_USER_ACCESS_MATRIX`, a
non-secret Airflow Variable mapping Jira identity to GitHub username, authorised
repositories, permission level, and an Airflow Connection ID. The token itself
is collected through the repository-access spreadsheet, stored in the cluster's
encrypted vault, and associated with the requester's Jira account. It must never
be copied into the access-matrix Variable, Jira, logs, runtime payloads, or this
document.

When Jira Cloud does not expose a requester email, record their
`jira_account_id`; see [Finding `jira_account_id`](../jira-chatops-gateway/docs/airflow_ui_configuration.md#finding-jira_account_id).

| Risk | Enforced control | Operator check |
|---|---|---|
| Jira user edits a repository they do not own | Gateway matches requester to a matrix row, then verifies GitHub identity and access before launch; runtime repeats identity verification | Every active requester has a unique, reviewed row and a scoped PAT Connection |
| LLM writes a default/protected branch | Target must be `quant/<ticket>` or a descendant; default branches are source branches only | Confirm branch protection remains enabled on every catalogue repository |
| LLM reads/writes outside task scope | `allowed_directories` is normalised and checked server-side on list, read, and commit; absolute paths and traversal are rejected | Require narrow scopes in operational tickets; do not use `.` without a documented reason |
| Read-only work creates a branch or commit | `read_only` / `zero_code_modifications` prevents branch creation and commits | Use it for analysis and baseline backtest canaries |
| Excessive changes or loop spend | Bounds cover turns, iterations, failed iterations, commits, tokens, timeout, CPU, memory, and PIDs | Set a low cap for first production runs; record any exception in the ticket |
| Invalid or invented generated content is committed | Complete content is deterministically validated before `commit_and_push`, then committed artifacts are read back and classified against source/target branches | Treat `QUALITY_VALIDATION_FAILED` as a repair/review event, not a successful change |

### Requester GitHub token configuration

The deployed requester-authentication configuration uses a **classic GitHub
Personal Access Token**, not a fine-grained token. Each requester creates the
token for the GitHub account that has access to their repositories:

1. Go to **GitHub > Settings > Developer settings > Personal access tokens >
   Tokens (classic)**.
2. Select **Generate new token (classic)**.
3. Name the token `QuantDevLoop`.
4. Set the expiry date for as long as possible.
5. Select exactly these scopes:
   - `repo` - Full control of private repositories;
   - `repo:status` - Access commit status;
   - `repo_deployment` - Access deployment status;
   - `public_repo` - Access public repositories;
   - `repo:invite` - Access repository invitations;
   - `security_events` - Read and write security events;
   - `workflow` - Update GitHub Action workflows;
   - `write:packages` - Upload packages to GitHub Package Registry; and
   - `read:packages` - Download packages from GitHub Package Registry.
6. Select **Generate token**, copy it immediately, and add it only to the PAT
   column of the repository-access spreadsheet for secure vault onboarding.

The classic PAT grants the GitHub capabilities above. The Quant Dev Loop still
enforces the narrower per-request repository, permission level, branch, and
directory scope through `QUANT_GITHUB_USER_ACCESS_MATRIX`, the repository
catalogue, and server-side validation. A matrix user with `permission_level:
read` may run only an explicit read-only command. Do not use a global GitHub
token as an implicit fallback for requester-scoped work.

## HDFS/YARN and backtester risks

| Risk | Safeguard | Operational check |
|---|---|---|
| Two runs overwrite a result | Global `run_id` plus per-run/per-iteration job names; names are sanitised and unique | Concurrent canary runs with distinct result directories |
| One ticket has competing commits | Airflow must allow at most one concurrent container per ticket | Scheduler/DAG concurrency configuration and a same-ticket race test |
| Partial file observed by watcher | Request delivery and result publication use atomic staging/rename; bridge checks stable terminal output | Canary request and terminal result timestamps/paths |
| Disk pressure on Bialobog | Intake pauses below 5 GiB and resumes at 7.5 GiB; verified local results can be cleaned after one day | `heartbeat.json`, free-space monitoring, and an escalation owner |
| Unsafe retention deletion | HDFS retention starts in `report` mode. Only verified bridge-managed terminal results are candidates; unmanaged/legacy files are excluded | Reviewed report-mode candidates before any `delete` mode change |
| HDFS outage or YARN failure | Optional single-run Docker fallback is explicitly configured and does not mutate future Airflow settings | Documented decision on whether fallback is enabled and which host paths are mounted |
| Watcher visibility mistaken for completion | Watcher intake status tracks queue receipt; terminal artefacts and parsed metrics determine the backtest outcome | Check terminal result artefacts and parsed metrics/rejection state |

# Deployment setup

## Airflow Connections: secrets only

Create these under **Admin > Connections**. Store credentials in the Password
field and never in Variables, Jira, docs, images, or logs.

\begingroup
\small
\renewcommand{\arraystretch}{0.95}
\begin{longtable}{@{}p{0.27\linewidth}p{0.25\linewidth}p{0.33\linewidth}@{}}
\toprule
\textbf{Connection} & \textbf{Required} & \textbf{Purpose} \\
\midrule
\endhead
\texttt{jira\_cloud} & Yes & Jira API host, service-account login, and API token for polling, validation, attachments, and write-back \\
\texttt{litellm\_default} & Yes & LiteLLM/OpenAI-compatible endpoint and API key for the runtime agent \\
\texttt{github\_default} & Conditional & DAG sync, package access fallback, and explicitly mapped global operations; not implicit requester fallback \\
Per-user GitHub PAT Connections & For repository editing/backtesting & Requester-scoped identity and repository capability \\
\texttt{GHCR\_CONNECTION\_ID} target & Conditional & Separate pull credential for a private sandbox image when different from GitHub credentials \\
\bottomrule
\end{longtable}
\endgroup

For scoped Jira tokens, use the Atlassian API gateway host
`https://api.atlassian.com/ex/jira/{cloudId}`, not a site-specific URL. Verify
`/myself`, JQL visibility, issue browsing, comment posting, and attachment
creation using the configured service account before enabling the DAG.

## Required and key Airflow Variables

| Variable | Required when | Documented/default value or rule |
|---|---|---|
| `JIRA_PROJECT_KEY` | Always | Jira project to poll |
| `POLL_INTERVAL_MINUTES` | Recommended | Positive integer; default `5`; controls both schedule and lookback |
| `QUANT_GITHUB_USER_ACCESS_MATRIX` | User-scoped repo work | Non-secret identity/repository/Connection mapping |
| `SANDBOX_IMAGE` | Production deployment | Default is the `:stable` GHCR image; record the selected immutable digest |
| `SANDBOX_EXECUTION_MODE` | Always | `docker` (default) or `yarn` |
| `USE_REAL_BACKTESTER` | Real engine only | `false` by default; enable after the controlled canary |
| `BACKTEST_STORAGE_KIND` | Real mode | `shared_fs` default; `hdfs` for YARN/HDFS route |
| `SIMULATION_REQUESTS_DIR` / `SIMULATION_RESULTS_DIR` | Real mode | HDFS canonical paths above for Bialobog; host/shared paths for Docker |
| `SANDBOX_HDFS_NAMENODE_URI`, `SANDBOX_HDFS_RUN_ROOT`, `SANDBOX_YARN_QUEUE` | YARN | Explicit NameNode URI, run root, and queue |
| `SANDBOX_INPUT_HDFS_ALLOWED_ROOTS` | HDFS input dataset | JSON array or comma-separated input allow-list |
| `SANDBOX_INPUT_MAX_FILE_BYTES` / `SANDBOX_INPUT_MAX_TOTAL_BYTES` | HDFS input dataset | Defaults 100 MiB / 250 MiB |
| `WORKFLOW_RECOVERY_STATE_DIR` | Retried workflows | Shared durable location in multi-worker production |
| `WORKFLOW_ENABLE_DOCKER_FALLBACK` | YARN fallback | `false` by default; opt in explicitly |
| `SANDBOX_YARN_CLEANUP_HDFS` | YARN cleanup | `false` by default |

The full supported Variables and default values are in
`jira-chatops-gateway/docs/airflow_ui_configuration.md`. Keep this handover
short by treating that file as the detailed configuration reference, not a
second source of truth.

## Deploy DAGs, schemas, and image as one compatible release

The gateway and sandbox carry duplicated runtime request/response schemas.
Deploy their compatible versions together.

1. Select and record a reviewed source commit and sandbox image digest.
2. Run gateway and runtime test suites plus an Airflow DAG import check.
3. Deploy `jira-chatops-gateway/dags/` and `jira-chatops-gateway/schemas/` to
   the Airflow locations together. The existing sync model uses
   `/opt/airflow3/dags` and `/opt/airflow3/schemas`.
4. Promote a reviewed sandbox image through the **Promote to Stable** GitHub
   Actions workflow, then record the resulting immutable digest.
5. Run the read-only bridge probe and a controlled canary before setting
   `USE_REAL_BACKTESTER=true` for normal traffic.

The local sync process deliberately does not delete unrelated deployed files.
If the sync helper itself changes, refresh it through the documented bootstrap
step before relying on cron. A manual server patch can be overwritten by the
next Git sync; commit and deploy the source change instead.

## HDFS bridge install, rollback, and monitoring

The bridge runs once per minute on Bialobog. It transfers HDFS requests to the
local simulation watcher and publishes stable terminal outputs back to HDFS.
Use the documented non-secret environment and cron entry from
`jira-chatops-gateway/docs/airflow_ui_configuration.md`.

Before enabling or replacing the bridge:

1. Preserve timestamped copies of the current script and crontab.
2. Preserve `/home/masteruser/.quant-hdfs-bridge`; it contains durable request
   markers used for safe recovery.
3. Run the non-mutating check:

   ```bash
   /opt/airflow3/venv/bin/python \
     /opt/airflow3/scripts/hdfs_simulation_service_bridge.py --check
   ```

4. Submit a uniquely named canary, verify it is consumed, and verify its stable
   terminal directory or rejection file in HDFS.
5. Start HDFS retention in `report` mode. Only after reviewing candidates may
   the owner deliberately change it to `delete`.

Rollback restores the saved bridge script and crontab, reverts the two
`SIMULATION_*_DIR` Variables, and validates that no active canary is being
stranded. It must not delete shared request/result directories as a rollback
shortcut.

# Operating the `/quant` interface

## Command grammar and safe defaults

A Jira comment may use block (`key: value`) or inline (`key=value`) options.
`strategy_type` is the only universally required field; it is never inferred
from natural language. All other parameters are optional unless their stated
condition applies.

### Minimum valid request

```text
/quant
Describe the requested task
strategy_type: analysis
```

\begin{longtable}{@{}p{0.19\linewidth}p{0.20\linewidth}p{0.51\linewidth}@{}}
\toprule
\textbf{strategy\_type} & \textbf{Backtest engine?} & \textbf{Behaviour} \\
\midrule
\endhead
\texttt{backtest} & Yes & Modify a strategy \texttt{.request} then backtest, unless explicitly read-only \\
\texttt{refactor} & No & Edit and commit code \\
\texttt{analysis} & No & Inspect and report; pair with \texttt{read\_only: true} for no Git write \\
\texttt{ingestion} & No & General data-preparation/code task \\
\texttt{other} & No & Other controlled repository task, including documentation \\
\bottomrule
\end{longtable}

Defaults matter:

- `backtest` permits iteration by default because the engine returns measured
  metrics.
- Every non-backtest type defaults to one pass because its review verdict is an
  advisory model judgement, not measured performance.
- `read_only: true` (alias `zero_code_modifications`) makes a backtest submit
  the committed baseline and makes other types report findings without branch
  creation or commits.
- Omitted `repo` selects `bankingscience/BSLAgenticQuantDevLoop`; its default
  source branch is `main`. The ATP repositories default to `develop`.

### Analysis mode: read-only inspection

`analysis` is for inspecting and reporting on existing code or data. Specify
`read_only: true` (alias `zero_code_modifications`) when it must create no
branch or commit. For a code change, first submit a separate `/quant`
`refactor` or `backtest` request, review its result, then use `analysis` to
inspect or report on the resulting implementation.

## Copy-paste examples

### Bounded strategy backtest

\begingroup\footnotesize

```text
/quant
Run a backtest of the momentum strategy with a smaller, more concentrated book and report Sharpe and drawdown
strategy_type: backtest
read_only: true
params: NPORT=50, NFREQ=4
start_date: 2020-01-01
end_date: 2024-12-31
max_iterations: 1
max_token_budget_per_run: 100000
```

\endgroup

### Baseline canary: no edit, no commit

```text
/quant
Submit the existing strategy baseline for one controlled backtest
strategy_type: backtest
read_only: true
params: NPORT=80, NFREQ=4
resource_path: rae_runtime/proxy/strategy.request
max_iterations: 1
max_failed_iterations: 1
timeout_seconds: 5400
```

### Scoped code change

```text
/quant
Add a new self-contained performance-metrics utility for backtest results, using only
the Python standard library. Create a new module at rae_runtime/proxy/backtest_metrics.py
with pure functions that take a list of periodic returns and compute cumulative return,
annualised return, annualised volatility, Sharpe ratio, and maximum drawdown, plus a
function that assembles all of these into a formatted multi-line text summary. Handle
edge cases sensibly, such as an empty return series and a zero-volatility series. Write
the full implementation yourself with clear docstrings. Also create a companion test
module at rae_runtime/proxy/test_backtest_metrics.py with thorough unit tests over
hand-computed inputs, covering each metric and the edge cases. Do not use pandas, numpy,
or any third-party library. Commit both new files.
strategy_type: refactor
resource_path: rae_runtime/proxy/backtest_metrics.py
repo: bankingscience/BSLAgenticQuantDevLoop
model: qwen3-coder
allow_iteration: false
max_agent_turns: 60
max_commits_per_run: 5
max_token_budget_per_run: 400000
```

### Read-only analysis of an attached dataset

```text
/quant
Analyse the attached monthly-return dataset and report the findings
strategy_type: analysis
zero_code_modifications: true
input_attachment: experiment_2_input.csv
input_format: csv
```

Upload the named attachment before the comment. An attachment is never selected
from wording alone; the exact filename is required. A synthetic, attach-ready
[sample CSV](../jira-chatops-gateway/docs/examples/experiment_2_input.csv) is
bundled for this example; it is not real Experiment 2 data.

## Required, optional, and conditional parameter reference

\begin{longtable}{@{}p{0.26\linewidth}p{0.37\linewidth}p{0.30\linewidth}@{}}
\toprule
\textbf{Parameter} & \textbf{Status and valid values} & \textbf{Meaning} \\
\midrule
\endhead
\texttt{strategy\_type} & \textbf{Required:} \texttt{backtest}, \texttt{refactor}, \texttt{analysis}, \texttt{ingestion}, \texttt{other} & Chooses workflow and evaluator \\
\texttt{resource\_path} & Optional; repository-relative, no \texttt{/} or \texttt{..} & Recommended starting context for a targeted task \\
\texttt{target\_path} & Optional; repository-relative, no \texttt{/} or \texttt{..} & Required deliverable when different from source; otherwise defaults to \texttt{resource\_path} \\
\texttt{repo}, \texttt{repos}, \texttt{branch}, \texttt{branch\_map} & Optional; approved catalogue only & Select repositories, source branches, and safe ticket target branch \\
\texttt{allowed\_directories}, \texttt{allowed\_directories\_map} & Optional; relative allowed paths & Server-enforced read/write scope; per-repository map overrides global scope \\
\texttt{read\_only} / \texttt{zero\_code\_modifications} & Optional Boolean & No branch or commit; baseline-only backtest when applicable \\
\texttt{allow\_iteration} & Optional Boolean & Enables repeated edit/evaluate passes; defaults to on for backtests and off otherwise \\
\texttt{max\_iterations}, \texttt{max\_failed\_iterations} & Optional; 1-10 & Completed passes and retryable failed attempts \\
\texttt{max\_agent\_turns} & Optional; 10-60 & Cap per agent/edit pass \\
\texttt{max\_commits\_per\_run} & Optional; 1-50 & GitHub commit cap per pass; deployment default is 5 when omitted \\
\texttt{timeout\_seconds} & Optional; 60-10800 & Whole-workflow budget \\
\texttt{max\_token\_budget\_per\_run} & Optional; 1000-1000000 & Hard run-wide model budget \\
\texttt{model} & Optional; \texttt{nova-micro}, \texttt{nova-pro}, \texttt{test-model}, \texttt{gpt-oss}, \texttt{qwen3-coder} & Validated model selection \\
\texttt{params} & Optional; comma-separated \texttt{KEY=value} pairs & Raw engine overrides; JSON object syntax is not supported \\
\texttt{start\_date}, \texttt{end\_date}, \texttt{stock\_type} & Optional; backtest only & Backtest window/universe overrides \\
\texttt{cpu\_vcpus}, \texttt{memory\_mb}, \texttt{gpu\_count} & Optional; 0.25-8; 512-16384; 0 only & Per-run resource request; GPUs unsupported \\
\texttt{execution\_timeout\_seconds}, \texttt{pids\_limit}, \texttt{yarn\_queue} & Optional; 60-10800; 64-1024; queue name & Runtime/YARN operational controls \\
\texttt{input\_hdfs\_uri} or \texttt{input\_attachment} & \textbf{Conditional:} one source only when analysing a dataset & Approved external input; supported formats CSV, JSON, XLSX, Parquet \\
\texttt{input\_mount\_path}, \texttt{input\_format} & Optional when an input source is selected & Read-only sandbox destination and declared/detected format \\
\bottomrule
\end{longtable}

## Common rejection and triage cases

| Symptom | Likely cause | Operator response |
|---|---|---|
| Validation comment, no sandbox run | Missing/invalid `strategy_type`, unsupported option, invalid numeric bound, invalid path, or bad repo/branch | Correct the ticket; do not attempt manual sandbox launch first |
| Path-scope contradiction | `resource_path`/`target_path` is outside `allowed_directories` | Narrow or correct the request intentionally; do not remove scopes merely to bypass it |
| GitHub identity/permission failure | Matrix row, PAT Connection, username, repository permission, or source branch mismatch | Fix the Connection/matrix and repeat the preflight identity check; never paste token values into Jira |
| `QUALITY_VALIDATION_FAILED` | Generated artifact failed deterministic or readback validation | Inspect structured diagnostics and branch/commit details; repair with a new controlled run |
| `performance_metrics: null` | Non-backtest workflow or backtest failed before metrics | Use `execution_summary.request_type` and diagnostics; null alone does not identify the cause |
| HDFS watcher saw request but no final result | Intake happened but completion/rejection has not been published | Check bridge heartbeat, local watcher, terminal HDFS result/rejection, and timeout; do not claim completion from intake alone |
| Payload validation after a source change | Gateway schemas/DAGs and sandbox image may be on different releases | Reconcile source commit, deployed DAG/schema version, image digest, and `:stable` promotion |

# Quality, assurance, and limits

## What is mechanically enforced

- The gateway validates Jira commands and the runtime request schema before
  sandbox launch; RAE validates the request again and validates final responses.
- The sandbox is designed for a non-root user with a read-only root filesystem,
  scoped writable tmpfs/mounts, dropped capabilities, and no-new-privileges.
- GitHub tools enforce repository branch/path restrictions and their operation
  limits. The editing prompt explicitly treats Jira content as untrusted, but
  enforcement is at the tool/validation layer rather than prompt text alone.
- Editing runs require `validate_content` before `commit_and_push`; the runtime
  then reads committed artifacts from the configured branch and reports what it
  actually found.
- Request/response contracts carry structured status, diagnostics, artifacts,
  repository branches, optional telemetry, and run correlation rather than
  relying on free-form logs.
- Run isolation uses fresh containers, globally unique run IDs, unique
  job/iteration names, and atomic final-result writes. Airflow remains
  responsible for serialising runs for the same ticket.
- Dependencies are hash-pinned for the sandbox image. Change them through the
  documented re-lock/rebuild workflow rather than editing the lockfile by hand.

## Multi-agent, review, and prompt assurances

The agentic editing/evaluation loop combines measured backtest criteria with an
advisory review for general repository tasks.

- Backtests are evaluated against requested criteria using engine metrics.
- Non-backtest tasks use a review agent to assess committed work against the
  ticket. Its recommendation is deliberately surfaced as **advisory**:
  `confidence` is `0.0` and there are no numerical criteria results.
- The backtest MCP policy hides redundant post-processing tools by default to
  narrow the LLM surface. Policy-application warnings are logged and should be
  monitored as part of normal runtime operations.
- Prompts require repository inspection before writing, content validation
  before committing, and accurate reporting of tool failures. These controls
  work alongside server-side restrictions, human code review, branch
  protection, and integration tests.

## Test and release expectations

For every compatible gateway/runtime release:

1. Run focused tests for changed gateway or runtime areas; run the relevant
   package test suite (`python -m pytest`) before release.
2. Verify duplicated request/response schemas remain byte-compatible where the
   integration expects them to be identical.
3. Build the sandbox image reproducibly from its pinned lockfile; record the
   resulting digest.
4. Run a sandbox health/introspection check and an offline/no-network smoke test
   before a real dependency test.
5. Perform a controlled read-only canary, then a real-engine canary only after
   access, HDFS bridge, storage, and rollback requirements pass.
6. Record test, image-digest, DAG/schema revision, and canary identifiers in
   the deployment record.

# Production configuration checklist

Complete this record as part of the deployment configuration. Each item has an
owner and a place to record the selected value or operational reference.

| Configuration item | Responsible role | Configuration / record | Done |
|---|---|---|---|
| Reviewed gateway commit installed in Airflow DAG folder | Airflow owner | Commit SHA and DAG import record | [ ] |
| Matching request/response schemas deployed beside DAGs | Airflow owner | Schema hash/version | [ ] |
| Immutable sandbox image digest selected and pull-tested | Runtime owner | GHCR digest and pull/run record | [ ] |
| `jira_cloud` service account tested for JQL, comments, and attachments | Jira owner | Account/permission test reference | [ ] |
| LiteLLM endpoint/connection tested without logging secrets | AI platform owner | Health/test reference | [ ] |
| GitHub access matrix reviewed; each requester uses the approved classic PAT scope set and is identity-verified | GitHub owner | Matrix and PAT-scope review record | [ ] |
| Branch protection confirmed for every catalogue repository | GitHub owner | Protection policy record | [ ] |
| HDFS NameNode, run root, queue, and input allow-list approved | Hadoop/YARN owner | Exact approved values | [ ] |
| Canonical simulation request/result roots writable only as required | Hadoop/YARN owner | ACL/path validation | [ ] |
| Bialobog bridge `--check` passed; cron and heartbeat monitored | Backtester owner | Check output and cron record | [ ] |
| HDFS retention remains `report` until reviewed | Storage owner | Current bridge mode | [ ] |
| Docker fallback policy and host mounts explicitly approved or disabled | Platform owner | Decision record | [ ] |
| Same-ticket concurrency prevention confirmed | Airflow owner | DAG/task concurrency configuration | [ ] |
| Read-only canary completed with terminal structured result and Jira write-back | Joint owners | Ticket key, run ID, result/artifact reference | [ ] |
| Real-backtester canary completed with verified terminal HDFS result | Joint owners | Ticket key, workflow/job ID, HDFS path | [ ] |
| Rollback owner and procedure rehearsed | Joint owners | Drill record | [ ] |

# Reference map

This document consolidates, but does not replace, the following source-of-truth
materials in this repository:

- `docs/quant_dev_loop_architecture_and_operational_sop.md` - component map,
  complete lifecycle sequence, operating procedures, and incident scenarios.
- `docs/quant_dev_loop_deployment_and_configuration_guide.md` - replication,
  platform preparation, configuration, permissions, and acceptance sequence.
- `README.md` - architecture, runtime contract overview, configuration summary.
- `jira-chatops-gateway/README.md` - command validation, repository selection,
  iterations, and Airflow workflow details.
- `jira-chatops-gateway/docs/airflow_ui_configuration.md` - Connections,
  Variables, user access matrix, Bialobog bridge, and deployment configuration.
- `jira-chatops-gateway/docs/runtime_payload_contract.md` - request/response
  contract, resource limits, output paths, inputs, and Jira artifact handling.
- `jira-chatops-gateway/docs/jira_ticket_examples.md` - parser-validated
  copy-paste ticket patterns and parameter semantics.
- `jira-chatops-gateway/docs/local_cron_dag_sync.md` - DAG/schema sync and
  sandbox-image promotion process.
- `rae_runtime/sandbox/docs/README.md` and `parallel_safety.md` - container
  posture, execution environment, and parallel-safety contract.
- `rae_runtime/mcp/TOOL_SURFACE.md` - exposed backtest MCP tools and their
  least-privilege policy.
