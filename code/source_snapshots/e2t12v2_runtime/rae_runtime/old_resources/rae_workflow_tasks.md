# RAE - Outline, Workflow & Tasks

This covers our segments of the pipeline: from the moment Airflow launches our
container, through generation -> backtest -> results, to handing a structured result
back to the ADF Formatter.

The other team owns everything outside our boundary:
- **Inbound:** Jira -> Webhook Listener -> ADF Parser -> Command Router -> Airflow Workflow Trigger
- **Outbound:** ADF Formatter -> Jira API Client -> Jira

Our boundary is the container launch (in) and the structured results JSON (out).

> **PoC goal:** prove a data payload can travel from a Jira ticket, through the pipeline, execute, and return a result. Single pass, mock where needed, no refinement loop, no push-back to GitHub. Everything marked **[FULL]** is deferred to later sprints; everything marked **[PoC]** is the target for the first two weeks.

---

## 1. How the container launch works (the inbound seam)

**This section needs to be confirmed with the other team**

Airflow's DockerOperator launches our container automatically as a DAG task. It:
- runs our image
- injects env vars + mounts
- applies run config (memory/pids limits, network rules, read-only rootfs),
- runs our entrypoint, waits for exit, captures **stdout + exit code**,
- removes the container.


---

## 2. RAE Internal Workflow

```
------------------------------------------------------------------------
| [INBOUND SEAM]  Airflow DockerOperator launches container            |
|   env:    TICKET_ID, BRANCH_REF, FEATURE_PROMPT, RUN_ID,             |
|           PROXY_BASE_URL, PROXY_VIRTUAL_KEY, MODEL_NAME,             |
|           MCP_ENDPOINT                                               |
|   mounts: /workspace (strategy code, read-write),                    |
|           /out (results output, read-write)                          |
|   runcfg: --memory, --pids-limit, --read-only + tmpfs,               |
|           --cap-drop ALL, --network <allowlist>                      |
------------------------------------------------------------------------
                                  |
                                  V
========================= HARDENED CONTAINER ===========================
||                                                                    ||
||  (1) SANDBOX ENTRYPOINT .................................. [ST-1]  ||
||      - read+validate env vars                                      ||
||      - prepare /workspace for strategy code                        ||
||      - run as non-root                                             ||
||      - possibly configure GitHub access from the container         ||
||                                |                                   ||
||                                V                                   ||
||  (2) INITIAL PROMPT BUILD: ............................... [ST-2]  ||
||      - fetch strategy code (from GitHub or have the orchestration  ||
||        team mount the code into the container directly)            ||
||      - build prompt from the feature request in the Jira ticket    ||
||        (in env vars) + strategy code                               ||
||                                |                                   ||
||                                V                                   ||
||     **Begin Strategy Modification Loop (SINGLE PASS FOR PoC)**     ||
||  (3) AGENT EXECUTION / GENERATION ........................ [ST-2]  ||
||      - handle AI gateway                                           ||
||      - call LiteLLM proxy with prompt                              ||
||      - returns modified strategy code                              ||
||                                |                                   ||
||                                V                                   ||
||  (4) STORE MODIFIED CODE ................................. [ST-1]  ||
||      - write the returned code into /workspace                     ||
||                                |                                   ||
||                                V                                   ||
||  (5) BACKTEST .............................................[ST-3]  ||
||      - call MCP server, trigger backtest                           ||
||      - polls telemetry until done                                  ||
||      - returns backtest results                                    ||
||      - for PoC: go straight to 6                                   ||
||      - for full system: loop 3->4->5 until criteria / budget       ||
||               **End Strategy Modification Loop**                   ||
||                                |                                   ||
||                                V                                   ||
||  (6) RESULTS COLLECTION & REPORT ......................... [ST-3]  ||
||      - parse telemetry -> metrics                                  ||
||      - assemble structured results object                          ||
||                                |                                   ||
||                                V                                   ||
||  (7) EMIT & EXIT ......................................... [ST-1]  ||
||      - hand over results (e.g. JSON, mechanism TBC)                ||
========================================================================
                                  |
                                  V
------------------------------------------------------------------------
| [OUTBOUND SEAM]  Airflow captures stdout (XCom) / reads /out;        |
|   hands results JSON -> ADF Formatter -> Jira API                    |
|   Client -> posted back to the Jira ticket.                          |
------------------------------------------------------------------------
```

---

## Task Breakdown

### Sub-Team 1 - Runtime & Sandbox Security
**[PoC]**
- [ ]
- [ ]

**[FULL]**
- [ ]
- [ ]

### Sub-Team 2 - AI Proxy & Gateway Layer
**[PoC]**
- [x] Set up `llm_client.py` — connects to LiteLLM proxy, sends prompt, returns response
- [ ] Update `llm_client.py` to read `PROXY_BASE_URL`, `PROXY_VIRTUAL_KEY`, `MODEL_NAME` from env vars injected by Airflow (not `.env` file)
- [ ] Set up `pipeline.py` — reads env vars (`FEATURE_PROMPT`, `TICKET_ID`, `BRANCH_REF`), builds prompt from strategy code + feature request, calls LiteLLM, writes modified code to `/workspace`
- [ ] Set up `github_client.py` — fetch strategy code from GitHub, create feature branch, push modified code back

**[FULL]**
- [ ] Budget fallback logic — if primary model quota exceeded, automatically fall back to cheaper model
- [ ] Multi-turn refinement loop — pass backtest results back to Claude for a second generation pass
- [ ] Prompt versioning — store and manage prompt templates rather than hardcoding them

### Sub-Team 3 - Tooling & Backtester Integration Layer
**[PoC]**
- [ ]
- [ ]

**[FULL]**
- [ ]
- [ ]
