# E2 T3 RAG v6 preparation log

Date: 2026-09-01

## Why this new directory exists

The v5 control package and its observations are historical evidence. They must
remain unchanged. The purpose of this v6 draft is to prepare a new study that
uses the shared T3 v2 answer-saving framework while preserving the original
Experiment 2 comparison: one Terra agent without RAG versus the same Terra agent
with RAG.

## Operations and their purpose

1. Read the v5 builder, protocol and offline tests from the clean
   `active-exp2-terra` worktree. The purpose was to retain its useful controls:
   three counterbalanced C0/C1 pairs, a single-agent architecture, identical
   budgets, precomputed RAG evidence, private evaluator hashes and a fail-closed
   boundary before any provider call.
2. Created this new `formal-terra-t3-rag-v6-draft` directory instead of editing
   any v5 file. The purpose was to keep previous evidence reproducible and make
   every v6 change prospective.
3. Added an unresolved configuration template. Source and control commits,
   source and control branches, image digest, corpus/index/retrieval hashes,
   exclusion hash, blinding-salt hash and private evaluator hashes are visibly
   marked `UNRESOLVED`. The purpose is to make a missing freeze impossible to
   mistake for a usable default.
4. Added an offline preflight. It first lists unresolved bindings. After those
   bindings are supplied, it checks local Git state, T3 v2 public/runtime schema
   equality, public file hashes, RAG file hashes, frozen retrieval identity and
   the hash-only evaluator manifest. It makes no network or model call. The
   purpose is to find preparation mistakes before any formal observation.
5. Added a preparation-only builder. It creates a common request template whose
   C0 and C1 overlays differ only at `rag_enabled`, plus a schedule and checksum
   manifest. It explicitly writes `agreement_generated=false`,
   `formal_execution_authorized=false` and
   `formal_run_command_available=false`. The purpose is to prepare evidence
   without accidentally authorizing or starting a study.
6. Added offline regression tests using temporary synthetic RAG records and a
   hash-only evaluator manifest. The purpose is to prove the fail-closed and
   fairness rules without touching external systems or private evaluator
   contents.
7. Added one shared outcome contract to the pair template. Every condition is
   scored as correct items out of all 25; missing items reduce that count but
   are also reported separately from submitted wrong answers. The purpose is
   to make incomplete work visible without changing the denominator or giving
   either RAG condition a different scoring rule.
8. Added a public, answer-free scorer-result schema and relationship validator
   to the shared T3 v2 suite, then added their paths and unresolved SHA-256
   bindings to this preparation. The schema permits only item IDs, submission
   states and correctness booleans. The validator requires the submitted and
   missing IDs to form a disjoint partition of 1 through 25 and checks that
   submitted-but-incorrect IDs match the per-item rows. The purpose is to make
   the promised distinction enforceable without exposing any candidate value,
   oracle value or private scoring logic.
9. Aligned the timeout fields with the shared two-attempt runtime. In this
   project, `timeout_seconds` is the maximum wall-clock time for one complete
   Terra attempt, so it is now 1,800 seconds. The shared budget also states the
   same 1,800-second attempt limit and the resulting 3,600-second limit for the
   whole observation containing at most two attempts. The purpose is to keep C0
   and C1 on the same time allowance and to avoid accidentally giving every
   attempt the earlier whole-observation value of 3,600 seconds.
10. Added the explicit answer-capture profile `t3_item_results_v2` to the
    preparation configuration and the common pair request. This field means
    that the 25-item store, rather than calculator availability, is the primary
    completion evidence. The purpose is to keep a future calculator-enabled T1
    or T2 run from being incorrectly treated as a 25-item T3 task.

## Current boundary

This directory is a draft, not a formal agreement. The template is expected to
fail closed until a new source commit, control commit, immutable image, RAG
binding, private evaluator binding and blinding-salt hash are deliberately
frozen. No model run should be described as E2V6 on the basis of these files.

## 2026-09-03 shared-framework synchronization

The prospective schedule now contains five complete C0/C1 pairs rather than
three. The per-attempt budget is synchronized with the current shared E2/E3
runtime profile: 20 model calls and 500,000 tokens per attempt, with two
attempts and 1,000,000 tokens per observation. The order alternates across
R1-R5. This edit changes only the still-unresolved preparation draft; it does
not alter or relabel any E2V5 observation.

## Offline verification result

- The focused preparation and shared scorer-contract tests passed.
- The default preflight returned `status=blocked`, listed every unresolved
  binding, and reported both provider and network call counts as zero.
- Python syntax compilation and the Git whitespace check passed.
- No agreement, formal run command, model call, remote Git query, Docker query
  or external retrieval was produced during this preparation work.
- Final common-runtime integration: the related E2/E3/E4 offline suite reported
  `616 passed`. The default E2V6 preflight still reported `status=blocked`,
  `provider_call_count=0`, `network_call_count=0`, no agreement and no formal
  run command. This check did not contact a model or external system.
