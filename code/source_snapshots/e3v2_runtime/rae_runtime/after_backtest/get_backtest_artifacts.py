"""
get_backtest_artifacts.py

Post-backtest only: it does not submit or run a backtest. After a completed run
exists under simulation-results, it locates artefacts and emits JSON using the
exact top-level field names from runtime_response.schema.json:

schema_version, run_id, execution_summary, performance_metrics,
generated_artifacts, diagnostics.

IW ingestion rule:
Airflow only ingests files referenced by these schema fields, and those files
must live under /workspace/output. RAE-15 writes copied artefacts under
/workspace/output/artefacts/<run_id>/ and writes the final runtime response to
/workspace/output/result.json:
- performance_metrics.time_series_data_path
- generated_artifacts.backtest_plots_path
- diagnostics.raw_log_reference

The HDFS path must live under /shared/model-logs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DEFAULT_USERNAME = os.environ.get("BIALOBOG_USERNAME", "zczqiav")
DEFAULT_RESULTS_DIR = Path(os.environ.get("BIALOBOG_RESULTS_DIR", f"/home/{DEFAULT_USERNAME}/simulation-results"))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("RAE_OUTPUT_DIR", "/workspace/output"))
SCHEMA_OUTPUT_ROOT = Path("/workspace/output")
DEFAULT_ARTIFACTS_SUBDIR = os.environ.get("RAE_ARTIFACTS_SUBDIR", "artefacts")
DEFAULT_RESULT_FILENAME = os.environ.get("RAE_RESULT_FILENAME", "result.json")
DEFAULT_WELES_HOST = os.environ.get("WELES_HOST", "weles.cs.ucl.ac.uk")
DEFAULT_AMOK_LANDING_BASE = Path(os.environ.get("AMOK_LANDING_BASE", f"/home/{DEFAULT_USERNAME}/BacktestsLanding"))
DEFAULT_HDFS_BASE = Path(os.environ.get("HDFS_BASE", "/shared/model-logs"))

FILE_PATTERNS = {
    "zip_file": ["*.zip"],
    "results_table": ["resultsTable.csv", "*resultsTable*.csv"],
    "zqq_file": ["*ZQQ*.csv", "*zqq*.csv"],
    "equity_curve_csv": ["*equity*curve*.csv", "*time_series*.csv", "*timeseries*.csv"],
    "plot_or_report": ["*equity*curve*.png", "*plot*.png", "*.png", "*report*.html", "*report*.pdf", "*.html", "*.pdf"],
    "log_file": ["*job*log*.txt", "*wrapper*.log", "wrapper.log", "*.log", "*log*.txt", "*.txt"],
    "runme": ["runMe.sh", "runme.sh", "*runMe*.sh", "*.sh"],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned or "unnamed"


def _is_schema_workspace_path(path: str | None) -> bool:
    return bool(path and path.startswith("/workspace/output/") and ".." not in path)


def _is_schema_hdfs_path(path: str | None) -> bool:
    return bool(path and path.startswith("/shared/model-logs/") and ".." not in path)


def _performance_metrics(time_series_data_path: str | None = None) -> dict[str, Any]:
    return {
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "alpha": None,
        "beta": None,
        "time_series_data_path": time_series_data_path if _is_schema_workspace_path(time_series_data_path) else None,
    }


def _generated_artifacts(
    backtest_plots_path: str | None = None,
    weles_forcluster_path: str | None = None,
    amok_landing_path: str | None = None,
    hdfs_uploaded_path: str | None = None,
    modified_files: list[str] | None = None,
    new_files: list[str] | None = None,
    workspace_artifact_dir: str | None = None,
) -> dict[str, Any]:
    return {
        "modified_files": modified_files or [],
        "new_files": new_files or [],
        "backtest_plots_path": backtest_plots_path if _is_schema_workspace_path(backtest_plots_path) else None,
        "data_paths": {
            "weles_forcluster_path": weles_forcluster_path,
            "amok_landing_path": amok_landing_path,
            "hdfs_uploaded_path": hdfs_uploaded_path if _is_schema_hdfs_path(hdfs_uploaded_path) else None,
            "workspace_artifact_dir": (
                workspace_artifact_dir
                if workspace_artifact_dir and _is_schema_workspace_path(workspace_artifact_dir.rstrip("/") + "/.")
                else None
            ),
        },
    }


def _diagnostics(
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    raw_log_reference: str | None = None,
    include_success_log: bool = False,
) -> dict[str, Any]:
    # IW skips diagnostic log ingestion for successful runs. Keep success logs out
    # of the schema field unless explicitly requested for debugging.
    if status == "SUCCESS" and not include_success_log:
        raw_log_reference = None
    return {
        "error_code": None if status == "SUCCESS" else (error_code or "RUNTIME_ERROR"),
        "error_message": None if status == "SUCCESS" else (error_message or "Backtest artefact discovery failed."),
        "raw_log_reference": raw_log_reference if _is_schema_workspace_path(raw_log_reference) else None,
    }


def _execution_summary(ticket_id: str, status: str, start_time: str, end_time: str, hdfs_upload_required: bool, message: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "iteration_traces": [
            {
                "iteration": 1,
                "agent": "mcp_backtester",
                "tool_call": "get_backtest_artifacts",
                "status": status,
                "message": message,
                "timestamp": end_time,
            }
        ],
        "zero_code_modifications": True,
        "hdfs_upload_required": hdfs_upload_required,
    }


def _runtime_response(
    *,
    run_id: str,
    ticket_id: str,
    status: str,
    start_time: str,
    end_time: str,
    hdfs_upload_required: bool,
    trace_message: str,
    performance_metrics: dict[str, Any],
    generated_artifacts: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "execution_summary": _execution_summary(ticket_id, status, start_time, end_time, hdfs_upload_required, trace_message),
        "performance_metrics": performance_metrics,
        "generated_artifacts": generated_artifacts,
        "diagnostics": diagnostics,
    }


def _first_file_match(base: Path, patterns: list[str]) -> Path | None:
    if not base.exists():
        return None
    for pattern in patterns:
        matches = [p for p in base.rglob(pattern) if p.is_file()]
        if matches:
            return max(matches, key=lambda p: p.stat().st_mtime)
    return None


def _first_dir_named(base: Path, name: str) -> Path | None:
    if not base.exists():
        return None
    matches = [p for p in base.rglob(name) if p.is_dir()]
    return sorted(matches, key=lambda p: (len(p.parts), str(p)))[0] if matches else None


def _find_run_dir(job_name: str, results_dir: Path) -> Path | None:
    exact = results_dir / job_name
    if exact.exists() and exact.is_dir():
        return exact
    safe_job = _safe_component(job_name)
    exact_safe = results_dir / safe_job
    if exact_safe.exists() and exact_safe.is_dir():
        return exact_safe
    if not results_dir.exists():
        return None
    candidates = [p for p in results_dir.iterdir() if p.is_dir() and (job_name in p.name or safe_job in p.name)]
    if candidates:
        return max(candidates, key=lambda p: p.stat().st_mtime)
    zip_candidates = [p for p in results_dir.rglob("*.zip") if job_name in p.name or safe_job in p.name]
    return max(zip_candidates, key=lambda p: p.stat().st_mtime).parent if zip_candidates else None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _durable_artifact_dir(output_dir: Path, run_id: str) -> Path:
    """
    Stable artefact directory collected by IW:
    /workspace/output/artefacts/<run_id>/
    """
    target = output_dir / DEFAULT_ARTIFACTS_SUBDIR / _safe_component(run_id)
    if not _is_relative_to(target, output_dir):
        raise RuntimeError(f"Unsafe artefact directory outside output root: {target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _workspace_uri(path: Path, output_dir: Path) -> str | None:
    """
    Convert a physical copied path into the stable schema URI IW expects.

    In production, output_dir is /workspace/output and this maps directly.
    In local or Bialobog shell smoke tests, output_dir may be a writable folder
    such as ~/rae_output, while the returned result still exposes the logical
    /workspace/output/... reference used by IW.
    """
    if not _is_relative_to(path, output_dir):
        return None
    rel = path.resolve().relative_to(output_dir.resolve())
    return str(SCHEMA_OUTPUT_ROOT / rel)


def _schema_uri_to_physical_path(uri: str, output_dir: Path) -> Path:
    """
    Map a /workspace/output/... schema URI back to the physical output_dir.

    This lets verification work both inside the real mounted container path and
    in Bialobog/local shell tests where output_dir may be ~/rae_output.
    """
    prefix = str(SCHEMA_OUTPUT_ROOT)
    if uri.startswith(prefix + "/"):
        rel = uri[len(prefix):].lstrip("/")
        return output_dir / rel
    return Path(uri)


def _verify_workspace_references(response: dict[str, Any], output_dir: Path) -> list[str]:
    """
    Verify all schema-ingested /workspace/output references exist before container exit.

    IW currently ingests:
    - performance_metrics.time_series_data_path
    - generated_artifacts.backtest_plots_path
    - diagnostics.raw_log_reference
    """
    warnings: list[str] = []
    candidate_paths = [
        response.get("performance_metrics", {}).get("time_series_data_path"),
        response.get("generated_artifacts", {}).get("backtest_plots_path"),
        response.get("diagnostics", {}).get("raw_log_reference"),
    ]

    generated = response.get("generated_artifacts", {})
    for value in generated.get("new_files", []) or []:
        candidate_paths.append(value)
    for value in generated.get("modified_files", []) or []:
        candidate_paths.append(value)

    for path_str in [p for p in candidate_paths if p]:
        if not _is_schema_workspace_path(path_str):
            warnings.append(f"Ignoring non-schema workspace reference: {path_str}")
            continue
        physical_path = _schema_uri_to_physical_path(path_str, output_dir)
        if not _is_relative_to(physical_path, output_dir):
            warnings.append(f"Workspace reference escapes output_dir: {path_str}")
            continue
        if not physical_path.exists() or not physical_path.is_file():
            warnings.append(f"Workspace reference missing before exit: {path_str}")
    return warnings


def _write_result_json(response: dict[str, Any], output_dir: Path, filename: str = DEFAULT_RESULT_FILENAME) -> str | None:
    """
    Write final runtime response to /workspace/output/result.json.

    Stdout can still print the same JSON as a transition fallback, but IW should
    read this durable file from the mounted output directory.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / _safe_component(filename)
    if not _is_relative_to(target, output_dir):
        raise RuntimeError(f"Unsafe result path outside output root: {target}")
    target.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    return _workspace_uri(target, output_dir)


def _finalise_response(response: dict[str, Any], output_dir: Path, write_result_json: bool) -> dict[str, Any]:
    warnings = _verify_workspace_references(response, output_dir)
    if warnings:
        trace = response.get("execution_summary", {}).get("iteration_traces", [])
        if trace:
            trace[0]["message"] = (trace[0].get("message") or "") + " Reference verification warnings: " + " | ".join(warnings)
    if write_result_json:
        result_target = output_dir / _safe_component(DEFAULT_RESULT_FILENAME)
        result_path = _workspace_uri(result_target, output_dir)
        if result_path:
            data_paths = response.setdefault("generated_artifacts", {}).setdefault("data_paths", {})
            data_paths["runtime_result_path"] = result_path
        _write_result_json(response, output_dir)
    return response


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            target = target_dir / member.filename
            if not _is_relative_to(target, target_dir):
                raise RuntimeError(f"Unsafe ZIP member path blocked: {member.filename}")
        archive.extractall(target_dir)


def _maybe_extract_zip(run_dir: Path, extract_zip: bool) -> list[str]:
    warnings: list[str] = []
    zip_file = _first_file_match(run_dir, FILE_PATTERNS["zip_file"])
    if not zip_file or not extract_zip:
        return warnings
    try:
        _safe_extract_zip(zip_file, run_dir)
    except Exception as exc:
        warnings.append(f"ZIP extraction failed for {zip_file}: {exc}")
    return warnings


def _copy_to_workspace_output(source: Path | None, output_dir: Path, job_name: str, label: str, run_id: str) -> str | None:
    """
    Copy an artefact into durable IW-collected storage and return a stable
    /workspace/output/... reference.

    RAE-15 durable layout:
        /workspace/output/artefacts/<run_id>/<label><suffix>
    """
    if source is None or not source.exists() or not source.is_file():
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dir = _durable_artifact_dir(output_dir, run_id)
    target = target_dir / f"{_safe_component(label)}{source.suffix}"
    if not _is_relative_to(target, output_dir):
        raise RuntimeError(f"Unsafe artefact target outside output root: {target}")
    shutil.copy2(source, target)
    return _workspace_uri(target, output_dir)


def _build_hdfs_paths(
    job_name: str,
    forcluster_dir: Path | None,
    username: str,
    weles_host: str,
    amok_landing_base: Path,
    hdfs_base: Path,
    hdfs_run_id: str | None,
    hdfs_uploaded_path: str | None,
    hdfs_upload_required: bool,
) -> tuple[str | None, str | None, str | None, list[str]]:
    warnings: list[str] = []
    weles_forcluster_path = str(forcluster_dir) if forcluster_dir else None
    safe_job = _safe_component(job_name)
    amok_landing_path = str(amok_landing_base / safe_job) if forcluster_dir else None

    if hdfs_uploaded_path:
        final_hdfs_path = hdfs_uploaded_path
    elif hdfs_upload_required and forcluster_dir:
        run_id = _safe_component(hdfs_run_id or f"{datetime.now(timezone.utc).date().isoformat()}_{safe_job}")
        final_hdfs_path = str(hdfs_base / run_id / safe_job)
        warnings.append("hdfs_uploaded_path is a planned final path; confirm scp and hdfs dfs -put have actually run before treating it as uploaded.")
        warnings.append(f"Run from amok: scp -r {username}@{weles_host}:{weles_forcluster_path} {amok_landing_path}")
        warnings.append(f"Run from amok: hdfs dfs -mkdir -p {hdfs_base / run_id} && hdfs dfs -put {amok_landing_path} {hdfs_base / run_id}")
    else:
        final_hdfs_path = None

    if final_hdfs_path and not _is_schema_hdfs_path(final_hdfs_path):
        warnings.append(f"Ignoring non-schema HDFS path: {final_hdfs_path}")
        final_hdfs_path = None
    return weles_forcluster_path, amok_landing_path, final_hdfs_path, warnings


def get_backtest_artifacts(
    job_name: str,
    *,
    ticket_id: str = "SCRUM-0",
    run_id: str | None = None,
    results_dir: str | None = None,
    output_dir: str | None = None,
    username: str = DEFAULT_USERNAME,
    weles_host: str = DEFAULT_WELES_HOST,
    amok_landing_base: str | None = None,
    hdfs_base: str | None = None,
    hdfs_run_id: str | None = None,
    hdfs_upload_required: bool = False,
    hdfs_uploaded_path: str | None = None,
    extract_zip: bool = False,
    include_success_log: bool = False,
    write_result_json: bool = True,
) -> dict[str, Any]:
    start_time = _utc_now()
    run_id = run_id or f"{_safe_component(job_name)}-{int(datetime.now(timezone.utc).timestamp())}"

    results_root = Path(results_dir).expanduser() if results_dir else DEFAULT_RESULTS_DIR
    output_root = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    amok_base = Path(amok_landing_base).expanduser() if amok_landing_base else DEFAULT_AMOK_LANDING_BASE
    hdfs_root = Path(hdfs_base) if hdfs_base else DEFAULT_HDFS_BASE

    run_dir = _find_run_dir(job_name, results_root)
    if run_dir is None:
        end_time = _utc_now()
        message = f"Could not find run directory for job_name={job_name} under {results_root}"
        response = _runtime_response(
            run_id=run_id,
            ticket_id=ticket_id,
            status="FAILED",
            start_time=start_time,
            end_time=end_time,
            hdfs_upload_required=hdfs_upload_required,
            trace_message=message,
            performance_metrics=_performance_metrics(),
            generated_artifacts=_generated_artifacts(),
            diagnostics=_diagnostics("FAILED", "RUNTIME_ERROR", message, None),
        )
        return _finalise_response(response, output_root, write_result_json)

    warnings = _maybe_extract_zip(run_dir, extract_zip)
    results_table = _first_file_match(run_dir, FILE_PATTERNS["results_table"])
    zqq_file = _first_file_match(run_dir, FILE_PATTERNS["zqq_file"])
    equity_curve_csv = _first_file_match(run_dir, FILE_PATTERNS["equity_curve_csv"])
    plot_or_report = _first_file_match(run_dir, FILE_PATTERNS["plot_or_report"])
    log_file = _first_file_match(run_dir, FILE_PATTERNS["log_file"])
    runme_file = _first_file_match(run_dir, FILE_PATTERNS["runme"])
    forcluster_dir = _first_dir_named(run_dir, "forCluster")

    missing = []
    if results_table is None: missing.append("resultsTable.csv")
    if zqq_file is None: missing.append("ZQQ csv")
    if log_file is None: missing.append("log file")
    if runme_file is None: missing.append("runMe.sh")
    if forcluster_dir is None: missing.append("forCluster directory")

    time_series_data_path = _copy_to_workspace_output(equity_curve_csv, output_root, job_name, "equity_curve", run_id) if equity_curve_csv else None
    backtest_plots_path = _copy_to_workspace_output(plot_or_report, output_root, job_name, "backtest_plot", run_id) if plot_or_report else None
    zqq_source_path = _copy_to_workspace_output(zqq_file, output_root, job_name, "zqq_source", run_id) if zqq_file else None
    results_table_path = _copy_to_workspace_output(results_table, output_root, job_name, "results_table", run_id) if results_table else None
    raw_log_reference = _copy_to_workspace_output(log_file, output_root, job_name, "raw_log", run_id) if log_file else None

    durable_new_files = [
        path for path in [
            time_series_data_path,
            backtest_plots_path,
            zqq_source_path,
            results_table_path,
            raw_log_reference if include_success_log else None,
        ]
        if path
    ]
    workspace_artifact_dir = _workspace_uri(
        output_root / DEFAULT_ARTIFACTS_SUBDIR / _safe_component(run_id),
        output_root,
    )

    weles_forcluster_path, amok_landing_path, final_hdfs_path, hdfs_warnings = _build_hdfs_paths(
        job_name=job_name,
        forcluster_dir=forcluster_dir,
        username=username,
        weles_host=weles_host,
        amok_landing_base=amok_base,
        hdfs_base=hdfs_root,
        hdfs_run_id=hdfs_run_id,
        hdfs_uploaded_path=hdfs_uploaded_path,
        hdfs_upload_required=hdfs_upload_required,
    )
    warnings.extend(hdfs_warnings)

    message = "Backtest artefact discovery completed."
    if missing:
        message += f" Missing optional artefacts: {', '.join(missing)}."
    if warnings:
        message += " Warnings: " + " | ".join(warnings)

    end_time = _utc_now()
    response = _runtime_response(
        run_id=run_id,
        ticket_id=ticket_id,
        status="SUCCESS",
        start_time=start_time,
        end_time=end_time,
        hdfs_upload_required=hdfs_upload_required,
        trace_message=message,
        performance_metrics=_performance_metrics(time_series_data_path),
        generated_artifacts=_generated_artifacts(
            backtest_plots_path=backtest_plots_path,
            weles_forcluster_path=weles_forcluster_path,
            amok_landing_path=amok_landing_path if hdfs_upload_required else None,
            hdfs_uploaded_path=final_hdfs_path,
            new_files=durable_new_files,
            workspace_artifact_dir=workspace_artifact_dir,
        ),
        diagnostics=_diagnostics("SUCCESS", raw_log_reference=raw_log_reference, include_success_log=include_success_log),
    )
    return _finalise_response(response, output_root, write_result_json)


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a schema-strict runtime_response JSON for completed backtest artefacts.")
    parser.add_argument("job_name")
    parser.add_argument("--ticket-id", default="SCRUM-0")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--username", default=DEFAULT_USERNAME)
    parser.add_argument("--weles-host", default=DEFAULT_WELES_HOST)
    parser.add_argument("--amok-landing-base", default=None)
    parser.add_argument("--hdfs-base", default=None)
    parser.add_argument("--hdfs-run-id", default=None)
    parser.add_argument("--hdfs-upload-required", action="store_true")
    parser.add_argument("--hdfs-uploaded-path", default=None)
    parser.add_argument("--extract-zip", action="store_true")
    parser.add_argument("--include-success-log", action="store_true")
    parser.add_argument(
        "--no-write-result-json",
        action="store_true",
        help="Do not write /workspace/output/result.json; stdout JSON is still printed.",
    )
    args = parser.parse_args()

    result = get_backtest_artifacts(
        job_name=args.job_name,
        ticket_id=args.ticket_id,
        run_id=args.run_id,
        results_dir=args.results_dir,
        output_dir=args.output_dir,
        username=args.username,
        weles_host=args.weles_host,
        amok_landing_base=args.amok_landing_base,
        hdfs_base=args.hdfs_base,
        hdfs_run_id=args.hdfs_run_id,
        hdfs_upload_required=args.hdfs_upload_required,
        hdfs_uploaded_path=args.hdfs_uploaded_path,
        extract_zip=args.extract_zip,
        include_success_log=args.include_success_log,
        write_result_json=not args.no_write_result_json,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
