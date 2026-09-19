# E3V16 prospective T3 comparison

E3V16 is a new prospective study version. It does not overwrite or relabel any
E3V9 through E3V15 observation.

The model-visible T3 task, public inputs, tools, two-attempt structure, 20-call
cap per attempt, five paired replicates and alternating M0/M1 order remain
unchanged. Both architectures receive 500,000 tokens for each attempt and
1,000,000 tokens for the complete two-attempt observation.

For M1, the Manager still has three calls, the Architect starts with three
planning calls and the Developer starts with fourteen implementation calls.
After the Architect has supplied a valid planning handoff, each Architect call
that was not used becomes an additional Developer call for that same attempt.
The combined cap stays at twenty calls: for example, one Architect call means
the Developer may use sixteen calls, not more than twenty calls in total.
The Manager's final review call is always reserved, so this transfer cannot
leave the workflow without its final review stage.

E3V16 binds the same shared runtime v6 source and immutable image as E3V15. Its
only change is in the host-side observation runner after the container has
finished. The runtime writes the workflow outcome at
`execution_summary.status`; E3V15 incorrectly looked for `status` at the top of
`result.json`, so it labelled a genuinely successful M1 observation as failed.
E3V16 reads the documented nested field. The task, scoring, item continuation,
RAG-off condition, token and call limits, role-transfer policy and telemetry v4
contract are unchanged from E3V15.

Before the private item scorer runs, `item_store_projection.py` validates the
complete v2 candidate and event histories, then creates a v1 scorer input with
the candidate map and candidate hash copied unchanged. The projection never
reads an answer key, selects an item, changes a value or supplies a missing
answer. Its source/output hashes and the unchanged candidate hash are retained
in a receipt for every scored observation.

Formal model execution remains disabled until the source commit is published,
a new immutable image is built, all public and private hash-only bindings are
resolved, the agreement and launch package are created, and the pair preflight
returns `status=ready` with zero provider calls.
