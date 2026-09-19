T3 Public Rubric V1 — Layered Synthetic Portfolio Analytics

Total: 100 points, reported as both 0–100 and 0–10 by dividing by 10.

Level A — 20 points

- 16 points: proportional credit across the 480 dated cumulative-return cells (120 dates multiplied by Asset A, Asset B, Asset C and portfolio).
- 4 points: one point for each correct terminal cumulative return.

Level B — 30 points

- 10 points: proportional credit across the 360 dated gross-wealth, running-peak and drawdown cells.
- 2 points: mean daily return.
- 3 points: sample daily volatility.
- 4 points: annualized volatility.
- 4 points: geometrically annualized return.
- 4 points: maximum drawdown.
- 3 points: maximum-drawdown date.

Level C — 40 points

- 22 points: proportional credit across every required numeric leaf in the six rebalance-event records.
- 3 points: total turnover.
- 3 points: total transaction cost.
- 4 points: terminal net wealth.
- 4 points: net cumulative return.
- 3 points: net maximum drawdown.
- 1 point: net maximum-drawdown date.

Output, scope and tool compliance — 10 points

- 4 points: exact required JSON schema, field types and fixed metadata.
- 2 points: exact input date order, 120-row counts and six configured rebalance dates.
- 1 point: every emitted number is finite and retains sufficient precision.
- 1 point: only the permitted output path changed.
- 2 points: at least one successful quant_calculate call is present in condition-blinded, content-free telemetry and the four-call cap was not exceeded.

Numeric comparison

Each numeric leaf passes when:

abs(actual - expected) <= 1e-10 + 1e-9 * abs(expected)

The oracle is not rounded before comparison. JSON object key order is ignored. Array order and dates are significant.

Partial credit and gates

- Numeric leaves are scored independently.
- A missing or incorrect later level does not erase a correct earlier level.
- A wrong date invalidates the numeric leaves for that dated row or event and the relevant date-order component, but not unrelated scalar metrics.
- A parseable partial artifact is still evaluated.
- A missing output, non-parseable JSON, or any submitted NaN/Infinity receives zero task-quality points.
- An out-of-scope repository change fails the scope point and the existing experiment scope gate is reported separately.
- Infrastructure, provider, isolation and budget failures remain operational outcomes; they are not silently relabelled as wrong arithmetic.

Complete task

complete_task is true only when every mandatory schema, numeric, date, scope and quant_calculate-use check passes.

Reported diagnostics

The evaluator should return:

- total_score_0_100 and total_score_0_10;
- complete_task;
- requirement_coverage;
- level_a, level_b, level_c and compliance subscores;
- correct_numeric_cells and total_numeric_cells;
- maximum_absolute_error;
- schema_valid and date_order_valid;
- quant_calculate_used;
- failure categories.

25-item completion count

In addition to the 100-point rubric, report `correct_items` out of exactly 25.
An item is correct only when every required value belonging to that item passes
the numeric/date rule; partial cells remain visible in the separate numeric-cell
diagnostics but do not turn an incomplete item into a correct item.

- Items 1-4: the complete 120-date cumulative-return path for Asset A, Asset B,
  Asset C and the fixed-weight gross portfolio, respectively.
- Items 5-8: the four corresponding terminal cumulative returns.
- Items 9-12: mean daily return, sample daily volatility, annualized volatility
  and annualized geometric return.
- Items 13-15: the complete 120-date gross-wealth, running-peak and drawdown
  paths, respectively.
- Items 16-17: maximum drawdown and maximum-drawdown date.
- Item 18: all six required rebalance-event dates in the required order.
- Item 19: every numeric leaf across all six rebalance-event records.
- Items 20-25: total turnover, total transaction cost, terminal net wealth, net
  cumulative return, net maximum drawdown and net maximum-drawdown date.

The evaluator must emit `correct_items`, `total_items: 25`, and a boolean result
for each numbered item. A checkpointed section is scored exactly like the same
section in the final artifact; the final artifact takes precedence only when it
contains a parseable version of that section.

The 120 dates are scoring leaves within one prompt. They are not 120 independent experimental samples. Primary analysis remains at run/prompt level; level scores are secondary difficulty diagnostics.
