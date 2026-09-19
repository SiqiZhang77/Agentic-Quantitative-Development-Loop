# E4V1 prospective protocol: T3 RAG × agent architecture

Status: prospective control awaiting immutable control-commit publication and
zero-model freeze.  No E4V1 model observation exists at this stage.

## Question and treatments

For the same 25-item T3 quantitative task, Terra model, source commit, tools,
resource limits and private scorer, how do project-memory retrieval and agent
architecture separately and jointly relate to answer quality and workflow
reliability?

The registered cells are `M0R0` (one agent, no memory), `M0R1` (one agent with
memory), `M1R0` (Manager/Architect/Developer without memory), and `M1R1`
(Manager/Architect/Developer with memory).  Five replicates are registered for
each cell.  An observation is one fresh container run with its own neutral run
ID, result branch and output directory.

Only architecture selection and the presence of the frozen retrieval context
may differ across cells.  Task bytes, public inputs, item-saving schema,
private scorer hashes, shared runtime source, image digest, model identity,
tool implementation and total budgets must be identical.

## Registered schedule and staged execution

The agreement contains 5 replicate blocks and 20 observations.  The first four
blocks use the balanced Williams rows below; the fifth repeats row one because
five replicates cannot place four conditions equally often in four positions.

1. `M0R0, M0R1, M1R1, M1R0`
2. `M0R1, M1R0, M0R0, M1R1`
3. `M1R0, M1R1, M0R1, M0R0`
4. `M1R1, M0R0, M1R0, M0R1`
5. `M0R0, M0R1, M1R1, M1R0`

At the operator's prospective decision, execution is staged by condition in
the fixed order `M1R1`, `M0R0`, `M0R1`, `M1R0`.  The first authorization covers
only the five `M1R1` observations.  One command may execute those five
replicates serially; no human audit is required between them.  The launcher
must finish preserving and attempting to score one observation before starting
the next.  A per-observation workflow or scoring failure is recorded and the
batch proceeds.  A common preflight failure involving source, control, image,
request identity or scorer stops the batch before any formal model call.

This condition-first staging is operationally convenient but makes condition
partly coincide with execution phase and date.  It is suitable for describing
five-repeat M1R1 performance.  Any later cross-cell analysis must report the
phase/order limitation and avoid claiming that every observed difference was
caused only by retrieval or architecture.

## Runtime and RAG delivery

Every observation allows at most two attempts.  Each attempt has at most 20
model calls and 500,000 tokens; an observation has at most 40 calls and
1,000,000 tokens.  CPU, memory, time and process limits are identical across
cells.  Manager-star uses the shared 3 Manager, 3 Architect and 14 Developer
nominal call allocation per attempt.  Unused Architect calls transfer to the
Developer only after a validated Architect handoff, and a Manager final-review
call remains reserved.  This changes role allocation, not total capacity.

RAG-on cells receive the exact same precomputed retrieval object.  It is hash
bound before agreement generation and is delivered under
`all_model_stages_v1`, meaning that every Terra role stage sees that same
context.  RAG-off cells retain the retrieval identity in audit records but the
runtime request omits the retrieval object.  No live retrieval occurs during a
formal observation.

## Outcomes and preservation

The primary T3 result is mathematical correctness out of 25.  Each item also
has a workflow state: structurally accepted, invalid format, explicit abstain,
tool failure, or not attempted.  Submitted-but-wrong items and missing items
remain distinct.  A valid saved item survives a later container or workflow
failure.  The independent private scoring step runs after each observation and
creates a public result that shows correct, incorrect and missing item IDs
without exposing the answer key.

Workflow success is reported separately from task score.  It means the runtime
completed its expected save/commit path; it does not mean all mathematics is
correct.  Conversely, workflow failure does not erase valid saved answers.

All five observations and failures are retained.  No favorable rerun replaces
an unfavorable registered observation.  If a framework repair is required
after agreement freeze, the repair creates a new experiment version and new
run namespace.

## Privacy and execution boundary

The agreement binds only evaluator hashes.  Builders and preflights must not
print private evaluator contents, salts, answer keys, API keys or the prior E2
private condition map.  The retrieval text is model-visible but stays in the
controlled freeze/launch package outside Git and is never printed by the
control scripts.

The prospective template cannot execute a model.  Formal execution becomes
possible only after a published clean control commit, resolved freeze package,
agreement, launch package, and a five-observation zero-model preflight all bind
the same source and image.
