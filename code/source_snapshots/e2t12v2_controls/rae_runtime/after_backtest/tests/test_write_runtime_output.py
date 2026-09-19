import json
from pathlib import Path

from write_runtime_output import (
    build_final_runtime_response,
    write_runtime_output,
)


def test_write_result_json_and_manifest(tmp_path):
    artefact_source = tmp_path / "resultsTable.csv"
    artefact_source.write_text("metric,value\nsharpe,1.2\n", encoding="utf-8")

    final_result = build_final_runtime_response(
        base_response={
            "schema_version": "1.0",
            "execution_summary": {"ticket_id": "SCRUM-TEST"},
        },
        status="SUCCESS",
    )

    paths = write_runtime_output(
        final_result=final_result,
        output_dir=tmp_path / "output",
        artefact_paths={"results_table": str(artefact_source)},
    )

    result_path = Path(paths["result_json_path"])
    manifest_path = Path(paths["artefacts_manifest_path"])
    copied_artefact = Path(paths["artefacts_dir"]) / "resultsTable.csv"

    assert result_path.exists()
    assert manifest_path.exists()
    assert copied_artefact.exists()

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["execution_summary"]["status"] == "SUCCESS"
    assert result["paths"]["result_json_path"] == str(result_path)
    assert result["artefacts"]["results_table"] == str(copied_artefact)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artefacts"][0]["type"] == "results_table"
    assert manifest["artefacts"][0]["exists"] is True


def test_missing_artefact_is_recorded_without_crash(tmp_path):
    final_result = build_final_runtime_response(status="SUCCESS")

    paths = write_runtime_output(
        final_result=final_result,
        output_dir=tmp_path / "output",
        artefact_paths={"raw_log": str(tmp_path / "missing.log")},
    )

    manifest = json.loads(Path(paths["artefacts_manifest_path"]).read_text(encoding="utf-8"))

    assert manifest["artefacts"][0]["type"] == "raw_log"
    assert manifest["artefacts"][0]["exists"] is False
    assert manifest["artefacts"][0]["mounted_path"] is None


def test_helper_patch_merge():
    patch = {
        "tool_name": "generate_equity_curve",
        "job_name": "demo_job",
        "status": "SUCCESS",
        "performance_metrics_update": {
            "total_return": 0.12,
            "max_drawdown": -0.05,
            "time_series_data_path": "/workspace/output/demo_equity_curve.csv",
        },
        "generated_artifacts": {
            "modified_files": [],
            "new_files": [],
            "backtest_plots_path": "/workspace/output/demo_equity_curve.png",
            "data_paths": {
                "weles_forcluster_path": None,
                "amok_landing_path": None,
                "hdfs_uploaded_path": None,
            },
        },
        "diagnostics_update": {
            "error_code": None,
            "error_message": None,
            "raw_log_reference": None,
        },
    }

    final_result = build_final_runtime_response(
        base_response={"execution_summary": {"ticket_id": "SCRUM-TEST"}},
        helper_patches=[patch],
        status="SUCCESS",
    )

    assert final_result["performance_metrics"]["total_return"] == 0.12
    assert final_result["performance_metrics"]["max_drawdown"] == -0.05
    assert final_result["generated_artifacts"]["backtest_plots_path"] == "/workspace/output/demo_equity_curve.png"
    assert final_result["tool_results"][0]["tool_name"] == "generate_equity_curve"
