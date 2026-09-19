# Jira `/quant` ticket examples

Copy-paste examples for triggering the quant dev loop from a Jira ticket comment. Every example here was validated against the live command parser and routes to the behaviour noted next to it.

Two forms are accepted and can be mixed:

- **Block form** - `key: value`, one per line, under a free-text objective.
- **Inline form** - `key=value` tokens on the same line as the objective.

## The one rule to remember

`strategy_type` is **required** and selects the workflow. It is never guessed from the text. Omit it and the comment is rejected with a comment posted back to the ticket.

| `strategy_type` | Runs the engine? | What it does |
|---|---|---|
| `backtest` | yes | Tunes a strategy `.request` and backtests it |
| `refactor` | no | Edits code and commits it |
| `analysis` | no | Inspects code and reports findings |
| `ingestion` | no | Data-prep task (currently behaves as a general code task) |
| `other` | no | Anything else |

Two behaviours that surprise people:

- **General tasks run once by default.** Only `backtest` iterates automatically. For `refactor`/`analysis`/`ingestion`/`other`, add `allow_iteration: true` if you want the loop to keep going until the reviewer is satisfied.
- **To backtest the existing strategy without editing it, add `read_only: true`.** A plain `backtest` ticket with a repo will *edit* the `.request` first. `read_only` (alias for `zero_code_modifications`) submits the committed baseline as-is.

---

## 1. Backtest - run a read-only parameterised baseline

```
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

Submits the committed momentum strategy with the stated deterministic engine
parameters. It creates no branch or commit; dates are optional and may be
omitted to use the strategy's own window.

## 2. Backtest the existing baseline - no edit, no commit

```
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

`read_only: true` skips the strategy rewrite and submits the committed baseline
straight to the engine with the declared parameters. This is the explicit
replacement for the old smoke-test tickets that described this in prose -
declare it, don't rely on wording.

Add `params:` to tune the engine without an LLM in the config path - the `.request` is built deterministically from the baseline plus your params, submitted as-is, and staged in the run's output for inspection. Nothing is branched or committed, so the config stays exactly what the ticket says it is:

```
params: NPORT=80, NFREQ=4
```

Values may contain commas (`VOLATILITY_ACTIVE_RULES=0,1`); a new key starts after a comma.

## 3. Refactor - edit code within a scoped set of directories

```
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

Creates the requested implementation and companion test module, then performs
one bounded review pass. The runtime applies the server-enforced repository and
branch scope; the request's `resource_path` identifies the primary deliverable.

## 4. Analysis - read-only, findings only

```
/quant
Analyse the attached monthly-return dataset and report the findings
strategy_type: analysis
zero_code_modifications: true
input_attachment: experiment_2_input.csv
input_format: csv
```

No branch, no commit. Upload an attachment named `experiment_2_input.csv`
before posting the comment; the exact filename selects the dataset. Use the
synthetic [sample CSV](examples/experiment_2_input.csv) for this example; it is
not real Experiment 2 data.

## 5. Documentation or a new file from existing repository context

```text
/quant
Create a comprehensive root README using the existing resource notes and verified codebase structure
strategy_type: other
resource_path: atp-handlers/src/main/resources/readme
target_path: README.md
allowed_directories: .
allow_iteration: true
repo: ATPDataHandlersRepo
```

`resource_path` is the starting evidence and `target_path` is the required
deliverable. When they differ, the agent must inspect broader repository context
before writing. Documentation remains a normal `other` repository edit; it does
not invoke the backtest engine.

## 6. Inline form - everything on one line

```
/quant Tidy the LiteLLM client strategy_type=refactor resource_path=rae_runtime/proxy/llm_client.py repo=bankingscience/BSLAgenticQuantDevLoop
```

Repository selectors accept these exact names, their full
`bankingscience/<name>` forms, or their GitHub URLs: `BSLAgenticQuantDevLoop`,
`ATPConnectorsRepo`, `ATPDataHandlersRepo`, `ATPSiftingAnalyticsRepo`, and
`ATPSiftingPreTradeRepo`. Casing and surrounding whitespace are normalized.
`BSLAgenticQuantDevLoop` defaults to `main`; ATP repositories default to
`develop`.

Multi-repository source branches can be selected explicitly:

```
/quant
Update the coordinated connector and data-handler integration
strategy_type: refactor
resource_path: src/contracts.py
branch_map: ATPConnectorsRepo=feature/connectors, ATPDataHandlersRepo=develop
```

Each selected repository uses `quant/<Jira-ticket-id>` as its target branch.
An explicit target may use that branch or a descendant, but never `main` or
`develop`.

For a coordinated edit, repository-specific path scopes override the global
scope and use `|` between paths:

```
/quant
Update the shared runtime contract and data-handler integration
strategy_type: refactor
repos: BSLAgenticQuantDevLoop, ATPDataHandlersRepo
resource_path: rae_runtime/proxy/contracts.py
allowed_directories: .
allowed_directories_map: BSLAgenticQuantDevLoop=rae_runtime|jira-chatops-gateway; ATPDataHandlersRepo=.
```

Multi-repository ATP backtests are intentionally rejected. ATP repository
branches are not ATRADE engine component versions and are never copied into the
six `atrade_sifting_*` request properties.

## 6. Large single-pass repository edit

Use a larger commit and agent-turn allowance when one pass must update several
files. `max_commits_per_run` is a per-agent-pass cap; if iteration is enabled,
each later pass receives a fresh allowance.

```
/quant
Update the repository-wide runtime contract and its consumers
strategy_type: refactor
max_commits_per_run: 20
max_agent_turns: 45
max_iterations: 1
timeout_seconds: 3600
max_token_budget_per_run: 300000
```

## 7. Maximal - every common option set explicitly

```
/quant
Tune the medium-term momentum strategy with a volatility filter
strategy_type: backtest
resource_path: rae_runtime/proxy/strategy.request
start_date: 2024-01-01
end_date: 2024-12-31
repo: bankingscience/BSLAgenticQuantDevLoop
allowed_directories: rae_runtime/proxy strategies
allow_iteration: true
model: qwen3-coder
params: NFREQ=4, NPORT=50
max_iterations: 5
max_failed_iterations: 2
max_agent_turns: 45
max_commits_per_run: 20
timeout_seconds: 10800
max_token_budget_per_run: 1000000
cpu_vcpus: 2
memory_mb: 4096
gpu_count: 0
pids_limit: 256
```

---

## Option reference

| Option | Aliases | Meaning |
|---|---|---|
| `strategy_type` | `action`, `workflow` | **Required.** `backtest` / `refactor` / `analysis` / `ingestion` / `other` |
| `resource_path` | `file`, `path`, `target_file` | Repo-relative file to start from. No leading `/`, no `..` |
| `target_path` | `destination`, `output_file` | Required repo-relative deliverable. Defaults to `resource_path` |
| `allowed_directories` | `allowed_dir`, `allowed_dirs` | Optional space/comma-separated per-run scope for dirs the run may read/write. It does not affect the requester's repository authorization. |
| `allowed_directories_map` | `allowed_dirs_map`, `allowed_paths_map` | Semicolon-separated `repo=path|path` scopes for selected repositories; overrides the global scope |
| `max_agent_turns` | `agent_turns` | Optional 10-60 cap on tool/LLM turns in one edit pass. Defaults to 12 for a simple edit and 30 for discovery or a distinct target path; multi-repo runs add 5 per extra repo, capped at 60. |
| `max_iterations` | `iterations` | Maximum completed edit/review passes. This does not count failed agent executions. |
| `max_failed_iterations` | `max_failures` | Maximum retryable failed execution attempts when `allow_iteration: true`; no retry occurs after this budget is exhausted. |
| `max_commits_per_run` | `max_commits` | Optional 1–50 GitHub commit cap for one agent/edit pass. Omitted requests retain runtime `MAX_COMMITS_PER_RUN` (default 5). Each iteration receives a fresh allowance. |
| `allow_iteration` | `iterate` | `true`/`false`. Default: `true` for `backtest`, `false` otherwise |
| `read_only` | `zero_code_modifications` | `true` = no branch/commit. Backtest submits the baseline; others report findings |
| `rag_enabled` | `rag` | `true` enables one deterministic retrieval from the configured frozen Jira index; default `false`. Never paste hand-selected memory into a formal C1 ticket. |
| `rag_top_k` | `rag_topk` | Retrieval depth from 1–10; default 5. Keep identical across paired conditions even though C0 does not call the retriever. |
| `repo` | `repository` | `bankingscience/<repo>` (or full `https://github.com/bankingscience/...`) |
| `branch` | `target_branch` | The ticket's `quant/<KEY>` branch or a descendant |
| `start_date` / `end_date` | `start` / `end` | Backtest window. Optional - inherits the `.request` window if omitted |
| `stock_type` | `ticker` | Ticker / universe |
| `model` | `llm_model` | `nova-micro`, `nova-pro`, `test-model`, `gpt-oss`, `qwen3-coder` |
| `params` | - | Engine overrides as `KEY=value` pairs, comma-separated. **Must** be `KEY=value` - a JSON object (`{"NPORT": 5}`) parses to nothing and is ignored (with a warning in the run log) |
| `max_iterations` | `iterations` | 1–10 |
| `max_failed_iterations` | `max_failures` | 1–10 |
| `timeout_seconds` | `timeout` | 60–10800 (whole-run budget) |
| `max_token_budget_per_run` | `max_tokens` | 1000–1000000 |
| `cpu_vcpus` | `cpu` | 0.25–8 |
| `memory_mb` | `memory` | 512–16384 |
| `gpu_count` | `gpu` | 0 only (GPUs unsupported) |
| `execution_timeout_seconds` | `runtime_timeout` | Orchestrator timeout for the container/app |
| `pids_limit` | - | 64–1024 |
| `yarn_queue` | `queue` | YARN queue override |

## Paired Jira RAG smoke run

Use two new Jira tickets with identical task text, source commit, model, budgets,
and acceptance criteria. Pin the source with
`branch_map: BSLAgenticQuantDevLoop=<FROZEN_SOURCE_BRANCH>`. The current workflow
requires a branch name here, not a raw commit SHA. Use a protected experiment
branch and independently verify its head against the approved source SHA. Set
`rag_enabled: false` for C0 and change only that value to `true` for C1. Keep
`rag_top_k` fixed. C1 requires `JIRA_RAG_INDEX_PATH` and
`JIRA_RAG_INDEX_SHA256`; C0 does not read them. C1 is Docker-only and sends the
bounded retrieved-memory block only to the coding agent, never the reviewer or
read-only analysis agent. The complete frozen template and
verification checklist live in `experiments/exp2/README.md`.

## Gotchas

- **Forgot `strategy_type`** -> rejected. The error lists the valid values.
- **`resource_path` or `target_path` outside `allowed_directories`** -> rejected up front.
- **Absolute paths or `..`** in `resource_path`/`target_path`/`allowed_directories` -> rejected.
- **A general task that "did nothing"** -> if you expected an edit but got a no-changes result, check that `resource_path` points at the right file; without it, general tasks fall back to a placeholder file.
- **No `repo`** -> defaults to `bankingscience/BSLAgenticQuantDevLoop`. Use
  `read_only: true` explicitly when no branch or commit should be created.
## Analyse a Jira-attached dataset

First attach `experiment_2_input.csv` to the Jira issue. The synthetic
[sample CSV](examples/experiment_2_input.csv) is attach-ready and is not real
Experiment 2 data. Then post:

```text
/quant
Analyse the attached Experiment 2 monthly-return dataset
strategy_type: analysis
zero_code_modifications: true
input_attachment: experiment_2_input.csv
input_format: csv
```

The gateway downloads the exact attachment, verifies its size and checksum,
stages it read-only at `/workspace/input/datasets/experiment_2_input.csv`, and
records the attachment ID and run-staged HDFS URI in the result lineage.
