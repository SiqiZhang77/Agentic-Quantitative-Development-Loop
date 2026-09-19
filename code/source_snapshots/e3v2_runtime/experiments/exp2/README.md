# RAG Experiment 2 runbook

This directory is the Jira project-memory RAG experiment. It is unrelated to
the ESG/Sanctum files named "Experiment 2" or "Experiment 3" on historical
`quant/*` branches. Those data and results must not be merged into this corpus
or treated as RAG evidence.

## Implementation status

The current branch implements frozen-index construction, deterministic
retrieval, hash/cutoff/exclusion validation, C0 bypass, sandbox propagation, and
generator-only prompt delivery with text-free retrieval telemetry. Retrieval
returns at most five memories and deterministically limits each historical
source ticket to two memories so one noisy ticket cannot occupy the full prompt.
The runtime also preserves cumulative model usage and already-committed GitHub
artifacts when a run-wide token budget or timeout terminates the iteration loop;
the failed result therefore remains auditable instead of incorrectly reporting
empty usage and no generated files.
The approved
boundary is enforced in code: each memory is at most 4,000 characters, the
complete retrieved-memory prompt block is at most 12,000 characters, review and
read-only analysis prompts never receive it, and results/logs retain only hashes
and memory IDs. This implementation is committed locally but is not deployed.

The feature-branch-only candidate deployment is documented in
[`ISOLATED_DEPLOYMENT.md`](ISOLATED_DEPLOYMENT.md). It uses the unscheduled
`jira_exp2_si_runner`, the `/quant-exp2` marker, candidate-only Airflow
Variables, a private runtime package, and an image pinned by digest. It does not
require a pull request to `main` and does not replace a shared Experiment 1 DAG.

The adviser-aligned, versioned formal protocol is in
[`formal-v2/`](formal-v2/README.md). It remains non-executable until the listed
approval, XCom-recovery, source/runtime, model and evaluator-isolation gates
are recorded; V5 remains diagnostic evidence rather than a formal result.

## What can run when

1. **Current local branch:** do not run; the prompt-enabled changes have not been
   pushed, merged, or deployed.
2. **After deployment and full deployment-image integration tests:** run paired
   C0/C1 smoke and pilot tickets. These are not formal evidence while the
   protocol is still a draft.
3. **After protocol approval, cutoff, corpus/index, exclusions, tasks, hidden
   tests, allocation, evaluation rubric, immutable runtime/model identifiers,
   and durable result export are frozen:** run the formal paired experiment.

Do not use manually selected memory text as formal C1. The formal treatment is
the deterministic retriever output, including empty or irrelevant results.

## Frozen index input

### Export the formal Jira API source snapshot

The all-fields CSV is sufficient for offline prototyping, but the formal source
should be a paginated Jira API snapshot so stable comment IDs, explicit UTC
offsets, authenticated bot identity, attachment IDs, and dated field changelogs
are available. Run the independent read-only exporter in the Airflow Python
environment; it reuses the existing `jira_cloud` Connection and has no Jira
mutation endpoint:

```bash
python jira-chatops-gateway/scripts/export_jira_api_snapshot.py \
  --project-key SCRUM \
  --cutoff-at 2026-08-09T11:05:50Z \
  --output-dir /private/e2/raw/jira-api-20260810-run1
```

The raw pages and intermediate record views contain private Jira data and must
remain outside Git and outside model-visible storage. They are not valid RAG
documents until a deterministic API adapter applies the approved cutoff,
redaction, exclusion, episode-pairing, and audit rules. See
[`jira_api_snapshot_export.md`](../../jira-chatops-gateway/docs/jira_api_snapshot_export.md)
for the read-only boundary, exact JSON outputs, pagination rules, tests, Airflow
execution instructions, and completion checks.

### Normalize a Jira all-fields CSV

Use Jira's **CSV - all fields** export. Repeated `Comment` and `Attachment`
columns are intentional; do not load this file with a dictionary reader that
would overwrite duplicate headers. Keep the raw export and all generated data
outside Git.

The offline normalizer produces a rich, sanitized audit corpus plus the exact
six-field `documents.jsonl` accepted by the existing frozen-index builder:

```bash
python jira-chatops-gateway/scripts/normalize_jira_csv_export.py \
  --input-csv /private/e2/Jira.csv \
  --excluded-ticket-ids /approved/e2/excluded_ticket_ids.json \
  --source-timezone Europe/London \
  --timezone-status verified \
  --bot-author-sha256 BOT_ACCOUNT_ID_SHA256 \
  --bot-author-trust-status verified \
  --additional-identities /private/e2/reviewed_identity_literals.json \
  --normalizer-repository-revision FULL_GIT_COMMIT_SHA \
  --cutoff-at 2026-08-09T11:05:50Z \
  --output-dir /approved/e2/e2-jira-corpus-v1
```

The output directory is immutable: the command refuses to overwrite it. It
contains normalized tickets, comments/events, attachment metadata (without
authors or URLs), run records, request/run episodes, builder documents, an
audit report, and a manifest with hashes. Structured actor names, reviewed
identity literals, email addresses, account IDs, common credentials, and URLs
are removed; ticket keys, candidate timestamps,
request/result text, run IDs, branches, full commit SHAs, and error types are
retained where present. Running and partial-progress reports never become
episodes. Duplicate terminal reports with the same run outcome are folded to a
canonical terminal; conflicting outcomes and multi-run request segments are
excluded from the primary episode set.

Bot-author hashes must come from trusted Jira account IDs obtained through
project administration/API data, not from comments that merely contain the
`[quant-loop-bot]` marker. Repeat the option for rotated bot accounts. The
optional additional-identity file is an operator-reviewed JSON array kept
outside Git; its content hash, not its path or raw values, is recorded in the
manifest. Structured Jira actors are removed automatically, but free-text
identity redaction still requires manual privacy review before formal freeze.
If the hashes were only inferred from marker-bearing comments, keep the default
`snapshot_candidate_unverified`; those events remain visibly flagged and the
corpus must remain a draft until an administrator/API independently verifies
the account IDs.

Because the CSV has no field-level changelog timestamps, summary and description
documents conservatively use the issue `Updated` timestamp rather than
backdating current text to issue creation. Comments and attachments use their
own envelope timestamps. Records after the cutoff remain visible in the rich
snapshot for audit, but never change pre-cutoff canonical runs or episode
pairing and never enter `documents.jsonl`.

`source-timezone` is an explicit interpretation of Jira's offset-free CSV
timestamps, not a guess made by the script. If the audit reports a material
conflict between CSV envelope times and embedded UTC bot timestamps, retain the
raw timestamp and candidate UTC fields but keep the corpus status as draft.
Confirm the Jira site/export timezone or re-export through an API that supplies
offsets before formal freeze.

The conservative, not-yet-approved ticket-key draft used during development is
`experiments/exp2/exclusions/e2_strict_draft_v1.json`. It deliberately treats
manual-review tickets as excluded. Record its approval and immutable hash in the
protocol/freeze manifest before calling an index formal or frozen.

### Build the frozen lexical index

Each generated index document is one bounded memory chunk:

```json
{"memory_id":"MEM-SCRUM-4-C10001","source_ticket_id":"SCRUM-4","source_type":"comment","source_id":"comment:10001","source_timestamp":"2026-05-01T00:00:00+00:00","text":"History remains chronological and capped."}
```

Allowed source types are `summary`, `description`, `comment`, `status_change`,
`description_update`, `bot_result`, `error_summary`, `result_summary`,
`attachment_metadata`, and `other`. Do not include credentials, email addresses,
post-cutoff data, current/formal evaluation tickets, paired tickets, RAG
implementation tickets, pilot outputs, or prohibited branches/results.

Normalizer versions using `jira-document-filter-v1` omit a retrieval document
when its source type is `summary` or `comment` and the payload after the
generated Jira label is at most 20 characters containing at most one word-like
token (for example `Test`, `Yarn`, or `104`). The corresponding rich ticket and
event records are retained for provenance and audit; only the BM25 input is
filtered. The audit records the omitted count as
`documents_skipped_low_information`, and the corpus manifest freezes the rule
version as `document_filter_rule_version`.

Keep the approved exclusions as a JSON array of Jira keys. Build a new output
path; the builder refuses to overwrite an existing frozen index:

```bash
python jira-chatops-gateway/scripts/build_jira_rag_index.py \
  --documents-jsonl /approved/e2/documents.jsonl \
  --excluded-ticket-ids /approved/e2/excluded_ticket_ids.json \
  --cutoff-at 2026-08-01T00:00:00+00:00 \
  --index-id e2-jira-index-v1 \
  --corpus-id e2-jira-corpus-v1 \
  --exclusion-list-id e2-exclusions-v1 \
  --output /approved/e2/e2-jira-index-v1.json
```

Record the printed hashes in the freeze manifest. Mount the file read-only at
the same absolute path on every Airflow worker that can execute
`docker_sandbox_runner`, then configure:

- `JIRA_RAG_INDEX_PATH`: mounted absolute path;
- `JIRA_RAG_INDEX_SHA256`: exact SHA-256 printed by the builder.

The runner fails closed if the path, digest, schema, cutoff, exclusion hash,
corpus hash, document timestamps, or sanitization checks fail. C0 does not load
either variable. C1 retrieves once per orchestration attempt before Docker
sandbox execution, and sandbox retries reuse that enriched request. C1 rejects
YARN because YARN would persist the raw memory-bearing request to HDFS. A
restarted failed workflow can retrieve again deterministically; the formal
recorder must persist the first text-free audit and reject provenance drift.
After BM25 scoring and deterministic score/memory-ID ordering, the retriever
scans the ranked candidates until it has `rag_top_k` memories, skipping any
candidate whose `source_ticket_id` has already supplied two memories. The
runtime request and text-free result audit record
`max_memories_per_source_ticket: 2`; this value is part of retriever version
`1.1.0` and must not change after the experiment freeze.

## Paired C0/C1 command template

Create two new Jira tickets with the same canonical task text and acceptance
criteria. Before the pair starts, create a protected, experiment-only branch
whose head is the approved source commit. Replace `<FROZEN_SOURCE_BRANCH>` with
that branch name. The current GitHub workflow accepts branch names here, not a
raw commit SHA. Record and verify the branch head SHA against the freeze
manifest before both runs; do not use a branch that can move during the pair.
Use a distinct ticket key/target branch for every run. Apart from
`rag_enabled`, keep every line identical.

```text
/quant
Refactor the Jira event-history construction while preserving its external behaviour.
- Keep the event schema, chronological ordering, validation rules, and history cap unchanged.
- Make the smallest maintainable code change.
- Update focused tests in the permitted test directory.
- Do not change, run, or add backtest, HDFS, YARN, or strategy-request logic.

strategy_type: refactor
resource_path: jira-chatops-gateway/dags/jira_command_validation.py
allowed_directories: jira-chatops-gateway/dags jira-chatops-gateway/tests
repo: bankingscience/BSLAgenticQuantDevLoop
branch_map: BSLAgenticQuantDevLoop=<FROZEN_SOURCE_BRANCH>
model: qwen3-coder
allow_iteration: false
max_iterations: 1
max_failed_iterations: 1
max_agent_turns: 30
max_commits_per_run: 5
timeout_seconds: 1800
max_token_budget_per_run: 50000
cpu_vcpus: 2
memory_mb: 4096
pids_limit: 256
rag_enabled: false
rag_top_k: 5
```

C1 changes only:

```text
rag_enabled: true
```

Do not paste retrieved memory into the Jira comment. The runner constructs the
canonical query. The coding prompt receives the ranked evidence up to the hard
per-memory and total-block limits; telemetry records the exact injected memory
IDs and hashes. Review and read-only analysis prompts receive none of it. C2 is a secondary
ablation and must not be run formally until its separate frozen guidance switch
and prompt hash are implemented.

## Verification for every smoke/pilot run

- Confirm the runtime request has normalized `rag_enabled` and `rag_top_k`.
- C0: confirm there is no `retrieval_context` and no index access.
- C1: confirm index/corpus/exclusion IDs and hashes, query hash, candidate count,
  ranked retrieved memory IDs, scores, top-k, and exact injected memory IDs are
  present in durable telemetry. Additionally verify the bounded evidence block reached
  the generator prompt and did not reach the evaluator prompt. Its telemetry
  must say `delivery_mode=generator_prompt`, `prompt_injected=true`, and include
  the frozen prompt-template and injected-evidence hashes.
- Confirm source SHA, image digest, model, budgets, tool policy, and hidden-test
  version match across the pair.
- Confirm the protected frozen source branch still points to the manifest's
  source SHA. A raw SHA in `branch_map` is invalid in the current workflow.
- Do not use a mutable image tag such as `:stable` for formal runs. Configure
  `SANDBOX_IMAGE` with an immutable image digest and record it.
- Record the exact served model/deployment identifier, endpoint routing, and
  sampling parameters. The Jira `model: qwen3-coder` label alone is not an
  immutable model identity. Use [`MODEL_RUNTIME_FREEZE.md`](MODEL_RUNTIME_FREEZE.md)
  for the administrator/non-administrator checks and redacted freeze record.
- Confirm a real commit exists on the new `quant/<TICKET>` branch.
- Pull the branch and run the frozen external focused/hidden tests. The GitHub
  MCP agent still does not provide a trustworthy substitute for this step.
- Store a neutral review package and keep condition/retrieval metadata hidden
  from the reviewer.

The current workflow does not yet provide a complete formal-experiment archive.
Before formal runs, add a durable recorder for the full validated request,
text-free result telemetry, branch/commit data, test artifacts, timing, token
usage, and a canonical task hash computed without `rag_enabled`, `rag_top_k`,
ticket/run IDs, or retrieved evidence. C0 and C1 for the same task must share
that hash. Airflow XCom, a Jira summary, or the default `/tmp` recovery state is
not sufficient long-term storage.

C1 is Docker-only. Do not enable YARN fallback for a RAG treatment run because
the YARN request staging path persists request JSON to HDFS.

## Outcomes

The primary coding outcome is all mandatory hidden tests passing at task level.
Answer completeness is frozen mandatory-requirement coverage, not response
length. Strategy-task outcomes are evaluated separately; use net out-of-sample
Sharpe with a drawdown guardrail and the additional metrics specified in
`PROTOCOL.md`. Do not combine strategy performance and general code correctness
into one score.
