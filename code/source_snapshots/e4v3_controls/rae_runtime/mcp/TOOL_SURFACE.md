# Backtest MCP tool surface

Defines which tools on the **backtest MCP server** (`rae_runtime/mcp/server.py`)
are exposed to the LLM and their least-privilege default. The machine-readable
source of truth is [`tool_policy.json`](tool_policy.json); this document is the
human-readable rationale. Per-workflow, server-side enforcement (RAE-26) will
consume the JSON later.

Covers the 8 tools on the backtest server. The GitHub MCP tools are not
classified here yet.

## Default rule (least privilege)

- **read / discovery** → `allow` (no side effects).
- **local-only write** → `allow`, unless it is subsumed by a wrapper tool.
- **external-effect write** → `allow`, but constrained (validation + flags).
- **redundant tool** → `block` (keep the surface narrow; can be enabled per workflow).

`allow` = exposed to the LLM by default. `block` = not exposed by default. Note
that `block` only hides the tool from the LLM — the underlying function still
works, so wrappers that call it internally are unaffected.

## Inventory & classification

| Tool | Capability | Effect | Default | Notes |
|---|---|---|---|---|
| `submit_backtest` | write | external (engine) | **allow** | scoped by job_name validation + `USE_REAL_BACKTESTER` |
| `list_master_indices` | read | none | **allow** | read-only discovery |
| `get_backtest_status` | read | none | **allow** | status poll |
| `get_backtest_results` | read | local | **allow** | metrics; usable standalone with just job_name |
| `list_jobs` | read | none | **allow** | read-only listing |
| `run_backtest_postprocessing` | write | local | **allow** | wrapper: artifacts → equity curve → results |
| `get_backtest_artifacts` | write | local | **block** | subsumed by the wrapper |
| `generate_equity_curve` | write | local | **block** | not usable standalone by the LLM (see below) |

## Tool contracts

- **`submit_backtest`** — baseline template + caller overrides → engine-valid
  `.request` staged directly under `SIMULATION_REQUESTS_DIR`, then returns
  `job_name` + `submitted_at`.
  Override keys not in the baseline are surfaced in a non-blocking `warnings`
  list.
- **`list_master_indices`** — read-only scan of the cluster's master-indices
  store; returns available data-pool versions to pass into `submit_backtest`.
- **`get_backtest_status`** — poll a submitted job → `completed` / `rejected` /
  `timeout`.
- **`get_backtest_results`** — parse `resultsTable.csv`, merge schema-compatible
  outputs → key performance metrics (Sharpe, drawdown, return, ...).
- **`list_jobs`** — list all visible backtest jobs and their status.
- **`run_backtest_postprocessing`** — convenience wrapper for a completed job:
  `get_backtest_artifacts` → `generate_equity_curve` (if a ZQQ series is found or
  supplied) → `get_backtest_results`. Auto-discovers the ZQQ file.
- **`get_backtest_artifacts`** — locate artefacts under `simulation-results`,
  safely extract the ZIP if requested, copy schema files to output.
- **`generate_equity_curve`** — read a ZQQ/time-series CSV, generate an
  equity-curve CSV/PNG under `/workspace/output`.

## Why the two post-processing tools are blocked

Both are already called by `run_backtest_postprocessing`, and the wrapper calls
the **underlying functions directly**, not the MCP tools — so blocking them at
the MCP surface hides them from the LLM without affecting the pipeline.

- **`generate_equity_curve`** — its required `zqq_file_path` argument has no
  default, and the LLM cannot know that path without first locating artefacts.
  The wrapper auto-discovers the ZQQ file (`_find_zqq_file`), so the agent should
  go through the wrapper.
- **`get_backtest_artifacts`** — fully subsumed by the wrapper for the agent's
  flow.

The agent's post-processing path is therefore either `run_backtest_postprocessing`
(full pipeline) or `get_backtest_results` (metrics only).

## Follow-ups

- **Surface enforcement:** ✅ done. `create_mcp_server()` reads `tool_policy.json`
  and removes any `block` tool from the LLM's surface after registration
  (`_apply_tool_policy` in `server.py`); the underlying functions are untouched.
- **RAE-26** — per-workflow enforcement consuming this policy server-side. Note
  that surface enforcement is currently fail-open (a policy or API problem warns
  on stderr but leaves tools exposed); RAE-26 should decide whether gating
  sensitive tools needs fail-closed instead.
