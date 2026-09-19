"""Agent-driven backtest over MCP (ported from the mcp-new branch, RAE-11).

Mirrors ``pipeline_mcp.py``, but points the agent at the BACKTEST MCP server
(``mcp/server.py``) instead of the GitHub one. Where ``mcp_client.run_backtest_via_mcp``
hard-codes the submit -> status -> results sequence, here the LLM is given the
backtest tools and decides to call them itself. ``run.py`` selects between the two
drivers with the ``USE_AGENT_BACKTEST`` flag (default: the scripted client), so the
deterministic path is never removed.

Mock vs. real engine is orthogonal to this driver and is governed by
``USE_REAL_BACKTESTER`` inside ``submit_backtest`` (default: mock). This module does
NOT force mock mode; it only supplies the mock runtime dirs as *defaults*, so the
same env that switches the scripted client to the real watched dir switches this one
too.
"""

import asyncio
import json
import os

from dotenv import load_dotenv
from agents import Agent, Runner, set_tracing_disabled
from agents.mcp import MCPServerStdio
from provider_config import (
    ProviderConfig,
    build_agents_model,
    build_agents_model_settings,
    resolve_provider_config,
)

# mcp_client (sandbox/) is on sys.path via run.py. Reuse its per-iteration job
# naming and deterministic postprocessing so the agent-driven result matches the
# scripted contract and drives the iterate loop.
from mcp_client import job_name_for, postprocess_via_mcp_async, resolve_backtest_baseline

load_dotenv()
set_tracing_disabled(disabled=True)

# The backtest MCP server (mcp/server.py). Same default as mcp_client.
BACKTEST_MCP_SERVER_PATH = os.getenv("MCP_SERVER_PATH", "/app/mcp/server.py")


def _server_env(baseline: str | None = None) -> dict:
    """Env for the spawned MCP server. Pass the whole environment through — so
    USE_REAL_BACKTESTER / SIMULATION_REQUESTS_DIR / MASTER_INDICES_CONF_DIR reach
    the tools — but default the mock runtime dirs when unset, so the offline loop
    keeps working without the real cluster (same defaults as mcp_client).

    When *baseline* is given (the strategy .request staged from the ticket's quant
    branch), expose it as BACKTEST_BASELINE so the agent's plain submit_backtest
    call runs the committed parameters without needing to know the staged path."""
    env = dict(os.environ)
    env.setdefault("MOCK_RUNTIME_DIR", "/tmp/mock_runtime")
    env.setdefault("BACKTEST_REQUESTS_DIR", "/tmp/mock_runtime/backtest-requests")
    if baseline:
        env["BACKTEST_BASELINE"] = baseline
    return env


def _parse_params(raw) -> dict:
    """Parse IW's Jira `params` raw string into a dict of engine overrides.

    IW stores the `/quant params:` option verbatim, e.g.
    "NFREQ=4, NPORT=50, VALUE_ACTIVE_RULES=4,5". Pairs are comma-separated; a comma
    inside a value (like active-rule lists) is re-joined onto the previous key.
    Keys/values are passed through untouched — submit_backtest handles coercion and
    warns on keys absent from the baseline. Returns {} when there's no params block.
    """
    if not raw:
        return {}
    out: dict = {}
    last = None
    for token in str(raw).split(","):
        token = token.strip()
        if "=" in token:
            key, value = token.split("=", 1)
            key = key.strip()
            if key:
                out[key] = value.strip()
                last = key
        elif last is not None and token:
            out[last] = f"{out[last]},{token}"  # comma that belonged to a value
    return out


def _window_clause(start_date: str | None, end_date: str | None) -> str:
    """How the agent should set the backtest window in submit_backtest. Only pass
    dates the request actually specified; otherwise keep the strategy .request's
    own (long) window — a short window fails the engine's warmup guard."""
    if start_date and end_date:
        return f' start_date "{start_date}", end_date "{end_date}",'
    if start_date:
        return f' start_date "{start_date}" (leave end_date to the strategy default),'
    if end_date:
        return f' end_date "{end_date}" (leave start_date to the strategy default),'
    return " (do not set start_date or end_date; use the strategy's own backtest window),"


def _build_prompt(job_name: str, start_date: str | None, end_date: str | None,
                  requested_data: str | None, params_clause: str = "") -> str:
    if requested_data:
        data_step = (
            f'1. The user requested data for "{requested_data}". Call list_master_indices. '
            'If one of the returned pools\' "version" values or "longStockDefs" universe '
            f'files matches "{requested_data}", use that version as master_indices and that '
            'universe file as universe. If nothing matches, clearly state that no data pool '
            f'matching "{requested_data}" was found, then use the latest pool '
            '(list_master_indices with latest_only=true) instead.'
        )
    else:
        data_step = (
            '1. Call list_master_indices with latest_only=true. From the response, take '
            'pools[0]["version"] as the master index version and the first entry of '
            'pools[0]["longStockDefs"] as the universe file. If the call fails or returns '
            'no pools, continue without these values.'
        )

    return f"""You are a quantitative backtesting agent. You have tools over MCP to
discover the current data pool and submit a backtest. Your job is to choose the
right data and submit the run; the system then waits for the (~10-minute) run to
finish and reads the results — you do NOT need to poll for completion or fetch the
final metrics yourself.

Work through these steps with your tools:
{data_step}
2. Call submit_backtest with job_name "{job_name}",{_window_clause(start_date, end_date)}
   plus master_indices and universe from step 1 if available.{params_clause} Use
   exactly the job_name "{job_name}"; do not change or invent a different name. Note
   the submitted_at value it returns.
3. You may call get_backtest_status ONCE to confirm the run was accepted, but do
   not loop waiting for it to complete - that is handled after you finish.
4. Report which data pool / universe you selected and confirm the submission. If any
   tool response contains a non-empty "warnings" list (for example an unknown data
   version, a missing universe file, or a mistyped parameter), include every warning
   verbatim under a "Warnings" heading, so the user always sees them.

Do not invent numbers or results - the performance metrics are computed by the
system from the completed run, not by you."""


async def _run_backtest_mcp_async(
    payload: dict,
    tracer=None,
    provider_config: ProviderConfig | None = None,
) -> dict:
    issue_key = payload.get("issue_key", "POC")
    args = payload.get("args", {})
    # Same per-iteration-unique name mcp_client submits/postprocesses under, so the
    # agent's submit and our deterministic scoring line up on one job dir.
    job_name = job_name_for(payload)
    # Only honour an explicitly requested window; otherwise the strategy .request's
    # own long window stands (a short default would fail submit_backtest's guard).
    start_date = args.get("start")
    end_date = args.get("end")
    # Optional data selection from the Jira request. The /quant command carries the
    # user's requested universe/asset as `stock_type` (alias `ticker:`), which reaches
    # us in args via the runtime request. It is free text, so the agent matches it
    # against what list_master_indices actually offers rather than assuming a version id.
    requested_data = args.get("stock_type") or args.get("universe")

    # Engine overrides from the Jira `params:` block (e.g. NFREQ, NPORT, SLIPPAGE).
    # IW passes them as one raw string; parse to a dict and instruct the agent to
    # hand it to submit_backtest as extra_params (permissive passthrough; unknown
    # keys warn). No-op when there's no params block.
    extra_params = _parse_params(args.get("params"))
    params_clause = (
        f' Also pass extra_params={json.dumps(extra_params)} to submit_backtest '
        '(engine settings the user asked for; pass them exactly as given).'
        if extra_params else ''
    )

    prompt = _build_prompt(job_name, start_date, end_date, requested_data, params_clause)

    # A selected Jira attachment is authoritative; otherwise use the parameters
    # committed to the ticket's quant branch (edit -> engine).
    baseline = resolve_backtest_baseline(payload)

    config = provider_config or resolve_provider_config()
    async with MCPServerStdio(
        name="Backtest MCP",
        params={
            "command": "python",
            "args": [BACKTEST_MCP_SERVER_PATH, "--serve"],
            "env": _server_env(baseline),
        },
        client_session_timeout_seconds=60,
    ) as backtest_server:
        agent = Agent(
            name="Backtest Agent",
            instructions="You are a backtesting agent with access to backtest tools over MCP.",
            model=build_agents_model(config),
            model_settings=build_agents_model_settings(config),
            mcp_servers=[backtest_server],
        )

        result = await Runner.run(agent, prompt, max_turns=12)

    # The agent chose the data pool and submitted under job_name; its final_output
    # is the human-readable report (data pool chosen + any warnings). Now wait for
    # the run to finish and score it deterministically so we return the structured
    # contract (metrics + evaluation + recommended_action) the iterate loop and
    # result_builder consume — identical to the scripted driver. We don't rely on the
    # LLM to wait out the ~10-min real run.
    contract = await postprocess_via_mcp_async(payload, job_name)

    if tracer:
        tracer.record("backtest_agent", "run_backtest_mcp", "succeeded",
                      "backtest submitted, polled, and fetched via MCP (agent-driven)")

    return {
        **contract,
        "status": "succeeded",
        "issue_key": issue_key,
        "job_name": job_name,
        # Keep the agent's own narrative (data-pool choice, warnings) alongside the
        # scored contract; build_response reads the structured fields, not this.
        "summary": result.final_output or contract.get("summary"),
        "agent_summary": result.final_output,
    }


def run_backtest_mcp(
    payload: dict,
    tracer=None,
    provider_config: ProviderConfig | None = None,
) -> dict:
    """Sync wrapper, same pattern as run_backtest_via_mcp / run_pipeline_mcp."""
    return asyncio.run(
        _run_backtest_mcp_async(
            payload,
            tracer=tracer,
            provider_config=provider_config,
        )
    )


if __name__ == "__main__":
    import json

    test_payload = {
        "run_id": "ALPHA-101-20001",
        "issue_key": "ALPHA-101",
        "command": "backtest",
        "args": {"start": "2020-01-01", "end": "2020-12-31"},
    }
    print(json.dumps(run_backtest_mcp(test_payload), indent=2))
