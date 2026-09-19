# E4V1 four-cell study controls (prospective and not executable)

This directory prepares a future experiment that crosses the two switches that
were previously studied separately:

| Cell | Architecture | Retrieval |
|---|---|---|
| `M0R0` | one Terra agent | off |
| `M0R1` | one Terra agent | on |
| `M1R0` | manager, architect and developer roles | off |
| `M1R1` | manager, architect and developer roles | on |

The study covers `T1`, `T2` and `T3`, uses four replicates, and therefore has
`3 × 4 × 4 = 48` observations.  A single executable block will eventually
contain the four observations for one task and one replicate.  Blocks must run
serially; the launcher must not start the next block until all four results and
their audit records have been preserved.

The four within-block orders use a balanced Williams design:

1. `M0R0, M0R1, M1R1, M1R0`
2. `M0R1, M1R0, M0R0, M1R1`
3. `M1R0, M1R1, M0R1, M0R0`
4. `M1R1, M0R0, M1R0, M0R1`

Each cell appears once in every position across the four replicates, and each
directed transition between two different cells appears once.  This reduces
the chance that an earlier run systematically helps or harms one cell.

## Current safety boundary

`config/e4v1.prospective.json` contains visible `__UNRESOLVED_...__`
placeholders for the source commit, image, runtime schemas, task bytes,
evaluators and frozen retrieval assets.  This is intentional.  While even one
placeholder remains:

- `preflight_e4v1.py` reports `status: unready` with zero provider calls;
- `build_e4v1_agreement.py` refuses to create an output directory;
- there is no formal launcher and no command in this directory that calls a
  model, retriever, evaluator, GitHub, Jira or Airflow.

The draft also fixes `agreement_generation_enabled: false`.  This version of
the builder therefore cannot create an agreement even if somebody replaces the
placeholders locally.  Enabling agreement generation requires a separately
reviewed follow-up change after all bindings have been independently verified.

The existing Experiment 2 and Experiment 3 agreements and observations are not
inputs to this prospective package and must not be overwritten or relabelled.

## What must be frozen before an agreement may exist

1. One published source ref and full source commit used by all four cells.
2. One immutable container digest and hashes of the runtime request schema,
   runtime response schema, item-submission schema, model-visible tool
   fingerprint and tool implementation.
3. Byte-identical task text and public inputs for each task in every cell.
   T3 additionally binds the eight exact public files under
   `experiments/shared/t3-quant-suite-v2` and the 25-item result contract.
4. Hash-only evaluator bindings for `T1`, `T2` and `T3`; evaluator contents stay
   outside the model-visible repository.
5. One retrieval corpus, index, exclusion list, retriever version and cutoff.
   The same frozen retrieval assets are bound to every cell; only the two
   retrieval-on cells receive the verified retrieval context.
6. Equal total model-call, token, time, CPU and memory budgets: at most two
   attempts, 20 calls/350,000 tokens per attempt and 40 calls/700,000 tokens per
   observation.  The manager cell divides its 20 calls as 3/3/14 among
   manager/architect/developer but does not receive more total capacity.
7. A fresh zero-model preflight for the selected four-observation block.

The checked-in configuration also fixes
`factorial_rag_architecture_v1` and `all_model_stages_v1`.  These strings are
part of every run identity, not condition-specific overrides.  Once local
paths and hashes are resolved, the offline preflight reads those public or
controlled files and compares their actual bytes with the declared hashes; it
never opens private evaluator content.

The RAG-on model receives the frozen retrieval text, but not the separate
delivery receipt containing hashes and memory identifiers; that receipt is
audit-only. T3 answer-saving and completion rules likewise come only from the
same frozen task text used by every cell, not from an architecture-specific
outer prompt.

The retrieval section carries a separate frozen query/context record for T1,
T2 and T3, including context byte and memory counts.  The control section also
has unresolved ref/commit fields and hashes of the reviewed identity, builder,
preflight, schema and protocol files.  These bindings make the future run
independent of which local worktree the operator happens to start from.

## Protocol document roles

`experiments/exp4/PROTOCOL_DRAFT.md` is the authoritative English prospective
protocol.  A future agreement must bind its exact bytes together with the
configuration, schema and control code.  The separate Chinese document at
`02_EXPERIMENT_CONTROL/protocols/exp4-e4v1-four-cell/PROSPECTIVE_PROTOCOL.md`
is an explanatory summary for the operator; it is not a second source of
experiment identity or execution rules.

Both documents are still drafts, so their SHA-256 commitments remain explicit
unresolved values:

- authoritative English protocol:
  `__UNRESOLVED_E4_AUTHORITATIVE_PROTOCOL_SHA256__`;
- explanatory Chinese summary:
  `__UNRESOLVED_E4_CHINESE_SUMMARY_SHA256__`.

At formal freeze, one reviewed agreement/control manifest must bind the exact
SHA-256 of both documents at the same time.  If either file changes or the two
documents describe inconsistent rules, preflight must fail rather than choose
one silently.
