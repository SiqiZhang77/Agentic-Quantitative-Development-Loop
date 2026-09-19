"""
Real MCP server mode:
   python server.py --serve

   Starts FastMCP and keeps running, waiting for MCP tool calls.

The MCP server also exposes post-backtest output tools:
- get_backtest_artifacts
- generate_equity_curve
- get_backtest_results
- run_backtest_postprocessing

Those tools are intended to run after a backtest has completed and a result
directory or ZIP exists under simulation-results.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any



# Make sibling modules importable from the nested layout.
_THIS_DIR = Path(__file__).resolve().parent
for p in (_THIS_DIR / "tools", _THIS_DIR / "backtester", _THIS_DIR.parent / "after_backtest"):
    p = str(p)
    if p not in sys.path:
        sys.path.insert(0, p)

# RAE-17: evaluate_result.py lives in the sibling after_backtest/ directory.
_AFTER_BACKTEST_DIR = str(_THIS_DIR.parent / "after_backtest")
if _AFTER_BACKTEST_DIR not in sys.path:
    sys.path.insert(0, _AFTER_BACKTEST_DIR)


def _load_function(
    module_name: str,
    function_name: str,
    fallback_filenames: tuple[str, ...] = (),
):
    """
    Import a function from a normal Python module, with optional fallback support
    for uploaded/dev filenames such as generate_equity_curve-2.py.

    In the repository, prefer importable names:
      generate_equity_curve.py
      get_backtest_artifacts.py
      get_backtest_results.py

    During manual testing, the uploaded files may still have '-2' in the name.
    """
    try:
        module = __import__(module_name, fromlist=[function_name])
        return getattr(module, function_name)
    except (ImportError, AttributeError):
        pass

    for filename in fallback_filenames:
        candidate = _THIS_DIR / filename
        if not candidate.exists():
            continue

        spec = importlib.util.spec_from_file_location(
            f"_{module_name}_fallback",
            candidate,
        )
        if spec is None or spec.loader is None:
            continue

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return getattr(module, function_name)

    raise ImportError(
        f"Could not import {function_name} from {module_name}. "
        f"Tried fallback files: {', '.join(fallback_filenames) or 'none'}"
    )


from submit_backtest import (
    submit_backtest as _submit_backtest,
    results_root as _results_root,
    _use_real,
)
from list_master_indices import list_master_indices as _list_master_indices
from get_backtest_status import get_backtest_status as _get_backtest_status
from get_backtest_results import get_backtest_results as _get_backtest_results
from list_jobs import list_jobs as _list_jobs
from evaluate_result import evaluate_result as _evaluate_result

_get_backtest_artifacts = _load_function(
    "get_backtest_artifacts",
    "get_backtest_artifacts",
    ("get_backtest_artifacts-2.py",),
)
_generate_equity_curve = _load_function(
    "generate_equity_curve",
    "generate_equity_curve",
    ("generate_equity_curve-2.py",),
)

try:
    from mock_model_builder import process_all_pending as _process_all_pending
except ImportError:
    _process_all_pending = None


def _process_current_mock_requests() -> list[dict[str, Any]]:
    """Process the current call's mock directories, never import-time defaults."""

    if _process_all_pending is None:
        return []
    return _process_all_pending(
        requests_dir=os.environ.get(
            "BACKTEST_REQUESTS_DIR",
            "/tmp/mock_runtime/backtest-requests",
        ),
        runtime_dir=os.environ.get("MOCK_RUNTIME_DIR", "/tmp/mock_runtime"),
    )

try:
    from fastmcp import FastMCP
except ImportError as exc:
    FastMCP = None
    _FASTMCP_IMPORT_ERROR = exc
else:
    _FASTMCP_IMPORT_ERROR = None


# Default virtual/demo parameters, so VS Code Run works without manual input.
DEFAULT_JOB_NAME = "test001"
DEFAULT_START_DATE = "2020-01-01"
DEFAULT_END_DATE = "2020-12-31"
DEFAULT_TIMEOUT_SECONDS = 5400
DEFAULT_TICKET_ID = "SCRUM-0"
DEFAULT_OUTPUT_DIR = "/workspace/output"
DEFAULT_RESULT_FILENAME = "result.json"
DEFAULT_BACKTEST_USERNAME = os.getenv("BIALOBOG_USERNAME", "zczqiav")


def _default_results_dir(results_dir: str | None = None) -> str:
    """Use the caller-supplied results dir, otherwise the real/mock results root
    resolved from USE_REAL_BACKTESTER (real: explicit SIMULATION_RESULTS_DIR;
    mock: MOCK_RUNTIME_DIR/simulation-results)."""
    if results_dir:
        return str(Path(results_dir).expanduser())
    return str(_results_root())


def _default_output_dir(output_dir: str | None = None) -> str:
    """Use /workspace/output by default because IW/Airflow ingests artefacts there."""
    if output_dir:
        return str(Path(output_dir).expanduser())
    return DEFAULT_OUTPUT_DIR


def _default_username(username: str | None = None) -> str:
    if username and username.strip():
        return username.strip()
    for env_name in ("BIALOBOG_USERNAME", "USER", "LOGNAME"):
        raw = os.getenv(env_name)
        if raw and raw.strip():
            return raw.strip()
    try:
        return getpass.getuser()
    except Exception:
        return DEFAULT_BACKTEST_USERNAME


def _safe_component(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value).strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unnamed"


def _first_file_match(base: Path, patterns: tuple[str, ...]) -> Path | None:
    if not base.exists():
        return None
    for pattern in patterns:
        matches = [p for p in base.rglob(pattern) if p.is_file()]
        if matches:
            return max(matches, key=lambda p: p.stat().st_mtime)
    return None


def _find_run_dir(job_name: str, results_dir: str) -> Path | None:
    """Small local copy of the run-dir lookup used only by the combined MCP tool."""
    results_root = Path(results_dir).expanduser()
    exact = results_root / job_name
    if exact.exists() and exact.is_dir():
        return exact

    safe_job = _safe_component(job_name)
    exact_safe = results_root / safe_job
    if exact_safe.exists() and exact_safe.is_dir():
        return exact_safe

    if not results_root.exists():
        return None

    candidates = [
        p for p in results_root.iterdir()
        if p.is_dir() and (job_name in p.name or safe_job in p.name)
    ]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)

    zip_candidates = [
        p for p in results_root.rglob("*.zip")
        if job_name in p.name or safe_job in p.name
    ]
    if zip_candidates:
        return max(zip_candidates, key=lambda p: p.stat().st_mtime).parent

    return None


def _find_zqq_file(job_name: str, results_dir: str) -> str | None:
    run_dir = _find_run_dir(job_name, results_dir)
    if run_dir is None:
        return None
    zqq = _first_file_match(run_dir, ("*ZQQ*.csv", "*zqq*.csv"))
    return str(zqq) if zqq else None


def _resolved_run_id(job_name: str, run_id: str | None = None) -> str:
    """Use one stable run_id across all postprocessing artefact writes."""
    if run_id:
        return _safe_component(run_id)
    return f"{_safe_component(job_name)}-{int(datetime.now(timezone.utc).timestamp())}"


def _artifact_dir(output_dir: str, run_id: str) -> Path:
    """
    RAE-15 durable helper directory:
        /workspace/output/artefacts/<run_id>/
    """
    output_root = Path(output_dir).expanduser()
    path = output_root / "artefacts" / _safe_component(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_helper_json(
    payload: dict[str, Any],
    output_dir: str,
    job_name: str,
    label: str,
    run_id: str | None = None,
) -> str:
    """
    Save helper responses so get_backtest_results can merge them using its
    --merge-response-json contract.

    With run_id, helper JSONs are also durable and grouped under:
        /workspace/output/artefacts/<run_id>/
    """
    if run_id:
        base_dir = _artifact_dir(output_dir, run_id)
    else:
        base_dir = Path(output_dir).expanduser()
        base_dir.mkdir(parents=True, exist_ok=True)

    path = base_dir / f"{_safe_component(job_name)}_{_safe_component(label)}_response.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


def _write_final_result_json(payload: dict[str, Any], output_dir: str) -> str:
    """
    Write the final response to /workspace/output/result.json, which is the
    durable mounted result file IW should collect.
    """
    output_root = Path(output_dir).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / DEFAULT_RESULT_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path)


# --- RAE-23a: least-privilege tool surface ---------------------------------
# tool_policy.json (co-located) is the source of truth for which tools are
# exposed to the LLM. After all tools are registered we drop any marked
# default="block" from the surface. This only hides them from the LLM - the
# underlying functions are untouched, so wrappers like
# run_backtest_postprocessing still call them directly. See TOOL_SURFACE.md.
_TOOL_POLICY_PATH = _THIS_DIR / "tool_policy.json"


def _blocked_tool_names(policy_path: Path = _TOOL_POLICY_PATH) -> list[str]:
    """Names of backtest-server tools whose default access is ``block``.

    Fail-open: a missing/malformed policy (or RAE_TOOL_POLICY_ENFORCE=0) returns
    an empty list so a config problem never takes the server down; it warns on
    stderr instead. RAE_TOOL_POLICY_FILE overrides the policy location.
    """
    if os.getenv("RAE_TOOL_POLICY_ENFORCE", "1").strip().lower() in ("0", "false", "off", "no"):
        return []
    path = Path(os.getenv("RAE_TOOL_POLICY_FILE", str(policy_path))).expanduser()
    try:
        tools = json.loads(path.read_text(encoding="utf-8"))["servers"]["backtest"]["tools"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"[tool-policy] could not apply {path} ({exc}); exposing all tools",
              file=sys.stderr)
        return []
    return [name for name, spec in tools.items() if spec.get("default") == "block"]


def _apply_tool_policy(mcp) -> list[str]:
    """Remove blocked tools from *mcp*'s surface; return the names removed.

    Still fail-open (a policy/API problem never takes the server down), but no
    longer fails *silently*: if a blocked tool cannot be removed - because this
    FastMCP has no remove-tool API, or removal errors for some other reason -
    the tool stays exposed and we warn on stderr. Silently leaving a blocked
    tool on the surface is exactly the least-privilege bypass this warns about.
    """
    blocked = _blocked_tool_names()
    if not blocked:
        return []
    provider = getattr(mcp, "local_provider", None)
    remover = getattr(provider, "remove_tool", None) or getattr(mcp, "remove_tool", None)
    if remover is None:
        print("[tool-policy] WARNING: no remove-tool API on this FastMCP; "
              f"blocked tools remain exposed to the LLM: {blocked}", file=sys.stderr)
        return []
    removed: list[str] = []
    failed: list[str] = []
    for name in blocked:
        try:
            remover(name)
            removed.append(name)
        except KeyError:
            pass  # tool already absent from the surface -> nothing to hide
        except Exception:  # API drift / other error -> tool likely still exposed
            failed.append(name)
    if removed:
        print(f"[tool-policy] not exposed to the LLM (blocked by policy): {removed}",
              file=sys.stderr)
    if failed:
        print("[tool-policy] WARNING: could not remove, tools remain exposed: "
              f"{failed}", file=sys.stderr)
    return removed


# --- IW seam: baseline from a Jira .request attachment ---------------------
# IW mounts an optional .request template attachment and names its path in the
# runtime request payload under input_paths.request_template_path. Our tools take
# a baseline via the BACKTEST_BASELINE env var, so bridge the two at server
# startup: if the payload names a template, point BACKTEST_BASELINE at it.
# The field name/location follow IW's runtime_request contract.
_REQUEST_JSON_PATH = os.getenv("RAE_REQUEST_FILE", "/workspace/input/request.json")


def _apply_request_template_baseline(request_path: str | Path | None = None) -> str | None:
    """Point BACKTEST_BASELINE at the mounted Jira .request attachment, if the
    runtime request names one, so submit_backtest builds on the user's template.

    Never overrides an already-set BACKTEST_BASELINE. If the payload is absent,
    unreadable, or names no template it leaves the default baseline in place. A
    named but unreadable template is an explicit contract violation and raises;
    silently using a different baseline would report results for the wrong run.
    """
    if os.getenv("BACKTEST_BASELINE"):
        return None  # explicitly provided elsewhere -> don't override
    try:
        payload = json.loads(Path(request_path or _REQUEST_JSON_PATH)
                             .expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # no / invalid payload (e.g. local, mock) -> default baseline
    tpl = (payload.get("input_paths") or {}).get("request_template_path")
    if not tpl:
        return None
    if not Path(tpl).is_file():
        raise RuntimeError(
            f"request_template_path {tpl!r} is not a readable file; "
            "refusing to use a different baseline"
        )
    os.environ["BACKTEST_BASELINE"] = str(tpl)
    print(f"[baseline] using Jira .request attachment as baseline: {tpl}", file=sys.stderr)
    return str(tpl)


def create_mcp_server():
    """Create and configure the FastMCP server."""
    if FastMCP is None:
        raise RuntimeError(
            "FastMCP is not installed. Install it before running MCP server mode, "
            "for example: pip install fastmcp"
        ) from _FASTMCP_IMPORT_ERROR

    # Bridge the IW runtime request's template attachment to BACKTEST_BASELINE
    # before any tool runs, so submit_backtest builds on the user's template.
    _apply_request_template_baseline()

    mcp = FastMCP("BSL Agentic Quant Backtest MCP")

    @mcp.tool()
    def submit_backtest(
        job_name: str,
        start_date: str | None = None,
        end_date: str | None = None,
        atrade_sifting_commons: str | None = None,
        atrade_sifting_pretrade: str | None = None,
        atrade_sifting_portfolio: str | None = None,
        atrade_sifting_hedge: str | None = None,
        atrade_sifting_model: str | None = None,
        atrade_sifting_config: str | None = None,
        nport: int | None = None,
        nfreq: int | None = None,
        ndelay: int | None = None,
        slippage: float | None = None,
        initcash: int | None = None,
        value_active_rules: str | None = None,
        carry_active_rules: str | None = None,
        master_indices: str | None = None,
        universe: str | None = None,
        short_stock_defs: str | None = None,
        short_indice_defs: str | None = None,
        extra_params: dict[str, Any] | None = None,
        remove_keys: list[str] | None = None,
        dry_run: bool = False,
        baseline: str | None = None,
    ) -> dict[str, Any]:
        """Build an engine-valid .request from a proven baseline + overrides and deliver it; then trigger the mock build unless dry_run."""
        result = _submit_backtest(
            job_name=job_name,
            start_date=start_date,
            end_date=end_date,
            atrade_sifting_commons=atrade_sifting_commons,
            atrade_sifting_pretrade=atrade_sifting_pretrade,
            atrade_sifting_portfolio=atrade_sifting_portfolio,
            atrade_sifting_hedge=atrade_sifting_hedge,
            atrade_sifting_model=atrade_sifting_model,
            atrade_sifting_config=atrade_sifting_config,
            nport=nport,
            nfreq=nfreq,
            ndelay=ndelay,
            slippage=slippage,
            initcash=initcash,
            value_active_rules=value_active_rules,
            carry_active_rules=carry_active_rules,
            master_indices=master_indices,
            universe=universe,
            short_stock_defs=short_stock_defs,
            short_indice_defs=short_indice_defs,
            extra_params=extra_params,
            remove_keys=remove_keys,
            dry_run=dry_run,
            baseline=baseline,
        )
        # The mock model-builder only exists to fake the engine offline. In real
        # mode the cluster's own watcher picks the request up, so never run it.
        if not dry_run and not _use_real() and _process_all_pending is not None:
            _process_current_mock_requests()
        return result

    @mcp.tool()
    def list_master_indices(
        latest_only: bool = False,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """List the master-indices versions available on the cluster. Call before submit_backtest to pick a current data universe."""
        return _list_master_indices(latest_only=latest_only, filter=filter)

    @mcp.tool()
    def get_backtest_status(
        job_name: str,
        submitted_at: str,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Check whether a submitted backtest has completed, been rejected, or timed out."""
        return _get_backtest_status(
            job_name=job_name,
            submitted_at=submitted_at,
            timeout_seconds=timeout_seconds,
        )

    @mcp.tool()
    def get_backtest_artifacts(
        job_name: str,
        ticket_id: str = DEFAULT_TICKET_ID,
        run_id: str | None = None,
        results_dir: str | None = None,
        output_dir: str | None = None,
        username: str | None = None,
        weles_host: str | None = None,
        amok_landing_base: str | None = None,
        hdfs_base: str | None = None,
        hdfs_run_id: str | None = None,
        hdfs_upload_required: bool = False,
        hdfs_uploaded_path: str | None = None,
        extract_zip: bool = True,
        include_success_log: bool = False,
        write_result_json: bool = True,
    ) -> dict[str, Any]:
        """
        Post-backtest tool: locate artefacts under simulation-results, safely
        extract the ZIP if requested, copy schema-ingested artefacts to
        /workspace/output, and return a runtime_response-shaped JSON object.
        """
        kwargs: dict[str, Any] = {
            "job_name": job_name,
            "ticket_id": ticket_id,
            "run_id": run_id,
            "results_dir": _default_results_dir(results_dir),
            "output_dir": _default_output_dir(output_dir),
            "username": _default_username(username),
            "hdfs_upload_required": hdfs_upload_required,
            "hdfs_uploaded_path": hdfs_uploaded_path,
            "extract_zip": extract_zip,
            "include_success_log": include_success_log,
            "write_result_json": write_result_json,
        }
        if weles_host is not None:
            kwargs["weles_host"] = weles_host
        if amok_landing_base is not None:
            kwargs["amok_landing_base"] = amok_landing_base
        if hdfs_base is not None:
            kwargs["hdfs_base"] = hdfs_base
        if hdfs_run_id is not None:
            kwargs["hdfs_run_id"] = hdfs_run_id
        return _get_backtest_artifacts(**kwargs)

    @mcp.tool()
    def generate_equity_curve(
        job_name: str,
        zqq_file_path: str,
        ticket_id: str = DEFAULT_TICKET_ID,
        run_id: str | None = None,
        output_dir: str | None = None,
        output_format: str = "both",
        initial_capital: float = 10000.0,
        return_scale: str = "auto",
    ) -> dict[str, Any]:
        """
        Post-backtest tool: read a ZQQ/time-series CSV, generate an equity curve
        CSV/PNG under /workspace/output, and return a runtime_response-shaped
        JSON object.
        """
        return _generate_equity_curve(
            job_name=job_name,
            zqq_file_path=zqq_file_path,
            ticket_id=ticket_id,
            run_id=run_id,
            output_dir=_default_output_dir(output_dir),
            output_format=output_format,
            initial_capital=initial_capital,
            return_scale=return_scale,
        )

    @mcp.tool()
    def get_backtest_results(
        job_name: str,
        ticket_id: str = DEFAULT_TICKET_ID,
        run_id: str | None = None,
        results_dir: str | None = None,
        results_table_path: str | None = None,
        merge_response_json: list[str] | None = None,
        time_series_data_path: str | None = None,
        raw_log_reference: str | None = None,
        include_success_log: bool = False,
        extract_zip: bool = True,
    ) -> dict[str, Any]:
        """
        Post-backtest tool: parse resultsTable.csv, merge schema-compatible
        outputs from get_backtest_artifacts/generate_equity_curve if provided,
        and return the final runtime_response-shaped JSON object.
        """
        return _get_backtest_results(
            job_name=job_name,
            ticket_id=ticket_id,
            run_id=run_id,
            results_dir=_default_results_dir(results_dir),
            results_table_path=results_table_path,
            merge_response_json=merge_response_json or [],
            time_series_data_path=time_series_data_path,
            raw_log_reference=raw_log_reference,
            include_success_log=include_success_log,
            extract_zip=extract_zip,
        )

    @mcp.tool()
    def run_backtest_postprocessing(
        job_name: str,
        ticket_id: str = DEFAULT_TICKET_ID,
        run_id: str | None = None,
        results_dir: str | None = None,
        output_dir: str | None = None,
        username: str | None = None,
        zqq_file_path: str | None = None,
        output_format: str = "both",
        extract_zip: bool = True,
        include_success_log: bool = False,
        target_criteria: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Convenience workflow for a completed backtest:
        1) get_backtest_artifacts
        2) generate_equity_curve, if a ZQQ/time-series CSV is found or supplied
        3) get_backtest_results, merging helper JSON responses
        4) evaluate_result, scoring performance_metrics against target_criteria
           (RAE-17), if supplied

        This does not submit or run a backtest. It assumes completed output
        already exists under simulation-results.
        """
        resolved_results_dir = _default_results_dir(results_dir)
        resolved_output_dir = _default_output_dir(output_dir)
        resolved_run_id = _resolved_run_id(job_name, run_id)

        artifacts_result = _get_backtest_artifacts(
            job_name=job_name,
            ticket_id=ticket_id,
            run_id=resolved_run_id,
            results_dir=resolved_results_dir,
            output_dir=resolved_output_dir,
            username=_default_username(username),
            extract_zip=extract_zip,
            include_success_log=include_success_log,
            write_result_json=False,
        )
        artifacts_json_path = _write_helper_json(
            artifacts_result,
            resolved_output_dir,
            job_name,
            "artifacts",
            run_id=resolved_run_id,
        )

        found_zqq = zqq_file_path or _find_zqq_file(job_name, resolved_results_dir)
        equity_json_path: str | None = None
        if found_zqq:
            equity_result = _generate_equity_curve(
                job_name=job_name,
                zqq_file_path=found_zqq,
                ticket_id=ticket_id,
                run_id=resolved_run_id,
                output_dir=resolved_output_dir,
                output_format=output_format,
            )
            equity_json_path = _write_helper_json(
                equity_result,
                resolved_output_dir,
                job_name,
                "equity",
                run_id=resolved_run_id,
            )
        else:
            equity_result = {
                "status": "SKIPPED",
                "message": "No ZQQ/time-series CSV found, so equity curve generation was skipped.",
            }

        merge_json = [artifacts_json_path]
        if equity_json_path:
            merge_json.append(equity_json_path)

        final_result = _get_backtest_results(
            job_name=job_name,
            ticket_id=ticket_id,
            run_id=resolved_run_id,
            results_dir=resolved_results_dir,
            merge_response_json=merge_json,
            include_success_log=include_success_log,
            extract_zip=extract_zip,
        )
        final_json_path = _write_helper_json(
            final_result,
            resolved_output_dir,
            job_name,
            "final_results",
            run_id=resolved_run_id,
        )

        evaluation_patch = _evaluate_result(
            performance_metrics=final_result.get("performance_metrics"),
            target_criteria=target_criteria,
            status=(final_result.get("execution_summary") or {}).get("status", "FAILED"),
        )
        final_result.update(evaluation_patch)

        result_json_path = _write_final_result_json(final_result, resolved_output_dir)

        return {
            "job_name": job_name,
            "run_id": resolved_run_id,
            "results_dir": resolved_results_dir,
            "output_dir": resolved_output_dir,
            "zqq_file_path": found_zqq,
            "artifacts_response_path": artifacts_json_path,
            "equity_response_path": equity_json_path,
            "final_results_response_path": final_json_path,
            "result_json_path": result_json_path,
            "artifacts_result": artifacts_result,
            "equity_result": equity_result,
            "final_result": final_result,
        }

    @mcp.tool()
    def list_jobs() -> dict[str, Any]:
        """List all backtest jobs currently visible and their status."""
        return _list_jobs()

    # RAE-23a: enforce the least-privilege tool surface before serving.
    _apply_tool_policy(mcp)

    return mcp


def run_click_demo(
    job_name: str = DEFAULT_JOB_NAME,
    start_date: str = DEFAULT_START_DATE,
    end_date: str = DEFAULT_END_DATE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a complete local mock backtest workflow once and return the result.

    This is for VS Code click-run testing. It does not start the long-running
    FastMCP server. It submits a dummy request, lets the mock model-builder
    process it, checks the backtest status, prints JSON, and exits.
    """
    submit_result = _submit_backtest(
        job_name=job_name,
        start_date=start_date,
        end_date=end_date,
    )

    processed_requests: list[dict[str, Any]] = []
    if _process_all_pending is not None:
        processed_requests = _process_all_pending()

    submitted_at = submit_result.get("submitted_at", "")
    status_result = _get_backtest_status(
        job_name=job_name,
        submitted_at=submitted_at,
        timeout_seconds=timeout_seconds,
    )

    return {
        "mode": "click_run_demo",
        "submit_result": submit_result,
        "processed_requests": processed_requests,
        "status_result": status_result,
        "note": "Use `python server.py --serve` only when you want to start the long-running FastMCP server.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtest MCP server. Default mode runs a local demo once and exits."
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the real FastMCP server. This will keep running and wait for MCP calls.",
    )
    parser.add_argument("--job-name", default=DEFAULT_JOB_NAME)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=DEFAULT_END_DATE)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    if args.serve:
        mcp = create_mcp_server()
        mcp.run()
        return

    result = run_click_demo(
        job_name=args.job_name,
        start_date=args.start_date,
        end_date=args.end_date,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


# Expose mcp for clients/importers that expect a module-level MCP object.
# Keep this lazy-safe: missing FastMCP should not break click-run demo mode.
if FastMCP is not None:
    mcp = create_mcp_server()
else:
    mcp = None


if __name__ == "__main__":
    main()
