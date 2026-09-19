# E3V9 prospective T3 control draft

This directory prepares the next T3-only comparison between the existing M0
single-agent architecture and M1 manager-star architecture. It is a new study;
it does not modify, replace or pool the frozen E3V8 agreement or observations.

The draft keeps E3V8's five paired replicates and alternating within-pair arm
order. Both arms are bound to the same `t3-quant-suite-v2` task, model, public
inputs, two-attempt limit, total model budget and model-visible tool contract.
Only the agent organisation differs.

The v2 task contract requires one non-empty item per `submit_t3_items` call and
an immediate atomic save as soon as that answer is available. A later failure
inside a multi-operation `quant_calculate` call may preserve only earlier
completed results explicitly named in `return_ids`; those results can be used
in a later call through `stored_ref`. Neither mechanism chooses a formula,
maps a result to an item ID or supplies an expected answer.

The primary score still uses all 25 items as its denominator, so an item with
no saved candidate cannot increase the number correct. The scorer must also
report missing items separately from submitted-but-incorrect items. This keeps
unfinished work visible without pretending that Terra submitted a specific
wrong value. The public `scorer_result_schema_v2.json` and
`scorer_result_validator.py` make that distinction machine-checkable without
containing candidate values or expected answers. They require 25 ordered item
rows, a fixed `total_items: 25`, and disjoint submitted/missing ID lists whose
union is exactly item IDs 1 through 25.

The checked-in prospective configuration remains unable to create an agreement
or run a model. Source ref/commit, image, runtime schemas, tool fingerprints,
public task hashes, private evaluator hashes and control-package identity remain
`__UNRESOLVED_...__`. The separately reviewed
`experiments/prospective_freezes/e3v9/build_freeze_config.py` may create a
resolved configuration outside the Git worktree only from the verified public
inventory and an answer-free private evaluator hash manifest. That resolved
configuration may enable agreement generation, but the schema and validation
continue to prohibit formal model execution. A separate later launcher version
and explicit authorization must control the model-execution gate. Editing
values at runtime is not sufficient.
When a public-file hash is resolved, readiness also compares it with the bytes
in the local source tree and remains unready on any mismatch.

Files in this directory are limited to deterministic config validation,
identity construction, a fail-closed builder, zero-model preflight and offline
tests. There is deliberately no E3V9 observation or pair launcher.
