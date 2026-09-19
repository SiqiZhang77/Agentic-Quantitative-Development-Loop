---
title: "Quant Dev Loop: Architecture and Operational SOP"
subtitle: "Component map, request lifecycle, operating procedures, and recovery guide"
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
    \usetikzlibrary{arrows.meta,positioning,fit}
  - |
    \usepackage{pdflscape}
    \usepackage{float}
  - |
    \definecolor{flowblue}{HTML}{1F4E79}
    \definecolor{flowteal}{HTML}{0F766E}
---

# Purpose and use

This is the operational companion to the
[production handover](quant_dev_loop_production_handover.md). It explains where
the components live, what each component owns, how a request moves through the
system, and how an operator starts, stops, recovers, and troubleshoots it.

Use the [deployment and configuration guide](quant_dev_loop_deployment_and_configuration_guide.md)
for installation, service accounts, Connections, Variables, environment
settings, and permissions.

# System architecture

## End-to-end topology

Airflow owns Jira polling, gateway validation, credential resolution, sandbox
launch, recovery, and Jira write-back. Each accepted request receives one fresh
RAE container. RAE owns repository work, model calls, MCP evaluation, bounded
iteration, and the structured terminal result.

```{=latex}
\begin{figure}[H]
\centering
\begin{tikzpicture}[
  box/.style={draw, rounded corners=2pt, align=center, minimum height=10mm,
              text width=29mm, font=\scriptsize},
  outer/.style={box, draw=flowblue, fill=blue!7},
  inner/.style={box, draw=flowteal, fill=teal!8},
  store/.style={box, draw=flowblue, fill=blue!3},
  arrow/.style={-{Stealth[length=2mm]}, thick, draw=flowblue},
  rt/.style={-{Stealth[length=2mm]}, thick, draw=flowteal}
]
  \node[outer] (jira) at (-54mm,32mm) {\textbf{Jira Cloud}\\request, attachment,\\ADF result};
  \node[outer] (airflow) at (0,32mm) {\textbf{Airflow gateway}\\poll, validate,\\launch, recover};
  \node[outer] (executor) at (54mm,32mm) {\textbf{Docker or YARN}\\fresh sandbox per run};
  \node[inner] (rae) at (0,7mm) {\textbf{RAE runtime}\\route task and run bounded loop};
  \node[inner] (llm) at (-57mm,-18mm) {\textbf{LiteLLM}\\bounded inference};
  \node[inner] (githubmcp) at (-19mm,-18mm) {\textbf{GitHub MCP}\\scoped read,\\validate, commit};
  \node[store] (github) at (-19mm,-43mm) {\textbf{GitHub/GHCR}\\source, ticket branch,\\image};
  \node[inner] (backmcp) at (19mm,-18mm) {\textbf{Backtest MCP}\\submit, poll,\\parse, evaluate};
  \node[store] (hdfs) at (19mm,-43mm) {\textbf{HDFS/shared storage}\\requests, results,\\run output};
  \node[outer] (bridge) at (57mm,-18mm) {\textbf{HDFS bridge}\\atomic HDFS/local\\transfer};
  \node[outer] (engine) at (57mm,-43mm) {\textbf{Watcher/backtester}\\terminal result\\or rejection};
  \draw[arrow] (jira) -- (airflow);
  \draw[arrow] (airflow) -- (executor);
  \draw[arrow] (executor) -- (rae);
  \draw[rt] (rae) -- (llm);
  \draw[rt] (rae) -- (githubmcp);
  \draw[rt] (githubmcp) -- (github);
  \draw[rt] (rae) -- (backmcp);
  \draw[rt] (backmcp) -- (hdfs);
  \draw[arrow] (hdfs) -- (bridge);
  \draw[arrow] (bridge) -- (engine);
  \draw[arrow] (engine.east) to[out=0,in=0,looseness=1.35] (bridge.east);
  \draw[arrow] (executor.north) to[out=90,in=90,looseness=1.2] node[above,font=\scriptsize]{result} (airflow.north);
  \draw[arrow] (airflow.north) to[out=90,in=90,looseness=1.2] node[above,font=\scriptsize]{ADF write-back} (jira.north);
\end{tikzpicture}
\caption{Quant Dev Loop service topology and primary dependencies.}
\end{figure}
```

## Component catalogue

```{=latex}
\begingroup\small
```

- **Jira Cloud** - Integration helpers are in `jira_quant_common.py`. Jira owns
  the issue, requester identity, command, selected attachments, progress, final
  report, and uploaded artefacts. Check `jira_cloud`, JQL visibility, and the
  issue/comment/attachment APIs.
- **Polling DAG** - `jira_quant_docker_orchestrator.py` polls at
  `POLL_INTERVAL_MINUTES`, ignores bot output, groups by issue, and triggers the
  runner. Check DAG import, pause state, interval, and project.
- **Runner DAG** - `docker_sandbox_runner.py` validates, resolves access and
  settings, stages input, launches Docker/YARN, ingests output, recovers safe
  stages, and writes back once. Check selected mode, workflow ID, and terminal
  structured result in its logs.
- **Gateway helpers** - `jira_command_validation.py`, `jira_quant_common.py`,
  `repository_catalog.py`, and `adf_utils/` own validation, configuration,
  identity mapping, repository selection, Jira calls, and ADF formatting. Check
  gateway tests and the masked secret probe.
- **Runtime schemas** - Gateway and sandbox `schemas/` define the request,
  response, and progress contracts. Deploy both copies from one release and run
  schema-sync tests.
- **Docker/YARN executor** - Creates one resource-bounded container, delivers
  stdin and input, and retrieves output. Check Docker access or YARN queue/HDFS
  access as the Airflow identity.
- **RAE sandbox** - `rae_runtime/sandbox/` revalidates, routes the task, runs
  bounded iterations, emits progress, and atomically writes `result.json`.
  Check non-root execution and the hardened container flags.
- **LiteLLM** - Proxy clients under `rae_runtime/proxy/` perform bounded model
  inference. Check `litellm_default` and endpoint/model reachability.
- **GitHub MCP** - `github_mcp_server.py` and `github_client.py` enforce the
  repository, branch, path, operation, and commit limits. Check PAT identity,
  source ref, ticket branch, validation, and committed-content readback.
- **GitHub/GHCR** - Store source, ticket branches, CI, and the sandbox image.
  Check repository/ref API access and image pull by digest.
- **Backtest MCP** - `rae_runtime/mcp/server.py`, `engine_adapter.py`, and
  `tool_policy.json` own submission, polling, metrics, artefacts, and tool
  policy. Check stdio startup and a controlled terminal baseline.
- **HDFS storage and bridge** - Per-run and engine roots hold requests/results;
  `hdfs_simulation_service_bridge.py` transfers them to/from the local watcher.
  Check unique paths, `--check`, heartbeat, cron, thresholds, and retention.
- **Bialobog watcher/backtester** - Consumes local `.request` files and publishes
  terminal results or rejections. Check that its paths match the bridge and a
  unique canary becomes terminal.
- **Recovery state** - `WORKFLOW_RECOVERY_STATE_DIR` prevents duplicate sandbox
  work and Jira completion comments. It must be shared and writable by every
  retry-capable Airflow worker.

```{=latex}
\endgroup
```

## Service interaction and dependency flow

| Caller | Dependency | Interface and data | Owning role |
|---|---|---|---|
| Jira user | Jira Cloud | Comment and selected attachment | Jira owner |
| Polling DAG | Jira Cloud | HTTPS JQL, comments, attachment metadata | Jira/Airflow |
| Runner DAG | GitHub API | PAT identity, repository, source ref | GitHub |
| Runner DAG | Docker or YARN/HDFS | CLI launch, request, mounts or HDFS run directory | Platform/Hadoop |
| RAE | LiteLLM | OpenAI-compatible authenticated calls | AI platform |
| GitHub MCP | GitHub API | Contents and ticket-branch commits | GitHub/runtime |
| RAE | Backtest MCP | Local stdio protocol | Runtime |
| Backtest MCP | HDFS/shared paths | Atomic engine request and terminal result polling | Runtime/backtester |
| Bridge | HDFS and local watcher | Atomic copy, checksum, heartbeat, retention | Backtester/storage |
| Runner DAG | Jira Cloud | Progress, ADF result, attachments | Jira/Airflow |

```{=latex}
\begin{landscape}
\section{Complete request lifecycle}
The sequence shows the real YARN/HDFS backtester route. Docker shared-filesystem
mode bypasses HDFS and the bridge. Read-only analysis omits GitHub writes.
Non-backtest work uses review instead of engine metrics.
\begin{center}
\begin{tikzpicture}[yscale=0.78,
  actor/.style={draw, rounded corners=1.5pt, fill=blue!7, draw=flowblue,
                text width=21mm, minimum height=8mm, align=center, font=\tiny\bfseries},
  life/.style={densely dashed, draw=black!45},
  msg/.style={-{Stealth[length=1.5mm]}, draw=flowblue, line width=0.45pt},
  opt/.style={-{Stealth[length=1.5mm]}, draw=flowteal, dashed, line width=0.45pt},
  label/.style={font=\fontsize{5.5}{6.2}\selectfont, fill=white, inner sep=1pt}
]
  \foreach \x/\name/\title in {0/jira/Jira,30/airflow/Airflow,60/exec/Docker--YARN,90/rae/RAE,120/llm/LiteLLM,150/github/GitHub,180/mcp/Backtest MCP,210/bridge/HDFS bridge,240/engine/Backtester}
    {\node[actor] (\name) at (\x mm,0) {\title}; \draw[life] (\x mm,-5mm) -- (\x mm,-151mm);}
  \draw[msg] (jira |- 0,-10mm) -- node[label,above]{1. /quant + attachment} (airflow |- 0,-10mm);
  \draw[msg] (airflow |- 0,-19mm) -- ++(13mm,0) -- ++(0,-5mm) -- node[label,below]{2. validate identity, schema, scope} ++(-13mm,0);
  \draw[msg] (airflow |- 0,-31mm) -- node[label,above]{3. verify PAT, repo, ref} (github |- 0,-31mm);
  \draw[msg] (airflow |- 0,-40mm) -- node[label,above]{4. launch request} (exec |- 0,-40mm);
  \draw[msg] (exec |- 0,-49mm) -- node[label,above]{5. stdin + read-only input} (rae |- 0,-49mm);
  \draw[msg] (rae |- 0,-58mm) -- node[label,above]{6. bounded inference} (llm |- 0,-58mm);
  \draw[opt] (rae |- 0,-67mm) -- node[label,above]{7. inspect/edit/validate/commit} (github |- 0,-67mm);
  \draw[msg] (rae |- 0,-76mm) -- node[label,above]{8. evaluate or submit} (mcp |- 0,-76mm);
  \draw[opt] (mcp |- 0,-85mm) -- node[label,above]{9. unique HDFS request} (bridge |- 0,-85mm);
  \draw[opt] (bridge |- 0,-94mm) -- node[label,above]{10. atomic local delivery} (engine |- 0,-94mm);
  \draw[opt] (engine |- 0,-103mm) -- node[label,above]{11. terminal result/rejection} (bridge |- 0,-103mm);
  \draw[opt] (bridge |- 0,-112mm) -- node[label,above]{12. publish HDFS result} (mcp |- 0,-112mm);
  \draw[msg] (mcp |- 0,-121mm) -- node[label,above]{13. metrics, artefacts, verdict} (rae |- 0,-121mm);
  \draw[opt] (rae |- 0,-130mm) -- node[label,above]{14. bounded iteration} (github |- 0,-130mm);
  \draw[msg] (rae |- 0,-139mm) -- node[label,above]{15. result.json + progress JSONL} (exec |- 0,-139mm);
  \draw[msg] (exec |- 0,-148mm) -- node[label,above]{16. result and diagnostics} (airflow |- 0,-148mm);
  \draw[msg] (airflow |- 0,-157mm) -- node[label,above]{17. ADF + attachments} (jira |- 0,-157mm);
\end{tikzpicture}
\par\small Figure 2: Complete request lifecycle. Teal dashed messages are conditional paths.
\end{center}
\end{landscape}
```

## Contract and data boundaries

- Jira text is untrusted. Permissions come from Connections, Variables,
  catalogue entries, and server-side validation.
- Airflow builds the schema-versioned request. Secrets are injected as process
  environment and are not stored in request JSON.
- RAE reads stdin and writes `output_paths.result_path`, normally
  `/workspace/output/result.json`. Progress is separate JSONL.
- GitHub writes stay on the approved repository and `quant/<Jira-key>` branch
  or descendant. Committed content is read back before success is reported.
- HDFS run paths are unique. Canonical engine paths are
  `hdfs:///quant-sandbox/simulation-requests` and
  `hdfs:///quant-sandbox/simulation-results`.
- Airflow uploads safe artefacts and posts the final Jira ADF report.

# Standard operating procedure

## Pre-run checks

1. Confirm the three Quant Dev Loop DAGs import and the intended DAGs are
   unpaused.
2. Run `jira_airflow_secret_probe`; inspect only masked presence/status.
3. Confirm the sandbox image digest can be pulled by the execution identity.
4. For Docker, run `docker info` as the Airflow service account and check real
   backtester host directories when enabled.
5. For YARN, check the NameNode, run root, queue, and approved engine/input roots.
6. On Bialobog, run bridge `--check`, inspect `heartbeat.json`, and match local
   watcher paths to the bridge environment.
7. Confirm the Jira requester has a current access-matrix row and a dedicated
   Connection backed by the approved classic PAT configuration: `repo`,
   `repo:status`, `repo_deployment`, `public_repo`, `repo:invite`,
   `security_events`, `workflow`, `write:packages`, and `read:packages`.

## SOP 1 - read-only baseline backtest

**Purpose:** exercise request, sandbox, engine, result, and Jira write-back
without changing or committing code.

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

1. Record Jira key and comment ID.
2. Confirm the polling DAG creates one ticket trigger.
3. Confirm the runner posts a running milestone and selects the intended mode.
4. Confirm RAE reports that edit and commit are skipped.
5. In real mode, confirm the unique request becomes terminal or rejected.
6. Confirm `result.json` validates and contains no feature-branch commit.
7. Confirm Jira receives one terminal ADF report and safe artefacts.

Completion means one structured result, one Jira report, no GitHub commit, one
unique engine job in real mode, and no duplicate completion comment after retry.

## SOP 2 - scoped code generation

**Purpose:** exercise requester-scoped GitHub inspection, deterministic
validation, ticket-branch commit, artefact readback, and Jira reporting.

```text
/quant
Create a self-contained Python standard-library performance-metrics module and
companion unit tests. Compute cumulative return, annualised return, annualised
volatility, Sharpe ratio, and maximum drawdown. Handle empty and zero-volatility
inputs and commit the implementation and tests.
strategy_type: refactor
repo: bankingscience/BSLAgenticQuantDevLoop
resource_path: rae_runtime/proxy/backtest_metrics.py
allowed_directories: rae_runtime/proxy
model: qwen3-coder
allow_iteration: false
max_agent_turns: 60
max_commits_per_run: 5
max_token_budget_per_run: 400000
```

1. Confirm the requester maps to the intended GitHub identity and the dedicated
   classic PAT Connection; the matrix permission level must allow writes.
2. Confirm RAE creates or reuses only `quant/<Jira-key>`.
3. Confirm repository context is read before writing and deterministic content
   validation occurs before `commit_and_push`.
4. Confirm all writes remain inside `rae_runtime/proxy`.
5. Confirm the commit exists and reported artefacts match readback.
6. Complete the normal human diff/test review; the review-agent verdict is an
   operational aid rather than a substitute for repository review.
7. Confirm Jira reports branch, SHA, changed files, diagnostics, and summary.

The existing worked trace is
[SCRUM-108](https://bankingscience-studentsproject.atlassian.net/browse/SCRUM-108)
with its [quant/SCRUM-108 branch](https://github.com/bankingscience/BSLAgenticQuantDevLoop/tree/quant/SCRUM-108).

# Service lifecycle

## Startup order

1. Confirm Jira, GitHub/GHCR, LiteLLM, Docker or YARN/HDFS, and watcher health.
2. Mount and check Airflow DAG/schema/script/log and recovery directories.
3. Confirm the selected image digest.
4. For YARN real mode, run bridge `--check`, enable its cron, and confirm heartbeat.
5. Deploy matching DAGs and schemas and check Airflow imports.
6. Run the secret probe and offline sandbox smoke.
7. Unpause `docker_sandbox_runner`, then `jira_quant_docker_orchestrator`.
8. Run SOP 1 before normal traffic.

## Planned shutdown

1. Pause `jira_quant_docker_orchestrator` to stop new intake.
2. Let active runner DAGs become terminal or record any intentional stop.
3. Pause `docker_sandbox_runner` before changing release/configuration.
4. Before bridge maintenance, confirm no active engine requests; disable only
   its cron entry and preserve the state directory.
5. Use the platform team's registered service-manager procedure for Airflow,
   Docker/YARN, and watcher processes.
6. Restart in the documented order and inspect recovery state before resubmission.

## Recovery by observed stage

| Last observed stage | Safe recovery |
|---|---|
| Validation comment; no sandbox | Correct request or access/configuration, then post a new `/quant` comment |
| Running milestone; no launch | Correct image, Docker/YARN, queue, HDFS, or credential configuration and rerun |
| Executor launched; no result | Preserve run paths; inspect progress JSONL and attached failure log before retry |
| GitHub commit exists; evaluation failed | Inspect ticket branch/commit; a new controlled run must report its actual new changes |
| Engine consumed request; no terminal result | Inspect bridge heartbeat, watcher, rejection/result path, and timeout; avoid duplicate job submission |
| Result exists; Jira write-back failed | Retry write-back using saved recovery state; do not rerun RAE only to recreate a comment |
| Terminal report exists; Airflow retries | Return saved result and suppress duplicate completion comments |

## Rollback

1. Pause intake and allow active work to finish where practical.
2. Restore the last compatible DAG and schema release together.
3. Restore the recorded prior sandbox image digest.
4. Revert only changed Variables; rotate Connections only when required.
5. Restore saved bridge script/crontab and preserve
   `/home/masteruser/.quant-hdfs-bridge`.
6. Run import checks, secret probe, bridge `--check`, and SOP 1 before reopening.

# Troubleshooting scenarios

| Scenario | Checks in order | Corrective action |
|---|---|---|
| Jira comment not detected | DAG, interval, project JQL, account visibility, timestamp, `/quant` marker | Correct Connection/project/polling, then post a new comment |
| Command rejected | `strategy_type`, ranges, repo alias, branch/path scope, attachment | Correct ticket; never bypass gateway validation |
| GitHub verification fails | Matrix identity, Connection, PAT, `/user`, repo, source branch | Correct PAT/matrix and repeat verification |
| GHCR pull fails | Connection, package-read permission, image/digest, worker network | Correct pull access/reference and test as execution identity |
| LiteLLM fails | Host/key presence, model, network, health, budget | Restore endpoint/model access and rerun bounded request |
| Docker fails before RAE | Daemon, mounts, permissions, resource/security flags | Correct platform setting; run hardened offline smoke |
| YARN submission fails | Hadoop config, NameNode, DistributedShell, queue ACL, resources, safe mode | Correct platform grant/configuration; use fallback only when enabled |
| Schema failure after release | DAG commit, gateway schema, sandbox digest, schema copies | Redeploy one compatible DAG/schema/image release |
| Engine request not terminal | Job name, bridge heartbeat, watcher, rejection/result, timeout | Recover existing job/result; avoid duplicate submission |
| Bridge intake paused | Heartbeat, free space, cleanup candidates, checksum state | Restore space using reviewed bridge cleanup procedure |
| Artefact missing from Jira | Output path, size/safety result, ingestion section | Correct generation/path in a new controlled run |
| Write-back fails after success | Recovery state, Jira token, issue permissions, API response | Restore Jira and retry write-back without rerunning RAE |

# Documentation and handover acceptance

The receiving team should be able to complete these activities without student
intervention:

- identify every component, source location, setting, dependency, health check,
  and owning role;
- explain Docker, YARN/HDFS, backtest, code-generation, and read-only paths;
- install or restore the system using the deployment guide;
- complete both SOPs and locate the matching Airflow, runtime, GitHub, HDFS, and
  Jira records;
- diagnose an injected configuration failure;
- pause intake, preserve state, roll back, and reopen; and
- update the affected README, configuration guide, SOP, examples, and tests in
  the same change when behaviour or required configuration changes.

Assign document ownership to roles: Airflow, runtime, Jira, GitHub, AI platform,
Hadoop/YARN, backtester, and storage owners.
