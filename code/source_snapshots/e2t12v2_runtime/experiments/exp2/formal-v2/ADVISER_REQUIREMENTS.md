# Adviser requirements traceability

Source: meeting recording `Screen Recording 2026-08-20 at 10.31.40.mov`.
Timestamps below are relative to the start of the recording. Wording is
paraphrased from the transcript; it is not presented as a verbatim quotation.

## Experiment 2

| ID | Time | Adviser recommendation | Protocol consequence |
|---|---|---|---|
| A0 | 04:47–05:11 | Compare the same task with and without RAG and assess how well each performs. | Every C0/C1 pair uses identical frozen task/runtime settings except retrieval. |
| A1 | 11:54–12:57 | Existing token/time/commit metrics are not conclusive because they do not measure generated-output quality. | Quality becomes the primary outcome. Tokens, time and commits remain secondary cost/process measures. |
| A2 | 13:02–14:03 | Use a better success metric or a problem whose solution can be measured clearly. | Every task has a frozen answer key, hidden checker, or blinded rubric before execution. |
| A3 | 14:08–16:28 | Use both a financial/mathematical problem and a programmatic problem. | Task set includes a tabular cumulative-return task and a class-design task. |
| A4 | 15:01–16:09 | The programming task can require a class, interface, overridden methods and documentation, scored on a ranking such as fully solved 10 / partially solved 5. | The class-design rubric scores interface, concrete overrides, behaviour, documentation, tests and code quality on a 0–10 scale. |
| A5a | 16:32–17:36 | AI-assisted code evaluation may inspect dead parameters/code, sensible comments and efficient methods. | A fixed condition-blinded AI score is exploratory only; deterministic tests and condition-blinded human scoring define the primary evidence. |
| A5b | 17:38–18:10 | A bounded task can test whether the system correctly understands and explains legacy code. | T1 asks for a short, symbol-grounded explanation of a small existing result-emission module. |
| A6 | 17:38–18:34 | Use three useful tests: understand legacy code, design code, and solve a financial-maths/table problem. | The formal task set is exactly these three pre-registered families. |
| A7 | 18:41–19:18 | Keep the tests simple rather than building a sophisticated financial-strategy study under the deadline. | No backtest or strategy optimisation is included in E2 formal-v2. |
| A12 | 26:08–26:41 | Confirm the exact underlying model and context configuration. | Alias mapping, context limit, sampling, cache, routing and fallback behaviour are hard freeze gates. |

## Experiment 3

| ID | Time | Adviser recommendation | Protocol consequence |
|---|---|---|---|
| A8 | 22:11–22:33 | Keep the multi-agent system simple and reuse the same prompts used for the RAG experiment. | E3 compares architectures on the frozen E2 formal-v2 task specifications and rubrics. |
| A9 | 23:08–23:53 | Prefer a manager coordinating architect and developer over a peer reviewer loop; use a star-shaped handoff. | E3 M1 has manager as the only coordinator, one architect handoff and one developer handoff, with no reviewer-agent cycle. |
| A10 | 24:19–25:15 | Compare whether the multi-agent system solves the same task better than an individual developer; specialised agents retain dedicated contexts. | E3 M0 is the existing individual developer path; M1 partitions architecture, implementation and coordination contexts. |
| A11 | 21:42–22:15 and 25:20–25:59 | Prioritise the immediate RAG evaluation and avoid a sophisticated multi-agent design. | E3 remains minimal; the stronger rule that E2 closes before E3 implementation is an operational addition below. |

## Methodological additions not explicitly dictated by the advisers

The following controls are added to make the comparison interpretable:

- three replicates per condition because the proxy seed and sampling defaults
  are not frozen;
- condition order counterbalancing within fixed prompt;
- condition-blinded scoring and neutral submission IDs;
- separation of infrastructure-invalid runs from genuine agent failures;
- task-level aggregation, rather than treating repeated runs as independent
  tasks;
- explicit model/runtime drift checks and immutable evidence manifests;
- retrieval-relevance annotation as a mechanism diagnostic, not a substitute
  for task quality;
- E2 closure before E3 implementation/execution, to reduce deadline and
  cross-experiment contamination risk;
- server-enforced source/target-ref isolation and negative leakage preflight;
- fixed replication counts, schedules and exceptional-rerun limits.
