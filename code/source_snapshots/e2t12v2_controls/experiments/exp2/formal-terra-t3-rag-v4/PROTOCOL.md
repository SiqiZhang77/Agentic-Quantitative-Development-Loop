# Experiment 2 Terra T3 RAG/no-RAG formal control v4

This package controls a new six-observation T3 study. V3 produced one complete
C0 observation and one post-model C1 instrumentation failure. The V3 C0 result
remains a valid V3 outcome but is not pooled with V4 because the pair is
incomplete and V4 uses a different runtime image. The V3 C1 files remain
instrumentation evidence and are not converted into a score. Neither V3 run is
deleted, relabelled, or rerun under its old identifier.

V4 corrects two shared runtime defects: the final response schema can now record
RAG as either enabled or disabled for the explicit Experiment 2 single-agent
contract, and the direct OpenAI HTTP client is awaited before its Python event
loop closes. The manager-star rule that forbids RAG remains unchanged. V4 does
not change the task text, frozen retrieval result, model, tools, two-attempt
limit, token budget, or private scorer.

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
