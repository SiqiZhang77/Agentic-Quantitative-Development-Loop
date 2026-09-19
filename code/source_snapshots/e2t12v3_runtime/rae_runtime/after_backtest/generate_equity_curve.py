"""
generate_equity_curve.py

It reads a ZQQ/time-series CSV and writes runtime-generated artefacts under
/workspace/output, then emits JSON using the exact top-level field names from
runtime_response.schema.json:

schema_version, run_id, execution_summary, performance_metrics,
generated_artifacts, diagnostics.

Generated schema-ingested outputs:
- performance_metrics.time_series_data_path -> /workspace/output/<job>_equity_curve.csv
- generated_artifacts.backtest_plots_path -> /workspace/output/<job>_equity_curve.png
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("RAE_OUTPUT_DIR", "/workspace/output"))

DATE_CANDIDATES = [
    "date", "datetime", "timestamp", "time", "Date", "Datetime", "Timestamp", "Time",
    "calendarDate", "CalendarDate", "bar_time", "barTime",
]
EQUITY_CANDIDATES = [
    "equity", "Equity", "nav", "NAV", "portfolio_value", "PortfolioValue",
    "portfolioValue", "value", "Value", "total_value", "TotalValue",
    "net_liquidation", "NetLiquidation", "strategy_equity", "StrategyEquity",
]
PNL_CANDIDATES = [
    "pnl", "PnL", "P&L", "cum_pnl", "cumulative_pnl", "CumulativePnL",
    "cumulativePnL", "total_pnl", "TotalPnL",
]
RETURN_CANDIDATES = [
    "return", "returns", "Return", "Returns", "ret", "Ret", "rtn",
    "strategy_return", "StrategyReturn", "log_return", "LogReturn",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return cleaned or "unnamed"


def _is_schema_workspace_path(path: str | None) -> bool:
    return bool(path and path.startswith("/workspace/output/") and ".." not in path)


def _generated_artifacts(backtest_plots_path: str | None = None) -> dict[str, Any]:
    return {
        "modified_files": [],
        "new_files": [],
        "backtest_plots_path": backtest_plots_path if _is_schema_workspace_path(backtest_plots_path) else None,
        "data_paths": {
            "weles_forcluster_path": None,
            "amok_landing_path": None,
            "hdfs_uploaded_path": None,
        },
    }


def _diagnostics(status: str, error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]:
    return {
        "error_code": None if status == "SUCCESS" else (error_code or "RUNTIME_ERROR"),
        "error_message": None if status == "SUCCESS" else (error_message or "Equity curve generation failed."),
        "raw_log_reference": None,
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
                "tool_call": "generate_equity_curve",
                "status": status,
                "message": message,
                "timestamp": end_time,
            }
        ],
        "zero_code_modifications": True,
        "hdfs_upload_required": False,
    }


def _runtime_response(
    *,
    run_id: str,
    ticket_id: str,
    status: str,
    start_time: str,
    end_time: str,
    message: str,
    performance_metrics: dict[str, Any],
    generated_artifacts: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "execution_summary": _execution_summary(ticket_id, status, start_time, end_time, message),
        "performance_metrics": performance_metrics,
        "generated_artifacts": generated_artifacts,
        "diagnostics": diagnostics,
    }


def _performance_metrics(
    *,
    total_return: float | None = None,
    sharpe_ratio: float | None = None,
    max_drawdown: float | None = None,
    alpha: float | None = None,
    beta: float | None = None,
    time_series_data_path: str | None = None,
) -> dict[str, Any]:
    return {
        "total_return": total_return,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": max_drawdown,
        "alpha": alpha,
        "beta": beta,
        "time_series_data_path": time_series_data_path if _is_schema_workspace_path(time_series_data_path) else None,
    }


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    if text.endswith("%"):
        try:
            return float(text[:-1]) / 100.0
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def _pick_column(fieldnames: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in fieldnames:
            return candidate
    lowered = {name.lower(): name for name in fieldnames}
    for candidate in candidates:
        found = lowered.get(candidate.lower())
        if found:
            return found
    for name in fieldnames:
        compact_name = name.lower().replace("_", "")
        for candidate in candidates:
            compact_candidate = candidate.lower().replace("_", "")
            if compact_candidate and compact_candidate in compact_name:
                return name
    return None


def _sniff_dialect(path: Path) -> csv.Dialect:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    dialect = _sniff_dialect(path)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, dialect=dialect)
        fieldnames = reader.fieldnames or []
        rows = [row for row in reader]
    if not fieldnames:
        raise ValueError(f"No header row found in CSV: {path}")
    if not rows:
        raise ValueError(f"No data rows found in CSV: {path}")
    return fieldnames, rows


def _scale_return(value: float, return_scale: str) -> float:
    if return_scale == "decimal":
        return value
    if return_scale == "percent":
        return value / 100.0
    if return_scale == "auto":
        return value / 100.0 if abs(value) > 1.5 else value
    raise ValueError("return_scale must be one of: decimal, percent, auto")


def _build_curve(fieldnames: list[str], rows: list[dict[str, str]], initial_capital: float, return_scale: str) -> tuple[list[dict[str, Any]], dict[str, str | None]]:
    date_col = _pick_column(fieldnames, DATE_CANDIDATES)
    equity_col = _pick_column(fieldnames, EQUITY_CANDIDATES)
    pnl_col = _pick_column(fieldnames, PNL_CANDIDATES)
    return_col = _pick_column(fieldnames, RETURN_CANDIDATES)
    selected = {"date_column": date_col, "equity_column": equity_col, "pnl_column": pnl_col, "return_column": return_col}
    if equity_col is None and pnl_col is None and return_col is None:
        raise ValueError(f"Could not infer equity, PnL, or return column. Available columns: {fieldnames}")

    curve: list[dict[str, Any]] = []
    running_equity = initial_capital
    for idx, row in enumerate(rows):
        timestamp = row.get(date_col) if date_col else str(idx)
        equity: float | None = None
        if equity_col:
            equity = _to_float(row.get(equity_col))
        elif pnl_col:
            pnl = _to_float(row.get(pnl_col))
            if pnl is not None:
                equity = initial_capital + pnl
        elif return_col:
            raw_return = _to_float(row.get(return_col))
            if raw_return is not None:
                ret = _scale_return(raw_return, return_scale)
                running_equity *= math.exp(ret) if "log" in return_col.lower() else 1.0 + ret
                equity = running_equity
        if equity is not None and math.isfinite(equity):
            curve.append({"source_index": idx, "timestamp": timestamp, "equity": equity})
    if not curve:
        raise ValueError("No valid equity curve points could be generated.")
    return curve, selected


def _write_curve_csv(curve: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_index", "timestamp", "equity"])
        writer.writeheader()
        writer.writerows(curve)


def _write_curve_png(curve: list[dict[str, Any]], path: Path) -> tuple[bool, str | None]:
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover
        return False, f"matplotlib unavailable, PNG skipped: {exc}"
    path.parent.mkdir(parents=True, exist_ok=True)
    x = [point["source_index"] for point in curve]
    y = [point["equity"] for point in curve]
    plt.figure()
    plt.plot(x, y)
    plt.xlabel("Observation")
    plt.ylabel("Equity")
    plt.title("Equity Curve")
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return True, None


def _derive_metrics(curve: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    values = [float(point["equity"]) for point in curve]
    total_return = None if not values or values[0] == 0 else (values[-1] / values[0]) - 1.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak != 0:
            max_drawdown = min(max_drawdown, (value / peak) - 1.0)
    return total_return, max_drawdown


def generate_equity_curve(
    job_name: str,
    zqq_file_path: str,
    *,
    ticket_id: str = "SCRUM-0",
    run_id: str | None = None,
    output_dir: str | None = None,
    output_format: str = "both",
    initial_capital: float = 10000.0,
    return_scale: str = "auto",
) -> dict[str, Any]:
    start_time = _utc_now()
    run_id = run_id or f"{_safe_component(job_name)}-{int(datetime.now(timezone.utc).timestamp())}"
    if output_format not in {"csv", "png", "both"}:
        end_time = _utc_now()
        message = "output_format must be one of: csv, png, both"
        return _runtime_response(run_id=run_id, ticket_id=ticket_id, status="FAILED", start_time=start_time, end_time=end_time, message=message, performance_metrics=_performance_metrics(), generated_artifacts=_generated_artifacts(), diagnostics=_diagnostics("FAILED", "PAYLOAD_VALIDATION_ERROR", message))
    if return_scale not in {"decimal", "percent", "auto"}:
        end_time = _utc_now()
        message = "return_scale must be one of: decimal, percent, auto"
        return _runtime_response(run_id=run_id, ticket_id=ticket_id, status="FAILED", start_time=start_time, end_time=end_time, message=message, performance_metrics=_performance_metrics(), generated_artifacts=_generated_artifacts(), diagnostics=_diagnostics("FAILED", "PAYLOAD_VALIDATION_ERROR", message))

    output_root = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    zqq_path = Path(zqq_file_path).expanduser()
    if not zqq_path.exists() or not zqq_path.is_file():
        end_time = _utc_now()
        message = f"ZQQ/time-series CSV not found: {zqq_path}"
        return _runtime_response(run_id=run_id, ticket_id=ticket_id, status="FAILED", start_time=start_time, end_time=end_time, message=message, performance_metrics=_performance_metrics(), generated_artifacts=_generated_artifacts(), diagnostics=_diagnostics("FAILED", "RUNTIME_ERROR", message))

    try:
        fieldnames, rows = _read_csv(zqq_path)
        curve, selected = _build_curve(fieldnames, rows, initial_capital, return_scale)
    except Exception as exc:
        end_time = _utc_now()
        message = str(exc)
        return _runtime_response(run_id=run_id, ticket_id=ticket_id, status="FAILED", start_time=start_time, end_time=end_time, message=message, performance_metrics=_performance_metrics(), generated_artifacts=_generated_artifacts(), diagnostics=_diagnostics("FAILED", "RUNTIME_ERROR", message))

    safe_job = _safe_component(job_name)
    csv_path = output_root / f"{safe_job}_equity_curve.csv"
    png_path = output_root / f"{safe_job}_equity_curve.png"
    time_series_data_path: str | None = None
    backtest_plots_path: str | None = None
    warnings: list[str] = []

    if output_format in {"csv", "both"}:
        _write_curve_csv(curve, csv_path)
        csv_path_str = str(csv_path)
        time_series_data_path = csv_path_str if _is_schema_workspace_path(csv_path_str) else None
    if output_format in {"png", "both"}:
        wrote_png, warning = _write_curve_png(curve, png_path)
        if warning:
            warnings.append(warning)
        if wrote_png:
            png_path_str = str(png_path)
            backtest_plots_path = png_path_str if _is_schema_workspace_path(png_path_str) else None

    total_return, max_drawdown = _derive_metrics(curve)
    message = f"Generated equity curve with {len(curve)} points. Selected columns: {selected}."
    if warnings:
        message += " Warnings: " + " | ".join(warnings)
    if output_format in {"csv", "both"} and time_series_data_path is None:
        message += " CSV was written outside /workspace/output, so time_series_data_path is null in the strict schema response."
    if output_format in {"png", "both"} and backtest_plots_path is None:
        message += " PNG was not written under /workspace/output or was skipped, so backtest_plots_path is null."

    end_time = _utc_now()
    return _runtime_response(
        run_id=run_id,
        ticket_id=ticket_id,
        status="SUCCESS",
        start_time=start_time,
        end_time=end_time,
        message=message,
        performance_metrics=_performance_metrics(total_return=total_return, sharpe_ratio=None, max_drawdown=max_drawdown, alpha=None, beta=None, time_series_data_path=time_series_data_path),
        generated_artifacts=_generated_artifacts(backtest_plots_path=backtest_plots_path),
        diagnostics=_diagnostics("SUCCESS"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit a schema-strict runtime_response JSON after generating equity curve artefacts.")
    parser.add_argument("job_name")
    parser.add_argument("zqq_file_path")
    parser.add_argument("--ticket-id", default="SCRUM-0")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--output-format", choices=["csv", "png", "both"], default="both")
    parser.add_argument("--initial-capital", type=float, default=10000.0)
    parser.add_argument("--return-scale", choices=["decimal", "percent", "auto"], default="auto")
    args = parser.parse_args()
    result = generate_equity_curve(job_name=args.job_name, zqq_file_path=args.zqq_file_path, ticket_id=args.ticket_id, run_id=args.run_id, output_dir=args.output_dir, output_format=args.output_format, initial_capital=args.initial_capital, return_scale=args.return_scale)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
