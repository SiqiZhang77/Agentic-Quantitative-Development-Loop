# Experiment 2 Terra T3 RAG/no-RAG formal control v3

This package controls a new six-observation T3 study. The first V2 observation
stopped before any provider call because the shared formal runtime interpreted
every explicit single-agent request as an Experiment 3 request and therefore
rejected Experiment 2's RAG fields. V3 gives Experiment 2 its own named runtime
contract and uses the runtime-native negative-ref manifest. The V2 evidence is
retained as an invalid, zero-model infrastructure observation and is not pooled
with V3. This study does not change or extend the existing T1/T2 agreement.

## Frozen comparison

- Three paired replicates; C0/C1 order is counterbalanced.
- C0 has no retrieval context. C1 contains one precomputed result from the
  frozen `e2-jira-index-v1` BM25 index.
- Both conditions use the same Terra model, single-agent architecture, public
  T3 task, source commit, image, tools, two-attempt limits and resource budget.
- `formal_execution_contract=exp2_single_agent_rag_v1` selects the E2 path for
  both conditions. It changes no task wording and exposes no answer material.
- The output is scored after each observation by the hash-bound private 25-item
  T3 scorer. Saved Level A/B/C checkpoints remain eligible for partial scoring.

## Safety boundary

`build_control_package.py` writes the retrieval-bearing requests, condition map
and agreement only to a directory outside Git. It refuses to overwrite an
existing package, requires an exact remote source commit, requires all six target
branches to be absent and makes no model call.

`run_formal_pair.py` preflights all six observations by default. Execution
requires one explicit replicate and a matching authorization value. It runs the
two conditions serially in the frozen order, preserves post-call failures and
does not allow an all-six execution command.

No formal observation may start until the source and control branches are
published, the final agreement is generated from the committed control files,
and the 6/6 zero-model preflight reports `status=ready` and
`provider_call_count=0`.
