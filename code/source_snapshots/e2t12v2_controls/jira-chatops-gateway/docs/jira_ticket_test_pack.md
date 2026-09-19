# Jira `/quant` test pack

A copy-paste set of tickets for exercising a freshly deployed gateway. Every
ticket below was run through `validate_jira_command_payload` at commit
`d6da543`; the accept/reject outcome and the quoted error text are what the
parser actually produced, not what it is expected to produce.

Post each as a comment on a Jira issue. Tickets 3, 4, 6 and 7 and every negative
except N5 need a file attached to the issue first - see the setup table.

For the reference documentation of each option, see
[jira_ticket_examples.md](jira_ticket_examples.md). This page is for verifying a
build, so it leads with the recently added dataset paths and the guardrails
around them.

## Attachment setup

| File to attach | Used by | Notes |
|---|---|---|
| `experiment_2_input.csv` | P3, P7, N1, N2, N4, N6, N7 | Synthetic [monthly-return sample](examples/experiment_2_input.csv); attach it unchanged for the positive cases |
| `positions_q3.xlsx` | P4 | Any small XLSX |
| `momentum_baseline.request` | P6 | A valid strategy `.request` |
| `experiment 2 input.csv` | N3 | Note the spaces in the name |
| `notes.txt` | N8 | Any text file |

## Two traps worth knowing before you start

**`input_attachment: <file>` works on its own line.** Written inline, on the
same line as the objective, the parser binds an empty value and the filename
falls into the objective text. That used to silently run with no dataset
mounted; it is now a hard rejection (N1). Inline, use `input_attachment=<file>`.

**No apostrophes in the objective.** The objective is tokenised with
`shlex.split`, so an ordinary English apostrophe is read as an unterminated
quote and the whole comment is rejected:

```text
/quant
Fix the client's retry logic
strategy_type: refactor
```

> Could not parse command line 'Fix the client's retry logic': No closing
> quotation.

This affects both forms and every `strategy_type`. Write "the client retry
logic" instead. Every objective in this pack is apostrophe-free for that reason.

---

# Positive tickets

## P1 - Backtest, read-only parameterised baseline

```text
/quant
Run a backtest of the momentum strategy with a smaller, more concentrated book and report Sharpe and drawdown
strategy_type: backtest
read_only: true
params: NPORT=50, NFREQ=4
start_date: 2020-01-01
end_date: 2024-12-31
max_iterations: 1
max_token_budget_per_run: 100000
```

Submits the committed baseline with the stated parameters. **Check:** no branch
or commit is created, and the result posts back with performance metrics.

## P2 - Read-only baseline with deterministic params

```text
/quant
Submit the existing strategy baseline for one controlled backtest
strategy_type: backtest
read_only: true
params: NPORT=80, NFREQ=4
resource_path: rae_runtime/proxy/strategy.request
max_iterations: 1
max_failed_iterations: 1
timeout_seconds: 5400
```

**Check:** no branch, no commit, and the submitted `.request` in the run output
carries exactly `NPORT=80`, `NFREQ=4`.

## P3 - Jira dataset attachment (CSV)

```text
/quant
Analyse the attached monthly-return dataset and report the findings
strategy_type: analysis
zero_code_modifications: true
input_attachment: experiment_2_input.csv
input_format: csv
```

The headline new path. **Check** the runtime request contains:

```
source_kind:    jira_attachment
source_uri:     jira-attachment://<id>
container_path: /workspace/input/datasets/experiment_2_input.csv
```

and that the run log reports a SHA-256 for the materialised file. If several
copies of the filename were uploaded, the highest attachment ID wins.

## P4 - Jira dataset attachment (XLSX) into a nested mount path

```text
/quant
Summarise the attached Q3 position book
strategy_type: analysis
read_only: true
input_attachment: positions_q3.xlsx
input_mount_path: /workspace/input/datasets/q3/positions.xlsx
```

**Check:** format is inferred as `xlsx` with no `input_format`, and the file
lands at the nested path, not at the default one.

## P5 - Approved HDFS dataset

```text
/quant
Analyse the approved Experiment 2 dataset from HDFS
strategy_type: analysis
read_only: true
input_hdfs_uri: hdfs:///user/masteruser/quant-experiment-data/SCRUM-195/experiment_2_input.csv
input_format: csv
```

**Check:** the URI is under `SANDBOX_INPUT_HDFS_ALLOWED_ROOTS` - validation
accepts any absolute `hdfs://` URI, and the allowed-roots check happens in the
runner, so a path outside the roots fails at run time, not at comment time.
Point this at a file that actually exists in your cluster before running.

## P6 - Baseline `.request` attachment

```text
/quant
Backtest the attached baseline request as-is
strategy_type: backtest
read_only: true
request_attachment: momentum_baseline.request
```

The pre-existing attachment path, included to confirm the dataset work did not
disturb it. Note `request_attachment` is backtest-only and is capped at 2 MB,
separately from the dataset limits.

## P7 - Inline dataset form

```text
/quant Analyse the attached returns file strategy_type=analysis read_only=true input_attachment=experiment_2_input.csv
```

Same result as P3. **Check:** this accepts while N1 rejects - that pair is the
whole point of the inline/own-line distinction.

## P8 - Scoped refactor with a turn cap

```text
/quant
Extract the retry/backoff logic in the GitHub client into a helper and reuse it
strategy_type: refactor
resource_path: rae_runtime/proxy/github_client.py
allowed_directories: rae_runtime/proxy
allow_iteration: true
max_agent_turns: 20
repo: bankingscience/BSLAgenticQuantDevLoop
```

**Check:** no file outside `rae_runtime/proxy` is written, and the run stops at
20 turns per edit pass.

## P9 - Coordinated multi-repository edit

```text
/quant
Update the shared runtime contract and the data-handler integration together
strategy_type: refactor
repos: BSLAgenticQuantDevLoop, ATPDataHandlersRepo
resource_path: rae_runtime/proxy/contracts.py
allowed_directories: .
allowed_directories_map: BSLAgenticQuantDevLoop=rae_runtime|jira-chatops-gateway; ATPDataHandlersRepo=.
```

**Check:** both repositories get a `quant/<KEY>` branch, and the per-repo scope
map overrides the global `.` scope.

---

# Negative tickets

Each must be **rejected at comment time**, with the gateway posting the error
back to the ticket. Quoted text is the exact message emitted.

## N1 - Inline `input_attachment:` with no value

```text
/quant Analyse the attached dataset strategy_type=analysis read_only=true input_attachment: experiment_2_input.csv
```

> input_attachment requires a filename, for example
> input_attachment=experiment_2_input.csv. Put the option on its own line to use
> the 'input_attachment: \<filename\>' form.

The most important negative in this pack: before the fix this was **accepted**
and ran the analysis with nothing mounted. If it succeeds, the deployed build
predates `85ca642`.

## N2 - Both dataset sources at once

```text
/quant
Analyse both sources at once
strategy_type: analysis
read_only: true
input_hdfs_uri: hdfs:///approved/experiment_2_input.csv
input_attachment: experiment_2_input.csv
```

> Use either input_hdfs_uri or input_attachment for one dataset, not both.

## N3 - Attachment filename containing spaces

```text
/quant
Analyse the attached file
strategy_type: analysis
read_only: true
input_attachment: experiment 2 input.csv
```

> input_attachment must be a plain .csv, .json, .xlsx, or .parquet filename
> without spaces or path components.

Spaces cannot survive into the container mount path, so such files must be
renamed before upload.

## N4 - Named attachment absent from the issue

```text
/quant
Analyse the attached file
strategy_type: analysis
read_only: true
input_attachment: missing_dataset.csv
```

> Jira dataset attachment 'missing_dataset.csv' was not found on this issue.
> Upload it before posting the /quant command.

## N5 - Missing `strategy_type`

```text
/quant
Analyse the attached dataset
read_only: true
input_attachment: experiment_2_input.csv
```

> strategy_type is required. Add one of: analysis, backtest, ingestion, other,
> refactor, for example strategy_type=backtest.

## N6 - Mount path outside the datasets root

```text
/quant
Analyse the attached file
strategy_type: analysis
read_only: true
input_attachment: experiment_2_input.csv
input_mount_path: /workspace/output/experiment_2_input.csv
```

> input_mount_path must name a file below /workspace/input/datasets/ and must
> not contain '..'.

## N7 - Declared format contradicts the extension

```text
/quant
Analyse the attached file
strategy_type: analysis
read_only: true
input_attachment: experiment_2_input.csv
input_format: parquet
```

> input_format=parquet does not match the file extension for
> 'experiment_2_input.csv' (csv).

## N8 - Unsupported dataset format

```text
/quant
Analyse the attached notes
strategy_type: analysis
read_only: true
input_attachment: notes.txt
```

> input_attachment must be a plain .csv, .json, .xlsx, or .parquet filename
> without spaces or path components.

## N9 - Known bug: a stray `.request` blocks non-backtest tickets

Attach `momentum_baseline.request` to the issue, then post:

```text
/quant
Analyse the attached dataset
strategy_type: analysis
read_only: true
```

> request_attachment is supported only for strategy_type=backtest.

This rejection is **wrong** - the ticket never asked for a `.request`.
`select_request_attachment` still auto-selects a lone `.request` attachment when
the objective contains "attached" or "attachment", then rejects it for not being
a backtest. The equivalent behaviour was removed from the dataset path in PR #65
but is still live here, so any issue carrying a `.request` file blocks every
non-backtest command whose objective mentions an attachment.

Workaround until it is fixed: keep `.request` files on backtest-only issues, or
avoid the words "attached"/"attachment" in non-backtest objectives.

---

## Operator notes

- **Size limits.** Datasets are bounded by `SANDBOX_INPUT_MAX_FILE_BYTES`
  (default 100 MB) and `SANDBOX_INPUT_MAX_TOTAL_BYTES` (default 250 MB).
  `.request` attachments have their own fixed 2 MB cap.
- **HDFS configuration is no longer required for attachment-only runs.** P3, P4,
  P6 and P7 must work on a Docker-mode deployment with no
  `SANDBOX_HDFS_NAMENODE_URI` set. If one of them fails with
  `Missing SANDBOX_HDFS_NAMENODE_URI ... for YARN mode`, the build predates
  `4cb8559`.
- **Attachments are never auto-selected.** A dataset is used only when
  `input_attachment` names it. Posting P3's objective with no `input_attachment`
  must run with no dataset - worth confirming, because an earlier build would
  pick up a single attached CSV from objective wording alone, including result
  artifacts the bot itself had attached to the issue.
- **YARN mode** additionally stages every input into the run-isolated HDFS
  directory and re-verifies size and checksum in the worker before the sandbox
  starts. Run P3 and P5 in both modes if both are deployed.
