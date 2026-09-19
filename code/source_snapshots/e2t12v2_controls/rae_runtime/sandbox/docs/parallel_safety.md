# Parallel safety & statelessness (RAE-19)

IW's Airflow launches many sandbox containers concurrently. This page is the
contract between the two teams: what the container assumes the orchestrator
guarantees, and what the container guarantees back. Share/align with IW (Umar)
when the DockerOperator setup changes.

## Assumptions RAE relies on (IW / Airflow must guarantee)

| # | Assumption | Why |
|---|---|---|
| 1 | **Fresh container per run** — one request, one container, then exit; containers are never reused across runs. | All in-process state (per-run GitHub op caps, MCP server processes) scopes to the process lifetime. |
| 2 | **Globally unique `run_id`** injected in every request payload. | `run_id` is baked into backtest job names on the shared cluster home; a reused `run_id` for the same ticket could collide. |
| 3 | **At most one concurrent container per ticket.** | The GitHub quant branch is ticket-scoped (`quant/<ticket>`, pre-created by the DAG). Two simultaneous runs of the same ticket would contend on that branch (concurrent commits, stale reads). RAE deliberately does not rename branches per run — exclusivity is the orchestrator's job. |
| 4 | **`/workspace` and `/tmp` are per-container tmpfs** (see the DockerOperator config in `README.md`). | Everything the run writes locally (results, artifacts, mock runtime) is isolated per container by the mounts, not by RAE code. |

## Guarantees RAE provides

| # | Guarantee | Where |
|---|---|---|
| 1 | Writes only under `/workspace` and `/tmp`. | Enforced by the read-only root FS + tmpfs mounts; nothing in the code targets other paths. |
| 2 | **Run/iteration-scoped job names** on the mounted real/shared filesystem (`$SIMULATION_REQUESTS_DIR/<job>.request`, `$SIMULATION_RESULTS_DIR/<job>/`): `<ticket>-<run_id>-i<n>`, sanitized to the engine's `^[a-zA-Z0-9_-]+$`, length-capped with a hash suffix. A request/result name is never reused — not across runs, and not across iterations of the RAE-18 loop within one run. | `sandbox/mcp_client.py::_job_name_from`, `sandbox/iteration_loop.py` |
| 3 | Atomic result write (`tmp` + rename) to the request's `result_path`. | `sandbox/result_io.py` |
| 4 | GitHub branch/commit caps count per MCP-server spawn (= per run); no cross-run counters. | `proxy/github_mcp_server.py::create_mcp_server` (closure state, no module globals) |
| 5 | No import-time side effects: MCP transport and env (`MCP_SERVER_PATH`, `MOCK_RUNTIME_DIR`, `BACKTEST_REQUESTS_DIR`) are read at call time. | `sandbox/mcp_client.py::_make_transport` |

## Verified by

`sandbox/tests/test_parallel_safety.py` — job-name scoping units plus a
concurrent integration test: two `run.py` processes (same ticket, different
`run_id`) run simultaneously against one shared `MOCK_RUNTIME_DIR` and must
produce two distinct job dirs with no overwrites.

Out of scope here: real-cluster concurrency/soak testing — that is RAE-33.
