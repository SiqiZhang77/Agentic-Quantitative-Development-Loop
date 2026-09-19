---
title: "Quant Dev Loop: Deployment and Configuration Guide"
subtitle: "Fresh installation, service configuration, permissions, and replication checklist"
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
    \usepackage{pdflscape}
    \usepackage{xurl}
    \usepackage{longtable,booktabs,array}
---

# Purpose

This guide describes how a DevOps and operations team can reproduce the Quant
Dev Loop without student assistance. It covers platform prerequisites, Airflow
service-account preparation, repository and image delivery, Connections,
Variables, Docker/YARN, HDFS bridge, permissions, and acceptance checks.

It complements the [production handover](quant_dev_loop_production_handover.md)
and [architecture and operational SOP](quant_dev_loop_architecture_and_operational_sop.md).
No credential values belong in this document.

# Deployment values record

The following non-secret values were read from the active Bialobog installation
on 7 August 2026. Secrets remain in Airflow Connections and are intentionally
not reproduced. Use this record as the replication baseline and update the
owner column when responsibility changes.

```{=latex}
\begingroup\small
\begin{longtable}{@{}p{0.29\linewidth}p{0.44\linewidth}p{0.19\linewidth}@{}}
\toprule
\textbf{Setting} & \textbf{Selected deployment value} & \textbf{Owner} \\
\midrule
\endhead
Airflow node and executor & Bialobog; \path{LocalExecutor} & Airflow owner \\
Airflow version and Unix/service identity & Airflow 3.2.2; \path{masteruser:masteruser}; account is in Airflow and Docker groups & Platform owner \\
Airflow home, Python, DAG, schema, script, and log paths &
  \path{/opt/airflow3}; \path{/opt/airflow3/venv/bin/python};
  \path{/opt/airflow3/dags}; \path{/opt/airflow3/schemas};
  \path{/opt/airflow3/scripts}; \path{/opt/airflow3/logs_masteruser} & Airflow owner \\
Airflow API/UI and service-manager units & Port \path{8008}; \path{airflow.service}; \path{airflowscheduler.service}; \path{airflowdagprocessor.service}; \path{airflowtriggerer.service} & Platform owner \\
Quant DAGs & \path{jira_airflow_secret_probe}; \path{jira_quant_docker_orchestrator}; \path{docker_sandbox_runner}; all unpaused & Airflow owner \\
Repository branch and release commit & \path{main}; \path{992865069d957a6a5f4db7684a3f8d6dfbcb0b90}; clean sync worktree & Release owner \\
Repository sync path and cadence & \path{/opt/airflow3/dag-sync/repo}; every 30 minutes from the Airflow user's crontab & Release owner \\
Deployed request/response schema SHA-256 & Recorded immediately below this table & Release owner \\
Sandbox image tag and immutable digest & \path{ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:stable}; \path{sha256:7d74f5f957d4159889c9f43dad1216f275fdc1183072973ff5b1718344868a32} (Linux AMD64) & Runtime owner \\
Docker endpoint or YARN execution mode & \path{yarn}; Docker fallback enabled; local Docker URL unset & Platform owner \\
Jira site, Cloud ID, project key, service account & Jira Cloud; project \path{SCRUM}; credential in \path{jira_cloud} & Jira owner \\
LiteLLM base URL and default model & Endpoint in \path{litellm_default}; model Variable unset, so DAG fallback \path{nova-micro} applies & AI platform owner \\
GitHub organisation, sync credential, requester access matrix & \path{bankingscience}; sync resolves to \path{github_default}; 11 requester mappings and all referenced Connections present & GitHub owner \\
GHCR pull Connection & \path{ghcr_default} & GitHub/platform owner \\
NameNode URI, Hadoop home/config, YARN queue & \path{hdfs://bialobog:8020}; \path{/opt/hadoop} resolves to \path{/opt/hadoop-3.4.1}; \path{root.uarbi} & Hadoop/YARN owner \\
HDFS run root and allowed input roots & \path{/user/masteruser/quant-sandbox-runs}; input allow-list unset & Hadoop/YARN owner \\
Engine HDFS request/result roots &
  \path{hdfs:///quant-sandbox/simulation-requests};
  \path{hdfs:///quant-sandbox/simulation-results} & Backtester/storage owner \\
Bridge host/account, state path, cron owner &
  Bialobog; \path{masteruser}; \path{/home/masteruser/.quant-hdfs-bridge}; one-minute cron & Backtester owner \\
Watcher local request/result paths &
  \path{/home/masteruser/simulation-requests};
  \path{/home/masteruser/simulation-results} & Backtester owner \\
Shared Airflow recovery-state path & Variable unset; DAG fallback \path{/tmp/jira_quant_workflow_recovery} & Airflow/storage owner \\
\bottomrule
\end{longtable}
\endgroup
```

Exact deployed schema hashes:

```text
runtime_request.schema.json  3390e009a5dda562eb8322bcc9fbd402c3b0d12ff9f73023fe8c5800a938f27b
runtime_response.schema.json daa9351852bbcaf16c2d812091932f44235d4e7f3b684bf20b20586591e61b98
```

# Infrastructure prerequisites

## Platform and software

- Airflow with `airflow.sdk` support and the standard provider containing
  `TriggerDagRunOperator`.
- Python capable of installing `requests==2.32.3` and `jsonschema==4.26.0`
  alongside the approved Airflow constraints.
- Docker CLI and daemon access for Docker mode. The runner uses the CLI rather
  than Airflow's DockerOperator.
- For YARN mode: Hadoop and YARN clients, cluster configuration, the
  DistributedShell example JAR, HDFS access, and a queue grant.
- Git, Bash, cron or an approved systemd timer, and a writable Airflow log path.
- Network routes to Jira Cloud, GitHub API, GHCR, LiteLLM, and the configured
  Docker/Hadoop services.
- Bialobog access for the `masteruser` bridge cron and existing watcher paths.

## Storage and isolation

- Airflow DAG, schema, script, log, sync-worktree, and durable recovery paths.
- Per-run local temporary input/output for Docker or unique HDFS run paths for
  YARN.
- HDFS request/result roots for the real backtester and read-only allowed roots
  for external input datasets.
- Docker host request/result directories when shared-filesystem real mode or
  Docker fallback is used.
- Bridge state must survive script upgrades and service restarts.

# Fresh installation and replication

## Prepare the Airflow service account

If the cluster uses centrally managed identities, request an equivalent service
account and directory grants. For a locally managed Linux account, this is the
reference shape; substitute the approved account and group:

```bash
sudo useradd --system --home-dir /opt/airflow3 --create-home --shell /bin/bash airflow
sudo install -d -o airflow -g airflow -m 0750 \
  /opt/airflow3/dags /opt/airflow3/schemas /opt/airflow3/scripts \
  /opt/airflow3/logs /opt/airflow3/dag-sync
```

The service account requires:

- read/write access to its DAG, schema, script, log, sync, and recovery paths;
- read access to Hadoop configuration and execute access to Hadoop/YARN clients;
- permission to use the selected Docker endpoint or submit to the selected YARN
  queue;
- outbound network access to required APIs; and
- no interactive personal credentials.

Docker daemon access can be root-equivalent. Grant only the organisation's
approved local socket group or remote-daemon capability and record that choice.

Check the existing Airflow installation before adding gateway dependencies:

```bash
sudo -u airflow /opt/airflow3/venv/bin/airflow version
sudo -u airflow /opt/airflow3/venv/bin/python -c \
  "from airflow.providers.standard.operators.trigger_dagrun \
  import TriggerDagRunOperator; print('ok')"
```

Install gateway dependencies through the platform's Airflow constraints and
change-control process. The repository requirements are in
`jira-chatops-gateway/requirements.txt`; do not create a second unmanaged
Airflow environment.

## Bootstrap repository delivery

1. Obtain a reviewed checkout of this repository using the organisation's
   approved Git transport. Do not place a PAT in a command line or remote URL.
2. Install `sync_airflow_dags_from_git.sh` from
   `jira-chatops-gateway/scripts/` under `/opt/airflow3/scripts/`, owned by the
   Airflow service account and executable only by the intended operators.
3. Create the `github_default` or dedicated DAG-sync Connection before running
   the script.
4. Run the documented manual sync as the Airflow service account. It deploys
   DAGs, matching schemas, and the HDFS bridge without deleting unrelated DAGs.
5. Install the selected sync cadence or the organisation's equivalent systemd
   timer. The repository example runs every five minutes; Bialobog currently
   runs every 30 minutes. Keep its environment secret-free.

The complete command and cron example are in
[`local_cron_dag_sync.md`](../jira-chatops-gateway/docs/local_cron_dag_sync.md).

## Configure Airflow Connections

Create these through **Admin > Connections** or the approved secrets backend.

```{=latex}
\begingroup\small
\begin{longtable}{@{}p{0.21\linewidth}p{0.22\linewidth}p{0.49\linewidth}@{}}
\toprule
\textbf{Connection} & \textbf{Required} & \textbf{Fields and purpose} \\
\midrule
\endhead
\path{jira_cloud} & Yes unless overridden & Host: Jira API base; Login: service-account email; Password: API token. Scoped tokens use \path{https://api.atlassian.com/ex/jira/{cloudId}} \\
\path{litellm_default} & Yes unless overridden & Host: OpenAI-compatible base URL; Password: API key; optional Extra model \\
\path{github_default} & Conditional & Login and token for DAG sync, approved global operations, and explicit mappings only \\
Per-user GitHub Connections & Repository workflows & One Connection per requester; Login is GitHub username; Password is that user's approved classic PAT after encrypted-vault onboarding \\
GHCR Connection & Conditional & Pull-only registry identity/token when different from \path{github_default}; selected by \path{GHCR_CONNECTION_ID} \\
\bottomrule
\end{longtable}
\endgroup
```

Never store tokens in Variables, Jira, runtime payloads, examples, logs, or this
guide.

## Configure core Airflow Variables

The **DAG fallback** is the value used by the checked-in DAG when a Variable is
absent from Airflow. **Required?** is deliberately Yes or No: Yes means the
Variable must be present for Bialobog's active mode; capability-specific detail
is stated under Purpose. **Active Bialobog value** records the non-secret value
read on 7 August 2026. “Unset - DAG fallback” means the value is not stored in
Airflow and the adjacent fallback is effective.

```{=latex}
\begingroup\scriptsize
\begin{longtable}{@{}p{0.27\linewidth}p{0.12\linewidth}p{0.17\linewidth}p{0.19\linewidth}p{0.17\linewidth}@{}}
\toprule
\textbf{Variable} & \textbf{Required?} & \textbf{DAG fallback} & \textbf{Active Bialobog value} & \textbf{Purpose} \\
\midrule
\endhead
\path{JIRA_PROJECT_KEY} & Yes & None & \path{SCRUM} & Jira project to poll \\
\path{POLL_INTERVAL_MINUTES} & No & \path{5} & \path{1} & Schedule and comment lookback \\
\path{JIRA_CONNECTION_ID} & No & \path{jira_cloud} & \path{jira_cloud} & Jira Connection override \\
\path{LITELLM_CONNECTION_ID} & No & \path{litellm_default} & \path{litellm_default} & LiteLLM Connection override \\
\path{LITELLM_BASE_URL} & No & None & Connection Host & Base URL if Connection Host is absent \\
\path{LITELLM_MODEL} & No & \path{nova-micro} & Unset - DAG fallback & Default validated model \\
\path{GITHUB_CONNECTION_ID} & No & Unset & \path{github_default} & Approved global GitHub Connection \\
\path{QUANT_GITHUB_USER_ACCESS_MATRIX} & Yes & None & Configured JSON; 11 users & Required for Bialobog repository workflows; maps requester, PAT Connection, repository, and permission \\
\path{DAG_SYNC_GITHUB_CONNECTION_ID} & No & \path{GITHUB_CONNECTION_ID}, then \path{github_default} & Unset - resolves to \path{github_default} & Dedicated DAG-sync Connection \\
\path{GHCR_CONNECTION_ID} & No & Unset & \path{ghcr_default} & Dedicated image-pull Connection \\
\path{SANDBOX_IMAGE} & No & \path{ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:stable} & Stable tag at \path{sha256:7d74f5f957d4159889c9f43dad1216f275fdc1183072973ff5b1718344868a32} & Sandbox image; deploy by immutable digest \\
\path{SANDBOX_EXECUTION_MODE} & No & \path{docker} & \path{yarn} & Executor selection \\
\path{DOCKER_URL} & No & Unset & Unset & Remote Docker endpoint \\
\path{SANDBOX_CONTAINER_TIMEOUT_SECONDS} & No & \path{1800} & Unset - DAG fallback & Docker base cap; if explicitly set, it becomes the hard cap \\
\bottomrule
\end{longtable}
\endgroup
```

## Configure workflow and engine Variables

```{=latex}
\begingroup\scriptsize
\begin{longtable}{@{}p{0.27\linewidth}p{0.12\linewidth}p{0.17\linewidth}p{0.19\linewidth}p{0.17\linewidth}@{}}
\toprule
\textbf{Variable} & \textbf{Required?} & \textbf{DAG fallback} & \textbf{Active Bialobog value} & \textbf{Purpose} \\
\midrule
\endhead
\path{USE_REAL_BACKTESTER} & No & \path{false} & \path{true} & Real watcher rather than mock \\
\path{BACKTEST_STORAGE_KIND} & No & \path{shared_fs} & \path{hdfs} & Real-backtester storage type \\
\path{SIMULATION_REQUESTS_DIR} & Yes & None & \path{hdfs:///quant-sandbox/simulation-requests} & Required by active real-backtester mode; engine request URI \\
\path{SIMULATION_RESULTS_DIR} & Yes & None & \path{hdfs:///quant-sandbox/simulation-results} & Required by active real-backtester mode; engine result URI \\
\path{BACKTEST_RESULT_TIMEOUT_SECONDS} & No & Request/runtime timeout & \path{5400} & Real-result polling timeout \\
\path{WORKFLOW_TASK_TIMEOUT_SECONDS} & No & \path{11100} & Unset - DAG fallback & Airflow runner task cap \\
\path{WORKFLOW_RECOVERY_STATE_DIR} & No & \path{/tmp/jira_quant_workflow_recovery} & Unset - DAG fallback & Retry/idempotency state; use shared durable storage if runners span hosts \\
\path{WORKFLOW_RETRY_MAX_ATTEMPTS} & No & \path{3} & \path{1} & Safe-stage retry count \\
\path{WORKFLOW_RETRY_INITIAL_DELAY_SECONDS} & No & \path{60} & Unset - DAG fallback & Initial retry delay \\
\path{WORKFLOW_RETRY_MAX_DELAY_SECONDS} & No & \path{300} & Unset - DAG fallback & Maximum retry delay \\
\path{WORKFLOW_RETRY_BACKOFF_MULTIPLIER} & No & \path{2} & Unset - DAG fallback & Delay multiplier \\
\path{WORKFLOW_RETRY_JITTER_RATIO} & No & \path{0.2} & Unset - DAG fallback & Delay jitter \\
\path{WORKFLOW_ENABLE_DOCKER_FALLBACK} & No & \path{false} & \path{true} & Optional YARN-to-Docker fallback \\
\bottomrule
\end{longtable}
\endgroup
```

## Configure YARN, HDFS, and input Variables

```{=latex}
\begingroup\scriptsize
\begin{longtable}{@{}p{0.27\linewidth}p{0.12\linewidth}p{0.17\linewidth}p{0.19\linewidth}p{0.17\linewidth}@{}}
\toprule
\textbf{Variable} & \textbf{Required?} & \textbf{DAG fallback} & \textbf{Active Bialobog value} & \textbf{Purpose} \\
\midrule
\endhead
\path{HADOOP_HOME} & No & \path{/opt/hadoop} & Unset - fallback resolves to \path{/opt/hadoop-3.4.1} & Hadoop installation \\
\path{HADOOP_CONF_DIR} & No & \path{/opt/hadoop/etc/hadoop} & Unset - DAG fallback & Cluster configuration \\
\path{SANDBOX_HDFS_NAMENODE_URI} & Yes & None & \path{hdfs://bialobog:8020} & Required by active YARN mode; explicit NameNode URI \\
\path{SANDBOX_HDFS_RUN_ROOT} & Yes & None & \path{/user/masteruser/quant-sandbox-runs} & Required by active YARN mode; per-run staging root \\
\path{SANDBOX_YARN_QUEUE} & Yes & None & \path{root.uarbi} & Required by active YARN mode; submission queue \\
\path{SANDBOX_YARN_MASTER_MEMORY_MB} & No & \path{512} & \path{512} & DistributedShell master memory \\
\path{SANDBOX_YARN_CONTAINER_MEMORY_MB} & No & \path{4096} & \path{2048} & Worker memory \\
\path{SANDBOX_YARN_TIMEOUT_SECONDS} & No & \path{2100} & \path{2100} & YARN execution timeout \\
\path{SANDBOX_HDFS_SAFE_MODE_WAIT_SECONDS} & No & \path{60} & Unset - DAG fallback & Safe-mode retry window \\
\path{SANDBOX_YARN_CLEANUP_HDFS} & No & \path{false} & \path{false} & Temporary run cleanup \\
\path{SANDBOX_INPUT_HDFS_ALLOWED_ROOTS} & No & None & Unset & Required only to enable HDFS input-source requests; read-only allow-list \\
\path{SANDBOX_INPUT_MAX_FILE_BYTES} & No & \path{104857600} & Unset - DAG fallback & Single input limit, 100 MiB \\
\path{SANDBOX_INPUT_MAX_TOTAL_BYTES} & No & \path{262144000} & Unset - DAG fallback & Combined input limit, 250 MiB \\
\bottomrule
\end{longtable}
\endgroup
```

The full parsing rules and user-access matrix format remain in
[`airflow_ui_configuration.md`](../jira-chatops-gateway/docs/airflow_ui_configuration.md).

## Configure requester-scoped GitHub access

For each enabled Jira requester:

1. Add a PAT column for the requester to the repository-access spreadsheet.
2. In GitHub, go to **Settings > Developer settings > Personal access tokens >
   Tokens (classic)** and select **Generate new token (classic)**.
3. Name the token `QuantDevLoop` and set the expiry date for as long as
   possible.
4. Select exactly these scopes:
   - `repo` - Full control of private repositories;
   - `repo:status` - Access commit status;
   - `repo_deployment` - Access deployment status;
   - `public_repo` - Access public repositories;
   - `repo:invite` - Access repository invitations;
   - `security_events` - Read and write security events;
   - `workflow` - Update GitHub Action workflows;
   - `write:packages` - Upload packages to GitHub Package Registry; and
   - `read:packages` - Download packages from GitHub Package Registry.
5. Select **Generate token**, copy it immediately, and add it only to the PAT
   column of the repository-access spreadsheet.
6. Store the submitted token securely in the cluster's encrypted vault and
   associate it with the requester's Jira account through the requester's
   dedicated Airflow Connection. Never place the token itself in an Airflow
   Variable.
7. Add a non-secret `QUANT_GITHUB_USER_ACCESS_MATRIX` row with Jira identity,
   GitHub username, Connection ID, authorised repositories, and `read` or
   `write` permission.
8. Run a read-only identity/repository check before permitting code generation.

The Variable contains JSON metadata only. A minimal write-enabled row is:

```json
{
  "users": [
    {
      "full_name": "Example Maintainer",
      "jira_email": "maintainer@example.edu",
      "github_username": "example-maintainer",
      "github_connection_id": "github_pat_example_maintainer",
      "authorized_repositories": [
        "bankingscience/BSLAgenticQuantDevLoop"
      ],
      "permission_level": "write"
    }
  ]
}
```

- Match a user by `jira_email` when available. If Jira omits email addresses,
  use `jira_account_id` or an explicit `jira_display_aliases` entry.
- `github_username`, `github_connection_id`, `authorized_repositories`, and
  `permission_level` are required for a usable repository mapping.
- `permission_level: read` permits only explicitly read-only requests;
  `permission_level: write` permits scoped editing and backtesting.
- The token is a classic PAT with the exact scope set above. The PAT is the
  encrypted secret behind the named Airflow Connection. Never place it in this
  JSON Variable, Jira, logs, runtime payloads, or documentation.
- The classic PAT's GitHub scopes are broader than a single Quant Dev Loop
  request. The access matrix, repository catalogue, ticket branch, operation
  policy, and allowed-directory checks enforce the narrower runtime scope.

Targets remain `quant/<ticket>` or an allowed descendant. Source branches are
`main` for BSLAgenticQuantDevLoop and `develop` for the ATP catalogue unless
`branch_map` selects another readable source.

## Build and promote the sandbox image

The Dockerfile uses a digest-pinned Python base image, a non-root runtime user,
and a hash-pinned dependency lock. Build changes through the repository CI.

1. Run gateway and runtime tests.
2. Build and publish the release image.
3. Use the **Promote to Stable** GitHub Actions workflow.
4. Record the promoted immutable digest.
5. Pull the image as the Docker or YARN execution identity.
6. Run the no-network hardened-container smoke before external integration.

## Configure Docker mode

The Airflow service account must be able to run the Docker CLI against the
selected endpoint. The runner supplies:

- `--pull always`, `--rm`, CPU, memory, and PID bounds;
- `--read-only`, `--cap-drop ALL`, and
  `--security-opt no-new-privileges`;
- tmpfs for `/tmp` and `/workspace`;
- read-only input and writable output mounts; and
- real backtester request/result mounts only when enabled.

Check `docker info`, an authenticated image pull, and an offline sandbox run as
the Airflow identity. For real shared-filesystem mode, create and permission the
host request/result paths before setting `USE_REAL_BACKTESTER=true`.

## Configure YARN/HDFS mode

1. Confirm the Airflow identity can run `hdfs dfs` and `yarn` with the selected
   Hadoop configuration.
2. Grant submission to `SANDBOX_YARN_QUEUE` and the permitted resource limits.
3. Grant create/read/write on `SANDBOX_HDFS_RUN_ROOT`.
4. Grant read-only access to approved input roots.
5. Grant the engine identities only the required request/result root access.
6. Confirm the DistributedShell JAR is installed and discoverable.
7. Submit one offline sandbox and retrieve its `result.json` from the HDFS run
   output before enabling the real backtester.

## Install the Bialobog HDFS bridge

The bridge runs as `masteruser`, because the watcher observes local directories
under `/home/masteruser`.

Reference environment:

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

Run the read-only check before cron:

```bash
/opt/airflow3/venv/bin/python \
  /opt/airflow3/scripts/hdfs_simulation_service_bridge.py --check
```

Preserve the current script, crontab, and state directory before replacement.
Install the documented one-minute cron only after `--check`. Start HDFS
retention in `report`; change to `delete` only through the storage owner's
reviewed procedure.

## Deploy and enable Airflow

1. Sync `jira-chatops-gateway/dags/` and `schemas/` from the same release.
2. Confirm `adf_utils/` and the bridge script were installed.
3. Check DAG import errors and confirm these DAGs are visible:
   `jira_airflow_secret_probe`, `docker_sandbox_runner`, and
   `jira_quant_docker_orchestrator`.
4. Run the secret probe and inspect masked status.
5. Unpause the runner, then the orchestrator.
6. Execute the read-only SOP from the operational guide.
7. Enable real backtesting only after Docker/YARN, storage, bridge, watcher, and
   rollback checks are complete.

# Permissions and access matrix

\begingroup\scriptsize
\begin{longtable}{@{}p{0.16\linewidth}p{0.21\linewidth}p{0.27\linewidth}p{0.27\linewidth}@{}}
\toprule
\textbf{Identity/service} & \textbf{Resource} & \textbf{Required access} & \textbf{Validation} \\
\midrule
\endhead
Jira end user & Jira issue & Browse issue, add comment, upload selected input attachment where policy permits & Post a non-secret test comment/attachment \\
Jira service account & Jira project/API & JQL search, browse issues, read comments/attachments, create comments, upload attachments & \texttt{/myself}, JQL, issue browse, comment, attachment checks \\
Airflow administrator & Airflow UI/secrets backend & Manage DAG state, Connections, Variables, logs, and manual probe runs & Create/update one masked test setting and run probe \\
Airflow Unix account & Airflow paths & Read/write DAG, schema, script, log, sync, temporary and recovery locations & \texttt{id}, path read/write checks, DAG import \\
DAG sync identity & GitHub repository & Read repository and selected branch; no source write required & Clone/fetch through installed sync script \\
GHCR pull identity & GHCR package & Read packages for sandbox image & Authenticated pull and digest inspection \\
Requester classic PAT & GitHub repositories, workflows, security events, and packages & Approved scopes: \texttt{repo}, \texttt{repo:status}, \texttt{repo\_deployment}, \texttt{public\_repo}, \texttt{repo:invite}, \texttt{security\_events}, \texttt{workflow}, \texttt{write:packages}, and \texttt{read:packages}; application policy separately enforces read/write, repository, branch, and path scope & GitHub \texttt{/user}, repository/source-ref check, read-only request, then scoped test commit for write-enabled users \\
Airflow Docker identity & Docker daemon & Pull image, create/run/remove bounded containers, configured mounts and network & \texttt{docker info}, pull, offline smoke \\
Airflow/YARN identity & YARN queue & Submit/query/terminate its applications within queue/resource policy & Controlled DistributedShell job \\
Airflow/YARN identity & HDFS run root & Create per-run dirs; write request/env/output; read results; optional cleanup & Unique run-directory round trip \\
Input materialiser & Allowed HDFS input roots & Read only & Stage an approved canary input; reject outside root \\
RAE sandbox & LiteLLM & Network and authenticated model entitlement & Bounded test inference \\
RAE sandbox & GitHub API & Access only through requester PAT and approved repositories/refs & Runtime identity check and tool audit \\
Bridge account & HDFS engine roots & Read requests; publish results; retention actions only in configured mode & Bridge \texttt{--check} and canary \\
Bridge account & Local watcher paths/state & Write request inbox; read terminal output; write state/heartbeat; reviewed cleanup & Heartbeat, permissions, atomic transfer \\
Watcher/backtester & Local request/result paths & Read delivered requests and publish terminal result/rejection & Unique canary becomes terminal \\
LiteLLM credential & LiteLLM endpoint/models & Invoke only approved models within service policy & Endpoint/model health test \\
\bottomrule
\end{longtable}
\endgroup

# Validation and acceptance

## Static and offline validation

1. Run `python3 -m pytest` in `jira-chatops-gateway`.
2. Run `python3 -m pytest` in `rae_runtime`.
3. Confirm duplicated runtime schemas match.
4. Confirm all DAGs import in the target Airflow environment.
5. Run `jira_airflow_secret_probe` and inspect masked output.
6. Run the sandbox no-network smoke with hardened flags.
7. Run bridge `--check` in YARN real-mode deployments.

## Replication acceptance sequence

The receiving team completes this sequence unaided:

1. Populate the deployment values record and permissions matrix.
2. Recreate or restore the Airflow paths, service account, dependencies, and
   repository sync.
3. Recreate Connections and Variables without copying plaintext secrets into
   commands or documentation.
4. Pull the recorded image digest and deploy matching DAG/schema revisions.
5. Complete Docker or YARN/HDFS smoke checks.
6. Complete bridge/watcher checks when real mode is used.
7. Run the operational guide's read-only baseline SOP.
8. Run the scoped code-generation SOP with a dedicated ticket branch.
9. Trace each run across Jira, Airflow, runtime result, GitHub where applicable,
   and HDFS/backtester state.
10. Perform a pause, compatible rollback, restart, and repeat the read-only SOP.

**Configuration change rule.** When behaviour or required settings change,
update the code, tests, relevant
README, `airflow_ui_configuration.md`, this guide, the operational SOP, and Jira
examples in the same reviewed change. Deploy DAGs, schemas, and the sandbox
image as one compatible release.
