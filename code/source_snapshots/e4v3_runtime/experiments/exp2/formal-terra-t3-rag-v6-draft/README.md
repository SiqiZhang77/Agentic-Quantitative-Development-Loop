# Experiment 2 T3 RAG v6 preparation draft

This directory prepares the next T3 RAG-versus-no-RAG study without changing,
overwriting or reusing the frozen v5 controls or observations.

## What this draft fixes

Both conditions use the public `experiments/shared/t3-quant-suite-v2` task and
the same single-agent runtime. That suite saves one answer item immediately,
keeps value-free evidence when a later attempt replaces an item, applies only
published deterministic format normalization, and returns safe type/length
feedback when an item is rejected.

Both conditions also use the same scoring meaning: the main result is the
number correct out of all 25 items, so a missing item cannot raise the score.
Missing items are nevertheless listed separately from answers that were saved
but mathematically incorrect, which keeps incomplete work visible for later
workflow analysis. The common pair binds the public, answer-free
`scorer_result_schema_v2.json` and `scorer_result_validator.py` by path and
SHA-256. Together they require 25 ordered item rows, keep the denominator fixed
at 25, and require the submitted and missing ID lists to be disjoint and cover
all item IDs. Neither file contains candidate values or expected answers.

The common pair template contains every task, model, source, image, budget and
RAG identity binding. Its two condition overlays are exactly:

```json
{"C0": {"rag_enabled": false}, "C1": {"rag_enabled": true}}
```

The future execution layer may deliver the already-frozen retrieval context
only when `rag_enabled` is true. It must prove that C0 did not receive that
context and C1 did. This treatment-delivery receipt is evidence about whether
the intended switch took effect; it is not an extra tunable condition.

## Deliberate stop boundary

`preparation_config.template.json` leaves every source/control commit, immutable
image, RAG corpus/index/context identity, blinding-salt hash and private evaluator
hash unresolved. The public scorer-result schema and validator hashes are also
unresolved until they are measured from the selected source commit. The offline
preflight therefore returns `status=blocked` and lists the exact fields. It
performs zero provider calls and zero network calls.

Even after an operator supplies all bindings, the builder creates only:

- an offline-preflight record;
- a non-runnable common pair template;
- a preparation manifest and checksums.

It never creates an agreement, run identity, target branch, authorization value
or formal model command. A later prospective phase must first commit and publish
the source and controls, build an immutable image, freeze the private hash-only
evaluator manifest and RAG inputs, then create a separate agreement and a
ten-observation zero-model preflight. The five paired replicates alternate
which condition runs first, and each observation permits two attempts with
20 model calls and 500,000 tokens per attempt.

## Private-data boundary

The builder reads only the RAG manifest/index/context files needed to verify
their hashes and a hash-only evaluator manifest. It does not read evaluator
source, hidden tests, answer keys or a raw blinding salt, and it never copies the
retrieval memories or private evaluator material into the preparation package.

## Offline checks

The tests inject local fake RAG and hash-only evaluator records. No network,
provider, Git remote, Docker registry or formal model execution is used.
