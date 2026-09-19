# Code handover

This package connects the dissertation to its actual frozen source versions. Start with **`e4v3_runtime`** for the main T3 implementation. The separately versioned control source prepares and launches observations; it is not a replacement for the runtime.

The source archives contain regular Git-tracked files exported at exact commits. Included source bytes are unchanged. Three temporary preview PNGs and three duplicate documentation PDFs per snapshot are omitted; the matching Markdown documents remain. No `.git` directory, populated local environment file, virtual environment, raw observation folder or uncommitted worktree edit is copied.

## 1. Quick start: no model calls

Use Python 3.11 or later. Run the following from **this `code/` directory**. The first three commands use only the Python standard library and require no credentials or network connection.

```bash
# Verify every source archive and every retained source file.
python3 tools/verify_and_extract.py

# Recalculate descriptive results from included data.
python3 analysis/reproduce_summaries.py --output reproduced_summaries.json

# Extract the main frozen runtime into a new directory.
python3 tools/verify_and_extract.py --snapshot e4v3_runtime --destination extracted
```

The last command creates `extracted/e4v3_runtime/` and refuses to overwrite an existing snapshot directory. The archive can also be unzipped using a normal ZIP tool. Extraction does not create a Git repository or alter a remote branch.

For the validated offline test subset, use a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/requirements-offline.txt
python tools/run_offline_tests.py
```

These tests exercise bounded numerical operations, safe audit records, role permissions, role budgets, hand-off contracts, public item-submission contracts and frozen BM25 retrieval. They use synthetic fixtures; they do not run agents, call production APIs or create experimental observations. The test runner extracts to a temporary directory and cleans that directory afterwards.

## 2. Contents

```text
code/
  README.md
  SOURCE_MANIFEST.json          Exact source commits and per-file SHA-256 hashes
  PACKAGE_SHA256.json           Integrity inventory of this handover
  source_archives/              Six independent, explicitly identified snapshots
  tools/                       Offline verification, extraction and test entry points
  analysis/                    Portable descriptive analysis and selected input tables
  docs/                        Packaging rationale and validation record
```

The archives make the historical versions explicit and keep the upload manageable in GitHub's browser interface. All implementation code, tests, dependency files, Dockerfiles, public schemas and task inputs remain inside their respective snapshots. The repository can be reviewed locally after extraction. Historical experiment folders within each snapshot are retained because launch scripts and tests share helpers across versions; their presence does not mean that every historical experiment was included in the dissertation.

## 3. Choose the correct source version

| Snapshot | Frozen commit | Purpose |
|---|---|---|
| `e4v3_runtime` | `265162d7ef12fa022bebc1ccd0591facb58b5d44` | Main T3 runtime: all 60 factorial observations |
| `e4v3_controls` | `24191e89aa5712b24436a155d86f05a860394ecc` | Final T3 batch-selection, identity and launch controls |
| `e2t12v2_runtime` | `6e7d29b3f220dd4cc639219aa7e765aee88e3988` | T1 primary retrieval pairs 1-5 and T2 primary pairs 1-4 |
| `e2t12v3_runtime` | `600e4f253cf4179467a6be9ec09b76edac268706` | Prospectively replaced T2 primary pair 5 |
| `e3v2_runtime` | `f6d8e7c65510387426ae7f693ca7eda3f7ddfa46` | Supplementary T1/T2 architecture observations, 3 pairs per task |
| `e2t12v2_controls` | `cdeb223900414ef96f07b208754d2321b78b74b1` | Retained E2T12V2 agreement and launch-control source |

Replace `e4v3_runtime` in the extraction command with any snapshot name above to inspect it. Do not merge these snapshots into one source tree and call it the experimental version. In particular, the original T2 fifth pair was excluded after a measurement-invalid parser/telemetry failure; its replacement used E2T12V3 for both conditions. The earlier T3 E2V7/E3V17 cohorts are not pooled with E4V3.

The operational repository uses some older version labels in filenames, default settings and README files. The dissertation's cohort, source SHA and resolved execution settings take precedence when interpreting the results. For example, the E4V3 batch runner is `experiments/exp4/run_e4v3_batch.py` in **`e4v3_controls`**; some helpers retain `e4v2` in their names. Generic legacy model aliases are not evidence that the reported study used that model.

## 4. Implementation reading map

Paths below are relative to an extracted snapshot. Use the snapshot indicated when needed.

| Component | Where to read |
|---|---|
| Shared platform overview | `README.md`; `docs/quant_dev_loop_architecture_and_operational_sop.md` |
| Jira and Airflow orchestration | `jira-chatops-gateway/dags/jira_quant_docker_orchestrator.py`; `dags/docker_sandbox_runner.py` |
| Jira corpus export and normalisation | `jira-chatops-gateway/scripts/export_jira_api_snapshot.py`; `scripts/normalize_jira_api_snapshot.py` |
| Frozen project-memory ranking | `jira-chatops-gateway/dags/jira_rag_retriever.py` |
| Runtime entry and iteration | `rae_runtime/sandbox/run.py`; `iteration_loop.py` |
| Developer pipeline and prompt handling | `rae_runtime/proxy/pipeline.py`; `pipeline_mcp.py`; `prompt_context.py` |
| Manager-star architecture, role policy and budgets | `rae_runtime/proxy/exp3/` |
| Repository tools and immediate T3 saving | `rae_runtime/proxy/github_mcp_server.py`; `t3_quant_item_submission.py`; `t3_quant_item_store.py` |
| Bounded quantitative calculator | `rae_runtime/proxy/quant_calculator.py`; `quant_calculator_audit.py` |
| T1/T2 task definitions | `experiments/exp2/formal-terra-t12-v1/` in the T1/T2 snapshots |
| T3 prompt, synthetic CSV, config, public rubric and schemas | `experiments/shared/t3-quant-suite-v2/` in `e4v3_runtime` |
| T3 formal launch protocol | `experiments/exp4/E4V3_PROTOCOL.md` and `run_e4v3_batch.py` in `e4v3_controls` |
| Runtime dependencies and container build | `rae_runtime/sandbox/requirements.in`; `requirements.txt`; `Dockerfile` |
| Gateway configuration | `jira-chatops-gateway/docs/airflow_ui_configuration.md` |

Relative filenames shortened after a semicolon stay in the directory of the preceding full path.

## 5. What can and cannot be reproduced from this package

**Source and offline behaviour.** Archive/file hashes can be checked directly. The selected frozen tests are runnable with the small offline dependency file. Runtime dependency locks are separately retained, including the Python 3.12-generated sandbox lock. Passing the offline subset is not certification of the entire deployment or all historical test suites.

**Descriptive results.** Included analysis recalculates T1/T2 group means, sample SDs, delivery counts and paired differences; T3 correct counts, submission coverage, level rates, descriptive contrasts and resource means; and retrieval metrics from the retained grades and delivery annotations. It does not reconstruct bootstrap confidence intervals, raw provider-request bodies or the private task-evaluator decisions.

**Live deployment and new agent runs.** These require a suitable Python/container environment, full runtime dependencies, the company's Jira/GitHub access, model-provider credentials, and the configured Airflow and execution infrastructure. The recorded experiments used the configured identifier `gpt-5.6-terra` through the OpenAI Chat Completions interface. This identifier does not guarantee an immutable model checkpoint. Optional operational backtesting paths additionally require the company's backtest/HDFS/YARN services. Configure credentials through the documented Airflow Connections or local environment; no credentials are supplied here.

**Historical experiment replay.** Exact source files alone do not reproduce a historical run. Resolved agreements, frozen request bindings, original private Jira index, authorised target repositories, image digests and private evaluator assets are separate retained evidence. The frozen scripts check Git/control identity and external paths; a ZIP extraction is not the original Git checkout. An authorised operator must use the matching original commit and resolved control package, or create a new prospective experiment namespace. Historical identities must not be reused for new runs.

The T3 runtime image was recorded as `sha256:904d1584bb1f0a4153981f738cd72c0cb3b8ce14dde35b317c942750cb82afab`. Source hashes and image identity serve different purposes; a new local image build is not that original image.

## 6. Interpretation and ownership

M0 means **individual developer**, not that every historical workflow contains only one model call or one model role. T1/T2 legacy M0 includes an advisory reviewer; in the primary T1/T2 runs iteration was disabled. Formal T3 M0 uses an answer-blind deterministic completion check instead of that reviewer. M1 is the manager-architect-developer protocol. The private evaluation is outside the task-solving loop.

A saved or structurally accepted item is not necessarily mathematically correct. Missing items remain in correctness and coverage denominators. Attempts and provider calls are resource records, not independent experimental replicates. T1/T2 confirmed scores include human scoring with user-approved Codex-assisted adjudication; T1's retrieval difference appears after that revision. Retrieval grades are retrospective Codex assessments with no independent human validation. These limitations are preserved in the analysis documentation.

Shared baseline code retains its collaborative provenance. The package introduces no change in licensing or company access rights. See the root README for the intended handover destination.
