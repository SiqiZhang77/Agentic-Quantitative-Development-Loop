# Experiment 2 Terra T1/T2 direct-local amendment V2

- Experiment ID: `E2-terra-t12-v2`
- Approval authority: user
- Scope: T1 and T2 only, five paired replicates per task
- Formal run count: 20
- Provider: direct OpenAI, `gpt-5.6-terra`
- Source: `exp2/terra-formal-source-v1` at
  `6e7d29b3f220dd4cc639219aa7e765aee88e3988`
- Runtime image ID:
  `sha256:8f135afdd816ffeac21f67834d10af91dda6509b4c8f993e33c65413b128465a`
- Frozen RAG index: `e2-jira-index-v1` at
  `72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1`

This amendment replaces the unavailable company LiteLLM/Jira/Airflow execution
transport with a serial direct-local Docker transport. It does not relabel any
earlier smoke output as formal and does not change the public T1/T2 text or the
formal-v2 scoring rubric.

Within each replicate pair, C0 and C1 use the same source, image, provider,
model, prompt, task, paths, tools, 15-call cap, 200,000-token cap, timeout, CPU,
memory, PID and commit limits. The only intended treatment difference is that
C1 sets `rag_enabled=true` and receives the deterministic top-five result from
the frozen BM25 index; C0 never invokes retrieval and receives no retrieval
context.

The first three T1/T2 pair orders are retained and the same alternation is
continued through replicates four and five:

- T1: R1 C0-C1, R2 C1-C0, R3 C0-C1, R4 C1-C0, R5 C0-C1
- T2: R1 C1-C0, R2 C0-C1, R3 C1-C0, R4 C0-C1, R5 C1-C0

The full frozen inventory contains ten pairs. The operator selects exactly one
task and replicate per command. The two members of that pair run adjacently and
serially in the frozen within-pair order.

Every run receives a salt-derived neutral submission ID and a fresh neutral
target branch. The salt, condition map, retrieved text and evaluator bindings
remain in a private package outside every Git worktree. Evaluators receive only
neutral artifacts. Model-facing repository tools may read only the fixed source
and current target, and may write only the current target. Other E2/E3 outputs,
paired targets, earlier targets, `main`, V5 and arbitrary refs are denied.

Before execution, the private package must bind hash-only T1/T2 evaluator
identities using the exact schema
`exp2-terra-t12-private-evaluator-hashes-v2`. All five fields are mandatory:

- `t1_gold_claims_sha256`
- `t2_hidden_tests_sha256`
- `t2_evaluator_runner_sha256`
- `private_evaluator_coordinator_sha256`
- `primary_scorer_commitment_sha256`

The private evaluator/coordinator is a distinct binding from both the T2
sandbox runner and the primary scorer commitment. The builder rejects the
earlier four-field v1 manifest, missing fields, malformed SHA-256 values and
unapproved extra fields. The v2 agreement records the evaluator schema, field
count and canonical aggregate digest. Preflight independently validates the
five-field manifest and its agreement binding before execution.

The package also binds the public protocol/rubric/task hashes, source, image,
provider, index, schedule and every request/identity/negative-ref file. A
missing or changed binding, existing target, source drift, image drift, index
drift, treatment-pair drift or checksum mismatch fails closed before a model
call.

Complete task-quality failures after the first model call are retained. Missing
results, wrong treatment delivery, provider/source/image/index drift,
instrumentation failure or prohibited-ref access are infrastructure-invalid and
stop the serial batch. Condition-blinded scoring and unblinding remain separate
from collection.
