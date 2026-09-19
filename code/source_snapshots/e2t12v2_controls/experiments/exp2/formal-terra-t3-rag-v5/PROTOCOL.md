# Experiment 2 Terra T3 RAG/no-RAG formal control v5

This package controls a new six-observation T3 study. V4 produced one valid C0
observation that scored 22/25 and one post-model C1 instrumentation failure.
The C0 observation remains a valid V4 outcome but is not pooled with V5 because
the V4 pair is incomplete. The C1 files remain evidence of ten Terra calls and
two unsuccessful answer attempts, but they are not converted into a formal C1
score because the final report lost proof that RAG reached the model prompt.
Neither V4 run is deleted, relabelled, or reused under a V5 identifier.

V5 carries the prompt-delivery receipt from each attempt-scoped request into the
final observation. Both completed C1 attempts must report the same receipt; a
missing or changed receipt fails closed. V5 also gives Terra a fixed, safe
explanation identifying the failed operation and argument when a constrained
calculation request has the wrong shape. Those tool rules and diagnostics are
identical in C0 and C1 and contain no answer values. V5 does not change the task
text, frozen retrieval result, model, two-attempt limit, token budget, or private
scorer.

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

`build_control_package.py` writes retrieval-bearing requests, the condition map
and the agreement only outside Git. It refuses to overwrite an existing
package, requires the exact published source and control commits, requires all
six target branches to be absent and makes no model call.

`run_formal_pair.py` preflights all six observations by default. Execution
requires one explicit replicate and a matching authorization value. It runs the
two conditions serially in the frozen order, preserves post-call failures and
does not allow an all-six execution command.

No formal observation may start until the source and control branches are
published, the immutable image is present, the final agreement is generated
from the committed control files, and the 6/6 zero-model preflight reports
`status=ready` and `provider_call_count=0`.
