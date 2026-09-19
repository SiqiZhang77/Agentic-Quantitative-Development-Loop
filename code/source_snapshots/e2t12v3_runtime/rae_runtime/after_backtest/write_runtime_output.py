#!/usr/bin/env python3
"""
write_runtime_output.py

RAE-08 mounted filesystem output writer.

This helper writes the canonical final runtime response to the mounted output
filesystem so IW can ingest it from a stable path instead of relying only on
stdout. During the transition, callers can still print the same final response
as the final stdout line for Airflow/XCom compatibility.

Default output layout:

/workspace/output/
├── result.json
├── artefacts_manifest.json
└── artefacts/

This module is intentionally small and dependency-free so it can be used by the
RAE runtime after MCP helper patches such as get_backtest_artifacts.py and
generate_equity_curve.py have produced schema-compatible updates.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = Path(os.environ.get("RAE_OUTPUT_DIR", "/workspace/output"))
DEFAULT_RESULT_FILENAME = "result.json"
DEFAULT_ARTEFACTS_DIRNAME = "artefacts"
DEFAULT_MANIFEST_FILENAME = "artefacts_manifest.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ensure_dict(value: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return default or {}


def _read_json_file(path: str | Path) -> dict[str, Any]:
    json_path = Path(path).expanduser()
    with json_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON file must contain an object at top level: {json_path}")
    return data


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _copy_file_to_artefacts(
    source_path: str | Path | None,
    artefacts_dir: Path,
    artefact_type: str,
) -> dict[str, Any]:
    """
    Copy one artefact into the mounted artefacts directory and return a manifest
    entry. Missing files are recorded rather than raising, so the final response
    can still be written and IW can see what is missing.
    """
    entry: dict[str, Any] = {
        "type": artefact_type,
        "source_path": str(source_path) if source_path else None,
        "exists": False,
        "filename": None,
        "mounted_path": None,
        "size_bytes": 0,
    }

    if not source_path:
        entry["missing_reason"] = "no source path supplied"
        return entry

    source = Path(source_path).expanduser()
    if not source.exists() or not source.is_file():
        entry["missing_reason"] = "source file does not exist"
        return entry

    artefacts_dir.mkdir(parents=True, exist_ok=True)
    target = artefacts_dir / source.name

    # Avoid SameFileError when a helper already wrote directly into artefacts_dir.
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)

    entry.update(
        {
            "exists": True,
            "filename": target.name,
            "mounted_path": str(target),
            "size_bytes": target.stat().st_size,
        }
    )
    return entry


def _collect_known_artefact_paths(
    final_result: dict[str, Any],
    helper_patches: list[dict[str, Any]],
) -> dict[str, str]:
    """
    Collect schema-relevant file paths from the final result and helper patches.

    This supports the shapes currently returned by:
    - get_backtest_artifacts.py
    - generate_equity_curve.py
    """
    collected: dict[str, str] = {}

    candidates = [final_result, *helper_patches]
    for payload in candidates:
        generated = _ensure_dict(payload.get("generated_artifacts"))
        diagnostics = _ensure_dict(payload.get("diagnostics_update")) or _ensure_dict(
            payload.get("diagnostics")
        )
        metrics = _ensure_dict(payload.get("performance_metrics_update")) or _ensure_dict(
            payload.get("performance_metrics")
        )
        sources = _ensure_dict(payload.get("artifact_sources"))
        artefacts = _ensure_dict(payload.get("artefacts"))

        path_candidates = {
            "backtest_plot": generated.get("backtest_plots_path"),
            "raw_log": diagnostics.get("raw_log_reference"),
            "time_series_data": metrics.get("time_series_data_path"),
            "results_table": sources.get("copied_results_table_path")
            or sources.get("results_table")
            or artefacts.get("results_table"),
            "zqq_source": sources.get("copied_zqq_path")
            or sources.get("zqq_file")
            or artefacts.get("zqq_file"),
            "equity_curve": artefacts.get("equity_curve"),
            "report_file": artefacts.get("report_file"),
            "log_file": artefacts.get("log_file"),
        }

        for key, value in path_candidates.items():
            if isinstance(value, str) and value:
                collected[key] = value

    return collected


def _merge_helper_patch(final_result: dict[str, Any], patch: dict[str, Any]) -> None:
    """
    Merge a helper result into the canonical final runtime response.

    The helper files return response patches, not the full runtime response.
    This function merges the common patch sections without overwriting unrelated
    final response fields.
    """
    if "generated_artifacts" in patch:
        final_result.setdefault("generated_artifacts", {})
        _deep_update(final_result["generated_artifacts"], patch["generated_artifacts"])

    if "performance_metrics_update" in patch:
        final_result.setdefault("performance_metrics", {})
        _deep_update(final_result["performance_metrics"], patch["performance_metrics_update"])

    if "diagnostics_update" in patch:
        final_result.setdefault("diagnostics", {})
        _deep_update(final_result["diagnostics"], patch["diagnostics_update"])

    final_result.setdefault("tool_results", [])
    final_result["tool_results"].append(
        {
            "tool_name": patch.get("tool_name"),
            "status": patch.get("status"),
            "job_name": patch.get("job_name"),
            "warnings": patch.get("warnings", []),
            "errors": patch.get("errors", []),
            "missing_artifacts": patch.get("missing_artifacts", []),
        }
    )


def _deep_update(target: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
    return target


def build_final_runtime_response(
    base_response: dict[str, Any] | None = None,
    helper_patches: list[dict[str, Any]] | None = None,
    ticket_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """
    Build or update the final runtime response object.
    """
    final_result = dict(base_response or {})
    helper_patches = helper_patches or []

    final_result.setdefault("schema_version", SCHEMA_VERSION)
    final_result.setdefault("execution_summary", {})
    final_result.setdefault("performance_metrics", {})
    final_result.setdefault("generated_artifacts", {})
    final_result.setdefault("diagnostics", {})
    final_result.setdefault("paths", {})

    if ticket_id:
        final_result["execution_summary"].setdefault("ticket_id", ticket_id)
    if status:
        final_result["execution_summary"]["status"] = status
    else:
        final_result["execution_summary"].setdefault("status", "SUCCESS")

    final_result["execution_summary"].setdefault("end_time", _utc_now_iso())
    final_result["execution_summary"].setdefault("iteration_traces", [])

    for patch in helper_patches:
        _merge_helper_patch(final_result, patch)

    return final_result


def write_runtime_output(
    final_result: dict[str, Any],
    output_dir: str | Path | None = None,
    artefact_paths: dict[str, str] | None = None,
) -> dict[str, str]:
    """
    Write result.json, artefacts_manifest.json and copied artefacts to the
    mounted output directory.
    """
    output_root = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    output_root = output_root.expanduser()
    artefacts_dir = output_root / DEFAULT_ARTEFACTS_DIRNAME
    result_json_path = output_root / DEFAULT_RESULT_FILENAME
    manifest_path = output_root / DEFAULT_MANIFEST_FILENAME

    output_root.mkdir(parents=True, exist_ok=True)
    artefacts_dir.mkdir(parents=True, exist_ok=True)

    artefact_paths = artefact_paths or {}
    manifest_entries = [
        _copy_file_to_artefacts(path, artefacts_dir, artefact_type)
        for artefact_type, path in sorted(artefact_paths.items())
    ]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": _utc_now_iso(),
        "output_dir": str(output_root),
        "artefacts_dir": str(artefacts_dir),
        "artefacts": manifest_entries,
    }

    final_result.setdefault("paths", {})
    final_result["paths"].update(
        {
            "output_dir": str(output_root),
            "result_json_path": str(result_json_path),
            "artefacts_dir": str(artefacts_dir),
            "artefacts_manifest_path": str(manifest_path),
        }
    )

    final_result.setdefault("artefacts", {})
    final_result["artefacts"].setdefault("manifest", str(manifest_path))
    for entry in manifest_entries:
        if entry.get("exists") and entry.get("mounted_path"):
            final_result["artefacts"][entry["type"]] = entry["mounted_path"]

    _write_json_file(manifest_path, manifest)
    _write_json_file(result_json_path, final_result)

    return {
        "output_dir": str(output_root),
        "result_json_path": str(result_json_path),
        "artefacts_dir": str(artefacts_dir),
        "artefacts_manifest_path": str(manifest_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write the final RAE runtime response and artefacts to the mounted output directory."
    )
    parser.add_argument(
        "--base-response-json",
        default=None,
        help="Optional JSON file containing an existing final runtime response object.",
    )
    parser.add_argument(
        "--patch-json",
        action="append",
        default=[],
        help="Optional helper patch JSON file. Can be passed multiple times.",
    )
    parser.add_argument("--ticket-id", default=None)
    parser.add_argument("--status", default=None, choices=["SUCCESS", "FAILED", "TIMEOUT"])
    parser.add_argument("--output-dir", default=None, help="Defaults to RAE_OUTPUT_DIR or /workspace/output.")
    parser.add_argument(
        "--stdout-last-line",
        action="store_true",
        help="Print compact final result JSON as the final stdout line for Airflow/XCom compatibility.",
    )
    args = parser.parse_args()

    base_response = _read_json_file(args.base_response_json) if args.base_response_json else None
    helper_patches = [_read_json_file(path) for path in args.patch_json]

    final_result = build_final_runtime_response(
        base_response=base_response,
        helper_patches=helper_patches,
        ticket_id=args.ticket_id,
        status=args.status,
    )

    artefact_paths = _collect_known_artefact_paths(final_result, helper_patches)
    write_runtime_output(
        final_result=final_result,
        output_dir=args.output_dir,
        artefact_paths=artefact_paths,
    )

    if args.stdout_last_line:
        print(json.dumps(final_result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
