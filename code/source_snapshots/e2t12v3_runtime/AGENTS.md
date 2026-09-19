# AGENTS.md

Guidance for agents and contributors working in this repository.

## Repository Shape

- `jira-chatops-gateway/` owns Airflow DAGs, Jira integration helpers, ADF
  formatting, runtime payload schemas, and ChatOps tests.
- `rae_runtime/` owns the sandbox runtime, proxy pipeline, MCP backtest tools,
  after-backtest processing, schemas, and runtime tests.
- Runtime request/response schemas are duplicated where needed between the
  gateway and sandbox. Keep schema changes synchronized and update examples and
  tests with contract changes.

## Airflow Configuration

- Store production secrets in Airflow Connections, not Variables.
- Store non-sensitive runtime settings in Airflow Variables.
- Keep connection lookup centralized through
  `jira-chatops-gateway/dags/jira_quant_common.py`.
- Use these default connection IDs unless there is a clear deployment reason to
  override them:
  - `jira_cloud`
  - `litellm_default`
  - `github_default`
- When adding a new Airflow setting, document it in
  `jira-chatops-gateway/docs/airflow_ui_configuration.md` and consider adding it
  to `jira_airflow_secret_probe.py`.
- Local `.env` fallback is for development only. Do not commit populated `.env`
  files or credentials.

## Runtime Contracts

- Treat `runtime_request.schema.json`, `runtime_response.schema.json`, and
  `progress_event.schema.json` as compatibility contracts.
- The sandbox reads the runtime request from stdin and writes the final result to
  `output_paths.result_path`; stdout is only a fallback for the final JSON.
- Progress events belong in the JSONL side channel specified by
  `output_paths.progress_events_path`, not in stdout.
- HDFS output paths must be unique per ticket and/or run ID. Do not introduce
  shared static output paths for parallel runs.
- For failed or timed-out sandbox executions, populate structured diagnostics
  rather than relying on free-form logs.

## Secrets And Logging

- Never print raw tokens, API keys, Jira API tokens, or connection passwords.
- Mask credentials in debug output. Prefer length/presence checks over values.
- Do not place secrets in Jira comments, runtime payloads, schema examples, test
  fixtures, or docs.
- GitHub and GHCR tokens should come from Airflow Connection password fields in
  production.

## Testing

- Run focused tests for the area changed.
- For Jira/Airflow gateway changes, start with:

```bash
cd jira-chatops-gateway
python -m pytest
```

- For sandbox/runtime changes, start with:

```bash
cd rae_runtime
python -m pytest
```

- Add or update tests when changing command parsing, Airflow config resolution,
  payload schemas, Jira ADF generation, sandbox execution behavior, or HDFS/YARN
  logic.

## Code Style

- Prefer existing helper modules over duplicating config, Jira, Docker, HDFS, or
  schema logic.
- Keep Airflow DAG files import-safe: avoid expensive side effects at module
  import time.
- Keep operator logs useful but sanitized.
- Prefer explicit validation with clear error messages for Airflow Variables,
  connection fields, runtime payload fields, and boolean/integer config.
- Preserve idempotency for Jira write-back and workflow progress comments.

## Documentation

- Update docs in the same change when behavior, required config, or operational
  steps change.
- Keep examples realistic but secret-free.
- Prefer small, task-specific docs under the relevant package `docs/` directory
  and link them from broader README sections when useful.
