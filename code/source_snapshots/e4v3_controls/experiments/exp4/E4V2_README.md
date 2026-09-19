# E4V2 T3 four-cell controls

E4V2 crosses the two switches already studied separately while keeping the T3
task, source, model, tools, budgets and private item scorer fixed.

E4V2 is a new version because the shared runtime changed after E4V1. Architect
and Developer handoffs now have a disclosed 50,000-character canonical JSON
limit, and an empty calculation array receives a content-free explanation that
identifies the argument, minimum length and actual zero length. The calculator
still rejects the empty input and never supplies missing numbers.

| Code | Architecture | Frozen project memory |
|---|---|---|
| `M0R0` | one Terra agent | absent |
| `M0R1` | one Terra agent | present |
| `M1R0` | Manager, Architect and Developer | absent |
| `M1R1` | Manager, Architect and Developer | present |

There are five registered replicates per condition and therefore 20 potential
observations.  The first authorized phase is the five `M1R1` observations.  A
single batch command preflights all five targets, asks for the OpenAI key once,
then launches, preserves and scores the five observations serially.  It does
not require a manual audit between replicates.  A workflow failure in one
completed observation is recorded and does not erase its saved item candidates
or stop the next replicate.  A shared identity, image, source or evaluator
failure found before model execution stops the batch before the API key is used.

The five-run command is intentionally condition-specific.  It never merges the
five answers: every replicate has a separate neutral run ID, target branch,
result directory and 25-item score.  The final batch summary points to all five
records.

## Budgets and shared runtime

All cells use at most two attempts.  Each attempt is capped at 20 model calls
and 500,000 tokens; one observation is capped at 40 calls and 1,000,000 tokens.
For `manager_star`, the nominal 3/3/14 Manager/Architect/Developer allocation,
the validated Architect-to-Developer unused-call transfer, and the reserved
final Manager review call are inherited unchanged from the shared runtime.

The same immutable runtime supports both switches through
`factorial_rag_architecture_v1`.  A RAG-on request includes one frozen retrieval
object and `all_model_stages_v1`, so the same memory reaches every Terra role
stage.  A RAG-off request contains no retrieval object.  No role performs a new
retrieval during an observation.

## Freeze sequence

1. Commit and publish these controls on a dedicated E4 control ref.
2. Run `build_e4v2_freeze_config.py` with the new runtime's reviewed public
   bindings and the hash-only evaluator manifest, plus one verified RAG-on
   request for the same T3 task.  E4V2 resolves these inputs directly instead
   of passing them through an older E3 version's source-ref gate.  It requires
   the published `exp/shared-t3-runtime-v7` ref, validates zero-model-call and
   private-content flags, and rechecks the source and control refs remotely.
   The retrieval request may name the earlier runtime source branch because
   E4V2 deliberately reuses the already frozen T3 retrieval result; the builder
   verifies repository, task profile, model, query hash and retrieval
   structure. It copies only the model-visible retrieval object and never
   prints its text or reads answer keys.
3. Run `build_e4v2_agreement.py`.  It freezes all 20 identities and the declared
   phase order without making model calls.
4. Run `build_e4v2_launch_package.py`.  It builds 20 request/negative-ref
   records and binds the clean control commit, still with zero model calls.
5. Run `run_e4v2_batch.py --condition M1R1` without `--execute` as the final
   five-observation zero-model preflight.
6. Only after all five are `ready`, use the separately authorized `--execute`
   command.  The operator enters the API key once and the launcher runs the five
   observations in replicate order.

The checked-in prospective template remains `prospective_unready` and contains
visible unresolved placeholders.  It cannot launch a model.  Resolved freeze,
agreement and launch packages must stay outside all Git worktrees.  Any runtime
or control repair after freezing requires a new experiment version; existing
observations are preserved rather than overwritten.

## Interpretation boundary

Running all five `M1R1` observations first estimates the repeat-to-repeat
behavior of that combined condition.  If the other three conditions are run in
later phases, condition and calendar/order are no longer perfectly separated.
The later four-cell comparison must report this phase-order limitation and may
not attribute every difference solely to RAG or agent count.
