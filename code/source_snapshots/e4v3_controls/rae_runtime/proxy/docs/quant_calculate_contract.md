# `quant_calculate` public contract

`quant_calculate` is a bounded deterministic arithmetic tool. It does not read
the task repository, execute code, access the network, or choose a solution
strategy. The calling agent must select and compose the numerical operations.

## One source of truth

Operation names, accepted argument names, required arguments, optional
arguments, and model-visible JSON kinds are defined in
`quant_calculator.OPERATION_ARGUMENT_KINDS` and
`quant_calculator.OPTIONAL_OPERATION_ARGUMENTS`.

The same definitions drive both boundaries:

1. `quant_calculate_request_json_schema()` generates the FastMCP schema shown
   to the model. Each operation is an `op`-discriminated schema variant with exact
   `args` properties.
2. `_require_operation_args()` enforces the required and allowed argument names
   before arithmetic begins.

Do not add a field to only one boundary. Add or change it in the shared
contract, preserve strict rejection of unknown fields, and update the offline
contract tests.

## General semantics

- A request contains an ordered operation graph. Later operations may refer to
  earlier results in that same request with `{"ref": "operation_id"}`.
- An object used as an operand must be exactly a `ref` or `stored_ref`. A new
  arithmetic operation cannot be nested inside another operation's arguments;
  give it its own earlier operation ID and reference that ID.
- A later call may continue arithmetic from a returned result of an earlier
  successful or partial call in the same run with the explicit form
  `{"stored_ref": {"calculation_id": "calculation-1", "result_id": "result"}}`.
  Ordinary `ref` never crosses a call boundary, so the two scopes cannot be
  confused.
- Scalar broadcasting is available only where the published operation schema
  allows a numeric shape.
- Vectors and matrices are bounded. Matrix rows must be rectangular.
- Runtime semantic checks remain authoritative for relationships JSON Schema
  cannot express, such as matching shapes, valid reference ordering, weights
  summing to one, and slice bounds relative to vector length.
- `rebalance_indices` is either one flat list of unique zero-based input-row
  positions or one whole-field reference to such a list. A list-valued
  reference must not be wrapped inside another array.
- Missing required arguments and unknown extra arguments fail closed.
- A rejected batch returns the zero-based failed operation position, operation
  type, argument name when known, and a fixed content-free explanation of the
  violated rule. The audit stores those same safe fields plus input/output
  hashes, but never stores operand values or the model-authored request.
- If an operation fails after earlier operations completed, the calculator
  retains only completed IDs that the caller explicitly listed in `return_ids`.
  The public response uses `status: partial`, gives those saved result IDs plus
  the same scalar/shape/hash summaries as a successful call, and assigns a
  `calculation_id`. A later call may use those results with `stored_ref`. If the
  first operation fails, or none of the completed operation IDs was selected in
  `return_ids`, the response remains `status: failed` and no calculation is
  stored. This rule preserves usable arithmetic without exposing internal
  intermediates or deciding which result answers which task item.
- A partial-call audit records the hash of the stored result set, the number of
  completed operations, result kinds and shapes, and safe failure fields. It
  does not record numeric values, result IDs, the request, or task-item mappings.
- Exact values returned by the calculator may be copied, indexed, labelled with
  non-numerical input metadata such as dates or JSON field names, and serialized
  into a repository artifact. That assembly does not perform new arithmetic.
- Tool-call caps, arithmetic formulas, audit sanitization, repository scope,
  and architecture permissions are independent of this schema contract.

## Experimental neutrality

The public contract documents how to call generic arithmetic primitives. It
must not contain task inputs, expected outputs, recommended operation order,
portfolio choices, or solution-specific examples. Both single-agent and
manager-star conditions receive the same generated contract.
