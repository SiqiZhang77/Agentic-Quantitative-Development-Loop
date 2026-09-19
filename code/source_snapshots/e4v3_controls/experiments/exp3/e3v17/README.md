# E3V17 prospective T3 comparison

E3V17 is a new prospective study version. It does not overwrite or relabel any
E3V9 through E3V16 observation.

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

E3V17 binds the same shared runtime v6 source and immutable image as E3V16. It
changes only the answer-blind item-scoring wrapper and the host's value-free
error report. E3V16 R1 proved that an independently saved candidate can have
the expected primitive type and length while still violating a public field
constraint. The old wrapper stopped all 25 scores when that happened. E3V17
marks only that submitted item incorrect and continues scoring every other
saved item. It never changes the candidate, infers a replacement, or reads an
answer key to make this decision.

The mathematical correctness rule, task, item continuation, RAG-off condition,
token and call limits, role-transfer policy, runtime status reader and telemetry
v4 contract are unchanged from E3V16. If the private scorer fails for another
reason, the host may retain only its allow-listed, value-free error code; raw
scorer output and candidate values remain hidden.

Before the private item scorer runs, `item_store_projection.py` validates the
complete v2 candidate and event histories, then creates a v1 scorer input with
the candidate map and candidate hash copied unchanged. The projection never
reads an answer key, selects an item, changes a value or supplies a missing
answer. Its source/output hashes and the unchanged candidate hash are retained
in a receipt for every scored observation.

Formal model execution remains disabled until the unchanged source and image
identities are reverified, all public and private hash-only bindings are
resolved, the agreement and launch package are created, and the pair preflight
returns `status=ready` with zero provider calls.
