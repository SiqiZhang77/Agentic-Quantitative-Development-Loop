# Experiment 2 formal-v2 frozen scoring rubric

All scores use a 0–10 scale. Ten means fully resolved; five means materially
partial; zero means no usable answer. The itemised rules below, rather than an
overall impression, determine the score.

## T1 legacy result-emission comprehension — 10 points

The following public claim categories are scored one point each (8 points total):

1. the `response_errors` validation gate;
2. the `minimal_failed_response` fallback and its inputs;
3. compact JSON serialization and reuse of the serialized value;
4. the `XCOM_BUDGET_BYTES` threshold and the fact that `len(line)` measures
   characters rather than encoded bytes;
5. the sole `diagnostics.error_message` shortening rule and reserialization;
6. target-parent creation and same-directory temporary path;
7. atomic replacement with `os.replace` and its scope;
8. the final stdout line and flush behaviour.

Additional points:

- 1 point: claims cite the relevant function/symbol names and distinguish
  observed behaviour from assumptions;
- 1 point: coherent explanation below 700 words with no material unsupported
  claim. A 700-word-or-longer note cannot receive this point but remains
  eligible if its file scope is otherwise valid.

The categories above are public. The private artifact contains reference
answers and scoring anchors, not additional hidden requirements. Correctness is
scored semantically under condition-blinded review; keyword matching alone is
insufficient.

## T2 class/interface design — 10 points

- 1 point: a clear abstract `MonotonicClock` interface exists;
- 1 point: the interface declares typed `now() -> float`;
- 1 point: no-argument `SystemMonotonicClock` correctly overrides the interface
  and delegates to `time.monotonic()`;
- 2 points: injected clocks control both the recorded start and all timeout
  checks, including the exact strict greater-than boundary;
- 1 point: default construction preserves the existing system-monotonic path;
- 1 point: token, iteration, usage, cost and public exception behaviour remain
  unchanged;
- 1 point: public classes/methods have useful Python documentation and types;
- 1 point: focused deterministic fake-clock tests are added and meaningful;
- 1 point: no dead parameters/code, unjustified complexity or out-of-scope
  changes.

Mandatory hidden behaviour failure caps the total score at 7. A change that
does not import or destroys existing behaviour scores at most 3.

## T3 tabular cumulative returns — 10 points

There are 15 frozen numeric cells: cumulative returns for Asset A, Asset B and
the equal-weight daily-rebalanced portfolio over five dates.

- 8 points: proportionally allocated across the 15 correct numeric cells,
  within absolute tolerance `1e-10`;
- 1 point: dates/order and required JSON schema are exact;
- 1 point: `method` is exactly `geometric_compounding` and no arithmetic return
  is substituted for geometric compounding.

Any non-finite output or output that cannot be parsed scores zero. Rounding for
display is allowed only if the machine-readable values still meet the frozen
tolerance.

## Exploratory blinded AI review

The fixed evaluator may return a separate 0–10 score with itemised findings for
correctness, dead code/parameters, comments/documentation, method efficiency and
clarity. It must not see condition labels, retrieved evidence or operational
metrics. This score is never substituted for the primary rubric.
