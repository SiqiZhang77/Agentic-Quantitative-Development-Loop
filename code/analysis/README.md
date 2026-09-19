# Offline descriptive result reproduction

Run from `code/`:

```bash
python3 analysis/reproduce_summaries.py --output reproduced_summaries.json
```

Only the Python standard library is required. Inputs are selected columns from frozen, existing analytical records; the exporter did not change observations or rescore answers. `DATA_PROVENANCE.json` identifies the original local analytical source by project-relative path and SHA-256. Those provenance paths document origins and are not runtime dependencies.

| File | Unit and scope |
|---|---|
| `data/t3_runs.csv` | 60 E4V3 observations, 15 per condition; counts of correct, incorrect and missing items |
| `data/t3_items.csv` | 1,500 required run-item slots: 25 per observation; accepted/not_attempted and correctness flags; no numeric reference answers |
| `data/t3_resources.csv` | 60 run aggregates from the complete provider-call ledger, including all attempts, failed calls and M1 roles |
| `data/t12_scores.csv` | 32 condition-joined, confirmed observations: 20 primary retrieval observations and 12 supplementary architecture observations |
| `data/relevance_grades.csv` | 15 task-record labels: 5 ranked records for each task, with de-identified summaries, grading reasons and delivery annotations |
| `data/retrieval_metrics.csv` | Three previously reported task-level diagnostic rows, used to cross-check recalculation |

## Definitions

- T3 overall correctness uses all 25 required items per run; aggregate condition denominator is 375.
- Level A = items 1-8 (120 slots per condition); B = 9-17 (135); C = 18-25 (120). The level complete-item score rate is correct/required. It is distinct from the public 100-point weighted rubric.
- Submission coverage = accepted/required. Submission-conditional accuracy = correct/accepted; it is undefined if no item was accepted.
- A correct item may require a complete vector or a single scalar. Item counts are not counts of scalar components.
- Resource latency is cumulative model-call time, not end-to-end workflow time or monetary cost. The `top_level_*` diagnostic fields are retained to show the older summary undercount; use `total_tokens` for audited totals.
- T1/T2 final scores apply delivery gates and caps to rubric content scores. SD is the sample standard deviation, not a confidence interval.
- Primary pairs use C1 minus C0. Supplementary pairs use M1 minus M0. The script exports both final-score and content-score differences.
- Precision@5 treats grades 1 and 2 as relevant. DCG uses gain `2**grade - 1` and discount `log2(rank + 1)`. The ideal DCG sorts the **same five returned records**; this is within-returned-set nDCG, not corpus-wide retrieval effectiveness.
- Delivered/returned counts a record if any of its text was delivered, including a truncated record. A direct-usefulness grade of 2 is not proof that the agent used that record.

Labels were assigned retrospectively by Codex and were not independently human-validated or outcome-blinded. There is one distinct ranking per task, not 40 independent ranking observations. In particular, T3's five grade-1 records yield Precision@5 and returned-set nDCG@5 of 1.0 while providing zero grade-2 records.

The script verifies observation counts, within-run item coverage, run/resource alignment and retrieval calculations. It reproduces descriptive tables from exported data, not the upstream extraction of raw traces, confidence intervals or private mathematical scoring. It never reads a company corpus or invokes an LLM.
