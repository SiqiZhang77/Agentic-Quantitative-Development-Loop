# Experiment 2 E2V6: project-memory comparison on the shared T3 runtime

E2V6 is a new prospective study. It preserves every E2V5 observation and does
not rename, overwrite or selectively replace any earlier result.

## Frozen comparison

- Five paired replicates compare C0 (project memory off) with C1 (project
  memory on). Their order alternates across pairs.
- Both conditions use one Terra agent, the same 25 mathematical questions,
  public inputs, tools, model, two-attempt limit, 20 model calls per attempt,
  500,000 tokens per attempt and local resource limits.
- C1 receives one precomputed result from the frozen `e2-jira-index-v1` BM25
  index. C0 receives no retrieval context. The retrieval result is identical in
  all five C1 observations.
- `formal_execution_contract=exp2_single_agent_rag_v2` selects the single-agent
  RAG route. The only treatment difference inside a pair is whether the frozen
  retrieval context is present.

## Shared-runtime repair and answer capture

E2V6 uses the same source commit and Docker image as E3V15. The common runtime
now checks RAG delivery only for attempts that actually called Terra. If the
second attempt merely reuses an answer saved by the first attempt and therefore
makes zero model calls, the absent second receipt cannot erase the valid first
receipt. A real Terra call without a matching receipt still fails closed.

The mathematical questions and input data are unchanged, but the visible tool
instructions now describe immediate item-level saving. Each accepted item is
preserved in the item store as soon as Terra submits it. The private scorer
reports submitted-correct, submitted-incorrect, invalid-format, tool-failure,
explicit-abstain and not-attempted states separately; missing items are never
silently turned into answers.

## Safety and execution boundary

`build_control_package.py` creates the retrieval-bearing requests, private
condition map and agreement only outside Git worktrees. It binds the exact
source commit, control commit, immutable image, RAG index and hash-only private
scorers, refuses to overwrite output, and performs no model call.

`run_e2v6_pair.py` may preflight all ten observations, but formal execution
requires one replicate number and a matching authorization value. The two
conditions run serially in the frozen order. Existing target branches and local
formal evidence block accidental reruns. There is no command that starts all
ten observations at once.

Formal Terra execution remains closed until the shared control commit is
published, the private package is built, and the 10/10 zero-model preflight
returns `status=ready` with `provider_call_count=0`.
