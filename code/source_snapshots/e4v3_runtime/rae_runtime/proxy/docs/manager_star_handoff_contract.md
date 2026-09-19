# Manager-star handoff size contract

The manager-star runtime validates the JSON passed between its logical roles.
It counts characters after converting the object to canonical JSON, so key
ordering and whitespace do not change the result.

- `architect_plan_v1`: at most 50,000 canonical JSON characters.
- `developer_result_v1`: at most 50,000 canonical JSON characters.
- `manager_final_v1`: no separate whole-document character limit; its JSON
  schema still bounds every field and list.

For Architect and Developer, the applicable number is included both in the
role instruction and in the prompt's `output_constraints` object before the
model writes its handoff. A handoff above the stated limit is rejected without
passing its content to the next role.

This boundary controls transport size only. It does not change the task,
arithmetic tools, model-call budget, token budget, or scoring rules.
