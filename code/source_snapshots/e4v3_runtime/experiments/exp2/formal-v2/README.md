# Experiment 2 formal-v2 public package

This directory contains only secret-free protocol, task and packaging assets.
It must not contain Jira exports, retrieved text, hidden evaluators, gold
answers, condition maps, Airflow logs or credentials.

The package builder creates a new private run skeleton without contacting Jira,
GitHub, Airflow, LiteLLM or the model. It refuses to overwrite an existing
directory and verifies that each C0/C1 command pair differs only in
`rag_enabled`.

Example, after freezing a source branch and commit:

```text
python experiments/exp2/formal-v2/tools/build_run_package.py \
  --source-branch exp2/si-formal-v2-source \
  --source-commit FULL_40_CHARACTER_SHA \
  --blinding-salt-file /private/exp2/formal-v2/blinding_salt.txt \
  --output /private/exp2/formal-v2/run-package-v1
```

The salt is private and must contain at least 32 non-whitespace characters. The
builder records only its SHA-256. Reviewers receive neutral submission IDs; the
condition map remains under the private output's `blinding/` directory.

Generation is not protocol approval and does not authorize external runs.
