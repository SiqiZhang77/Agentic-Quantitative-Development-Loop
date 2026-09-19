"""
get_backtest_results.py

This tool should run AFTER:
1) the backtest has completed;
2) get_backtest_artifacts.py has located artefacts, if available;
3) generate_equity_curve.py has written equity curve CSV/PNG under /workspace/output, if available.

It parses resultsTable.csv, merges schema-compatible outputs from the previous
artifact/equity tools, and emits a compact runtime_response.schema.json object.

Top-level output fields are exactly:
- schema_version
- run_id
- execution_summary
- performance_metrics
- generated_artifacts
- diagnostics

Important IW/Airflow ingestion rules:
- Airflow ingests only these response fields:
  performance_metrics.time_series_data_path
  generated_artifacts.backtest_plots_path
  diagnostics.raw_log_reference
- Runtime-generated files must be under /workspace/output.
- HDFS uploaded paths must be under /shared/model-logs.
- Diagnostic logs are not included on successful runs unless explicitly requested.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DEFAULT_USERNAME = os.environ.get("BIALOBOG_USERNAME", "zczqiav")
DEFAULT_RESULTS_DIR = Path(os.environ.get("BIALOBOG_RESULTS_DIR", f"/home/{DEFAULT_USERNAME}/simulation-results"))
DEFAULT_OUTPUT_DIR = Path(os.environ.get("RAE_OUTPUT_DIR", "/workspace/output"))

ALLOWED_STATUS = {"SUCCESS", "FAILED", "TIMEOUT"}

METRIC_ALIASES = {
    "total_return": [
        "total_return", "total return", "totalreturn", "cumulative return",
        "cumulative_return", "net return", "net_return", "return", "returns",
        "profit",
    ],
    "sharpe_ratio": [
        "sharpe", "sharpe ratio", "sharpe_ratio", "sharperatio",
    ],
    "max_drawdown": [
        "max drawdown", "maximum drawdown", "max_drawdown", "maximum_drawdown",
        "maxdd", "mdd", "drawdown",
    ],
    "alpha": [
        "alpha", "annualised alpha", "annualized alpha", "strategy alpha",
    ],
    "beta": [
        "beta", "market beta", "strategy beta",
    ],
    # Parsed for trace message only; strict schema has no fields for them.
    "sortino_ratio": [
        "sortino", "sortino ratio", "sortino_ratio", "sortinoratio",
    ],
    "annualised_return": [
        "annualised return", "annualized return", "annualised_return",
        "annualized_return", "annual return", "annual_return", "cagr",
    ],
    "volatility": [
        "volatility", "annualised volatility", "annualized volatility",
        "annualised_volatility", "annualized_volatility", "vol",
    ],
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    cleaned = cleaned.strip("._-")
    return cleaned or "unnamed"


def _normalise_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[_\-/]+", " ", text)
    text = re.sub(r"[^a-z0-9. ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _workspace_path(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    if path.startswith("/workspace/output/") and ".." not in path:
        return path
    return None


def _hdfs_path(path: Any) -> str | None:
    if not isinstance(path, str):
        return None
    if path.startswith("/shared/model-logs/") and ".." not in path:
        return path
    return None


def _empty_generated_artifacts() -> dict[str, Any]:
    return {
        "modified_files": [],
        "new_files": [],
        "backtest_plots_path": None,
        "data_paths": {
            "weles_forcluster_path": None,
            "amok_landing_path": None,
            "hdfs_uploaded_path": None,
        },
    }


def _empty_performance_metrics() -> dict[str, Any]:
    return {
        "total_return": None,
        "sharpe_ratio": None,
        "max_drawdown": None,
        "alpha": None,
        "beta": None,
        "time_series_data_path": None,
    }


def _diagnostics(status: str, error_code: str | None = None, error_message: str | None = None, raw_log_reference: str | None = None) -> dict[str, Any]:
    return {
        "error_code": None if status == "SUCCESS" else (error_code or "RUNTIME_ERROR"),
        "error_message": None if status == "SUCCESS" else (error_message or "Backtest result parsing failed."),
        "raw_log_reference": _workspace_path(raw_log_reference),
    }


def _execution_summary(ticket_id: str, status: str, start_time: str, end_time: str, message: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket_id,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "iteration_traces": [
            {
                "iteration": 1,
                "agent": "mcp_backtester",
                "tool_call": "get_backtest_results",
                "status": status,
                "message": message[:900],
                "timestamp": end_time,
            }
        ],
        "zero_code_modifications": True,
        "hdfs_upload_required": False,
    }


def _runtime_response(
    run_id: str,
    ticket_id: str,
    status: str,
    start_time: str,
    end_time: str,
    message: str,
    performance_metrics: dict[str, Any] | None = None,
    generated_artifacts: dict[str, Any] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "execution_summary": _execution_summary(ticket_id, status, start_time, end_time, message),
        "performance_metrics": performance_metrics if performance_metrics is not None else _empty_performance_metrics(),
        "generated_artifacts": generated_artifacts if generated_artifacts is not None else _empty_generated_artifacts(),
        "diagnostics": diagnostics if diagnostics is not None else _diagnostics(status),
    }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as archive:
        for member in archive.infolist():
            target = target_dir / member.filename
            if not _is_relative_to(target, target_dir):
                raise RuntimeError(f"Unsafe ZIP member path blocked: {member.filename}")
        archive.extractall(target_dir)


def _first_file_match(base: Path, patterns: list[str]) -> Path | None:
    if not base.exists():
        return None
    for pattern in patterns:
        matches = [p for p in base.rglob(pattern) if p.is_file()]
        if matches:
            return max(matches, key=lambda p: p.stat().st_mtime)
    return None


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
    if zip_candidates:
        return max(zip_candidates, key=lambda p: p.stat().st_mtime).parent

    return None


def _maybe_extract_zip(run_dir: Path, extract_zip: bool) -> str | None:
    if not extract_zip:
        return None
    zip_file = _first_file_match(run_dir, ["*.zip"])
    if zip_file is None:
        return None
    _safe_extract_zip(zip_file, run_dir)
    return str(zip_file)


def _to_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "-"}:
        return None
    text = text.replace(",", "")
    negative_parentheses = text.startswith("(") and text.endswith(")")
    if negative_parentheses:
        text = text[1:-1]
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    if not match:
        return None
    try:
        number = float(match.group(0))
    except ValueError:
        return None
    if negative_parentheses:
        number = -number
    if is_percent:
        number /= 100.0
    return number if math.isfinite(number) else None


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]], list[list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = [row for row in csv.reader(handle, dialect=dialect) if any(str(c).strip() for c in row)]

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        fieldnames = reader.fieldnames or []
        dict_rows = [row for row in reader]

    return fieldnames, dict_rows, raw_rows


def _metric_name_to_key(name: Any) -> str | None:
    normalised = _normalise_key(name)
    compact = normalised.replace(" ", "")

    for key, aliases in METRIC_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalise_key(alias)
            alias_compact = alias_norm.replace(" ", "")
            if normalised == alias_norm or compact == alias_compact:
                return key

    for key, aliases in METRIC_ALIASES.items():
        for alias in aliases:
            alias_compact = _normalise_key(alias).replace(" ", "")
            if alias_compact and alias_compact in compact:
                return key

    return None


def _parse_key_value_table(fieldnames: list[str], rows: list[dict[str, str]]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    if not fieldnames or not rows:
        return parsed

    lower_fields = {f.lower().strip(): f for f in fieldnames}
    metric_field = next((lower_fields[c] for c in ["metric", "name", "measure", "stat", "statistic", "field"] if c in lower_fields), None)
    value_field = next((lower_fields[c] for c in ["value", "result", "metric_value", "number"] if c in lower_fields), None)

    if not metric_field or not value_field:
        return parsed

    for row in rows:
        key = _metric_name_to_key(row.get(metric_field))
        value = _to_number(row.get(value_field))
        if key and value is not None:
            parsed[key] = value
    return parsed


def _parse_wide_table(fieldnames: list[str], rows: list[dict[str, str]]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    if not fieldnames or not rows:
        return parsed
    first_row = rows[0]
    for field in fieldnames:
        key = _metric_name_to_key(field)
        value = _to_number(first_row.get(field))
        if key and value is not None:
            parsed[key] = value
    return parsed


def _parse_raw_rows(raw_rows: list[list[str]]) -> dict[str, float]:
    parsed: dict[str, float] = {}
    for row in raw_rows:
        if len(row) < 2:
            continue
        for i, cell in enumerate(row):
            key = _metric_name_to_key(cell)
            if not key:
                continue
            for candidate in row[i + 1:] + row[:i]:
                value = _to_number(candidate)
                if value is not None:
                    parsed[key] = value
                    break
    return parsed


def _parse_results_table(path: Path) -> tuple[dict[str, float], dict[str, float]]:
    fieldnames, dict_rows, raw_rows = _read_csv_rows(path)
    if len(raw_rows) < 2:
        raise ValueError("resultsTable.csv has a header but no data row")

    parsed: dict[str, float] = {}
    parsed.update(_parse_key_value_table(fieldnames, dict_rows))
    parsed.update(_parse_wide_table(fieldnames, dict_rows))

    # Only use the loose row scanner for non-wide/key-value tables. On the real
    # engine's wide resultsTable.csv, running it over the header row can extract
    # fake numbers from config column names such as GROWTH_MAXEPS5YGROWTHRATE.
    if not parsed:
        parsed.update(_parse_raw_rows(raw_rows))

    schema_keys = {"total_return", "sharpe_ratio", "max_drawdown", "alpha", "beta"}
    schema_metrics = {k: v for k, v in parsed.items() if k in schema_keys}
    extra_metrics = {k: v for k, v in parsed.items() if k not in schema_keys}
    return schema_metrics, extra_metrics


def _load_json_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    json_path = Path(path).expanduser()
    if not json_path.exists() or not json_path.is_file():
        return {}
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge_previous_response_fields(base_generated_artifacts: dict[str, Any], json_paths: list[str]) -> tuple[dict[str, Any], str | None, str | None]:
    """
    Merge schema-compatible paths from get_backtest_artifacts / generate_equity_curve outputs.

    Returns:
      generated_artifacts, time_series_data_path, raw_log_reference
    """
    generated_artifacts = json.loads(json.dumps(base_generated_artifacts))
    time_series_data_path: str | None = None
    raw_log_reference: str | None = None

    for json_path in json_paths:
        payload = _load_json_file(json_path)
        if not payload:
            continue

        perf = payload.get("performance_metrics") or {}
        candidate_ts = _workspace_path(perf.get("time_series_data_path"))
        if candidate_ts:
            time_series_data_path = candidate_ts

        gen = payload.get("generated_artifacts") or {}
        candidate_plot = _workspace_path(gen.get("backtest_plots_path"))
        if candidate_plot:
            generated_artifacts["backtest_plots_path"] = candidate_plot

        data_paths = gen.get("data_paths") or {}
        if isinstance(data_paths.get("weles_forcluster_path"), str):
            generated_artifacts["data_paths"]["weles_forcluster_path"] = data_paths["weles_forcluster_path"]
        if isinstance(data_paths.get("amok_landing_path"), str):
            generated_artifacts["data_paths"]["amok_landing_path"] = data_paths["amok_landing_path"]
        candidate_hdfs = _hdfs_path(data_paths.get("hdfs_uploaded_path"))
        if candidate_hdfs:
            generated_artifacts["data_paths"]["hdfs_uploaded_path"] = candidate_hdfs

        diag = payload.get("diagnostics") or {}
        candidate_log = _workspace_path(diag.get("raw_log_reference"))
        if candidate_log:
            raw_log_reference = candidate_log

    return generated_artifacts, time_series_data_path, raw_log_reference


def _metric_summary(schema_metrics: dict[str, float], extra_metrics: dict[str, float], table_path: Path) -> str:
    shown = []
    for key in ["total_return", "sharpe_ratio", "max_drawdown", "alpha", "beta"]:
        if key in schema_metrics:
            shown.append(f"{key}={schema_metrics[key]}")
    for key in ["sortino_ratio", "annualised_return", "volatility"]:
        if key in extra_metrics:
            shown.append(f"{key}={extra_metrics[key]}")
    metric_part = ", ".join(shown) if shown else "no recognised metrics"
    return f"Parsed {table_path.name}: {metric_part}"


def get_backtest_results(
    job_name: str,
    *,
    ticket_id: str = "SCRUM-0",
    run_id: str | None = None,
    results_dir: str | None = None,
    results_table_path: str | None = None,
    merge_response_json: list[str] | None = None,
    time_series_data_path: str | None = None,
    raw_log_reference: str | None = None,
    include_success_log: bool = False,
    extract_zip: bool = False,
) -> dict[str, Any]:
    start_time = _utc_now()
    run_id = run_id or f"{_safe_component(job_name)}-{int(datetime.now(timezone.utc).timestamp())}"
    results_root = Path(results_dir).expanduser() if results_dir else DEFAULT_RESULTS_DIR
    merge_response_json = merge_response_json or []

    run_dir: Path | None = None
    table_path: Path | None = None

    try:
        if results_table_path:
            table_path = Path(results_table_path).expanduser()
            run_dir = table_path.parent if table_path.exists() else None
        else:
            run_dir = _find_run_dir(job_name, results_root)
            if run_dir:
                _maybe_extract_zip(run_dir, extract_zip)
                table_path = _first_file_match(run_dir, ["resultsTable.csv", "*resultsTable*.csv"])

        if run_dir is None:
            raise FileNotFoundError(f"Could not find completed run directory for job_name={job_name} under {results_root}")
        if table_path is None or not table_path.exists() or not table_path.is_file():
            raise FileNotFoundError(f"Could not find resultsTable.csv for job_name={job_name} under {run_dir}")

        schema_metrics, extra_metrics = _parse_results_table(table_path)
        missing_metrics = [
            key for key in ("total_return", "sharpe_ratio", "max_drawdown")
            if schema_metrics.get(key) is None
        ]
        if missing_metrics:
            raise ValueError(
                "resultsTable.csv did not contain required metrics: "
                + ", ".join(missing_metrics)
            )
    except Exception as exc:
        end_time = _utc_now()
        return _runtime_response(
            run_id=run_id,
            ticket_id=ticket_id,
            status="FAILED",
            start_time=start_time,
            end_time=end_time,
            message=str(exc),
            diagnostics=_diagnostics("FAILED", "RUNTIME_ERROR", str(exc), _workspace_path(raw_log_reference)),
        )

    generated_artifacts, merged_ts_path, merged_log_ref = _merge_previous_response_fields(
        _empty_generated_artifacts(),
        merge_response_json,
    )

    final_time_series_path = _workspace_path(time_series_data_path) or merged_ts_path
    final_raw_log_reference = _workspace_path(raw_log_reference) or merged_log_ref

    performance_metrics = _empty_performance_metrics()
    performance_metrics.update({
        "total_return": schema_metrics.get("total_return"),
        "sharpe_ratio": schema_metrics.get("sharpe_ratio"),
        "max_drawdown": schema_metrics.get("max_drawdown"),
        "alpha": schema_metrics.get("alpha"),
        "beta": schema_metrics.get("beta"),
        "time_series_data_path": final_time_series_path,
    })

    message = _metric_summary(schema_metrics, extra_metrics, table_path)
    if final_time_series_path:
        message += f"; time_series_data_path={final_time_series_path}"
    if generated_artifacts.get("backtest_plots_path"):
        message += f"; backtest_plots_path={generated_artifacts['backtest_plots_path']}"
    if generated_artifacts["data_paths"].get("hdfs_uploaded_path"):
        message += f"; hdfs_uploaded_path={generated_artifacts['data_paths']['hdfs_uploaded_path']}"

    # IW avoids diagnostic log uploads on successful runs. Keep the field null
    # unless the caller explicitly wants it for debugging.
    success_log = final_raw_log_reference if include_success_log else None

    end_time = _utc_now()
    return _runtime_response(
        run_id=run_id,
        ticket_id=ticket_id,
        status="SUCCESS",
        start_time=start_time,
        end_time=end_time,
        message=message,
        performance_metrics=performance_metrics,
        generated_artifacts=generated_artifacts,
        diagnostics=_diagnostics("SUCCESS", raw_log_reference=success_log),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse resultsTable.csv and emit schema-strict runtime_response JSON.")
    parser.add_argument("job_name")
    parser.add_argument("--ticket-id", default="SCRUM-0")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--results-dir", default=None)
    parser.add_argument("--results-table-path", default=None)
    parser.add_argument("--merge-response-json", action="append", default=[], help="JSON output from get_backtest_artifacts.py or generate_equity_curve.py. Can be repeated.")
    parser.add_argument("--time-series-data-path", default=None)
    parser.add_argument("--raw-log-reference", default=None)
    parser.add_argument("--include-success-log", action="store_true")
    parser.add_argument("--extract-zip", action="store_true")
    args = parser.parse_args()

    result = get_backtest_results(
        job_name=args.job_name,
        ticket_id=args.ticket_id,
        run_id=args.run_id,
        results_dir=args.results_dir,
        results_table_path=args.results_table_path,
        merge_response_json=args.merge_response_json,
        time_series_data_path=args.time_series_data_path,
        raw_log_reference=args.raw_log_reference,
        include_success_log=args.include_success_log,
        extract_zip=args.extract_zip,
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
