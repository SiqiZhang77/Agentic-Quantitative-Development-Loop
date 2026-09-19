# Isolated deployment for Experiment 2 C0 versus C1

This runbook deploys `feature/exp2-rag-v1` without merging or copying it over
the shared Experiment 1 DAGs. It creates one unscheduled Airflow DAG,
`jira_exp2_si_runner`, and installs the feature-branch runtime as a private
Python package under `/opt/airflow3/scripts/exp2_si_runtime`.

The candidate command marker is `/quant-exp2`. The production poller accepts
only the exact first token `/quant`, so it ignores the candidate request. The
candidate DAG is triggered manually with an issue key, fetches every Jira
comment page for that issue, selects the candidate comment, converts only its
marker to `/quant` in memory, and reuses the normal validation, Docker, agent,
GitHub, progress, and Jira final-writeback path.

## Deployment boundary

The deployment must not:

- merge `feature/exp2-rag-v1` into `main`;
- run `sync_airflow_dags_from_git.sh` against the shared DAG folder with the
  feature branch;
- overwrite `docker_sandbox_runner.py` or another production DAG;
- change the shared `SANDBOX_EXECUTION_MODE=yarn` value;
- publish or configure the mutable sandbox tags `latest` or `stable`;
- place the Jira corpus or index inside Git or the Docker sandbox mount.

The deployment deliberately reuses the existing Jira, LiteLLM, GitHub-user,
and GHCR Airflow Connections. It isolates the settings that define the
experimental runtime through the `EXP2_SI_` Variable namespace.

## 1. Commit and push only the personal branch

Run on the Mac from the clean project worktree:

```bash
cd "/Users/angelzhang/Documents/Codex/2026-08-09/wof/work/BSLAgenticQuantDevLoop"
git status --short --branch
git branch --show-current
git log -1 --format='%H %s'
```

The branch must be `feature/exp2-rag-v1`. Push that branch to the GitHub remote;
do not create a pull request to `main`. This worktree may use a local clone as
`origin`, so inspect `git remote -v` and use the remote that resolves to the
actual `bankingscience/BSLAgenticQuantDevLoop` GitHub repository.

Record the resulting full commit SHA as `EXP2_CODE_SHA`.

## 2. Build an immutable candidate sandbox image

In GitHub, open **Actions → Build & Push Sandbox Image → Run workflow**, select
`feature/exp2-rag-v1`, and run it. The feature-branch workflow publishes the
unique `sha-<full-commit-sha>` tag but does not update `latest`.

Open the completed action and record `Published digest: sha256:<64 hex>`. The
Airflow candidate must use the digest form, not the tag form:

```text
ghcr.io/bankingscience/bslagenticquantdevloop/sandbox@sha256:<IMAGE_DIGEST>
```

Confirm that the action's source SHA equals `EXP2_CODE_SHA`.

## 3. Build the Airflow candidate bundle locally

The packager refuses a dirty worktree and a branch other than
`feature/exp2-rag-v1`:

```bash
cd "/Users/angelzhang/Documents/Codex/2026-08-09/wof/work/BSLAgenticQuantDevLoop"
./jira-chatops-gateway/scripts/package_exp2_candidate.sh
ls -lh dist/exp2/
shasum -a 256 dist/exp2/exp2-si-airflow-*.tar.gz
```

It produces:

- `exp2-si-airflow-<short-sha>.tar.gz`: candidate loader, private runtime,
  schemas, source-revision marker, and installer;
- the adjacent `.sha256`: transfer digest.

The bundle contains code only. It does not contain the Jira snapshot, corpus,
index, credentials, or Airflow configuration.

## 4. Prepare protected server directories

Connect to Bialobog and create experiment-owned locations:

```bash
ssh masteruser@bialobog.cs.ucl.ac.uk
umask 077
install -d -m 700 /opt/airflow3/private-rag/experiment2
install -d -m 700 /opt/airflow3/private-runs/experiment2/state
install -d -m 700 /opt/airflow3/private-exports/experiment2/deployment
exit
```

These paths are outside the Docker/YARN sandbox mounts.

## 5. Upload the bundle and frozen index

Run on the Mac. Replace `<SHORT_SHA>` with the bundle filename produced above:

```bash
scp \
  "dist/exp2/exp2-si-airflow-<SHORT_SHA>.tar.gz" \
  "dist/exp2/exp2-si-airflow-<SHORT_SHA>.tar.gz.sha256" \
  masteruser@bialobog.cs.ucl.ac.uk:/opt/airflow3/private-exports/experiment2/deployment/
```

Upload the approved frozen index separately:

```bash
scp \
  "/Users/angelzhang/Documents/Codex/2026-08-09/wof/private/exp2/e2-freeze-v1/e2-jira-index-v1.json" \
  masteruser@bialobog.cs.ucl.ac.uk:/opt/airflow3/private-rag/experiment2/
```

SCP is the encrypted file-transfer operation. The adjacent SHA-256 value lets
the operator prove that the received archive is byte-for-byte identical to the
local archive.

## 6. Verify and install only the candidate files

On Bialobog:

```bash
ssh masteruser@bialobog.cs.ucl.ac.uk
cd /opt/airflow3/private-exports/experiment2/deployment
sha256sum -c exp2-si-airflow-<SHORT_SHA>.tar.gz.sha256
tar -tzf exp2-si-airflow-<SHORT_SHA>.tar.gz
```

The checksum must say `OK`. The archive listing must contain only one candidate
loader plus `scripts/exp2_si_runtime` and its installer.

Extract into a new temporary directory and install:

```bash
EXP2_INSTALL_DIR="$(mktemp -d /tmp/exp2-si-install.XXXXXX)"
tar -C "$EXP2_INSTALL_DIR" -xzf exp2-si-airflow-<SHORT_SHA>.tar.gz
cd "$EXP2_INSTALL_DIR"/exp2-si-airflow-<SHORT_SHA>
AIRFLOW_HOME=/opt/airflow3 ./install_exp2_candidate.sh
```

The installer verifies every internal file, then writes only:

```text
/opt/airflow3/dags/jira_exp2_si_loader.py
/opt/airflow3/scripts/exp2_si_runtime/**
```

It does not modify the shared production DAG files.

Verify the installed revision and DAG import:

```bash
cat /opt/airflow3/scripts/exp2_si_runtime/dags/.bslagenticquantdevloop_revision
AIRFLOW_HOME=/opt/airflow3 /opt/airflow3/venv/bin/airflow dags list | grep jira_exp2_si_runner
AIRFLOW_HOME=/opt/airflow3 /opt/airflow3/venv/bin/airflow dags list-import-errors
```

The revision must equal `EXP2_CODE_SHA`, and there must be no candidate import
error.

Verify the index copied correctly:

```bash
sha256sum /opt/airflow3/private-rag/experiment2/e2-jira-index-v1.json
chmod 600 /opt/airflow3/private-rag/experiment2/e2-jira-index-v1.json
```

The approved current index digest is:

```text
72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1
```

Stop if the server prints a different digest.

## 7. Add candidate-only Airflow Variables

In **Airflow → Admin → Variables**, add exactly these non-secret settings:

| Key | Value |
| --- | --- |
| `EXP2_SI_ALLOWED_PROJECT_KEY` | `SCRUM` |
| `EXP2_SI_SANDBOX_EXECUTION_MODE` | `docker` |
| `EXP2_SI_SANDBOX_IMAGE` | `ghcr.io/bankingscience/bslagenticquantdevloop/sandbox@sha256:<IMAGE_DIGEST>` |
| `EXP2_SI_JIRA_RAG_INDEX_PATH` | `/opt/airflow3/private-rag/experiment2/e2-jira-index-v1.json` |
| `EXP2_SI_JIRA_RAG_INDEX_SHA256` | `72786c237fb1912ebc03babc2aad571490f76cfadadd13e8593eedd45b5b2be1` |
| `EXP2_SI_WORKFLOW_RECOVERY_STATE_DIR` | `/opt/airflow3/private-runs/experiment2/state` |
| `EXP2_SI_USE_REAL_BACKTESTER` | `false` |

Do not edit the unprefixed production Variables. The candidate fails before Jira
access when one of these overrides is absent, when execution is not Docker, or
when the image is not pinned by digest.

## 8. Create the paired C0 and C1 Jira tickets

Create two new tickets with identical title, task body, acceptance criteria,
model, source branch, budgets, and hidden-test version. Use separate ticket
keys. Post `/quant-exp2`, not `/quant`.

C0 command:

```text
/quant-exp2
<CANONICAL TASK AND ACCEPTANCE CRITERIA>

strategy_type: refactor
resource_path: <TARGET FILE>
allowed_directories: <ALLOWED CODE DIR> <ALLOWED TEST DIR>
repo: bankingscience/BSLAgenticQuantDevLoop
branch_map: BSLAgenticQuantDevLoop=<FROZEN_SOURCE_BRANCH>
model: qwen3-coder
allow_iteration: false
max_iterations: 1
max_failed_iterations: 1
max_agent_turns: 30
max_commits_per_run: 5
timeout_seconds: 1800
max_token_budget_per_run: 50000
cpu_vcpus: 2
memory_mb: 4096
pids_limit: 256
rag_enabled: false
rag_top_k: 5
```

C1 must differ only in:

```text
rag_enabled: true
```

The `<FROZEN_SOURCE_BRANCH>` must be an experiment-only protected branch whose
head SHA was recorded before either condition ran. Do not use a moving personal
development branch as the paired source.

## 9. Trigger the candidate DAG manually

In Airflow, open `jira_exp2_si_runner`, choose **Trigger DAG**, and use:

```json
{"issue_key":"SCRUM-<C0_NUMBER>"}
```

Then trigger the C1 ticket:

```json
{"issue_key":"SCRUM-<C1_NUMBER>"}
```

For a formal run, pin the exact Jira comment ID when it has been recorded:

```json
{"issue_key":"SCRUM-<NUMBER>","comment_id":"<COMMENT_ID>"}
```

The candidate is `schedule=None` and `max_active_runs=1`; it cannot poll other
students' tickets or run two conditions simultaneously.

## 10. Verify the paired smoke result

For C0:

- the Jira command records `rag_enabled: false`;
- the task log does not contain `Prepared frozen Jira memory evidence`;
- there is no `retrieval_context` or memory-delivery audit.

For C1:

- the log contains `Prepared frozen Jira memory evidence`;
- it records the frozen index/corpus/exclusion IDs and hashes;
- it records candidate count, ranked memory IDs, scores, query hash, and
  generator-prompt delivery hashes without logging retrieved free text;
- the prompt-delivery audit reports `delivery_mode=generator_prompt` and
  `prompt_injected=true`.

For both:

- source branch head, source SHA, image digest, model alias, budgets, task text,
  acceptance criteria, and hidden tests match;
- a distinct `quant/<TICKET>` branch and Jira final report exist;
- external tests are run after pulling the generated branch.

This completes an end-to-end paired smoke test. It is not yet formal thesis
evidence until the protocol, model/runtime identity, task allocation, hidden
tests, and durable result recorder are frozen as described in `PROTOCOL.md`.
