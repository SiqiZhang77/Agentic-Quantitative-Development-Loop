# Sandbox - Runtime & Sandbox Security Layer

The hardened, run-once container Airflow launches per Jira ticket. It derives the
quant branch from the ticket, pulls the strategy from GitHub, has the LLM modify
it, commits it back, runs a backtest over MCP, and emits a structured result.

`run.py` is the entry point (`CMD ["python", "run.py"]`). It never imports the MCP
team's code - it spawns `mcp/server.py` and talks to it over the MCP protocol (stdio).

## Run

```bash
cd sandbox
docker compose up --build
```

The security posture prints at startup. To query it live:

```bash
curl localhost:8000/health
curl localhost:8000/sandbox/introspect
```

## Airflow DockerOperator - minimum reccommended config

```python
from docker.types import Mount

DockerOperator(
    user="1000:1000",              # entrypoint hard-exits if it detects root
    tmpfs={
        "/tmp": "",
        "/workspace": "uid=1000,gid=1000,mode=0750",
    },
    mounts=[
        Mount(
            source="/host/path/to/input",   # directory Airflow writes ticket JSON into
            target="/workspace/input",
            type="bind",
            read_only=True,                 # container must not modify its own job spec
        ),
        Mount(
            source="/host/path/to/output",  # directory Airflow reads backtest results from
            target="/workspace/output",
            type="bind",
            read_only=False,                # container writes results here; host reads after exit
        ),
    ],
    read_only=True,
    cap_drop=["ALL"],
    no_new_privileges=True,
    ...
)
```

Memory limits, network, and environment variables are left to the operator.

Running many containers concurrently? See [parallel_safety.md](parallel_safety.md)
for the assumptions/guarantees contract (unique `run_id`, one container per ticket,
run-scoped backtest job names).

## Large source-file edits

The GitHub MCP layer bounds model-facing file reads so a repeated large file cannot
exhaust the model context. A truncated read preserves both the file head and tail;
the same file is not returned twice in one run. For a named symbol in a large file,
the coding agent uses `find_in_file` to obtain a small line-numbered window. For a
focused change, `replace_in_file` requires one unique exact old snippet, applies the
replacement to the server-read complete file, validates the reconstructed result,
and commits it. This avoids committing a model-produced partial copy of a large
file. Notebooks remain excluded from that replacement path.

**Note:** the input-file mount above is the fallback path. The current PoC passes
`TICKET` + `REQUEST` as env vars (see Inputs below), so only the `/workspace/output`
mount is required.


## Testing for walking skeleton (full integration)

`run.py` is the container's entry point and the only file Airflow invokes
(`CMD ["python", "run.py"]`). It never imports the MCP team's code - it spawns
`mcp/server.py` and talks to it over the MCP protocol (stdio).

### Inputs

#### Request (PoC: two plain-string env vars)

| Var | Meaning |
|-----|---------|
| `TICKET` | The Jira ticket id (e.g. `SCRUM-5`). The container derives `quant/<ticket>`, creates it off `main` if absent (reuses it if present), and reads/commits the strategy there. |
| `REQUEST` | The feature-request text (the Jira comment) passed to the LLM. Plain string, not a JSON blob. |

`issue_key` is the ticket; the quant branch is `quant/<ticket>` (e.g. `SCRUM-5` → `quant/SCRUM-5`).

Fallback delivery (kept for transition / local use, in priority order):
`BRANCH` (explicit `quant/<ticket>`) → `RAE_REQUEST` (inline JSON) →
`RAE_REQUEST_FILE` (path) → `/workspace/input/request.json`.

#### Secrets / config (env vars)

| Var | Required | Notes |
|-----|----------|-------|
| `GITHUB_TOKEN` | yes * | Classic GitHub PAT using the approved Quant Dev Loop scope set. Used by `proxy/`. |
| `API_KEY` | yes * | LiteLLM key. The endpoint is UCL-network-only, so it's mocked off-cluster - any value works locally. |
| `RESULT_PATH` | no | Where the result JSON is written. Default `/workspace/output/result.json` (must be inside the writable output mount). |
| `MOCK_RUNTIME_DIR` | no | Backtester runtime root. `run.py` defaults it to `/tmp/mock_runtime` (writable under the read-only FS). |
| `BACKTEST_REQUESTS_DIR` | no | Defaults to `/tmp/mock_runtime/backtest-requests`. |
| `MCP_SERVER_PATH` | no | Path to the MCP server. Default `/app/mcp/server.py` (in-container). Set to `mcp/server.py` for local runs. |
| `USE_AGENT_BACKTEST` | no | Selects the backtest *driver*. Default (`false`) uses the scripted MCP client (`run_backtest_via_mcp`). `true` hands the backtest tools to the LLM and lets it drive them (`proxy/backtest_mcp.py`). Orthogonal to `USE_REAL_BACKTESTER`. |
| `USE_REAL_BACKTESTER` | no | Selects the *engine*. Default (`false`) delivers requests to the local mock dir (`BACKTEST_REQUESTS_DIR`) and reads results under `MOCK_RUNTIME_DIR`; `true` delivers to the real watched dir and reads real results (requires on-cluster filesystem access). |
| `SIMULATION_REQUESTS_DIR` | yes in real mode | Absolute real-mode request staging dir inside the container, usually `/workspace/backtester/simulation-requests` mounted by the gateway from the watcher-visible host path. Requests are staged atomically as `<job_name>.request`. |
| `SIMULATION_RESULTS_DIR` | yes in real mode | Absolute real-mode results dir inside the container, usually `/workspace/backtester/simulation-results` mounted by the gateway from the watcher-visible host path. Results are polled under `<job_name>/`. |
| `BACKTEST_RESULT_TIMEOUT_SECONDS` | no | Optional polling timeout for real results. Overrides `BACKTEST_POLL_TIMEOUT` when request-level `iteration_controls.timeout_seconds` is absent. |
| `RAE_OFFLINE` | no | If `1`, mocks the LLM and all GitHub calls so the container runs with no network. Used for the hardened off-network e2e. No real `GITHUB_TOKEN`/`API_KEY` needed. |

\* API_KEY and GITHUB_TOKEN not required in the case of an offline run, flagged by RAE_OFFLINE.

For Bialobog requester runs, `GITHUB_TOKEN` is the requester's classic PAT named
`QuantDevLoop`, with the expiry set for as long as possible and these exact
scopes: `repo`, `repo:status`, `repo_deployment`, `public_repo`, `repo:invite`,
`security_events`, `workflow`, `write:packages`, and `read:packages`. The token
is stored only through the cluster's encrypted-vault/Airflow Connection path;
the access matrix and server-side controls enforce the narrower request scope.

### Outputs

A single JSON object, written to `RESULT_PATH` **and** printed as the final
stdout line (for XCom). Large artifacts are referenced by path (currently a local container path - durable storage is a follow-up).:

```json
{
  "status": "succeeded | failed",
  "issue_key": "TEST-16",
  "command": "backtest",
  "summary": "Backtest completed for TEST-16 (via MCP).",
  "metrics": { "sharpe_ratio": 1.2, "max_drawdown": "-8.0%", "total_return": "45.0%" },
  "artifacts": {
    "feature_branch": "quant/TEST-16",
    "changed_file": "rae_runtime/proxy/strategy.py",
    "equity_curve": "/tmp/mock_runtime/simulation-results/TEST-16/ZQQ_TEST-16.csv"
  },
  "raw_output": { "run_id": "TEST-16" }
}
```

Exit code: `0` on `succeeded`, `1` on `failed` (mirrors `status`).

### What's real vs mocked (walking skeleton)

- **Real:** payload handling, GitHub branch/fetch/commit (`proxy/`), the MCP
  protocol round-trip (client <-> server over stdio), result shaping, the
  hardened container.
- **Mocked:** the LLM edit (LiteLLM endpoint is UCL-only - mocked off-cluster),
  and the backtest engine (the MCP team's `dummy_backtester` writes fixed
  metrics; the real Jenkins/model-builder is never touched).

### Testing locally

Run from the repo root (`rae_runtime/`). Requires a real `GITHUB_TOKEN`; the
LLM is mocked, so `API_KEY` can be a dummy.

```bash
export GITHUB_TOKEN="github_pat_..."
export API_KEY="dummy"
export MOCK_RUNTIME_DIR=/tmp/mock_runtime
export BACKTEST_REQUESTS_DIR=/tmp/mock_runtime/backtest-requests
export MCP_SERVER_PATH=mcp/server.py

rm -rf /tmp/mock_runtime && mkdir -p /tmp/rae/output

TICKET="TEST-1" \
REQUEST="add an RSI filter" \
RESULT_PATH=/tmp/rae/output/result.json \
PYTHONPATH=proxy:sandbox python3 sandbox/run.py

cat /tmp/rae/output/result.json
```

Use a fresh `TICKET` per run. Branch creation is create-or-reuse, so re-running the
same ticket reuses its branch - a new ticket exercises the create-from-scratch path.

### Testing in the hardened container

Build from the repo root and run under the DockerOperator's flags:

```bash
docker build -f sandbox/Dockerfile -t rae-local .

mkdir -p /tmp/rae/output && chmod 777 /tmp/rae/output

docker run --rm \
  --user 1000:1000 --read-only \
  --tmpfs /tmp --tmpfs /workspace:uid=1000,gid=1000,mode=0750 \
  -v /tmp/rae/output:/workspace/output \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --memory 512m \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" -e API_KEY="dummy" \
    -e TICKET="TEST-1" -e REQUEST="add an RSI filter" \
  -e RESULT_PATH="/workspace/output/result.json" \
  rae-local

cat /tmp/rae/output/result.json
```

In-container, `MOCK_RUNTIME_DIR` / `MCP_SERVER_PATH` use their defaults - do not
pass them. The image is published to GHCR by CI on push to `main`:
`ghcr.io/bankingscience/bslagenticquantdevloop/sandbox:latest` (private - the
cluster needs a token with `read:packages` to pull).


## Dependencies (pinned + reproducible)

Direct deps live in `sandbox/requirements.in`; they're compiled with pip-tools to a fully hash-pinned `sandbox/requirements.txt` (entire transitive tree, every line `==` with `--hash`). The Dockerfile installs only from that lock with `--no-deps --require-hashes`, and the base image is pinned by digest in both build stages, so a clean rebuild resolves an identical set.

To bump a dependency, follow `sandbox/UPGRADE.md` - never hand-edit requirements.txt.

## Testing offline container:

```bash
docker run --rm --network none \
  --user 1000:1000 --read-only \
  --tmpfs /tmp --tmpfs /workspace:uid=1000,gid=1000,mode=0750 \
  -v /tmp/rae/output:/workspace/output \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 256 --memory 512m \
  -e RAE_OFFLINE=1 -e TICKET="TEST-1" -e REQUEST="add an RSI filter" \
  -e RESULT_PATH="/workspace/output/result.json" \
  rae-local
```
