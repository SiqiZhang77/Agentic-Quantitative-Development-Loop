T3 — Layered Synthetic Portfolio Analytics

Inputs

Read only:

- experiments/shared/t3-quant-suite-v1/input/synthetic_portfolio_returns_v1.csv
- experiments/shared/t3-quant-suite-v1/input/portfolio_config_v1.json

The CSV contains 120 strictly ordered daily observations for three synthetic assets. Returns are decimal daily returns, not percentages. The configuration supplies the fixed target weights, annualization factor, initial wealth, scheduled rebalance dates and transaction-cost rate.

Required tool

Use the quant_calculate tool for numerical work. It provides bounded arithmetic and statistical primitives but does not decide which calculation method fits each requirement. Select and compose the operations yourself.

- At least one successful quant_calculate call is mandatory.
- A run may attempt at most four quant_calculate calls.
- Batch related calculations wherever practical.
- Inside one `quant_calculate` request, `{"ref": "operation_id"}` may only refer to an earlier operation in that same request. To continue arithmetic from a result returned by an earlier successful call in this run, use `{"stored_ref": {"calculation_id": "calculation-1", "result_id": "returned_result_id"}}`. Do not use an ordinary `ref` for a prior call.
- Do not use shell arithmetic, executable Python, another model, an external API or downloaded data for the calculations.
- Do not ask the tool to read files, branches, environment values, credentials, RAG context or another run's output.

How to preserve mathematical work after calculation

Primary item submission:

1. A successful `quant_calculate` call returns a short `calculation_id`. Exact scalar results remain visible; complete array results are retained inside the current run and represented by their shape, hash and bounded preview. Do not retype long arrays.
2. As soon as one or more requested mathematical results are available, call `submit_t3_items`. One call may submit any batch of 1 through 25 item candidates and may cover any subset of item IDs 1 through 25. The request must match `experiments/shared/t3-quant-suite-v1/item_submission_schema_v1.json`. Calling it with an empty `items` list does not submit or replace anything; it only queries which item IDs are already saved.
3. Never inline or retype a numeric array, a 120-row path or six rebalance rows. Instead, point to a result retained from a successful `quant_calculate` call. Every candidate has an `item_id` and exactly one of these five public request structures:
   - `numeric_vector_ref`, for items 1–4 and 13–15: provide `calculation_id` and `result_id` for the retained 120-number vector;
   - `numeric_scalar_ref`, for items 5–12, 16 and 20–24: provide `calculation_id` and `result_id` for the retained finite number;
   - `date`, for items 17 and 25: provide one `YYYY-MM-DD` date string in `value`;
   - `date_list`, for item 18: provide exactly six ordered date strings in `values`;
   - `rebalance_rows_ref`, for item 19: provide `calculation_id`, `result_id` and exactly six unique, zero-based, non-negative row positions in `indices`. The server copies those six positions in the supplied order from the retained numeric result.
4. The server resolves each calculation reference inside the current run and copies the referenced value; the model does not send the hidden full value again. The five values saved internally are called `numeric_vector`, `numeric_scalar`, `date`, `date_list` and `rebalance_rows`. A saved `numeric_vector` contains only 120 numbers. Its dates are the 120 public input dates in their original order and are not duplicated in the item submission. Item 18 separately supplies event dates; item 19 contains event numbers only.
5. Item IDs in one non-empty batch must be unique. The server checks every item independently: it saves each structurally valid candidate and reports a short error code for each invalid candidate. A malformed candidate therefore cannot erase valid candidates in the same call or any candidate saved earlier. If the outer request itself has the wrong schema version, wrong top-level fields, a non-list `items` value or more than 25 entries, nothing from that request is saved. A later structurally valid submission for an already stored item ID replaces that item only; it does not erase other stored items. Structural acceptance says only that the reference exists and its retained result has the required public shape. It does not say whether the mathematics is correct and supplies no expected answer.
6. The item store is the primary mathematical submission for this task. It belongs only to the current formal observation. If the outer experiment runner grants the second complete attempt, candidates accepted during the first attempt remain in that observation's item store. For each item ID, a valid stored candidate takes precedence during scoring. A parseable checkpoint or full artifact may supply only an item ID that is absent from the store; it must never replace a stored candidate.

Public item map:

- Item 1: complete 120-date geometrically compounded cumulative-return path for Asset A.
- Item 2: complete 120-date geometrically compounded cumulative-return path for Asset B.
- Item 3: complete 120-date geometrically compounded cumulative-return path for Asset C.
- Item 4: complete 120-date geometrically compounded cumulative-return path for the fixed-weight gross portfolio.
- Item 5: terminal cumulative return for Asset A.
- Item 6: terminal cumulative return for Asset B.
- Item 7: terminal cumulative return for Asset C.
- Item 8: terminal cumulative return for the fixed-weight gross portfolio.
- Item 9: mean daily return of the fixed-weight gross portfolio.
- Item 10: sample daily volatility of the fixed-weight gross portfolio.
- Item 11: annualized volatility of the fixed-weight gross portfolio.
- Item 12: geometrically annualized return of the fixed-weight gross portfolio.
- Item 13: complete 120-date gross-wealth path of the fixed-weight gross portfolio.
- Item 14: complete 120-date running-peak path of the fixed-weight gross portfolio.
- Item 15: complete 120-date drawdown path of the fixed-weight gross portfolio.
- Item 16: maximum drawdown of the fixed-weight gross portfolio.
- Item 17: maximum-drawdown date of the fixed-weight gross portfolio.
- Item 18: all six configured rebalance-event dates in required order.
- Item 19: every numeric field across all six rebalance events: pre-trade NAV and weights, turnover, transaction cost, and post-trade NAV and weights.
- Item 20: total turnover.
- Item 21: total transaction cost.
- Item 22: terminal net wealth.
- Item 23: net cumulative return.
- Item 24: maximum drawdown of the net post-trade NAV path.
- Item 25: maximum-drawdown date of the net post-trade NAV path.

Secondary full-artifact record:

1. After submitting available item candidates, use `submit_calculation_checkpoint` and `commit_calculation_artifact` if the remaining time and tool budget allow. These actions create a secondary record of the model's ability to assemble and commit the complete JSON artifact. Failure to complete that secondary record does not erase item candidates already accepted by `submit_t3_items`.
2. For a checkpoint, pass the completed section name, `experiments/shared/t3-quant-suite-v1/output_schema_v1.json` as `schema_path`, and only that section's JSON object as `document`. Use `$calc` references for retained results and `$zip_rows` to pair equal-length result columns with dates and field names.
3. For the complete artifact, call `commit_calculation_artifact` with the exact required output path, the same `schema_path`, and the complete top-level JSON document. A successfully saved section may be inserted without rebuilding it by using exactly `{"$checkpoint": "level_a"}`, `{"$checkpoint": "level_b"}` or `{"$checkpoint": "level_c"}` as that section's value.
4. Item submission and artifact assembly only copy, select, label and serialize values already calculated. They perform no new arithmetic. The model must choose every calculation, result reference, date, output field and row mapping. No submission tool chooses a formula, corrects a value or supplies an expected answer.
5. The frozen outer experiment runner allows at most two complete attempts for each architecture observation. A complete attempt means one full model execution from receipt of this task until that execution ends. `submit_t3_items`, checkpointing and artifact commit do not start, authorize or add an attempt. The same limit is imposed outside the model on both architectures.

Level A — basic return paths

Using the configured fixed weights, treat the gross portfolio as daily rebalanced to those weights.

For every input date, report geometrically compounded cumulative returns from initial wealth 1.0 for:

- Asset A;
- Asset B;
- Asset C;
- the fixed-weight gross portfolio.

Also report the terminal cumulative return for each of those four series.

Level B — portfolio risk and annualization

For the Level A fixed-weight gross portfolio, report:

- the mean daily return;
- the sample daily volatility;
- annualized volatility;
- geometrically annualized return;
- the gross wealth, running peak and drawdown for every date;
- maximum drawdown;
- maximum-drawdown date.

Level C — holdings drift, scheduled rebalancing and costs

Start with initial holdings equal to initial wealth multiplied by the configured target weights. Do not charge an initial setup cost.

For each date, first apply that date's asset returns to the existing holdings. On a configured rebalance date, then:

1. calculate pre-trade NAV and pre-trade weights;
2. calculate one-way turnover as half of the total absolute difference between pre-trade and target weights;
3. calculate transaction cost from pre-trade NAV, one-way turnover and the configured basis-point rate;
4. deduct the cost;
5. reset the holdings to the configured target weights using the remaining NAV.

On other dates, retain the drifted holdings.

Report every configured rebalance event with:

- date;
- pre-trade NAV;
- pre-trade weights;
- turnover;
- transaction cost;
- post-trade NAV;
- post-trade weights.

Also report:

- total turnover;
- total transaction cost;
- terminal net wealth;
- net cumulative return;
- maximum drawdown of the net post-trade NAV path;
- the corresponding date.

Calculation conventions

These conventions remove legitimate implementation ambiguity; they are not a list of complete solution formulas.

- Preserve CSV date order exactly.
- Use the configuration's annualization factor of 252.
- Sample daily volatility uses ddof=1.
- Include initial wealth 1.0 when determining running peaks and drawdowns.
- If the same maximum drawdown occurs on more than one date, select the earliest input date.
- Apply returns before any end-of-day rebalance.
- Transaction cost is deducted at the rebalance after pre-trade NAV and turnover are known.
- Do not round intermediate values.
- Emit only finite JSON numbers; never emit NaN, Infinity or numeric strings.
- Retain enough precision for the public comparison tolerance:
  abs(actual - expected) <= 1e-10 + 1e-9 * abs(expected).

Secondary full-artifact output

When completing the secondary repository artifact, create exactly:

experiments/shared/t3-quant-suite-v1/submissions/quant_portfolio_analytics.json

The JSON must match the supplied `output_schema_v1.json` exactly. That public
schema defines every required top-level constant, section, row and scalar field;
do not add, omit or rename fields.

Change boundary

`submit_t3_items` writes to the run-local item store, not to the repository. The only changed repository path may be:

experiments/shared/t3-quant-suite-v1/submissions/quant_portfolio_analytics.json

Do not modify any other repository path, the source branch, a previous run or
the paired target branch.
