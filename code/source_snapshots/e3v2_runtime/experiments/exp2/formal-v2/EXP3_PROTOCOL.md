# Experiment 3 minimal manager-star protocol

## Metadata and objective

- Experiment ID: `E3-manager-star-v1`
- Protocol version: `1.0.0-rc2`
- Status: draft; implementation and execution remain separate from E2

Objective: test whether a minimal manager-centred multi-agent architecture
improves condition-blinded quality on the same three fixed E2 prompts relative
to the individual-developer workflow.

Starting E3 implementation only after E2 closes is an operational sequencing
decision for deadline and contamination control, not a direct adviser mandate.

## Conditions and topology

- M0: the existing individual-developer path.
- M1: one manager, one architect and one developer in a star topology.

M1 permits only these bounded handoffs:

1. manager → architect: original task and minimum repository context, requesting
   one plan;
2. architect → manager: `architect_plan_v1` JSON, at most 12,000 characters,
   containing risks, files and acceptance mapping;
3. manager → developer: original task plus the approved plan;
4. developer → manager: `developer_result_v1` JSON, at most 24,000 characters,
   containing implementation and verification evidence;
5. manager: validate completeness and finalize `manager_final_v1`.

Architect and developer never communicate directly. There is no reviewer-agent
loop, recursion, debate, dynamic role creation or second implementation pass.
The manager and architect have read-only repository tools; only the developer
has the existing scoped write tools.

## Fair comparison and estimand

- Reuse the exact E2 T1/T2/T3 task text, task-relevant source bytes, public
  inputs, hidden evaluators and quality rubrics.
- M0 and M1 run in one immutable candidate image containing both paths behind a
  frozen `architecture_mode` switch. Within a pair, that switch is the only
  intended condition difference.
- RAG is held disabled in both. Combining RAG and multi-agent architecture is a
  separate factorial study and is out of scope.
- Both receive the same model alias, tool implementations, source ref, allowed
  paths, 1,800-second timeout, CPU/memory/PID limits, 200,000 total model-token
  ceiling and 15 aggregate model-call/turn ceiling.
- M1 has fixed role caps: manager 3 turns / 45,000 tokens, architect 3 turns /
  35,000 tokens, developer 9 turns / 120,000 tokens. Every role's prompt and
  completion tokens count against both its role cap and the single shared cap;
  unused role capacity is not reallocated.
- M0 receives all 15 turns and the 200,000-token ceiling in one context.

The primary estimand is therefore the architecture effect under equal total
compute. Dedicated contexts are part of M1, but increased total compute is not.
Report per-role available capacity, actual usage, calls, latency and failure
phase as well as totals.

## Fixed execution design

E3 has exactly `3 prompts × 2 architectures × 3 replicates = 18` planned runs.
If fewer are completed, the study is incomplete; it is not retrospectively
redesignated as a pilot.

Within-prompt order:

- T1: R1 M0→M1, R2 M1→M0, R3 M0→M1;
- T2: R1 M1→M0, R2 M0→M1, R3 M1→M0;
- T3: R1 M0→M1, R2 M1→M0, R3 M0→M1.

Keep pair members adjacent. Sort block IDs by
`SHA256("E3-manager-star-v1|20260820|" + block_id)`; the frozen block order is
`T1R2`, `T2R2`, `T2R1`, `T3R1`, `T1R1`, `T2R3`, `T3R3`, `T1R3`, `T3R2`.
Run serially and never reorder in response to outcomes.

## Isolation and contamination gate

The same server-enforced ref policy as E2 permits only the frozen source and
current fresh target. Before every model call, negative preflight must deny all
V5 refs, every E2 formal output ref, all earlier E3 targets, the paired target,
`main` and arbitrary refs before GitHub content is returned. The current target
must be absent before orchestration creates it. Failure stops E3.

This preserves literal prompt reuse without allowing M0 or M1 to copy an E2 or
earlier-replicate answer. The frozen evidence binds the complete deny-ref set,
tool-server/policy hashes and sanitized allow/deny audit.

## Outcomes, failures and replacement

Primary outcome is the E2 0–10 quality score and prompt-level macro-average.
Secondary outcomes are full success, requirement coverage, total/per-role
tokens and calls, duration, commits, handoff-schema failures, role-cap
exhaustion and manager acceptance-map completion.

A role or shared-budget exhaustion, incomplete handoff or correctly delivered
context failure is a genuine M1 outcome. Apply E2's zero ordinary-replacement
rule and maximum one exceptional paired rerun for proven instrumentation or
treatment contamination before unblinding. A second contaminated pair ends the
study incomplete.

## Implementation acceptance criteria

- manager is the only coordinator;
- topology and role tool permissions are enforced server-side;
- the three handoff schemas, size limits and hash canonicalization are versioned
  and frozen;
- each role receives only its dedicated minimum context;
- global and role budgets are atomically accounted, including failed calls;
- the final result records role, input/output hashes, calls, tokens and failure
  phase without logging private prompt text;
- M0 remains byte-equivalent behind the architecture switch;
- focused tests prove topology, no direct architect↔developer path, permissions,
  budget accounting, schema/size rejection, failure propagation and repository-
  ref isolation before any external run.
