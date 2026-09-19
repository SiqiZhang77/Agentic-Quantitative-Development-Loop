# Jira API snapshot export for Experiment 2

This runbook creates the private, structured Jira source snapshot used before
Experiment 2 normalization. It does not modify Jira and does not replace the
sanitization/index-building stages.

## Why the CSV remains useful but is not the formal source

The Jira **CSV - all fields** export is useful for corpus prototyping, ticket
triage, and checking an independent extraction. It is not sufficient as the
only formal E2 source because it cannot prove several properties needed by the
protocol:

| Requirement | Jira all-fields CSV | API snapshot |
| --- | --- | --- |
| Comment identity | Repeated comment columns contain a display envelope but no stable comment ID | Stable comment ID and hashed author account ID |
| Comment completeness | No per-ticket page/total evidence | Explicit `startAt`, returned count, and `total` for every page |
| Timezone | Exported timestamps can omit their UTC offset | Jira timestamps include an offset; instance and API-user timezones are recorded |
| Field history | Usually only the current summary, description, status, and other fields | Bulk changelog gives dated field changes |
| Bot identity | Display name or inferred marker | `/myself` account ID can be hashed and compared with comment authors |
| Concurrent changes | No beginning/end consistency check | Issue IDs and `updated` timestamps are fetched again before completion |
| Provenance | One file hash | Raw page hashes, request ledger, endpoint/page counts, and a snapshot manifest |
| Attachments | Repeated text envelopes/URLs | Stable attachment IDs and metadata, without downloading content |

The API snapshot is not automatically “more truthful” than the CSV in every
respect. Both only contain content visible to the exporting account. Issue
security and comment visibility still apply. For the formal freeze, compare API
counts with the CSV and investigate material differences instead of silently
choosing one.

## Read-only boundary

`scripts/export_jira_api_snapshot.py` has a hard method/endpoint allowlist:

```text
GET  /rest/api/3/serverInfo
GET  /rest/api/3/myself
POST /rest/api/3/search/jql
GET  /rest/api/3/issue/{key}/comment
POST /rest/api/3/changelog/bulkfetch
GET  /rest/api/3/attachment/{id}
```

The two POST requests are query operations: their request bodies carry JQL,
issue lists, and pagination tokens. The exporter has no allowlisted endpoint
for adding comments, editing issues, deleting issues, or downloading attachment
content. A dedicated Jira credential with only Browse/read access is preferable
when the project administrator can provide one.

## Output

The output directory must not already exist. It is created with mode `0700`,
and files are written with mode `0600`:

```text
<snapshot>/
  manifest.json
  audit.json
  requests.jsonl
  raw/
    server_info.json
    myself.json
    issues/page-*.json
    issue-verification/page-*.json
    comments/<TICKET>/attempt-*/page-*.json
    changelogs/batch-*/page-*.json
    attachments/<ID>.json
  records/
    issues.jsonl
    comments.jsonl
    changelogs.jsonl
    attachments.jsonl
```

`raw/` is the private response snapshot. It contains names, account IDs, URLs,
and possibly email addresses. Never commit it, attach it to Jira, or send it to
an LLM.

`records/` is a deterministic convenience view. Structured account IDs are
hashed and ADF text is rendered, but names, emails, or secrets may still occur
inside free text. `audit.json` therefore sets
`safe_for_direct_rag_ingestion` to `false`. A separate API normalizer must redact
and apply the cutoff before these records can feed the frozen index.

### Normalize the private API snapshot

Run the deterministic adapter locally, keeping both input and output outside
Git. It verifies every source-manifest hash, converts offset-aware and epoch-ms
timestamps to UTC, rewinds summary/description updates made after the cutoff,
excludes comments edited after the cutoff, and extracts structured identities
from private raw pages only for redaction:

```bash
python jira-chatops-gateway/scripts/normalize_jira_api_snapshot.py \
  --snapshot-dir /private/e2/jira-api-candidate \
  --excluded-ticket-ids experiments/exp2/exclusions/e2_broad_candidate_v1.json \
  --output-dir /private/e2/jira-api-corpus-candidate \
  --cutoff-at 2026-08-09T11:05:50Z \
  --bot-author-sha256 <CURRENT_BOT_ACCOUNT_ID_SHA256> \
  --bot-author-sha256 <HISTORICAL_BOT_ACCOUNT_ID_SHA256>
```

Do not mark the output frozen while `bot_author_trust_status` or the exclusion
list remains a candidate. The adapter emits `candidate_not_formally_frozen`,
even when all technical validation succeeds.

`requests.jsonl` records only the allowlisted method, endpoint, query body,
response status, safe rate-limit headers, raw-page path, and hash. It never
records Authorization or Jira connection credentials.

## Run the tests first

From `jira-chatops-gateway/`:

```bash
python -m pytest -q tests/test_export_jira_api_snapshot.py
```

The tests simulate multi-page issues, comments, and changelogs; attachment
metadata; a changing comment total; repeated pagination tokens; HTTP 429; the
read-only endpoint boundary; private file modes; atomic output; and manifest
hashes. They make no network calls.

## Run in the Airflow environment

The CLI deliberately has no `--token` or `--password` option. It imports the
existing `jira_quant_common.get_jira_config`, which resolves the configured
`JIRA_CONNECTION_ID` (default `jira_cloud`) and `JIRA_PROJECT_KEY`. After that
single resolution, the standalone process uses a private HTTP session instead
of asking Airflow 3's task-only `HttpHook` context to resolve the Connection a
second time. Credentials remain only in process memory and are never written to
the snapshot or request ledger. An explicit `--project-key` is applied before
the Airflow configuration is loaded, so it also works from an SSH shell where
the Variable SDK context is unavailable.

If the exporter is installed as `/opt/airflow3/scripts/export_jira_api_snapshot.py`,
it automatically imports the live helper from `/opt/airflow3/dags`:

```bash
umask 077
AIRFLOW_HOME=/opt/airflow3 \
  /opt/airflow3/venv/bin/python \
  /opt/airflow3/scripts/export_jira_api_snapshot.py \
  --project-key SCRUM \
  --cutoff-at 2026-08-09T11:05:50Z \
  --output-dir /srv/quant-rag/private/raw/jira-api-20260810-run1
```

The timestamp above is the current draft E2 cutoff. Replace it if the protocol
approves a different cutoff. The exporter intentionally downloads the full
visible project and records the cutoff without destructively removing later
events. The normalizer decides which individual events are eligible.

For a formal snapshot, do not use `--skip-changelog`,
`--skip-attachment-metadata`, or `--skip-end-verification`. Those switches are
only for a development smoke test.

If there is no shell access to the Airflow host, ask the Airflow operator to:

1. install this one script in `/opt/airflow3/scripts` without changing a live
   poller or DAG;
2. run the command as the Airflow service user;
3. retain the raw directory in restricted storage;
4. provide the student only with `audit.json`, `manifest.json`, and a secure
   transfer of the private snapshot for local normalization.

Do not ask the operator to reveal the Connection password.

## Completion checks

Open `audit.json` and confirm:

- `status` is `complete`;
- `issue_count` is plausible compared with the CSV snapshot;
- comment, changelog, and attachment counts are non-negative and plausible;
- `comment_concurrent_update_tickets` is empty or documented;
- `issue_end_verification_enabled` is `true`;
- `server_timezone` and `exporter_timezone` are present or their absence is
  documented;
- `safe_for_direct_rag_ingestion` is `false` (normalization is still required).

The command fails closed and does not create the final directory when Jira
repeats a search token, omits a required next token, returns duplicate issues,
changes issue IDs/updated timestamps during export, exhausts retries, or returns
an inconsistent comment page. HTTP 429 is retried using `Retry-After` with
bounded jitter.

For the formal freeze, run the export twice after a quiet period and compare
issue/comment IDs and content hashes. Different extraction timestamps make the
whole manifests different, so compare the record file hashes and investigate
any changed records.
