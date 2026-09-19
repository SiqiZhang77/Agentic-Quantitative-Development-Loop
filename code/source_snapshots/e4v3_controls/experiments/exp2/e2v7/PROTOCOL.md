# Experiment 2 E2V7: project-memory comparison on the shared T3 runtime

E2V7 is a new prospective study. It preserves every E2V5 and E2V6 observation and does
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

## Host-side pair continuation repair

E2V7 uses the same source commit and Docker image as E2V6 and E3V16. E2V6
successfully scored its first C0 observation, then its host-side pair runner
looked for three fields that no longer exist in the frozen v2 public score and
stopped before C1. E2V7 reads the v2 item counts and submission-state fields
that the scorer actually publishes, so recording C0 can no longer prevent the
paired C1 observation from starting.

The mathematical questions, public inputs, retrieval index, visible tool
instructions, item-level saving, private scoring rule, model, token and call
budgets are unchanged. The repair runs only after an observation has returned
and been scored; it does not create, alter or infer a model answer.

## Safety and execution boundary

`build_control_package.py` creates the retrieval-bearing requests, private
condition map and agreement only outside Git worktrees. It binds the exact
source commit, control commit, immutable image, RAG index and hash-only private
scorers, refuses to overwrite output, and performs no model call.

`run_e2v7_pair.py` may preflight all ten observations, but formal execution
requires one replicate number and a matching authorization value. The two
conditions run serially in the frozen order. Existing target branches and local
formal evidence block accidental reruns. There is no command that starts all
ten observations at once.

Formal Terra execution remains closed until the shared control commit is
published, the private package is built, and the 10/10 zero-model preflight
returns `status=ready` with `provider_call_count=0`.
