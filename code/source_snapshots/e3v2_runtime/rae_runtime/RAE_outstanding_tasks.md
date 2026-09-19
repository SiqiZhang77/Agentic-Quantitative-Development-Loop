# RAE - Outstanding Tasks (Weeks 3–6)

> **What this is.** The full set of outstanding tickets for our team (RAE) from now to the end of the project, with priority, sprint, and enough detail per ticket to be picked up and finished in a single session/sprint. Each ticket cross-references the Team IW ticket(s) it integrates with, so the seams line up.

---

## Read me first - conventions & key decisions

**Teams.** *RAE* (us) owns everything **inside the container**: the sandbox/runtime boundary (`run.py`), the GitHub + LLM proxy, the MCP backtester, result shaping, and security hardening. *IW* owns **orchestration**: Jira polling, the Airflow DAG that launches our container, and posting results back to Jira. The two teams meet at two seams - the **request payload in** and the **result/artefacts out**.

**Sprint mapping.** Weeks map to IW's sprint codes so both boards align:

| Our week | IW sprint code | Focus |
|---|---|---|
| Week 3 | `3`  | Unblock deployment; lock the request/response seams; GitHub over MCP |
| Week 4 | `4` | Real backtester, reports, guardrails, MCP-driven triggering |
| Week 5 | `5` | The loop, multi-repo, parallel-safety, timeouts/cancellation, refinements |
| Week 6 | `6` | Multi-agent, MCP narrowing, dynamic params, final integration |

> *Note:* The CSV file for importing issues into Jira uses different codes for the sprints: sprint 3 -> 1, sprint 4 -> 35, sprint 5 -> 36, sprint 6 -> 37.

**Priority.** Every ticket is **High** or **Medium**. High = on the critical path / blocks integration or other tickets. Medium = needed for full requirements but can slip within its sprint without breaking the chain.

> *Timing flag:* IW schedules multi-repository support in **week 4** (sprint 35); our container-side multi-repo work (RAE-20/21) lands in **week 5** because it depends on GitHub-over-MCP and the request payload being in place. IW can build their routing side ahead; integrate in week 5.

---

# Week 3 - Sprint `3`

## RAE-01 - Deploy the hardened container to the shared cluster (GHCR pull) - **High**
- **Description:** The image is private on GHCR and the cluster can't pull it, so IW's DockerOperator still runs a mock. Provide a least-privilege `read:packages` credential and verify a real pull + run. The gate between "works on our machines" and "runs live".
- **Sub-tasks:** create a scoped, short-lived `read:packages` token/deploy key; with cluster admin (Amol) configure the image-pull secret; verify a manual pull then one end-to-end hardened-flag run; confirm with IW the DockerOperator pulls the real image.
- **Dependencies:** cluster admin access (Amol); IW DockerOperator wiring.
- **Risks:** token scope too broad -> least privilege + expiry; pull-secret leak -> store as a cluster/Airflow secret.
- **Aligns with IW:** DockerOperator real-container swap.

## RAE-02 - Switch container input to a JSON request payload over stdin - **High**
- **Description:** Replace env-var input with a single JSON request document written to the container's **stdin** (a stream); `run.py` reads stdin to EOF and parses it. (This is the "stream via terminal" item clarified: env vars are size-limited and leak via `/proc`.) Secrets (`GITHUB_TOKEN`/`API_KEY`) stay injected via env/Airflow Connections, not in the payload.
- **Sub-tasks:** implement stdin read + parse + schema validation in `run.py` with a `schema_version`; keep the env-var path as a flag-gated fallback; reject malformed/truncated payloads with a clean `failed` result; document the schema.
- **Dependencies:** IW "Define Runtime Request Payload Contract" (joint schema).
- **Risks:** schema drift -> version + validate; truncated stdin -> read to EOF before acting.
- **Aligns with IW:** Define Runtime Request Payload Contract; Jira Command Validation Layer.

## RAE-03 - Define & implement the result payload contract - **High**
- **Description:** Agree and implement the result JSON schema with IW - a single normalised object (same shape on success or failure): status, metrics, artefact locations, error fields, `schema_version`. (Writing it to the mounted FS is RAE-08.)
- **Sub-tasks:** finalise the schema with IW; implement result-shaping in `run.py`; keep the stdout last-line for XCom; create a shared fixture both teams test against.
- **Dependencies:** IW "Define Runtime Response Payload Contract".
- **Risks:** what RAE emits ≠ what IW reads -> joint schema + shared fixture.
- **Aligns with IW:** Define Runtime Response Payload Contract.

## RAE-04 - Expose GitHub read & branch operations as MCP tools - **High**
- **Description:** Move repo-read and branch lifecycle (create/reuse `quant/<ticket>`) out of hardcoded scripts and into MCP tools the LLM calls, backed by PyGithub. First half of the GitHub-over-MCP migration.
- **Sub-tasks:** scaffold the GitHub MCP tool server; implement read-file / list and branch create-or-reuse tools; expose to the LLM; integration-test against a sandbox repo (done).
- **Dependencies:** existing MCP client/server (done); refactor proxy `github_client`.
- **Risks:** unintended ops -> covered by guardrails (RAE-13); rate limits -> least-priv token.
- **Aligns with IW:** GitHub Operation Approval Framework.

## RAE-05 - Expose GitHub commit & push as MCP tools + retire scripted path - **High**
- **Description:** Second half of the migration: commit and push as MCP tools, then switch the pipeline to the MCP path and remove the old scripted GitHub flow once at parity.
- **Sub-tasks:** implement commit + push tools; cut the pipeline over to the MCP tools; remove/disable the scripted path; verify end-to-end on a sandbox repo.
- **Dependencies:** RAE-04.
- **Risks:** parity gaps cause regressions -> keep scripted path behind a flag until the MCP path is verified.
- **Aligns with IW:** GitHub Operation Approval Framework.

## RAE-06 - Reproducibility: pin `fastmcp` and lock dependencies - **Medium**
- **Description:** Pin floating deps (notably `fastmcp`) for reproducible builds before parallel work scales.
- **Sub-tasks:** pin `fastmcp` + transitive deps; add a lockfile; rebuild + verify a hardened run; document.
- **Dependencies:** none.
- **Risks:** unpinned deps -> non-reproducible builds.
- **Aligns with IW:** supports stable image pulls (RAE-01).

## RAE-07 - Emit structured stage/progress events for IW reporting - **Medium**
- **Description:** Emit structured progress (stage, status, timestamps) so IW's structured Jira reporting can show execution stage/progress, not just a final blob.
- **Sub-tasks:** agree a small progress-event schema with IW; emit events at each stage (fetch/modify/commit/backtest/report); write to the agreed channel; document.
- **Dependencies:** IW "Structured Jira Workflow Reporting"; RAE-03 (result shape).
- **Risks:** log noise -> keep events structured + minimal.
- **Aligns with IW:** Structured Jira Workflow Reporting; Iteration Reporting to Jira.

---

# Week 4 - Sprint `4`

## RAE-08 - Write results & artefacts to the mounted filesystem - **High**
- **Description:** Implement the output side of the seam: write the result JSON and artefacts to the agreed mounted output directory (not just stdout) so IW can ingest them. Stdout last-line stays in parallel during transition.
- **Sub-tasks:** write result JSON + artefacts to the mounted dir; define artefact path / naming conventions; verify IW can read them against the shared fixture.
- **Dependencies:** RAE-03 (contract); IW "Mounted Filesystem Artefact Ingestion".
- **Risks:** path/format mismatch -> shared fixture + joint test.
- **Aligns with IW:** Mounted Filesystem Artefact Ingestion; Runtime Artefact Discovery Service.

## RAE-09 - Invoke the real backtest engine via MCP (replace the mock) - **High**
- **Description:** Wire the MCP backtester's tools (`submit`/`status`/`logs`) to the firm's real backtesting engine, keeping the same tool surface so nothing upstream changes. Keep the mock behind a flag for off-network/local testing.
- **Sub-tasks:** obtain access/credentials (raise early); map `submit_backtest` onto the  real engine job; map `status`/`logs`; flag-gate the mock.
- **Dependencies:** access to the firm's backtester; ST-3 MCP owners.
- **Risks:** engine API differs from assumptions -> adapter layer; access delays -> raise now.
- **Aligns with IW:** Integrate Real Backtester Workflow.

## RAE-10 - Normalise backtester output + async status polling - **High**
- **Description:** Translate the real engine's output into the result-contract metrics shape, and handle long-running/async runs via status polling rather than blocking.
- **Sub-tasks:** poll `status` to completion with backoff; adapt engine output -> metrics shape; contract-test the adapter against sample engine outputs.
- **Dependencies:** RAE-09; RAE-03 (metrics shape).
- **Risks:** slow/async runs -> polling + timeouts (RAE-22); output shape drift -> adapter + tests.
- **Aligns with IW:** Integrate Real Backtester Workflow.

## RAE-11 - Trigger the backtest via the LLM over MCP - **High**
- **Description:** Stop hardcoding the backtest call in `run.py`; let the LLM call `submit_backtest` over MCP as a step, so the flow is agent-driven and reusable.
- **Sub-tasks:** expose backtest tools to the LLM; scaffold so the agent calls submit -> status -> logs; keep a deterministic fallback; verify end-to-end.
- **Dependencies:** RAE-04/05 (MCP tool pattern); RAE-09.
- **Risks:** agent skips/duplicates the backtest -> constrain via tool availability + validate the result before reporting.
- **Aligns with IW:** Implement MCP-Based Backtest Triggering.

## RAE-12 - Integrate the firm's report scripts (one report type) - **High**
- **Description:** Produce a report artefact (not only metrics) using the firm's existing report-generating scripts - one report type working end-to-end, written to the mounted output dir and referenced in the result `artifacts`.
- **Sub-tasks:** obtain access to the report scripts; wire one report type into the post-backtest step; write it to the mounted dir; reference it in the result.
- **Dependencies:** access to report scripts; RAE-08 (mounted output); ideally RAE-10.
- **Risks:** scripts assume an env the container lacks -> containerise deps / call a report service.
- **Aligns with IW:** Runtime Artefact Discovery Service; Mounted Filesystem Artefact Ingestion.

## RAE-13 - GitHub operation guardrails - **High**
- **Description:** Safety rails for LLM-driven Git, enforced at the **tool layer**: quant branches only; block force-push/deletes/protected-branch writes; cap branches and commits per run.
- **Sub-tasks:** allowlist of permitted ops; branch-name policy (`quant/<ticket>`); per-run caps; rely on repo branch-protection for `main`; reject disallowed ops cleanly.
- **Dependencies:** RAE-04/05; align the allowlist with IW's approval framework.
- **Risks:** LLM touches/deletes important code -> tool-layer enforcement; runaway branches -> per-run caps.
- **Aligns with IW:** GitHub Operation Approval Framework.

## RAE-14 - LiteLLM budget guardrails (caps + enforcement + usage capture) - **High**
- **Description:** Define and enforce budget guardrails on the LiteLLM proxy (per-run token/cost caps; soft vs hard limits), fail cleanly on a hard cap, and record usage/cost per run for later reporting.
- **Sub-tasks:** decide soft vs hard caps and scope; implement enforcement; fail cleanly on a hard cap; record per-run usage/cost (consumed by RAE-32).
- **Dependencies:** ST-2 proxy owners; UCL-network-only endpoint (mock off-network).
- **Risks:** runaway cost / loop spend -> hard cap + loop bounds (RAE-18).
- **Aligns with IW:** Workflow-Level Budget Tracking Integration.

## RAE-15 - Durable artefact storage (replace local-path placeholders) - **Medium**
- **Description:** `equity_curve` and reports are local-path placeholders that don't  survive the container. Write artefacts to durable storage (mounted volume IW collects, or an object store) with stable references in the result.
- **Sub-tasks:** choose storage with IW; write artefacts there; put durable URIs in `artifacts`; clean up scratch.
- **Dependencies:** RAE-08; IW artefact ingestion/discovery.
- **Risks:** artefacts lost on exit -> write before exit; verify IW can read.
- **Aligns with IW:** Mounted Filesystem Artefact Ingestion; Runtime Artefact Discovery Service.

## RAE-16 - Container-side retry idempotency (safe re-runs) - **Medium**
- **Description:** IW adds retry/recovery, so the container must be safe to re-run for the same ticket: reuse the branch, no duplicate commits, no double-submitted backtests.
- **Sub-tasks:** keep branch create/reuse idempotent (largely done); no-op commit when unchanged; idempotent backtest submission or in-flight detection; verify a re-run is consistent.
- **Dependencies:** IW "Workflow Retry and Recovery Logic".
- **Risks:** duplicate branches/commits/backtests -> idempotency keys per ticket + attempt.
- **Aligns with IW:** Workflow Retry and Recovery Logic.

---

# Week 5 - Sprint `5`

## RAE-17 - Result evaluation step (score metrics against the request's criteria) - **High**
- **Description:** Add a step that evaluates the backtest result against the request's criteria and emits a machine-readable **evaluation** in the result (met criteria? recommended next action? confidence). Useful regardless of how loop ownership resolves.
- **Sub-tasks:** parse target criteria from the request; score the result; add `evaluation` + `recommended_action` fields to the result contract; document.
- **Dependencies:** RAE-03 (contract); RAE-10 (real metrics).
- **Risks:** ambiguous criteria -> define a clear evaluation schema + defaults.
- **Aligns with IW:** Iteration Stop Conditions; Iteration Reporting to Jira.

## RAE-18 - Iteration mechanics + bounds (the loop) - **High**
- **Description:** The edit -> backtest -> evaluate -> repeat mechanics loop is owned by RAE. Define a max-iteration bound and make each iteration reportable.
- **Sub-tasks:** implement the agreed mechanics; enforce a hard max-iteration bound; ensure each iteration is reportable (RAE-07).
- **Dependencies:** RAE-17; RAE-16; IW loop tickets
- **Risks:** loop never terminates -> max-iteration + budget cap (RAE-14) + timeout (RAE-22); double-owned logic -> resolved ownership (RAE) first.
- **Aligns with IW:** Agent Iteration Loop Controller; Iteration Stop Conditions; Iteration State Persistence.

## RAE-19 - Parallel-safety / statelessness of the container - **High**
- **Description:** IW runs many containers concurrently; the container must be parallel-safe - no shared mutable state, unique branch/workspace per ticket, no host-path collisions, bounded resources per instance.
- **Sub-tasks:** audit `run.py` for shared/global state; ensure per-run scratch + unique branch (largely done); per-container MCP server spawn; verify N concurrent runs don't collide; confirm limits hold under concurrency.
- **Dependencies:** IW "Parallel Docker Orchestration", "Concurrent Workflow Execution", "Dynamic Airflow Task Mapping".
- **Risks:** cross-run interference -> per-ticket isolation; node exhaustion -> per-container limits.
- **Aligns with IW:** Parallel Docker Orchestration; Concurrent Workflow Execution Support; Dynamic Airflow Task Mapping.

## RAE-20 - Repo-parameterise the request schema + GitHub MCP tools - **Medium**
- **Description:** Multi-repo plumbing: extend the request schema (with IW) to carry one-or-many repositories and make the GitHub MCP tools repo-parameterised.
- **Sub-tasks:** document current single-repo assumptions; extend the request schema; parameterise the GitHub tools by repo; per-repo token scope.
- **Dependencies:** RAE-02, RAE-04/05.
- **Risks:** per-repo auth/scope -> validate access before acting.
- **Aligns with IW:** Add Multi-Repository Workflow Support; Repository Selection from Jira Commands.

## RAE-21 - Agent repository selection/routing + example scenarios - **Medium**
- **Description:** Let the agent select the relevant repository for a task, and provide worked single- and multi-repo request scenarios.
- **Sub-tasks:** implement repo-selection/routing logic; validate the chosen repo; write example scenarios covering single- and multi-repo workflows.
- **Dependencies:** RAE-20.
- **Risks:** agent picks the wrong repo -> explicit routing + validation.
- **Aligns with IW:** Repository Selection from Jira Commands.

## RAE-22 - Honour timeouts + graceful cancellation (container side) - **Medium**
- **Description:** Honour a max-runtime and handle `SIGTERM` gracefully - clean up the branch/scratch, release resources, emit a clean terminal (`failed`/`cancelled`) result.
- **Sub-tasks:** accept a timeout in the payload and self-enforce; trap `SIGTERM` -> graceful shutdown + cleanup; emit a clean terminal result; verify mid-backtest cancel is safe.
- **Dependencies:** IW "Workflow Timeout Controls" + "Workflow Cancellation from Jira".
- **Risks:** orphaned subprocess/backtest on kill -> child cleanup; partial commits on cancel -> only push complete units.
- **Aligns with IW:** Workflow Timeout Controls; Workflow Cancellation from Jira.

## RAE-23 - Define & narrow the MCP tool surface - **Medium**
- **Description:** Decide exactly which functions/tools are exposed to the LLM over MCP (GitHub ops, backtest, reports, repo/index discovery), with least-privilege defaults.
- **Sub-tasks:** inventory candidate tools; classify allowed/blocked by default; document each tool's contract; remove/disable anything unneeded.
- **Dependencies:** RAE-04/05, RAE-09/11 (tools exist by now).
- **Risks:** over-broad access -> least privilege; under-provisioned -> agent can't complete tasks.
- **Aligns with IW:** Agent Tool Permission Configuration; Runtime Capability Discovery Integration.

## RAE-24 - Support multiple/configurable report types - **Medium**
- **Description:** Generalise the report step (RAE-12) to produce multiple report types, selectable per request.
- **Sub-tasks:** parameterise the report step by type; support ≥2 types; reference all produced reports in the result `artifacts`.
- **Dependencies:** RAE-12.
- **Risks:** type sprawl -> start with the two highest-value types.
- **Aligns with IW:** Runtime Artefact Discovery Service.

## RAE-25 - LiteLLM model-selection / switching logic - **Medium**
- **Description:** Add logic to choose and switch models (e.g. cheaper model first, escalate on failure/complexity), within the budget guardrails.
- **Sub-tasks:** define the switching policy; implement model selection in the proxy; ensure switches respect the budget caps (RAE-14); log which model was used and why.
- **Dependencies:** RAE-14.
- **Risks:** escalation loops -> bound switches per run.
- **Aligns with IW:** Workflow-Level Budget Tracking Integration.

---

# Week 6 - Sprint `6`

## RAE-26 - Per-workflow MCP tool-permission enforcement - **Medium**
- **Description:** Let a workflow specify which tools an agent may use, enforced **server-side** at the MCP layer (never trust the prompt).
- **Sub-tasks:** accept a tool-permission set in the payload; enforce at the MCP layer; reject disallowed calls cleanly; contract-test with IW.
- **Dependencies:** RAE-23; IW "Agent Tool Permission Configuration".
- **Risks:** permission bypass -> enforce server-side.
- **Aligns with IW:** Agent Tool Permission Configuration.

## RAE-27 - MCP capability-discovery endpoint - **Medium**
- **Description:** Expose a "list capabilities/tools" call so IW can query available tools dynamically instead of hardcoding them.
- **Sub-tasks:** add a capability-discovery MCP call; return tool names + contracts; document; contract-test with IW.
- **Dependencies:** RAE-23.
- **Risks:** discovery drifts from reality -> generate the list from the live tool registry.
- **Aligns with IW:** Runtime Capability Discovery Integration.

## RAE-28 - Define agent roles + inter-agent handoff contract - **Medium**
- **Description:** Design step for multi-agent: define the initial specialised roles (e.g. planner/coder) and the message/handoff shape between them. Keeps the build small.
- **Sub-tasks:** define initial roles + responsibilities; define the inter-agent handoff schema; document; agree scope with IW.
- **Dependencies:** IW "Multi-Agent Workflow Coordination" + "Agent Role Routing Logic".
- **Risks:** scope creep -> start with two roles only.
- **Aligns with IW:** Multi-Agent Workflow Coordination; Agent Role Routing Logic.

## RAE-29 - Implement minimal multi-agent routing (flag-gated) - **Medium**
- **Description:** Implement role routing inside the container for the roles defined in RAE-28, behind a flag, with the single-agent path as the default/fallback.
- **Sub-tasks:** implement routing for the defined roles; flag-gate it; keep single-agent as default; test on one representative request.
- **Dependencies:** RAE-28; RAE-14 (more agents = more spend).
- **Risks:** complexity/cost blow-up late -> minimal + flagged; inter-agent loops -> bounds + budget cap.
- **Aligns with IW:** Multi-Agent Workflow Coordination; Agent Role Routing Logic.

## RAE-30 - Dynamic parameters from master indices - **Medium**
- **Description:** Let agents scan the firm's master index list and feed the most recent / user-specified values back into the request before execution.
- **Sub-tasks:** expose a tool/step to read the master index list; select most-recent or user-specified entries; inject into the request; validate + log what was injected.
- **Dependencies:** IW "Dynamic Parameter Injection Framework"; access to the index source.
- **Risks:** stale/wrong parameters -> validate + log injected values.
- **Aligns with IW:** Dynamic Parameter Injection Framework.

## RAE-31 - Configurable runtime resource controls - **Medium**
- **Description:** Let the payload specify resource limits (memory/CPU/PID) applied per run, instead of the current hardcoded limits.
- **Sub-tasks:** read resource constraints from the payload; apply at container/process level; sane defaults + hard ceilings; verify limits hold.
- **Dependencies:** IW "Runtime Resource Allocation Controls"; RAE-19.
- **Risks:** excessive requested resources -> enforce hard ceilings.
- **Aligns with IW:** Runtime Resource Allocation Controls.

## RAE-32 - Expose budget/usage to IW (budget tracking, part 2) - **Medium**
- **Description:** Surface per-run model usage/cost (from RAE-14) in the result/usage payload so IW can track budget at the workflow level.
- **Sub-tasks:** add a usage block to the result contract; populate it from the proxy's per-run records; document; contract-test with IW.
- **Dependencies:** RAE-14; IW "Workflow-Level Budget Tracking Integration".
- **Risks:** inaccurate accounting -> reconcile against proxy logs.
- **Aligns with IW:** Workflow-Level Budget Tracking Integration.

## RAE-33 - Joint end-to-end + concurrency/soak validation on the cluster - **High**
- **Description:** Full Jira -> ... -> Jira round-trip on the shared cluster with the real container, real backtester, reports and guardrails - then a concurrency/soak test under multiple simultaneous tickets. Fix integration gaps.
- **Sub-tasks:** joint end-to-end test with IW on the cluster; concurrency/soak test; triage + fix integration gaps; record results.
- **Dependencies:** most prior tickets; IW orchestration complete.
- **Risks:** gaps surface late -> keep a week-6 buffer.
- **Aligns with IW:** all integration tickets; final delivery.

## RAE-34 - Final security re-check + production runbook + sign-off - **High**
- **Description:** Re-verify the hardened container (non-root, read-only FS, no extra capabilities, no privilege escalation, memory/PID limits) end-to-end, finalise the runbook/README and the contracts, and produce a sign-off checklist.
- **Sub-tasks:** re-verify all hardened flags under the live flow; finalise runbook + contract docs; produce a sign-off checklist; hand off.
- **Dependencies:** RAE-33.
- **Risks:** security regressions slipped in during integration -> re-verify, don't assume.
- **Aligns with IW:** final delivery.

---

## Quick sprint summary

| Sprint | RAE tickets | High | Medium |
|---|---|---|---|
| Week 3 (`3`)  | RAE-01 ... RAE-07 | 01, 02, 03, 04, 05 | 06, 07 |
| Week 4 (`4`) | RAE-08 ... RAE-16 | 08, 09, 10, 11, 12, 13, 14 | 15, 16 |
| Week 5 (`5`) | RAE-17 ... RAE-25 | 17, 18, 19 | 20, 21, 22, 23, 24, 25 |
| Week 6 (`6`) | RAE-26 ... RAE-34 | 33, 34 | 26, 27, 28, 29, 30, 31, 32 |

34 tickets total (17 High / 17 Medium), ~7/9/9/9 across the weeks. Week 4 stays the heaviest as it holds the real-engine + reports + guardrails integration.
