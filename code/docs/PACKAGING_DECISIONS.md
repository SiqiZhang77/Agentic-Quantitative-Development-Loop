# Packaging decisions

Prepared on 8 September 2026 for the author's company handover.

## Why six source archives

The reported study is not bound to one current worktree. Four runtime commits support different included cohorts, and two separately frozen control commits supply important agreement/launch logic. Each archive is independently exported from Git objects at its stated SHA. The latest working-directory contents were not used as a substitute for those commits.

The main reading path is `e4v3_runtime`. Earlier T1/T2 snapshots are preserved so the source relevant to those results can be inspected without reconstructing later changes. E4V3 launch controls remain separate because their Git identity differs from that of the runtime image. The archive structure preserves all original import paths when extracted and makes a small number of files available for browser upload.

## Included and preserved

- Runtime/proxy code, repository tool contracts, role coordination, quantitative calculator, Jira/Airflow code, corpus construction tools, tests and synthetic fixtures.
- Original dependency files, container definitions, schemas, public task instructions, synthetic task inputs and public rubrics.
- Versioned experiment scripts and protocols. Historical helper directories remain because later controls reuse them; consult the top-level code README for the versions actually reported.
- Original Markdown architecture, deployment and operating documentation.
- Selected analytical rows, retrospective relevance labels and de-identified record summaries, plus a new portable descriptive recalculation script.

The original source files are byte-preserved, including historical terminology and machine-specific configuration examples. The new README explains differences from final dissertation terminology instead of silently rewriting a historical implementation. Example settings require review before use in a different deployment; they are not a configured deployment for the recipient.

## Excluded and why

| Material | Reason |
|---|---|
| `.git` metadata and complete repository history | Preserve explicit source snapshots without transferring unrelated branch history or local remotes |
| Uncommitted modifications, alternate worktrees and superseded local trial outputs | They do not identify the code used for a reported frozen cohort |
| Populated local environment files, local credentials and virtual environments | Configuration belongs to the recipient's environment; installed dependencies are recreated from declared requirements |
| Private evaluator/answer assets, original company Jira records and resolved runtime requests | These remain separately controlled evidence; they are unnecessary for source inspection and the supplied offline descriptive checks |
| Full raw provider prompts, raw observation traces and private condition-join mappings | The package provides selected analytical columns and provenance, rather than redistributing complete private runtime evidence |
| Three `tmp/pdfs/` PNGs per source snapshot | Temporary documentation previews |
| Three duplicate documentation PDFs per source snapshot | The corresponding original Markdown sources remain |
| Thesis drafts, previous methodology variants and presentation drafts | They are separate document deliverables; the code handover does not select a final thesis or presentation |

`SOURCE_MANIFEST.json` enumerates each omitted tracked file and each retained file's hash. No exclusions modify the original Git objects or the existing evidence directories.

## Analysis provenance

The T3 records are restricted to E4V3: 60 observations and 1,500 required run-item slots. T1/T2 use the confirmed condition-joined 32-observation dataset, not the older pending-score export. Public-facing run identifiers, task conditions, scores and required denominator fields are retained; machine-local raw-source paths and private joining identifiers are omitted from the tables.

The added analysis script recomputes descriptive summaries from those tables. It is a handover convenience script, not a claim that it was the original experiment controller or private scorer. It does not claim to reproduce inferential intervals or to revalidate raw model-prompt delivery. Upstream source-file hashes and the selection logic are recorded in `analysis/DATA_PROVENANCE.json`.

## Next steps for the recipient

1. Verify the archives and inspect the appropriate extracted snapshot.
2. Run the supplied descriptive analysis and optional offline tests.
3. For operational deployment, read the original configuration documents and obtain the required company access.
4. Any new formal model collection requires its own approved setup and new observation identities. This package does not perform that collection.
