# E3V9 public-binding collection control

This small control package records only facts that can be reproduced from the
published runtime source and the local immutable container: source commit,
container ID, public T3 task files, runtime request/response structures, the
explicit tool-code inventory, and the public provider identity.

It is deliberately **not** a model launcher or an agreement builder.  A model
cannot be run from this directory.  The private T3 evaluator and its
coordinating code must be supplied in a separate hash-only manifest after an
independent review. `model_visible_trace_schema_v1.json` separately fixes the
allowed record shape for model-visible output and model-requested tool calls.
The existing E2 V5 evaluator
cannot be reused: it is bound to `t3-quant-suite-v1`, while this study is bound
to `t3-quant-suite-v2`.

The collector fails if the source worktree is dirty, if its commit does not
match the claimed published commit, if a required public file is missing, or if
the selected Docker tag does not resolve to one immutable SHA-256 image ID.
It performs zero Terra/provider calls.

Some Docker Desktop versions can inspect a locally built multi-platform image
by its raw `sha256:...` ID while failing to resolve the equivalent local
`repository@sha256:...` name. In that case `--image-ref` remains the canonical
repository-and-digest identity written into the binding, while
`--image-lookup-ref` supplies the raw local ID used only for inspection. The
collector compares the inspected ID with the digest embedded in `--image-ref`
and fails closed on any difference.

`build_scorer_result_v2.py` is an answer-blind result adapter. Mathematical
correctness remains owned by the separately hashed private 25-item scorer. The
adapter converts only its per-item correct/incorrect/missing statuses into the
public v2 result contract and optionally uses captured submit-tool results to
distinguish an invalid-format or tool-failure item from an item never attempted.
Candidate values and expected answers are never copied to its output.

`collect_private_evaluator_bindings.py` reads the reviewed scorer, base scorer
and result adapter only as opaque bytes and writes their SHA-256 values. Its
output deliberately contains no local path, source text, candidate answer or
expected answer. `build_freeze_config.py` accepts only that exact hash-only
manifest and the verified public-binding inventory. It then resolves a new
configuration outside the Git worktree, records the clean control commit and
reviewed control-file hashes, enables agreement generation, and keeps formal
model execution disabled.
