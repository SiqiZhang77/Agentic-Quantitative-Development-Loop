# Experiment 2 Protocol: Retrieved Jira Project Memory

## Protocol metadata

- Experiment ID: `E2-v1`
- Protocol version: `1.1-draft`
- Status: Proposed; pending review and approval
- Approval record candidate: `SCRUM-123` (no approval comment, reviewer,
  decision, or UTC approval timestamp recorded in the 2026-08-09 snapshot)
- Pilot excluded from formal evaluation: `SCRUM-114` and `quant/SCRUM-114`

This protocol becomes frozen only after the approval record identifies the
reviewer, approval decision, and UTC approval timestamp. After approval, changes
to the data boundary, conditions, primary outcome, task-selection rules, or
invalid-run rules require a new protocol version. Results produced under
different protocol versions must not be pooled without an explicit analysis
plan.

## Objective

Define and approve Experiment 2 before building the formal Jira memory index or
running the formal C0/C1 evaluation.

The experiment tests whether adding retrieved Jira project memory improves the
coding agent's performance when all other runtime conditions remain unchanged.

`SCRUM-114` is an end-to-end C0 pilot only. It is not part of the formal
evaluation, and its branch `quant/SCRUM-114` must remain unchanged.

## Research question

When the coding agent, source code, runtime environment, prompt template, model,
tools, and execution budget are kept the same, does retrieved Jira project
memory improve coding-task performance?

## Hypotheses

### H0 — Null hypothesis

When all other runtime conditions are held constant, enabling retrieved Jira
project memory does not improve paired task-level coding success compared with
the no-retrieval condition.

### H1 — Alternative hypothesis

When all other runtime conditions are held constant, enabling relevant
retrieved Jira project memory improves paired task-level coding success compared
with the no-retrieval condition, primarily measured by whether all mandatory
acceptance or hidden tests pass.

## Experimental conditions

### C0 — No retrieval

The agent uses the RAG-capable runtime, but Jira retrieval is disabled. C0 must
not call the retriever and must not receive retrieved Jira evidence.

### C1 — Jira RAG

The agent uses the same runtime with Jira retrieval enabled. Relevant historical
Jira evidence is retrieved from the approved frozen index and added to the
coding-agent prompt.

The only intended difference between C0 and C1 is whether historical Jira memory
is retrieved and injected into the coding prompt.

C1 must inject the deterministic retriever's original ranked result, including
an empty result or an irrelevant result. Evidence must not be hand-picked,
removed, reordered, rewritten, or supplemented after seeing retrieval or agent
outcomes. Manually selected `MEM-*` evidence is an oracle-context prototype and
must not be reported as formal C1.

### C2 — Guided Jira RAG (secondary ablation)

C2 is optional and is not part of the primary C0-versus-C1 hypothesis. For the
same task and replicate, C2 receives exactly the same query, memory IDs, ranks,
scores, text, and injected-text hash as C1. Its only treatment difference is one
frozen instruction telling the agent to judge memory relevance, verify it
against repository evidence, and ignore conflicting or irrelevant memory.

C1 versus C2 tests whether explicit memory-use guidance changes performance. A
C2 run is invalid when its retrieved evidence differs from its paired C1 run.

## C0/C1 invariants

The following must remain identical within each paired comparison:

- source repository commit SHA;
- sandbox image digest;
- coding-agent prompt template;
- agent model and model parameters;
- token, turn, iteration, and timeout limits;
- GitHub MCP tools and permissions;
- tool-policy version and hash;
- CPU, memory, and YARN configuration;
- canonical task specification;
- acceptance criteria and hidden tests;
- reviewer rubric;
- backtest data and parameters, where applicable.

If the source SHA or sandbox image differs between C0 and C1, the comparison is
invalid because the agents are no longer working from the same codebase or
runtime.

## Jira memory scope

E2-v1 may include historical Jira data with an eligible timestamp at or before
the approved cutoff:

- ticket key, summary, and issue type;
- description;
- human comments;
- status and description change events available from the Jira API or approved
  snapshot source;
- bot workflow results;
- structured error and result summaries;
- attachment metadata.

E2-v1 will not include:

- arbitrary attachment contents or OCR;
- user email addresses, account identifiers not required for stable provenance,
  credentials, or secrets;
- GitHub code diffs;
- full CI or test logs;
- complete reviewer transcripts;
- data created after the approved cutoff.

These sources may be considered in a later memory version.

## Cutoff and freeze rules

A UTC `cutoff_at` value must be approved and recorded before the formal Jira
snapshot is created.

Only Jira content with an eligible source timestamp at or before `cutoff_at` may
enter the frozen E2-v1 corpus and index. Extraction time must not be used as the
eligibility timestamp.

The cutoff must be earlier than the creation of every formal C0/C1 evaluation
ticket. The normalized corpus and built index must be content-addressed or
otherwise assigned immutable IDs and hashes before the first formal run.

## Leakage-prevention rules

The following data must not enter the formal E2-v1 corpus or RAG index:

- the current ticket being processed;
- the paired C0 or C1 copy of the same evaluation task;
- formal experiment outputs, including generated code, bot results, reviews,
  test results, backtest results, and target branches;
- all formal evaluation tickets;
- `SCRUM-114` and `quant/SCRUM-114`;
- E2-RAG implementation tickets;
- data with an eligible timestamp after `cutoff_at`.

The current ticket must still be provided directly to the agent as current task
context. It is excluded only from historical retrieval.

These exclusions prevent the retriever from returning the current requirement,
exposing the paired condition's solution, or using records created specifically
while building the RAG system. E2-v1 therefore measures the value of genuine
pre-existing project history.

Completed E2-RAG implementation tickets may be included in a later experiment
about whether development history helps future maintenance tasks. They are not
included in E2-v1.

If prohibited evidence is retrieved during a C1 run, that run is invalid. If C0
calls the retriever or receives retrieved evidence, that run is invalid.

## Evaluation-task design

Formal evaluation tasks must:

- be created after the Jira memory cutoff and index freeze;
- not already appear in the memory corpus;
- use the same canonical task specification for C0 and C1;
- use separate Jira keys, unique run IDs, and clean target branches;
- start from the same source commit SHA;
- have identical acceptance criteria and hidden tests;
- be selected without inspecting C0/C1 outcomes.

The evaluation set should include a mixture of:

- small bug fixes;
- refactoring tasks;
- schema or configuration changes;
- documentation and code-integration tasks;
- tasks that require historical project knowledge;
- tasks for which historical memory is unlikely to help.

`SCRUM-114` may be referenced as an end-to-end pilot, but it must not be counted
as a formal C0 result.

## Execution-order allocation

Within each task pair, C0/C1 execution order must be randomized or
counterbalanced. The allocation method, seed where applicable, assigned order,
and actual start time must be recorded before either result is inspected.

For a small evaluation set, counterbalancing is acceptable: approximately half
the task pairs run C0 first and half run C1 first. A rerun must not silently
change the originally assigned order.

## Blinded human review

Human reviewers must not be told whether a submission was produced by C0 or C1.
Review packages should use neutral submission IDs and hide condition-revealing
branch names, retrieved-evidence records, prompts, and run metadata.

The code diff, relevant test results, canonical task specification, and approved
review rubric may be shown. The condition-to-submission mapping must remain
separate until review scores are finalized. Any unblinding must be recorded.

The production `review_agent` and every human review package must be isolated
from retrieved memory, retrieval scores, condition labels, and condition-specific
prompt text. Read-only analysis generation may use C1/C2 evidence, but the agent
or person that evaluates its answer must not receive that evidence through the
evaluation channel.

Hidden tests and answer keys must be stored outside every repository/ref the
coding agent can inspect. Their version and hash must be frozen before execution.
The agent must not have GitHub-tool access to paired-condition branches, earlier
formal experiment outputs, hidden-test repositories, or neutral-review mappings.

## Evaluation metrics

### Primary outcome

Paired task-level success: a task succeeds only if all mandatory acceptance or
hidden tests pass. Report the C0 and C1 task-level success rates and the
within-pair outcomes.

### Supporting test metric

The proportion of individual mandatory acceptance or hidden test cases passed.
This is reported separately from task-level success.

### Secondary outcomes

- completion of all non-testable acceptance criteria;
- regression-test results;
- unrelated or out-of-scope changes;
- blinded human code-review score;
- number of repair attempts;
- execution time;
- token usage and cost.

### Requirement and answer completeness

Before any run, each task must be decomposed into frozen atomic mandatory
requirements. Report, per task, the fraction satisfied and whether all mandatory
requirements were satisfied. Aggregate requirement coverage by macro-averaging
task-level proportions so tasks with more rubric items do not receive more
weight merely because they are longer.

For read-only analysis or question-answering tasks, additionally freeze a gold
claim set and report claim recall, claim precision/correctness, citation coverage,
citation correctness, and unsupported-claim rate. Answer length is not a
completeness metric. Reviewers remain blinded; at least 20 percent of formal
answers should receive independent duplicate review, with an agreement statistic
chosen before unblinding.

### Retrieval diagnostics for C1

- Precision@k;
- Recall@k;
- MRR;
- nDCG@k where graded relevance labels are available;
- retrieved-source relevance;
- evidence provenance;
- whether retrieved evidence was used correctly.

Recall@k may be reported only when the denominator is a pre-labelled set of gold
memory IDs. If relevance labels come from pooled candidates, label and report the
metric as pooled-candidate recall rather than corpus-wide Recall@k.

### Quantitative-strategy task subset

Backtest metrics apply only to pre-registered strategy tasks and must not be
combined with ordinary code correctness into one overall score. The recommended
primary strategy outcome is net out-of-sample Sharpe with a maximum-drawdown
guardrail. Also report CAGR, annualized volatility, Sortino, maximum drawdown,
Calmar, alpha/beta, tracking error or information ratio where applicable,
turnover, transaction-cost-adjusted return, hit rate, and exposure.

Freeze the input-data hash, benchmark, risk-free series, transaction-cost model,
formation/holding timing, annualization convention, and walk-forward or
out-of-sample split. Repeated in-sample optimization followed by reporting the
best Sharpe is prohibited. Confidence intervals and any multiple-comparison or
deflated-Sharpe adjustment must be pre-specified.

## Scale and statistical analysis

Use three distinct stages:

- smoke: 2 tasks × C0/C1 × 1 run = 4 infrastructure checks, excluded;
- primary pilot: 6 tasks × C0/C1 × 3 replicates = 36 development runs,
  excluded;
- optional C2 pilot: the same 6 tasks × C2 × 3 replicates = 18 additional
  development runs, only if the C2 ablation is pre-registered;
- formal: determine size by a pilot-based power calculation; if size must be
  frozen now, use at least 24 distinct tasks × C0/C1 × 3 replicates = 144 runs,
  plus C2 on a pre-registered subset if budget permits.

Balance memory-dependent and memory-neutral tasks and task families. Replicates
are nested within task and are not independent experimental units. Freeze the
task-level estimand and replicate aggregation rule before formal execution.
Report task-clustered uncertainty, including a task-cluster bootstrap confidence
interval for the C1-minus-C0 success-rate difference. Any mixed-effects or
paired model, covariates, missing-data handling, and multiplicity correction
must be selected before condition labels are unblinded. The 24-task fallback is
a planning floor, not evidence of adequate power; the final size should use the
pilot's task-clustered, discordant-outcome assumptions.

Retrieval quality metrics are diagnostic and are not substitutes for coding-task
correctness.

## Invalid runs and valid agent outcomes

A run must not be included in the formal C0/C1 comparison when:

- C0 and C1 use different source commit SHAs;
- C0 and C1 use different sandbox image digests;
- C0 calls the retriever or receives retrieved evidence;
- C1 uses the wrong, mutable, or unfrozen corpus/index;
- the target branch contains pre-existing experiment changes;
- the treatment condition cannot be verified from durable evidence;
- prohibited or future data is retrieved;
- Jira, GitHub, Airflow, YARN, or HDFS infrastructure fails before the agent can
  begin the intended coding task.

The following are normally valid agent outcomes and must remain in the results:

- generated code fails tests;
- the agent does not complete the requested change;
- the agent exceeds its configured token or iteration budget;
- the agent misuses relevant retrieved evidence;
- the agent times out after beginning the intended coding task.

For every excluded run, record the reason, supporting evidence, and whether a
replacement run was attempted. Replacement policy must be applied equally to C0
and C1 and must not depend on task outcome.

## Freeze manifest

Before formal evaluation, record at least:

- `experiment_version`;
- `protocol_version`;
- protocol approval record and UTC approval timestamp;
- `source_commit_sha`;
- protected frozen source branch and independently verified head SHA;
- `sandbox_image_digest`;
- `prompt_template_hash`;
- `tool_policy_hash`;
- `agent_model`;
- `model_parameters`;
- token, turn, iteration, and timeout limits;
- `cutoff_at`;
- Jira snapshot ID and content hash;
- normalized corpus ID and content hash;
- index ID and content hash;
- retrieval backend name and version;
- tokenizer, analyzer, or lexical-index configuration, where applicable;
- embedding model, version, dimension, and normalization only where dense or
  hybrid retrieval is used; otherwise these fields must be `null` or absent;
- retrieval configuration, including `top_k`, score threshold, filters, and
  reranking/diversification configuration, including the frozen maximum of two
  returned memories per historical source ticket;
- normalized-document filter version and the audited number of low-information
  retrieval documents omitted while retaining their rich source records;
- exclusion-list version and hash;
- evaluation task IDs and pair mappings;
- canonical task IDs and hashes computed without treatment, ticket/run IDs, or
  retrieved evidence;
- randomization/counterbalancing method, seed, and assigned order;
- test-suite version;
- review-rubric version;
- reviewer-blinding procedure version.

## Acceptance criteria

- The research question, H0, and H1 are documented.
- C0 and C1 are clearly defined.
- The only intended experimental difference is retrieval.
- C0/C1 invariants are documented.
- Jira memory include/exclude rules are approved.
- A UTC cutoff and freeze rule is approved.
- Current-ticket, paired-ticket, implementation-ticket, and future-data leakage
  rules are documented.
- `SCRUM-114` is recorded as an end-to-end pilot and excluded from formal
  evaluation.
- Formal task-selection rules are documented.
- Paired execution-order allocation is documented.
- Reviewer blinding is documented.
- Primary, supporting, secondary, and retrieval metrics are documented.
- Invalid infrastructure runs are distinguished from genuine agent failures.
- The freeze-manifest fields are agreed.
- The protocol is reviewed and approved before formal C0/C1 evaluation begins.

## Scope of the protocol-approval artifact

The original E2-00 protocol ticket and this protocol document define the study;
they do not by themselves deliver the implementation below. Some items (frozen
index construction, retrieval, and generator-only prompt injection) may exist on an implementation branch
while the protocol remains a draft. Formal execution is still blocked until
every applicable item has been implemented, verified, frozen, and approved.

- implementing Jira data extraction;
- modifying the Jira poller;
- memory normalization or chunking;
- embeddings or index construction;
- retrieval or reranking;
- coding-prompt injection;
- running formal C0/C1 tasks;
- modifying `main`;
- modifying `quant/SCRUM-114`;
- fixing backtest or HDFS infrastructure.

## Completion definition

This protocol is complete when the Experiment 2 rules have been reviewed and
approved and the formal experiment can be run without changing the data
boundary, experimental conditions, or evaluation rules after observing results.

Before changing the status from Proposed to Frozen, record:

- the exact Jira E2-00 ticket key;
- reviewer name or approved reviewer identifier;
- approval decision;
- approval timestamp in UTC;
- final protocol version.
