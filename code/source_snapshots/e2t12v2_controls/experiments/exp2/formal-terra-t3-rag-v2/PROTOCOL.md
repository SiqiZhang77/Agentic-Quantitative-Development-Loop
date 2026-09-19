# Experiment 2 Terra T3 RAG/no-RAG formal control v2

This package controls a new six-observation T3 study. Version 1 stopped during
local preflight before any provider call because it pointed at a source
worktree shared with Experiment 3. Version 2 uses a dedicated source worktree
and the repaired two-attempt continuation runtime. It does not change or extend
the existing T1/T2 agreement.

## Frozen comparison

- Three paired replicates; C0/C1 order is counterbalanced.
- C0 has no retrieval context. C1 contains one precomputed result from the
  frozen `e2-jira-index-v1` BM25 index.
- Both conditions use the same Terra model, single-agent architecture, public
  T3 task, source commit, image, tools, two-attempt limits and resource budget.
- The dedicated local source worktree is fixed to the same commit as the remote
  source ref and cannot move when another experiment advances its checkout.
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

No formal observation may start until the final agreement has been regenerated
after the control files are committed and the 6/6 zero-model preflight reports
`status=ready` and `provider_call_count=0`.
