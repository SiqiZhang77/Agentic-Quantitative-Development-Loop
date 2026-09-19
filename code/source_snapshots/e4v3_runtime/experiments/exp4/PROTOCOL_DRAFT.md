# E4V1 prospective protocol: RAG × agent architecture

Status: **draft, not registered, not frozen, and not executable**.

## Document authority

This checked-in English document is the authoritative prospective protocol
that a future E4V1 agreement must bind.  The Chinese document at
`02_EXPERIMENT_CONTROL/protocols/exp4-e4v1-four-cell/PROSPECTIVE_PROTOCOL.md`
is an operator-facing explanatory summary.  It may explain the same rules in
plainer language, but it cannot add, remove or override experiment identity or
execution rules defined by this protocol, the configuration and the schema.

While the study remains a draft, the protocol commitments are intentionally
unresolved:

- `authoritative_protocol_sha256`:
  `__UNRESOLVED_E4_AUTHORITATIVE_PROTOCOL_SHA256__`;
- `explanatory_summary_sha256`:
  `__UNRESOLVED_E4_CHINESE_SUMMARY_SHA256__`.

At formal freeze, the reviewed agreement/control manifest must bind the exact
SHA-256 of both this authoritative protocol and the Chinese explanatory
summary in one record.  A missing hash, byte change or disagreement between
their stated rules blocks agreement generation.

## Research question

For the same task, model, source code, tools, budgets and evaluator, how do
retrieval and agent architecture separately and jointly affect solution quality
and workflow reliability?

The two treatment switches are:

- architecture: `single_agent` or `manager_star`;
- retrieval: disabled or enabled with one frozen, verified context.

All other scientific inputs must remain equal.  A different target branch is a
necessary per-observation storage address, not a treatment.  Target names use
only task, replicate and position (`...-K1-P1` through `...-K4-P4`) and never
contain `M0`, `M1`, `R0`, `R1`, `single`, `manager` or `rag`.  `K` is the
neutral replicate label; it avoids overloading `R`, which denotes the retrieval
switch inside the private condition code.

## Unit, blocks and schedule

An observation is one fresh container run.  A block contains the four cells for
one task and one replicate.  The experiment has twelve blocks (three tasks by
four replicates) and forty-eight observations.  Exactly one block may be
authorized and executed at a time.

The within-block order is the balanced four-treatment Williams order declared
in `config/e4v1.prospective.json`.  Replicate number selects the corresponding
row.  The twelve task-replicate blocks use a deterministic SHA-256 ordering
seed.  No outcome may be inspected to choose or change the later order.

## Equality rule

The identity preflight creates four identities for a block and compares a
condition-neutral projection.  That projection must be byte-identical after
removing only:

- the architecture selector and the role topology it necessarily creates;
- `rag_enabled` and whether a verified retrieval context is included;
- neutral per-observation storage identifiers and the Williams position.

In particular, task bytes, public inputs, evaluator hash, source commit, image,
tool hashes, model identity, total budgets and runtime limits must remain equal.
The frozen RAG asset hashes are present in every identity even when retrieval is
off, so the comparison cannot silently swap the corpus for one cell.

Every identity also fixes `factorial_rag_architecture_v1` as the formal runtime
contract and `all_model_stages_v1` as the RAG delivery rule.  For an RAG-on
manager-star observation, this means the same already-frozen retrieval block is
included in all five model stages; no role performs a new retrieval during its
stage.  The audit keeps only the attempt number, stage, hashes and item counts,
never the retrieved text.  That content-free receipt remains outside the
model-visible task context: the RAG treatment adds the frozen retrieval text,
not an extra set of hashes or memory identifiers.

The public runtime permits at most two complete attempts.  Each attempt has the
same 20-call and 350,000-token cap in both architectures; one observation is
therefore capped at 40 calls and 700,000 tokens.  The manager-star call slices
are 3 manager, 3 architect and 14 developer calls per attempt and share the same
350,000-token pool.  A completed T3 observation stops only after all 25 primary
item candidates have been saved; committing a secondary artifact cannot stand
in for missing items.  T3 answer-saving, format and completion rules come only
from the frozen task text shared by all four cells.  An architecture-specific
outer prompt must not repeat, strengthen or weaken those rules.

## Outcome layers

Task quality and workflow reliability are reported separately.

- Task quality: item-level score and coverage.  For T3 this includes all 25
  item states (`accepted`, `invalid_format`, `explicit_abstain`, `tool_failure`, or
  `not_attempted`) rather than converting a missing artifact into an unexplained
  all-wrong score.
- Delivery: whether the answer/checkpoint and final artifact were preserved.
- Runtime: whether the container and model calls ended normally.
- Tool use: accepted/rejected submissions, feedback shown to Terra, retries and
  commit evidence.

A runtime failure does not erase a valid saved answer.  Conversely, a clean
container exit does not make an incorrect answer correct.

For T3, the identity binds all eight files in
`experiments/shared/t3-quant-suite-v2`: `TASK.md`, `RUBRIC.md`, both public
inputs, `item_submission_schema_v2.json`, `output_schema_v1.json`,
`scorer_result_schema_v2.json`, and `scorer_result_validator.py`.  Each
item receives one workflow state (`accepted`, `invalid_format`,
`explicit_abstain`, `tool_failure`, or `not_attempted`) and a separate
mathematical correctness result.  Here `accepted` means structurally accepted;
it does not mean numerically correct.  An accepted saved candidate survives a later
runtime failure, and a checkpoint or artifact may fill only an item that is
still absent from the item store.

Each of T1, T2 and T3 has its own frozen query and retrieval-context binding:
path, SHA-256, context byte count and memory count.  Both RAG-on architectures
for that task use the same binding.  RAG-off identities retain the binding for
audit comparison but set `retrieval_context_included=false`, so no context is
placed in their model request.

Source code and experiment-control code are separate bindings.  The control
binding fixes a control ref, full control commit and hashes for six reviewed E4
control files.  This prevents an operator from running a source commit with a
different local scheduler or identity checker without changing the agreement.

## Analysis plan

Preserve all forty-eight observations.  Do not select a successful rerun and
discard its paired failure.  Report cell summaries and planned factorial
contrasts for architecture, retrieval and their interaction, with task and
replicate retained in the analysis.  With four replicates this remains a small
study; emphasize estimates, uncertainty and failure modes instead of relying on
one binary significance claim.

Any framework repair after the agreement is frozen creates a new version and a
new namespace.  Old evidence remains historical evidence and is never silently
replaced.
