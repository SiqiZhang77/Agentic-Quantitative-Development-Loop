# XCom repair deployment and recovery-verification runbook

Recovery ID: `exp2-xcom-recovery-cfb27068-scrum184-v1`

This is infrastructure recovery evidence only. It must never be counted as a
new C1 observation.

## Fixed identities

- Repair commit: `cfb27068abb041b77a56e37820e628a9537e0dda`
- Bundle: `dist/exp2/exp2-si-airflow-cfb27068abb0.tar.gz`
- Bundle SHA-256:
  `74afe468e333d481c0b57fa14b59a5d0652bf27e19ec52b4087343ea9c874377`
- Jira input: `SCRUM-184`, comment `14207`
- Workflow ID: `SCRUM-184_14207_a6970e41d2de`
- Full request SHA-256:
  `a6970e41d2de3ff51154d8540e1f91134732f4cd9ea9554f3a3fcdf033a91639`
- Original C1 Airflow DagRun ID:
  `manual__2026-08-19T23:59:17.045495+00:00`
- Saved runtime run ID: `run_SCRUM-184_20260820T0505516240530`
- Generated branch/head: `quant/SCRUM-184` /
  `4a9e4e4e0580cc7b9f7f1015e5f1e6df5b3bb8d7`
- Existing workflow bot comments expected: running `14210`, final `14211`
- Existing sandbox image remains:
  `sha256:cf8d8cd9b64400d247e09bf3eff456ead21869c42863b0ce27f55fab83e1641d`
- Existing RAG index SHA-256 remains:
  `72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1`

## Why the remote state is a hard gate

The XCom-repaired task discards the verbose return and has
`do_xcom_push=False`. The inner workflow skips retrieval/container/model/Jira
writeback only when the saved state has all of the following:

- top-level status is `succeeded` or `failed`;
- `final_result` is a JSON object;
- `stages.jira_writeback.status` is `succeeded`.

Credential/source validation occurs before this terminal shortcut. Therefore
the recovery must not be triggered if the read-only preflight or exact state
check fails. Jira idempotency alone is not a sufficient safety gate.

## Phase 1 — exact candidate-only deployment

On the Mac, verify and transfer only the fixed bundle (or use the approved UCL
transfer hop if direct SCP is unavailable):

```bash
set -euo pipefail
EXP2_REPO=/Users/angelzhang/Documents/Codex/2026-08-09/wof/work/BSLAgenticQuantDevLoop
EXP2_BUNDLE="$EXP2_REPO/dist/exp2/exp2-si-airflow-cfb27068abb0.tar.gz"
EXP2_SHA=74afe468e333d481c0b57fa14b59a5d0652bf27e19ec52b4087343ea9c874377
test "$(shasum -a 256 "$EXP2_BUNDLE" | awk '{print $1}')" = "$EXP2_SHA"
test "$(tar -xOf "$EXP2_BUNDLE" exp2-si-airflow-cfb27068abb0/scripts/exp2_si_runtime/dags/.bslagenticquantdevloop_revision | tr -d '\r\n')" = cfb27068abb041b77a56e37820e628a9537e0dda
scp "$EXP2_BUNDLE" "$EXP2_BUNDLE.sha256" masteruser@bialobog.cs.ucl.ac.uk:/opt/airflow3/private-exports/experiment2/deployment/
```

On Bialobog, install only the candidate package:

```bash
set -euo pipefail
EXP2_DEPLOY_DIR=/opt/airflow3/private-exports/experiment2/deployment
EXP2_AIRFLOW=/opt/airflow3/venv/bin/airflow
cd "$EXP2_DEPLOY_DIR"
test "$(sha256sum exp2-si-airflow-cfb27068abb0.tar.gz | awk '{print $1}')" = 74afe468e333d481c0b57fa14b59a5d0652bf27e19ec52b4087343ea9c874377
sha256sum -c exp2-si-airflow-cfb27068abb0.tar.gz.sha256
df -hT / /srv
EXP2_INSTALL_DIR="$(mktemp -d /tmp/exp2-si-install.XXXXXX)"
tar -C "$EXP2_INSTALL_DIR" -xzf exp2-si-airflow-cfb27068abb0.tar.gz
cd "$EXP2_INSTALL_DIR/exp2-si-airflow-cfb27068abb0"
AIRFLOW_HOME=/opt/airflow3 ./install_exp2_candidate.sh
test "$(tr -d '\r\n' </opt/airflow3/scripts/exp2_si_runtime/dags/.bslagenticquantdevloop_revision)" = cfb27068abb041b77a56e37820e628a9537e0dda
AIRFLOW_HOME=/opt/airflow3 "$EXP2_AIRFLOW" dags list | grep -F jira_exp2_si_runner
AIRFLOW_HOME=/opt/airflow3 "$EXP2_AIRFLOW" dags list-import-errors 2>&1 | grep -F 'No data found'
```

1. Check free disk without deleting anything.
2. Verify the transferred bundle with `sha256sum -c`.
3. Install only through the bundled candidate installer.
4. Read back installed revision `cfb27068abb041b77a56e37820e628a9537e0dda`.
5. Confirm `jira_exp2_si_runner` is listed and there are no DAG import errors.
6. Import the deployed DAG read-only and assert the leaf task's
   `do_xcom_push is False`.
7. Do not rebuild the sandbox image and do not change any Airflow Variable.
8. Read back the unchanged candidate image and frozen index hash.
9. Record the installer evidence that production DAGs were not modified and
   that no Airflow Variables were changed.

Abort on any mismatch.

## Phase 2 — read-only safety preflight

Using the deployed candidate modules and `EXP2_SI_` namespace:

1. Run `validate_candidate_configuration()`.
2. Build only the selected payload for `SCRUM-184` / `14207`.
3. Validate the command and perform the existing read-only GitHub
   credential/source-branch checks. Never print returned credentials.
4. Recompute and assert the exact request hash and workflow ID above.
5. Load only this state file:

   `/opt/airflow3/private-runs/experiment2/state/SCRUM-184_14207_a6970e41d2de.json`

6. Assert, without printing the full state:
   - top-level `status == "succeeded"`;
   - `request_hash` equals the fixed full hash;
   - `latest_comment_id == "14207"`;
   - `stages.sandbox.status == "succeeded"`;
   - `stages.jira_writeback.status == "succeeded"`;
   - saved sandbox result equals `final_result`;
   - saved workflow/run/branch/commit identities equal the fixed values above.
7. Record the exact state path, SHA-256, size and modification time.
8. Read Jira comments and record every bot-comment ID carrying this workflow
   ID, regardless of success/failure/partial/validation category. Separately
   assert there is exactly one successful terminal bot comment, ID `14211`.
9. Read the remote `quant/SCRUM-184` head and assert the fixed commit.
10. Confirm there is no candidate DagRun currently queued or running.
11. Confirm no DagRun in any state already uses the reserved recovery ID
    `manual__exp2_xcom_recovery_cfb27068`.

If any assertion fails, stop. Do not trigger a run and do not repair state by
hand.

## Phase 3 — one guarded recovery DagRun

Create one fresh DagRun, not a retry/clear of the old failed task instance, with
the exact configuration:

```json
{"issue_key":"SCRUM-184","comment_id":"14207"}
```

Use the exact recovery run ID `manual__exp2_xcom_recovery_cfb27068`. If the
trigger response is ambiguous, query that run ID; never issue a second trigger
blindly. If that ID already exists, stop and inspect it instead of inventing a
replacement ID.

```bash
set -euo pipefail
EXP2_AIRFLOW=/opt/airflow3/venv/bin/airflow
EXP2_RECOVERY_DAG_RUN_ID=manual__exp2_xcom_recovery_cfb27068
AIRFLOW_HOME=/opt/airflow3 "$EXP2_AIRFLOW" dags trigger \
  --run-id "$EXP2_RECOVERY_DAG_RUN_ID" \
  --conf '{"issue_key":"SCRUM-184","comment_id":"14207"}' \
  jira_exp2_si_runner
AIRFLOW_HOME=/opt/airflow3 "$EXP2_AIRFLOW" dags list-runs --no-backfill -o table jira_exp2_si_runner | grep -F "$EXP2_RECOVERY_DAG_RUN_ID"
```

If the trigger command returns an error, run only the final `list-runs` query
to determine whether it created the DagRun; never submit the trigger again
without first resolving that query.

## Phase 4 — required postflight proof

The task and DAG must succeed. The task log must contain:

`Workflow SCRUM-184_14207_a6970e41d2de already terminal; returning saved result`

It must not contain evidence of any of these paths:

- sandbox-stage attempt or completed-sandbox reuse;
- frozen-memory preparation or prompt injection;
- Jira-writeback-stage attempt or HTTP writeback;
- LiteLLM/model call;
- XCom push.

`Done. Returned value was: None` is acceptable.

After completion, prove:

- recovery-state hash/size/mtime and saved top-level status/runtime/branch/commit
  identities are unchanged;
- `quant/SCRUM-184` head is unchanged;
- the complete set of bot-comment IDs carrying this workflow ID is unchanged,
  and the successful terminal subset is still exactly `[14211]`;
- there was no model/proxy request in the recovery window, if proxy audit is
  available;
- the saved recovery task log is complete, with path, SHA-256, byte size, line
  count, start/end timestamps and exactly one terminal-short-circuit line;
- there is no XCom row for the task/run if read-only metadata access is
  available. Task success, `do_xcom_push=False`, no `Pushing xcom` log line and
  an absent XCom row form the strongest available proof.

Record the outcome in a new recovery evidence directory and append a factual
entry to the implementation log. Do not modify the V5 manifest or README.

## Local regression evidence

Before deployment, run in an approved environment with pytest:

```text
python -m pytest -q \
  jira-chatops-gateway/tests/test_jira_exp2_si_runner.py \
  jira-chatops-gateway/tests/test_docker_sandbox_runner.py::test_run_and_writeback_returns_terminal_saved_result_without_reposting \
  jira-chatops-gateway/tests/test_docker_sandbox_runner.py::test_post_result_comment_once_skips_existing_workflow_comment
```

On 2026-08-23, the exact three test selectors above were run against clean
XCom-relevant files at repository HEAD
`cfb27068abb041b77a56e37820e628a9537e0dda`, using the existing isolated
`rag-audit-venv`. Pytest reported `7 passed` (the first selector expands to five
tests). A read-only AST check found the candidate leaf task's sole
`do_xcom_push` value to be `False`; the fixed bundle checksum and embedded
revision also matched this runbook. The structured local evidence is stored in
the private incident directory as `LOCAL_REGRESSION_EVIDENCE.json`.

This proves the local repair and fixed bundle, not deployment. The Bialobog
install, guarded recovery DagRun and postflight evidence remain mandatory and
must not be marked complete from local tests.
