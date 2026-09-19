"""MCP client: drives the backtester MCP server over stdio.

run.py calls run_backtest_via_mcp(payload). We spawn the server
(python mcp/server.py --serve), call submit -> status -> postprocessing as
MCP tools, and translate the result into our promised contract. run.py
never imports the MCP team's code - it only talks over the protocol.

The postprocessing step (run_backtest_postprocessing) chains
get_backtest_artifacts -> generate_equity_curve -> get_backtest_results ->
evaluate_result (RAE-17), so this is also where target_criteria from the
request's execution_objectives gets scored against the run's metrics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _make_transport() -> PythonStdioTransport:
    """Built per call so env (MCP_SERVER_PATH / MOCK_RUNTIME_DIR /
    BACKTEST_REQUESTS_DIR) is read at call time, not import time — the module
    carries no state between runs (RAE-19)."""
    return PythonStdioTransport(
        script_path=os.getenv("MCP_SERVER_PATH", "/app/mcp/server.py"),
        args=["--serve"],
        env={
            **os.environ,
            "MOCK_RUNTIME_DIR": os.environ.get("MOCK_RUNTIME_DIR", "/tmp/mock_runtime"),
            "BACKTEST_REQUESTS_DIR": os.environ.get(
                "BACKTEST_REQUESTS_DIR", "/tmp/mock_runtime/backtest-requests"
            ),
        },
    )


def _job_name_from(issue_key: str | None, run_id: str | None = None, iteration: int = 1) -> str:
    """Their engine requires ^[a-zA-Z0-9_-]+$. The name must be unique per run
    AND per iteration (RAE-19): simulation-results/<job_name>/ lives on the
    shared cluster home, and the engine breaks when a job dir is reused."""
    base = _UNSAFE.sub("-", issue_key or "poc-run") or "poc-run"
    rid = _UNSAFE.sub("-", run_id) if run_id else ""
    if rid and rid != base:  # legacy env path defaults run_id to issue_key
        base = f"{base}-{rid}"
    if len(base) > 60:  # cap length; hash suffix keeps long run_ids unique
        base = f"{base[:51]}-{hashlib.sha1(base.encode()).hexdigest()[:8]}"
    return f"{base}-i{iteration}"


def _pct(value: Any) -> str | None:
    """Their metrics are floats (0.45); our contract wants percent strings."""
    if value is None:
        return None
    return f"{float(value) * 100:.1f}%"


def _to_dict(result: Any) -> dict:
    """FastMCP tool results: prefer structured .data, fall back to text JSON."""
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    content = getattr(result, "content", None)
    if content:
        text = getattr(content[0], "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError(f"unexpected MCP tool result: {result!r}")


async def _call(client: Client, name: str, args: dict) -> dict:
    return _to_dict(await client.call_tool(name, args))


def _output_dir_for(payload: dict) -> str:
    """Where run_backtest_postprocessing should stage artefacts/result.json.
    run.py writes the final, authoritative result to payload['result_path']
    afterwards, so this only needs to be a writable dir near it."""
    result_path = payload.get("result_path") or "/workspace/output/result.json"
    return os.path.dirname(result_path) or "/workspace/output"


def job_name_for(payload: dict) -> str:
    """The engine-safe, per-iteration-unique job name for this run (RAE-19).

    Exposed so the agent-driven driver (proxy/backtest_mcp.py) submits under the
    exact same name this module postprocesses, and so each iterate-loop pass gets
    a fresh job dir on the shared cluster home."""
    return _job_name_from(
        payload.get("issue_key"), payload.get("run_id"), payload.get("iteration", 1)
    )

class BacktestBaselineError(RuntimeError):
    """A real backtest could not obtain the committed strategy .request. We refuse
    to silently fall back to the default template, because the run would then
    report metrics that do not represent the committed edit (the original bug)."""


def _use_real() -> bool:
    return os.getenv("USE_REAL_BACKTESTER", "false").strip().lower() == "true"


def _branch_for(payload: dict) -> str:
    """The quant branch the edit agent committed the strategy to (mirrors
    pipeline_mcp's ``quant/<ticket>``)."""
    return f"quant/{payload.get('issue_key') or 'poc-run'}"


def _zero_code(payload: dict) -> bool:
    """Mirrors run.py's routing: a declared zero-code run, or a ticket with no
    repository in scope (nothing to edit — a pure backtest)."""
    objectives = payload.get("execution_objectives") or {}
    if objectives.get("zero_code_modifications") is True:
        return True
    return payload.get("code_free") is True


# A new engine KEY starts after a comma; values may themselves contain commas
# (VOLATILITY_ACTIVE_RULES=0,1), so only split where a KEY= follows.
_PARAMS_SPLIT = re.compile(r",\s*(?=[A-Za-z_][A-Za-z0-9_.-]*\s*=)")


def _engine_params(payload: dict) -> dict:
    """Engine KEY=value overrides from the ticket's ``params:`` option.

    The validator passes the whole option through as one raw string (IW's
    parsed_task_parameters holds the *options*, not the pairs inside params),
    so the split into engine keys happens here — nothing else in the pipeline
    parses it."""
    raw = (payload.get("args") or {}).get("params")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    out: dict = {}
    for pair in _PARAMS_SPLIT.split(raw.strip()):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key.strip():
            out[key.strip()] = value.strip()
    if not out:
        # A params: the caller clearly meant but we could not read (e.g. the JSON
        # object form, {"NPORT": 5}, which has no KEY=value pairs). Dropping it in
        # silence would run the baseline's own parameters while the ticket looks
        # like it asked for something lighter, so say so loudly.
        print(
            f"Engine params ignored: could not read any KEY=value pair from "
            f"params={raw!r}. Use the KEY=value form, e.g. params: NPORT=5, NFREQ=1. "
            "The run will use the baseline's own parameters.",
            file=sys.stderr,
        )
    return out


def _local_baseline_path(path: str) -> str | None:
    """Resolve a repo-relative .request path to a readable local file: as-is
    from the CWD (local runs, tests), else inside the image next to the MCP
    server (MCP_SERVER_PATH's parent directory is the deployed rae_runtime/)."""
    p = Path(path)
    if p.is_file():
        return str(p)
    server_root = Path(
        os.getenv("MCP_SERVER_PATH", "/app/mcp/server.py")).resolve().parent.parent
    parts = p.parts
    if parts and parts[0] == "rae_runtime":
        candidate = server_root.joinpath(*parts[1:])
        if candidate.is_file():
            return str(candidate)
    return None


def stage_strategy_baseline(payload: dict, required: bool = True) -> str | None:
    """Fetch the strategy ``.request`` the edit agent committed to the ticket's
    quant branch and stage it as a local file, so the backtest runs exactly the
    parameters that were just committed (edit -> engine connection).

    Returns the staged path to hand to ``submit_backtest`` as its baseline, or
    ``None`` to fall back to the image's default template when the ticket's
    strategy artifact isn't a ``.request`` (a general code ticket that never
    reaches here) or when offline.

    In **real** mode a fetch failure raises :class:`BacktestBaselineError` rather
    than falling back: a real run must reflect the committed edit, so we fail with
    diagnostics instead of silently backtesting the default template. Offline/mock
    still degrade to the default (there is no real engine, results are canned)."""
    if os.getenv("RAE_OFFLINE") == "1":
        return None  # offline get_strategy_code returns strategy.py, not the .request
    strat = payload.get("strategy") or {}
    path = strat.get("path")
    if not path or not str(path).endswith(".request"):
        return None
    branch = _branch_for(payload)
    try:
        from github_client import get_strategy_code  # proxy/ is on sys.path
        text = get_strategy_code(branch=branch, path=str(path))
    except Exception as exc:
        if _use_real():
            if required:
                raise BacktestBaselineError(
                    f"could not fetch the committed strategy {path} from {branch} for a "
                    f"real backtest ({type(exc).__name__}: {exc}). Refusing to fall back "
                    "to the default template, which would report metrics that do not "
                    "represent the committed edit. Ensure the edit stage committed "
                    f"{path} to {branch}."
                ) from exc
            print(f"Backtest baseline: no committed strategy at {branch}:{path} "
                  f"({type(exc).__name__}); building from the declared baseline instead",
                  file=sys.stderr)
            return None
        print(f"Backtest baseline: could not fetch {path} from {branch}; "
              f"falling back to the default template ({type(exc).__name__}: {exc})",
              file=sys.stderr)
        return None
    out_dir = _output_dir_for(payload)
    os.makedirs(out_dir, exist_ok=True)
    staged = os.path.join(out_dir, f"{_UNSAFE.sub('-', branch)}.baseline.request")
    with open(staged, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Backtest baseline staged from {branch}:{path} -> {staged}", file=sys.stderr)
    return staged


def attached_request_baseline(payload: dict) -> str | None:
    """Return the selected read-only Jira attachment or fail without fallback."""

    raw_path = (payload.get("input_paths") or {}).get("request_template_path")
    if not raw_path:
        return None
    path = Path(str(raw_path))
    attachment_root = Path("/workspace/input/attachments")
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(attachment_root)
    except (OSError, ValueError) as exc:
        raise BacktestBaselineError(
            f"Jira request attachment path is outside {attachment_root}: {path}"
        ) from exc
    if path.suffix != ".request" or not path.is_file():
        raise BacktestBaselineError(
            f"selected Jira request attachment is not readable: {path}. "
            "Refusing to run a different baseline."
        )
    print(f"Backtest baseline selected from Jira attachment: {path}", file=sys.stderr)
    return str(path)


def resolve_backtest_baseline(
    payload: dict,
    *,
    required: bool = True,
) -> str | None:
    """Prefer the declared Jira attachment, then the ticket's GitHub strategy."""

    attachment = attached_request_baseline(payload)
    if attachment:
        return attachment
    return stage_strategy_baseline(payload, required=required)


async def _generate_baseline(client: Client, payload: dict, params: dict) -> str:
    """Zero-code tickets with engine params skip the LLM entirely: build the
    .request deterministically from the declared baseline + the ticket's params
    (submit_backtest's own byte-preserving merge) and return a staged local copy
    to submit as-is.

    Deliberately does not commit the generated request. This path only runs for
    ``zero_code_modifications`` / code-free tickets, which declare that the run
    creates no branch and no commit; run.py reports exactly that
    (``zero_code_modifications: true``, empty ``modified_files``), so writing to
    the ticket branch here would make the Jira write-back state something untrue.

    The audit copy instead travels as an artifact: the generated text is staged
    under the run's output directory and reported as
    ``generated_artifacts.executed_request_path``, which the orchestrator uploads
    to the ticket as an attachment. That matters — the output directory is a
    temporary mount which is deleted once the run is ingested, so staging alone
    would leave no durable record of the config behind the metrics.

    Baseline priority: the strategy already committed on the quant branch (so
    params stack on an earlier edit), else the ticket's declared strategy file
    resolved locally (e.g. a template under rae_runtime/mcp/templates/), else
    the server's default baseline. Every choice is printed — declared, never
    silent."""
    args = payload.get("args") or {}
    branch = _branch_for(payload)
    path = str((payload.get("strategy") or {}).get("path")
               or "rae_runtime/proxy/strategy.request")

    declared = resolve_backtest_baseline(payload, required=False)
    baseline = declared or _local_baseline_path(path)
    print("Generated backtest baseline source: "
          + (str(declared) if declared
             else (baseline or "server default template")),
          file=sys.stderr)

    gen_args: dict = {"job_name": job_name_for(payload),
                      "extra_params": params, "dry_run": True}
    if baseline:
        gen_args["baseline"] = baseline
    if args.get("start"):
        gen_args["start_date"] = args["start"]
    if args.get("end"):
        gen_args["end_date"] = args["end"]

    generated = await _call(client, "submit_backtest", gen_args)
    if generated.get("status") != "dry_run" or not generated.get("request"):
        raise RuntimeError(f"request generation failed over MCP: {generated}")
    for warning in generated.get("warnings") or []:
        print(f"Request generation warning: {warning}", file=sys.stderr)
    text = generated["request"]

    out_dir = _output_dir_for(payload)
    os.makedirs(out_dir, exist_ok=True)
    staged = os.path.join(out_dir, f"{_UNSAFE.sub('-', branch)}.generated.request")
    with open(staged, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"Generated request staged for upload (not committed: zero-code run) "
          f"-> {staged}", file=sys.stderr)
    return staged


def _translate_final(final: dict, issue_key: str) -> dict:
    """Map a run_backtest_postprocessing ``final_result`` onto the backtest dict
    run.py's iterate loop + result_builder consume (metrics, evaluation,
    recommended_action, equity_curve)."""
    m = final.get("performance_metrics") or {}
    artifacts = final.get("generated_artifacts") or {}
    return {
        "summary": f"Backtest completed for {issue_key} (via MCP).",
        "metrics": {
            "sharpe_ratio": m.get("sharpe_ratio"),
            "max_drawdown": _pct(m.get("max_drawdown")),
            "total_return": _pct(m.get("total_return")),
        },
        "equity_curve": artifacts.get("backtest_plots_path"),
        "evaluation": final.get("evaluation"),
        "recommended_action": final.get("recommended_action"),
    }


async def _postprocess(client: Client, payload: dict, job_name: str) -> dict:
    """Deterministic scoring of an already-run job -> our backtest contract.

    Assumes the engine has produced output under simulation-results/<job_name>/;
    chains artifacts -> equity -> results -> evaluate_result (against
    target_criteria) via the run_backtest_postprocessing tool, then translates.
    Shared by the scripted driver (which submits/polls first) and the agent-driven
    driver (which submits/polls itself), so both return the identical shape."""
    target_criteria = (payload.get("execution_objectives") or {}).get("target_criteria")
    # Don't pin results_dir to the mock path: let the server resolve it from
    # USE_REAL_BACKTESTER (real -> SIMULATION_RESULTS_DIR, mock ->
    # MOCK_RUNTIME_DIR), so real submissions read real results instead of an
    # empty mock dir.
    postprocessed = await _call(client, "run_backtest_postprocessing", {
        "job_name": job_name,
        "ticket_id": payload.get("issue_key", "POC"),
        "output_dir": _output_dir_for(payload),
        "target_criteria": target_criteria,
    })
    final = postprocessed.get("final_result") or {}
    run_status = (final.get("execution_summary") or {}).get("status")
    if run_status != "SUCCESS":
        raise RuntimeError(f"could not parse backtest results over MCP: {final}")
    return _translate_final(final, payload.get("issue_key", "POC"))


_POLL_INITIAL_DELAY = 5
_POLL_MAX_DELAY = 60
_TERMINAL_STATUSES = {"completed", "rejected", "failed", "timeout"}


def _poll_timeout(payload: dict) -> int:
    """How long to wait for a backtest to finish. A real run takes ~10 min, so we
    honour the request's iteration_controls.timeout_seconds (IW contract, 60-10800s),
    falling back to BACKTEST_RESULT_TIMEOUT_SECONDS, BACKTEST_POLL_TIMEOUT, or
    5400s (90 min)."""
    ic = payload.get("iteration_controls") or {}
    try:
        return int(ic["timeout_seconds"])
    except (KeyError, TypeError, ValueError):
        for env_name in ("BACKTEST_RESULT_TIMEOUT_SECONDS", "BACKTEST_POLL_TIMEOUT"):
            try:
                return int(os.getenv(env_name, ""))
            except ValueError:
                continue
        return 5400


async def _poll_until_terminal(client: Client, job_name: str, submitted_at: str,
                               timeout_seconds: int) -> dict:
    """Poll get_backtest_status until the job reaches a terminal state or we hit
    timeout. Exponential backoff 5s -> 60s. In mock mode the first check already
    reports 'completed' (the submit synchronously ran the mock builder), so this
    returns immediately with no sleep; in real mode it waits out the ~10-min run."""
    import time

    delay = _POLL_INITIAL_DELAY
    start = time.monotonic()
    while True:
        status = await _call(client, "get_backtest_status", {
            "job_name": job_name,
            "submitted_at": submitted_at,
            "timeout_seconds": timeout_seconds,
        })
        print(
            "Backtest poll status: "
            f"job_name={job_name}, "
            f"status={status.get('status')}, "
            f"elapsed_seconds={status.get('elapsed_seconds')}, "
            f"timeout_seconds={status.get('timeout_seconds', timeout_seconds)}, "
            f"expected_results_path={status.get('expected_results_path') or status.get('results_dir')}",
            file=sys.stderr,
        )
        if status.get("status") in _TERMINAL_STATUSES:
            return status
        if time.monotonic() - start > timeout_seconds:
            return {"status": "timeout", "job_name": job_name,
                    "error": f"gave up after {timeout_seconds}s waiting for completion"}
        await asyncio.sleep(delay)
        delay = min(delay * 2, _POLL_MAX_DELAY)


async def _run_backtest(payload: dict) -> dict:
    args = payload.get("args", {})
    job_name = job_name_for(payload)
    submit_args: dict = {"job_name": job_name}
    params = _engine_params(payload)

    async with Client(_make_transport()) as client:
        if _zero_code(payload) and params:
            # "Type a comment, get a backtest": a zero-code ticket that carries
            # engine params runs the deterministic generate–run path — no LLM in
            # the config path, and nothing committed (the ticket declared no code
            # modifications). Dates and params are already merged into the
            # generated text, so it is submitted with no further overrides.
            submit_args["baseline"] = await _generate_baseline(client, payload, params)
        else:
            # Run the parameters the edit agent committed to the ticket's quant
            # branch; fall back to the image default template when there's no
            # branch file.
            baseline = resolve_backtest_baseline(payload)
            if baseline:
                submit_args["baseline"] = baseline
            # Only override the window when the request explicitly asks for one;
            # otherwise keep the baseline's own (long) STARTDATE/ENDDATE. Injecting
            # a short default here used to produce all-zero results (see
            # submit_backtest's window guard).
            if args.get("start"):
                submit_args["start_date"] = args["start"]
            if args.get("end"):
                submit_args["end_date"] = args["end"]

        submitted = await _call(client, "submit_backtest", submit_args)
        if submitted.get("status") != "submitted":
            raise RuntimeError(f"backtest submission failed over MCP: {submitted}")
        submitted_at = submitted.get("submitted_at")
        if not isinstance(submitted_at, str) or not submitted_at.strip():
            raise RuntimeError(
                f"backtest submission did not return submitted_at over MCP: {submitted}"
            )
        print(
            "Backtest handoff paths: "
            f"job_name={job_name}, "
            f"request_path={submitted.get('request_path') or submitted.get('request_file')}, "
            f"expected_results_path={submitted.get('expected_results_path')}, "
            f"timeout_seconds={_poll_timeout(payload)}",
            file=sys.stderr,
        )
        status = await _poll_until_terminal(
            client, job_name, submitted_at, _poll_timeout(payload))
        if status.get("status") != "completed":
            raise RuntimeError(f"backtest did not complete over MCP: {status}")

        result = await _postprocess(client, payload, job_name)
        # Report the exact .request handed to the engine so the gateway uploads it
        # as an attachment. The staging dir is a temporary mount the orchestrator
        # deletes after the run, so a path that is not referenced in the response
        # leaves no durable record of the config that produced these metrics.
        executed_request = submit_args.get("baseline")
        if executed_request:
            result.setdefault("executed_request_path", executed_request)
        return result


async def postprocess_via_mcp_async(payload: dict, job_name: str) -> dict:
    """Open a fresh MCP session, wait for the job to finish, then score it. Used by
    the agent-driven driver: the agent submits (and may poll), but we do NOT rely on
    the LLM to wait out a ~10-min real run — we poll deterministically here, then
    reuse the same postprocessing so the result matches the scripted contract
    (metrics + evaluation + recommended_action) and drives the iterate loop."""
    async with Client(_make_transport()) as client:
        # The agent submitted under job_name; we don't have its submitted_at, so
        # start our own clock (only affects the status timeout branch, not the
        # dir-exists completion check).
        submitted_at = datetime.now(timezone.utc).isoformat()
        status = await _poll_until_terminal(
            client, job_name, submitted_at, _poll_timeout(payload))
        if status.get("status") != "completed":
            raise RuntimeError(f"backtest did not complete over MCP: {status}")
        return await _postprocess(client, payload, job_name)


def run_backtest_via_mcp(payload: dict) -> dict:
    """Sync wrapper so run.py's main() doesn't need to be async."""
    return asyncio.run(_run_backtest(payload))
