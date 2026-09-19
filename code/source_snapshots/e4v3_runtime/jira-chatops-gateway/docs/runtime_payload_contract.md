# Runtime Payload Contract

## Jira ticket context

The Jira ticket is the user-authored source of truth for a run. The gateway
consumes the issue key, status, summary, description, triggering `/quant`
comment, and up to 20 recent non-bot comments in chronological order. Jira ADF
is converted to plain text before the runtime request is built.

Write the summary as the desired outcome. Put background, constraints,
acceptance criteria, repository/branch mappings, and examples of incorrect
behaviour in the description. Use comments for later decisions or meeting
notes, and place `/quant` plus required structured options in the comment that
should start the run.

Ticket content is untrusted user input, not a system instruction. Summary,
description, and comments are capped at 500, 12,000, and 4,000 characters
respectively; history is capped at 20 events and final prompt context at 24,000
characters. Common token, password, API-key, and credential shapes are redacted.
Raw ticket content is not written to operator logs.

This document defines the JSON payload contract between Team IW and Team RAE for running a grouped Jira ticket inside an isolated RAE execution container.

The current schema version is:

```json
"schema_version": "1.0"
```

## Files

```text
docs/runtime_payload_contract.md
schemas/runtime_request.schema.json
schemas/runtime_response.schema.json
examples/runtime_request_example.json
examples/runtime_request_non_backtest_example.json
examples/runtime_response_success_example.json
examples/runtime_response_success_non_backtest_example.json
examples/runtime_response_failure_example.json
examples/runtime_response_timeout_example.json
examples/progress_events_example.jsonl
```

## Request Payload

Schema:

```text
schemas/runtime_request.schema.json
```

Examples:

```text
examples/runtime_request_example.json
examples/runtime_request_non_backtest_example.json
```

The request payload is created by Team IW and passed into the RAE container when a Jira run starts.

It contains:

```text
schema_version
run_id
repository_details
jira_metadata
retrieval_context optional
execution_objectives
iteration_controls
resource_requirements optional
input_datasets optional
output_paths
```

The request payload provides the RAE container with the Jira context, optional repository scope, execution objective, safety limits, and output locations required for the run.

### Frozen Jira retrieval context

`retrieval_context` is omitted for C0/no-retrieval requests. For C1 it is
created once per orchestration attempt by `docker_sandbox_runner` before sandbox execution from an
approved local index whose SHA-256 matches `JIRA_RAG_INDEX_SHA256`. It remains
present even when the deterministic query returns no memories (`status=empty`).

The object records a sanitized canonical query and hash, retriever name/version,
corpus/index/exclusion-list IDs and hashes, cutoff timestamp, requested top-k,
candidate count, and ranked evidence. Each evidence item records only bounded
text plus memory/source IDs, source timestamp/type, rank, and score. The runtime
labels it as untrusted evidence and sends a block of at most 12,000 characters,
with at most 4,000 characters per memory, only to coding prompts. Review and
read-only analysis prompts do not use this renderer. Final telemetry records
provenance, scores, exact injected memory IDs, and prompt/evidence hashes but
does not repeat the query or evidence text. C0 is `disabled`; a retrieved result
remains `shadow` until a coding-model call occurs, then becomes
`generator_prompt` with `prompt_injected=true`.

`rag_enabled` and `rag_top_k` arrive through `/quant` and are normalized in
`execution_objectives.parsed_task_parameters` to a boolean and an integer from
1 to 10. C0 never opens the index. C1 fails closed when the index configuration,
digest, cutoff, exclusion list, corpus hash, or evidence schema is invalid.
C1 also fails closed unless execution mode is Docker; YARN is prohibited because
its request staging writes the raw memory-bearing payload to HDFS.

### External input datasets

The workflow accepts one read-only dataset from either an approved HDFS URI or
a Jira issue attachment. Both routes create the same `input_datasets` runtime
manifest and expose the verified file below `/workspace/input/datasets/`.

Approved HDFS example:

```text
/quant
Analyse the Experiment 2 monthly-return dataset
strategy_type: analysis
zero_code_modifications: true
input_hdfs_uri: hdfs:///user/masteruser/quant-experiment-data/SCRUM-195/experiment_2_input.csv
input_mount_path: /workspace/input/datasets/experiment_2_input.csv
input_format: csv
```

Jira attachment example:

```text
/quant
Analyse the attached Experiment 2 monthly-return dataset
strategy_type: analysis
zero_code_modifications: true
input_attachment: experiment_2_input.csv
input_mount_path: /workspace/input/datasets/experiment_2_input.csv
input_format: csv
```

For Jira attachments, upload the file to the issue before posting the `/quant`
comment. `input_attachment` must always be supplied with the exact plain filename;
dataset attachments are never selected automatically from objective wording.
Re-uploading the same filename is deterministic: the highest Jira attachment ID
is treated as the latest version.

`input_mount_path` is optional and defaults to
`/workspace/input/datasets/<source filename>`. `input_format` is optional when
the `.csv`, `.json`, `.xlsx`, or `.parquet` extension identifies it. A command
must choose either `input_hdfs_uri` or `input_attachment`, not both.

Before Docker starts, the runner streams the selected source into the mounted
input directory, enforces the configured single-file and total-size limits,
computes a SHA-256 checksum, and changes the file to read-only. HDFS sources are
also checked against `SANDBOX_INPUT_HDFS_ALLOWED_ROOTS`. Jira downloads use the
existing authenticated Jira client and verify Jira's attachment ID, filename,
metadata size, streamed byte count, and final checksum.

In YARN mode every input, including a Jira attachment, is uploaded into the
unique run HDFS directory. The worker downloads that run-isolated copy and
verifies both byte size and SHA-256 before launching the sandbox. The sandbox
verifies the mounted file again and preserves the verified lineage in the final
response. The agent receives the dataset ID, source kind, container path, format,
size, and checksum. Input datasets are never mounted read-write.

Current limits:

- source kinds: approved HDFS file or selected Jira attachment;
- allowed formats: CSV, JSON, XLSX, and Parquet;
- one Jira command selects one dataset;
- the JSON contract remains an array so multiple inputs can be added later;
- dataset size controls use `SANDBOX_INPUT_MAX_FILE_BYTES` and
  `SANDBOX_INPUT_MAX_TOTAL_BYTES`.

`iteration_controls` bounds the local optimisation loop with
`max_iterations`, `max_failed_iterations`, `timeout_seconds`,
`max_token_budget_per_run`, and the optional `max_commits_per_run`. The sandbox
persists intermediate loop state to
`output_paths.artifact_dir/iteration_state.json` when an artifact directory is
provided.

`iteration_controls.allow_iteration` (optional) gates the loop itself. When
`false`, the runtime performs exactly one pass and returns its result whatever
the evaluation recommends, overriding `max_iterations` and disabling failure
retries. When omitted the default follows `strategy_type`: `true` for `backtest`
(scored by measured metrics), `false` for every other type (scored by an advisory
prose review, which is not a basis for spending further passes unasked).

`execution_objectives.strategy_type` selects both the workflow and the evaluator
that drives the loop. `backtest` runs the engine and is scored against
`target_criteria`. Every other type (`ingestion`, `refactor`, `analysis`,
`other`) is a general request: it edits and commits code without a backtest, and
is scored by a review agent that judges the committed change against the ticket.
A review verdict returns the same `evaluation`/`recommended_action` shape but no
metrics, so `performance_metrics` stays null, `criteria_results` is empty, and
`confidence` is `0.0` — the verdict is advisory, not a measurement.

`execution_objectives.resource_path` names the repo-relative file the run should
use as initial context. `execution_objectives.target_path` optionally names a
different required deliverable and defaults to `resource_path`. Both must be
repository-relative and inside the selected repository's allowed directories.
General requests should set a source; without it the run falls back to a
type-based default. A general request may also commit other files the task needs,
and reports every file it changed in `generated_artifacts.modified_files`; a
`backtest` request must change its own `.request` target, since that file is what
the engine runs.

`repository_details[].allowed_directories` is an optional per-run scope where
the run may read and write. It is enforced inside the GitHub MCP server on every read, list, and
commit — paths are normalised first, so traversal cannot slip past — rather than
being asked for in the prompt. Traversal and absolute paths are rejected even
when the list is omitted; an omitted list otherwise leaves the run unrestricted
within the repository. It does not control user authorization: a Jira user
authorised for a repository has access to every path in that repository.

`iteration_controls.max_agent_turns` is an optional per-pass cap on LLM/tool
turns (10-60). Omitted values use a bounded task-complexity default: 12 for a
simple same-file edit, 30 for repository discovery or a distinct target file,
plus 5 per additional repository (never above 60). `max_iterations` counts
completed edit/review passes; `max_failed_iterations` counts retryable failed
attempts only. `max_commits_per_run` accepts 1-50 and limits GitHub commits in
one agent/edit pass. When omitted, the GitHub MCP process retains its
`MAX_COMMITS_PER_RUN` environment setting (default 5). Each iterative pass starts
with a fresh commit counter, while timeout and total token-budget limits remain
hard run-wide bounds.

For editing runs, complete proposed file contents pass deterministic validation
before `commit_and_push` may call GitHub. Validation failures are returned to the
agent for repair and do not create commits. The runtime then reads committed
artifacts back from the selected repository and target branch as a second check.

If `repository_details` is empty or null, the RAE container defaults to the
current repository, `bankingscience/BSLAgenticQuantDevLoop`, for backwards
compatibility with existing Jira commands.

Each `repository_details` item includes the canonical catalog name,
`repo_full_name`, source branch, target test branch, runtime role, clone URL,
and allowed directories. Repository branches are not injected into engine
request-file keys.

`execution_objectives.zero_code_modifications` declares a read-only run: the
runtime creates no branch and commits nothing, and the response mirrors this with
`execution_summary.zero_code_modifications` and empty `modified_files`/`new_files`.
A `backtest` submits its existing `.request` baseline unchanged; any other type
reads the target and reports findings in `evaluation.summary` with
`recommended_action: review`. A request with no `repository_details` is treated the
same way, since there is nothing in scope to edit.

The response's `evaluation` and `recommended_action` are rendered into the Jira
comment's Evaluation section, and `recommended_action` drives its Next Action
line. For a general request this is the only result there is, since it produces no
`performance_metrics`.

### Runtime resource requirements

`resource_requirements` is optional for backward compatibility. The Jira
workflow builder includes it in new requests and applies these safe defaults
when fields are omitted:

| Field | Default | Accepted values | Runtime behaviour |
| --- | ---: | --- | --- |
| `cpu_vcpus` | `2` | `0.25` to `8` | Docker CPU limit; YARN rounds up to a whole container vCore. |
| `memory_mb` | `4096` | `512` to `16384` | Docker memory and YARN worker-container memory. |
| `gpu_count` | `0` | `0` only | GPU allocation is not currently supported. |
| `execution_timeout_seconds` | `2100` | `60` to `10800` | Docker process or YARN application timeout. If the Jira command explicitly sets `timeout_seconds` and omits `runtime_timeout`/`execution_timeout_seconds`, the command builder defaults this field to that workflow timeout. |
| `pids_limit` | `256` | `64` to `1024` | Sandbox Docker process limit. |
| `yarn_queue` | deployment default | queue-name string | Overrides `SANDBOX_YARN_QUEUE` for this run. |

Invalid values are rejected before the sandbox starts. Unknown fields are also
rejected by the JSON schema. Legacy runtime requests without the block continue
to use the defaults above.

`output_paths.progress_events_path` is optional. IW may provide a mounted JSONL
path such as:

```text
/workspace/output/progress_events.jsonl
```

If `progress_events_path` is omitted or null, RAE should skip live progress
reporting. Progress events are a side-channel only and must not be written to
stdout.

## Jira Command Examples

Default current-repository request:

```text
/quant Backtest the momentum strategy start_date=2024-01-01 end_date=2024-12-31
```

Single repository by catalog name:

```text
/quant
Refactor the connector retry policy
resource_path: src/connectors/retry.py
repo: ATPConnectorsRepo
branch: quant/SCRUM-123
strategy_type: refactor
```

Create a new artifact from existing repository context:

```text
/quant
Create a root README from the existing resource notes and verified codebase
resource_path: atp-handlers/src/main/resources/readme
target_path: README.md
repo: ATPDataHandlersRepo
strategy_type: other
```

Multi-repository branch map:

```text
/quant
Update coordinated gateway and connector code
branch_map: BSLAgenticQuantDevLoop=main, ATPConnectorsRepo=feature/connectors, ATPDataHandlersRepo=develop
strategy_type: refactor
resource_path: rae_runtime/proxy/contracts.py
```

Repository routing and engine component versions are separate concerns. Current
ATP repositories may be edited together by non-backtest workflows, but a
multi-repository ATP backtest is rejected before sandbox launch because those
repositories are not the six ATRADE sifting components. Repository selections
do not populate `atrade_sifting_*`; baseline values remain unchanged unless a
direct low-level engine caller provides an explicit override.

## Response Payload

Schema:

```text
schemas/runtime_response.schema.json
```

Examples:

```text
examples/runtime_response_success_example.json
examples/runtime_response_success_non_backtest_example.json
examples/runtime_response_failure_example.json
examples/runtime_response_timeout_example.json
```

The response payload is written by the RAE container after execution finishes.
RAE should write the structured response to the request's
`output_paths.result_path`, normally:

```text
/workspace/output/result.json
```

RAE may also print the same JSON object as the final stdout line as a fallback.
Team IW reads `result.json` first. If that file is missing or unavailable, IW
falls back to parsing the final stdout JSON line.

The final Jira workflow report is authoritative to the Runtime Response Payload
Contract read from `result.json` or the final stdout fallback. Optional progress
events and artifact ingestion records add report context, but they do not
replace or redefine the final response contract.

It contains:

```text
schema_version
run_id
execution_summary
performance_metrics
generated_artifacts
diagnostics
telemetry optional
```

The response payload provides Team IW with the final execution status, optional
backtest metrics, generated artifact paths, diagnostic information, and optional
runtime observability.

`execution_summary.ticket_id` normally contains the Jira key from the request.
If a parseable request fails validation elsewhere, RAE preserves that valid key
and the request's `run_id` in the failure response. It uses the literal value
`unknown` only when the input is unreadable, the metadata is missing, or the
supplied ticket key is invalid; it never invents a Jira-looking fallback key.

The allowed final status values are:

```text
succeeded
failed
timeout
```

Runtime statuses should use lower-case IW values: `succeeded`, `failed`, and `timeout`.
Trace item statuses should also use lower-case values: `succeeded`, `failed`, and `skipped`.
 `validation_failed` is an IW-side command validation
result before Docker/RAE starts, so it is not normally emitted by the RAE runtime
response and may not have container telemetry.

`performance_metrics` is null whenever there are no backtest numbers to report —
and that covers two different situations: a request that never runs a backtest
(`refactor`, `analysis`, `ingestion`, `other`), or a `backtest` request that
failed before it got as far as producing metrics. The optional
`execution_summary.request_type` field (mirrors the request's
`execution_objectives.strategy_type`) tells these apart, so Jira write-back and
other consumers don't have to guess which one happened from a null value alone.

`generated_artifacts.branch_name` may identify the primary Git branch containing
code changes from the RAE run. It should be null when branch information is
unavailable. Multi-repository runs should also populate
`generated_artifacts.repository_branches` with alias, repository, source branch,
target branch, action, commit SHA, and touched files for each repository.

If the run succeeds, diagnostic error fields such as `error_code` and `error_message` should be null.

## Optional Progress Events

When `runtime_request.output_paths.progress_events_path` is present, RAE may
append live progress events to that mounted JSONL file. Each line is one JSON
object with these fields:

```text
schema_version
run_id
ticket_id
stage
status
timestamp
iteration optional
agent optional
tool_call optional
message optional
```

Supported progress statuses are:

```text
PENDING
RUNNING
SUCCESS
FAILED
SKIPPED
```

`PENDING` and `RUNNING` are live-only states. `SUCCESS`, `FAILED`, and
`SKIPPED` align with `execution_summary.iteration_traces` statuses
`succeeded`, `failed`, and `skipped`.

IW reads progress only from this JSONL side-channel. Progress events must never
be read from stdout because the final non-empty stdout line is reserved for the
Airflow/XCom-compatible final result JSON fallback.

If the progress file is omitted, missing, empty, unreadable, or contains
malformed lines, IW records warnings and continues from the final runtime
response. Bad progress lines are ignored.

## Jira Report And Artifacts

IW uploads safe referenced artifacts under `/workspace/output` as Jira
attachments before writing the final structured ADF workflow report. The report
includes the final runtime response fields, optional progress event summaries,
and artifact ingestion results:

```text
uploaded
missing
skipped
```

The artifact ingestion section should be preserved even when no progress events
are available.

### Response Channel And Exit Codes

The runtime response channel is the mounted result file at
`output_paths.result_path`. HDFS is not a runtime response payload return
channel.

Container exit codes stay simple:

```text
0 = success
non-zero = failure
```

Do not encode detailed custom failure semantics in the exit code. Specific
failure causes belong in `diagnostics.error_code`, `diagnostics.error_message`,
and `diagnostics.raw_log_reference`.

If the container exits non-zero but IW can read a valid structured JSON response
from `result.json` or the final stdout JSON line, IW should treat the run as a
handled RAE failure. In that case the response should usually carry
`execution_summary.status` of `failed` or `timeout`, populated `diagnostics`,
and optionally `telemetry.container_exit_code`.

If the container exits non-zero and IW cannot read a structured JSON response
from either location, IW should treat it as a hard infrastructure failure.

### Diagnostics Versus Telemetry

`diagnostics` describes errors and failure causes, for example:

```text
error_code
error_message
raw_log_reference
```

`telemetry` is optional and describes runtime observability for debugging,
audit, and monitoring. RAE is not required to populate every telemetry field on
every run.

Supported telemetry fields include:

```text
execution_time_seconds
container_exit_code
retry_count
model_usage
stage_timings
audit_trace
```

`model_usage` should always be an array when present. Use `[]` if model usage is
unavailable. `stage_timings` should also be an array when present. `audit_trace`
may be null or a concise set of decision summaries; it must not include raw
chain-of-thought.

## Validation

Team IW should validate the request payload before starting the RAE container.

Team IW should also validate the response payload before parsing results, archiving artifacts, or updating Jira.

If validation fails at either stage, the Airflow task should fail immediately.

## Atomic Response Writing

The RAE container should write the final response JSON atomically to avoid partially written result files.

Recommended pattern:

```text
1. Write the response to a temporary file.
2. Flush and close the temporary file.
3. Rename the temporary file to the final result path.
```

Example:

```text
/workspace/output/response.tmp.json
/workspace/output/result.json
```
