# Experiment 2 formal-v2 protocol: adviser-aligned RAG quality evaluation

## Protocol metadata

- Experiment ID: `E2-formal-v2`
- Protocol version: `2.0.0-rc2`
- Status: preregistered draft; execution prohibited until approval and freeze
- Date prepared: `2026-08-20` (Europe/London)
- Primary comparison: `C1_rag - C0_no_rag`
- Preserved diagnostic predecessor: `exp2-v5-paired-smoke`
- Frozen historical index: `e2-jira-index-v1`, SHA-256
  `72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1`

This protocol does not relabel or overwrite V5. V5 remains diagnostic evidence
from SCRUM-183/184 and is excluded from the formal estimate.

## Objective and research question

Determine whether deterministic BM25 retrieval of pre-cutoff Jira project
memory improves condition-blinded output quality on three fixed prompts, one
from each adviser-suggested task family, while measuring the additional cost,
latency, reliability and retrieval relevance.

Research question: when the task, source revision, model alias, prompt template,
tools, resource limits and evaluation rules are held fixed, does enabling the
frozen Jira-memory retriever improve task-quality score relative to the same
coding agent without retrieval?

## Directional expectation and scope

- Directional expectation: the task-level macro-average quality score is higher
  under C1 than C0.

Because formal-v2 contains only three deliberately different fixed prompts,
replication estimates stochastic variation for those prompts; it does not
sample task variation or estimate task-family effects. Inference is descriptive
and bounded to these exact prompts. No population-wide or statistically
significant RAG claim will be made from this study.

## Conditions

### C0 — no RAG

The RAG-capable candidate runtime is used with `rag_enabled: false`. It must not
load the index, retrieve memory or inject a retrieval block.

### C1 — frozen BM25 Jira RAG

The same runtime is used with `rag_enabled: true`, `rag_top_k: 5`, and no other
intended difference. It must use the frozen BM25 index and deliver the original
deterministic ranked result to the generator prompt. Retrieved items may not be
hand-picked, removed, reordered or rewritten after inspection.

## Task set

The task wording, output contract, scoring rubric and hidden evaluator are
frozen before the first run.

### T1 — legacy result-emission comprehension

The agent inspects the small existing `result_io.py` implementation and creates
a bounded developer note. It must explain validation and fallback, compact JSON,
the named XCom budget and actual measurement, the sole trimming rule, atomic
file replacement and final stdout contract, citing code symbols. Runtime code
must not be changed.

Primary evaluation: a condition-blinded 0–10 gold-claim rubric. Scope and
structure are checked deterministically; semantic claim correctness is scored
against private reference answers for the public claim categories.

### T2 — programmatic class/interface design

The agent adds a typed abstract monotonic-clock interface and a no-argument
system-clock implementation, then injects the clock into the existing
`BudgetGuard`. The task requires explicit overrides, preserved default
`time.monotonic` behaviour, deterministic fake-clock tests, documentation and no
change to the strict timeout or budget semantics.

Primary evaluation: hidden behavioural/interface checks plus a condition-
blinded 0–10 code-quality rubric covering interface design, override
correctness, preserved behaviour, documentation, focused tests, dead
code/parameters and unnecessary complexity.

### T3 — tabular cumulative returns

The agent reads a fixed five-row, two-asset return table and produces a
machine-readable cumulative-return table using geometric compounding. The task
also requests the equally weighted daily-rebalanced portfolio path. It is a
small arithmetic task, not a backtest or trading-strategy experiment.

Primary evaluation: exact private numeric answer key with a frozen tolerance,
scored on a 0–10 scale.

## Replication and allocation

- Three replicates per condition and task: `3 tasks × 2 conditions × 3 = 18`
  formal runs.
- Replicates begin from the same immutable source commit and use fresh Jira
  ticket/comment IDs and target branches.
- Runs are serial (`max_active_runs=1`).
- Predeclared within-prompt pair order:
  - T1: R1 C0→C1, R2 C1→C0, R3 C0→C1;
  - T2: R1 C1→C0, R2 C0→C1, R3 C1→C0;
  - T3: R1 C0→C1, R2 C1→C0, R3 C0→C1.
- This gives five C0-first and four C1-first pairs, while both orders occur for
  every prompt.
- The nine adjacent pair blocks are globally ordered by ascending SHA-256 of
  `E2-formal-v2-block-order-v1`, a NUL byte, and the block ID. The frozen order
  is: `T3-R2`, `T2-R3`, `T2-R2`, `T3-R1`, `T2-R1`, `T1-R3`, `T1-R2`,
  `T3-R3`, `T1-R1`.
- The generated run package records and hashes the resulting 18-position
  sequence. Pair members remain adjacent.

Replicates are nested within prompts and are not treated as nine independent
tasks or task families.

## Frozen within-pair invariants

Within each C0/C1 replicate pair, freeze and verify:

- canonical task text and hash;
- source repository commit and protected source branch head;
- candidate Airflow revision and sandbox image digest;
- prompt-template and tool-policy hashes;
- server-enforced readable-ref policy and its negative-preflight evidence;
- model alias and observed response model/deployment metadata;
- client-side sampling fields (currently none) and proxy-config status;
- token, turn, iteration, timeout, commit, CPU, memory and PID limits;
- allowed paths and target artifact contract;
- hidden evaluator, gold answer/claims and rubric hashes;
- corpus/index IDs and hashes and fixed retrieval configuration;
- evaluator prompt/model for exploratory AI review;
- cache disabled/read-through status and stable routing/fallback policy.

The only intended command difference inside a pair is `rag_enabled`.

### Server-enforced repository-ref isolation

Model-facing repository tools are restricted by the server, not only by prompt
instruction. For each run, the immutable source branch and that run's fresh
target branch are the only readable refs; only the target is writable. Reads of
`main`, V5, a sibling condition, another replicate, earlier formal output or an
arbitrary ref must be rejected before a GitHub content/list operation. Target
branch creation must accept only the configured source as its base. Missing
source/target maps fail closed in the deployed runtime.

The policy version, source SHA and tool-server SHA are frozen. A sanitized
negative-preflight audit exercises `read_file`, `read_files`, `find_in_file`,
`list_files` and target creation. It records only refs, allow/deny outcomes and
error categories, never file contents or credentials.

## Outcomes

### Primary outcome

Each submission receives a frozen 0–10 task-quality score. For each task and
condition, average its three replicate scores. The predeclared descriptive
estimand is the macro-average across the three prompt means:

`Delta_quality = mean_prompt(score_C1 - score_C0)`.

Also report every replicate score and paired difference so aggregation cannot
hide failures.

### Key supporting outcomes

- full task success (`10/10` and all mandatory gates passed);
- fraction of mandatory deterministic checks passed;
- atomic requirement coverage;
- out-of-scope change/scope-gate result;
- for T1, public-category claim recall, correctness and unsupported-claim rate;
- for T2, hidden behaviour/interface pass rate and blinded code-quality score;
- for T3, correct-cell proportion and maximum absolute numeric error.

### Secondary cost/process outcomes

- prompt, completion and total tokens;
- pipeline duration and end-to-end duration;
- actual model-call count, retries and infrastructure failures;
- generated commit count and repair attempts;
- changed-file count and diff size.

Commit count is descriptive only. It is not an efficiency proxy and cannot by
itself demonstrate improvement.

### Retrieval diagnostics for C1

- returned memory IDs, scores, source-ticket diversity and provenance hashes;
- condition-blinded relevance grade per memory: 0 irrelevant, 1 indirectly
  relevant, 2 directly useful;
- Precision@5, defined as `count(grade >= 1) / 5`, with missing ranks assigned
  grade 0;
- within-return ranking nDCG@5 using gain `2^grade - 1`, discount
  `log2(rank + 1)` and IDCG from the five returned-item grades sorted descending;
  all-zero grades yield zero;
- a qualitative used/ignored/misled interpretation only when supported by
  explicit output claims or an auditable provenance link. Otherwise record
  `not_determinable`.

Retrieval relevance is a mechanism diagnostic, not a task-quality outcome.
The nDCG value is not corpus-wide retrieval completeness, and Recall@5 is not
reported without a frozen corpus-level relevant set.

### Exploratory AI code/answer review

A fixed evaluator may score T1/T2 submissions for accurate understanding,
dead code/parameters, sensible comments, method efficiency and clarity. The
evaluator receives neutral submission IDs, never condition labels or retrieved
memory. This score is exploratory and cannot replace deterministic or blinded
human primary evidence.

## Blinding and scoring

- One designated primary scorer, whose identity and commitment are frozen
  privately before execution, receives neutral submission IDs and the
  task/rubric, but not Jira
  ticket, target branch, condition, retrieval telemetry, token usage or timing.
- The condition map is stored separately until all primary scores are locked.
- T3 is scored deterministically. T1 and the manual portion of T2 are scored by
  the single designated condition-blinded scorer. The study does not claim
  inter-rater reliability unless a separately preregistered second-scorer plan
  is added before the first run.
- Hidden tests, gold claims and exact T3 answers remain outside every
  agent-visible repository/ref. T2 generated code is executed only in a pinned,
  unprivileged, network-disabled evaluator sandbox with no private mounts; the
  private coordinator consumes its sanitized result and never imports submission
  code. The host runner keeps its attestation key outside the container and
  signs a result envelope bound to the source commit, generated commit, image
  digest, hidden-test hash and runner hash. The coordinator verifies that
  signature and every binding before scoring. Every evaluator uses a detached,
  clean, non-symlinked artifact checkout and writes results outside that
  checkout.

## Validity and replacement rules

An interruption before the first intended model call may resume the same frozen
run identity after repair; that is operational recovery, not replacement. Wrong
source/image/model, invalid treatment delivery, index/hash drift, prohibited-ref
access or instrumentation defects discovered after a model call invalidate the
entire adjacent pair.

Retain as genuine agent outcomes: incorrect output, hidden-test failure,
incomplete task, budget exhaustion after the first intended model call,
model-context failure after task start, misuse of memory, or timeout after task
execution begins.

A post-execution orchestration failure does not erase an already persisted
business result. Record operational state separately from task quality.

At most one invalid pair in the whole study may be replaced, using a precreated
replacement pair with the same order. The decision and defect evidence must be
locked before viewing task output or condition-blinded scores. A second invalid
pair stops formal-v2 and requires a new protocol version. Genuine agent
failures, budget exhaustion and correctly delivered context-limit failures are
never replaced.

## Analysis

1. Lock neutral-ID scores before joining condition labels.
2. Report replicate-level scores and costs.
3. Average replicates within each task and condition.
4. Calculate the three prompt-level C1−C0 quality differences and their
   macro-average.
5. Report medians/ranges of cost metrics within task/condition; do not infer
   benefit from token, time or commit counts alone.
6. Provide sensitivity results using binary full-success and deterministic
   requirement coverage.
7. Describe retrieval relevance and failure modes alongside, but separate from,
   the primary estimate.
8. Make no significance, task-family or population-generalisation claim from
   three fixed prompts.

Missing results are never silently scored zero. Valid agent failures use their
rubric outcome; infrastructure-invalid runs are listed separately and replaced
only under the frozen rule.

## Leakage and safety boundaries

- Preserve the 2026-08-09 Jira cutoff and frozen index; do not re-export Jira.
- Do not add formal tickets/results to the corpus or index.
- Never expose hidden evaluators, gold claims or expected numeric answers to
  the coding agent or Jira.
- Do not edit shared `main`, production DAGs or non-`EXP2_SI_` Airflow
  Variables.
- Do not overwrite V5 or earlier diagnostic evidence.
- Do not commit raw Jira data, private evidence, logs, identities or secrets.
- Work only on `feature/exp2-rag-v1` and explicitly frozen personal branches.
- C1 is Docker-only; YARN may not stage a memory-bearing request.
- The GitHub MCP server must permit reads only from the exact frozen source ref
  and the current fresh target ref, and branch creation only from the frozen
  source ref. Before execution, negative tests must prove that `main`, V5,
  earlier-formal, paired and arbitrary refs are blocked without a GitHub read.
- Every formal target branch must be absent before its run; target reuse is an
  invalid-run condition.
- Stop if readable-ref or condition isolation cannot be proved. Do not expose
  credentials, private task answers, retrieved text or private prompts in logs.
- Check disk capacity without deleting Docker, Airflow, HDFS or experiment data;
  no cleanup action is authorised by this protocol.

## Freeze and approval gates

Execution is prohibited until all are true:

1. XCom repair `cfb27068abb041b77a56e37820e628a9537e0dda` is deployed to
   the isolated candidate runtime and the terminal SCRUM-184 recovery check
   proves no new model call, commit or duplicate Jira writeback.
2. The exact task texts, public inputs, hidden evaluators, rubrics, run schedule
   and manifest hashes are complete.
3. The source branch, candidate bundle, image digest, corpus/index, prompt and
   tool-policy hashes are read back and frozen.
4. LiteLLM alias, client/proxy sampling, cache status, routing and fallback
   behaviour are verified stable. Cache must be disabled for formal calls. If
   these cannot be verified, formal-v2 remains non-executable rather than being
   downgraded silently.
5. The researcher records protocol approval, approver and UTC timestamp.
6. In the exact deployed sandbox, a source-ref sentinel read succeeds while
   every model-facing read tool rejects `main`, `quant/SCRUM-183`,
   `quant/SCRUM-184`, a sibling-condition ref and an arbitrary ref before a
   GitHub backend call. The current target is absent before creation, then alone
   becomes readable/writable; base-branch substitution is rejected. Any failure
   prohibits model calls.

Any change to tasks, primary outcome, hidden tests, condition definition,
replication, allocation or invalid-run rules after approval requires a new
protocol version. Pilot-driven changes cannot be applied silently to formal-v2.
