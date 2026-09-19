# E3V11 prospective T3 comparison

E3V11 is a new prospective study version. It does not overwrite or relabel any
E3V9 or E3V10 observation.

The model-visible T3 task, public inputs, tools, two-attempt structure, 20-call
cap per attempt, five paired replicates and alternating M0/M1 order remain
unchanged. Both architectures receive 500,000 tokens for each attempt and
1,000,000 tokens for the complete two-attempt observation.

The runtime derives the per-attempt cap from the frozen observation request.
It accepts the historical 350,000-token profile and this 500,000-token profile,
so the same implementation can verify either agreement without silently
changing an older study.

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
