from __future__ import annotations

import importlib.util
import json
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_t3_quant_agreement.py"
SPEC = importlib.util.spec_from_file_location("build_t3_quant_agreement", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _remote_heads(source_commit: str) -> dict[str, str]:
    return {
        MODULE.DEFAULT_SOURCE_REF: source_commit,
        "main": "1" * 40,
        "V5-reference": "2" * 40,
        "experiment-2-data": "3" * 40,
        "quant/E2-T1-C0": "4" * 40,
        "quant/E3V2-T1-R2-M0": "5" * 40,
        "quant/E3V2-T2-R1-M1": "6" * 40,
        "quant/E3V2-T1-R1-M0": "7" * 40,
    }


def test_t3_agreement_builds_six_isolated_equal_pair_requests(tmp_path: Path) -> None:
    source_commit = "a" * 40
    output = tmp_path / "agreement"
    result = MODULE.build(
        output,
        source_ref=MODULE.DEFAULT_SOURCE_REF,
        source_commit=source_commit,
        image_digest="sha256:" + "b" * 64,
        remote_heads=_remote_heads(source_commit),
    )

    assert result["run_count"] == 6
    agreement = json.loads((output / "agreement.json").read_text())
    assert agreement["execution_epoch"] == "E3V2T3Q1"
    assert agreement["scope"] == "prospective_T3_only"
    assert agreement["pre_amendment_formal_observations"] == 7
    assert agreement["formal_run_count"] == 6
    assert agreement["rag_enabled"] is False
    assert agreement["shared_budget"] == {"max_calls": 15, "max_tokens": 200000}
    assert agreement["tool_profile"] == {
        "name": "quant_calculate_v1",
        "max_attempted_calls": 4,
        "developer_only": True,
        "counts_toward_model_call_cap": False,
    }
    assert all(item["task_id"] == "T3" for item in agreement["runs"])
    assert all(
        item["target_ref"].startswith("quant/E3V2T3Q1-T3-")
        for item in agreement["runs"]
    )

    by_pair: dict[int, dict[str, dict]] = {}
    for item in agreement["runs"]:
        request = json.loads(
            (output / item["files"]["request"]).read_text(encoding="utf-8")
        )
        parameters = request["execution_objectives"]["parsed_task_parameters"]
        assert parameters["quant_calculator_enabled"] is True
        assert parameters["rag_enabled"] is False
        assert "retrieval_context" not in request
        assert request["repository_details"][0]["allowed_directories"] == [
            "experiments/shared/t3-quant-suite-v1"
        ]
        assert request["execution_objectives"]["target_path"].endswith(
            "/submissions/quant_portfolio_analytics.json"
        )
        by_pair.setdefault(item["replicate"], {})[item["arm"]] = request

    assert set(by_pair) == {1, 2, 3}
    for pair in by_pair.values():
        left = json.loads(json.dumps(pair["M0"]))
        right = json.loads(json.dumps(pair["M1"]))
        left.pop("run_id")
        right.pop("run_id")
        left.pop("architecture_mode")
        right.pop("architecture_mode")
        left_repo = left["repository_details"][0]
        right_repo = right["repository_details"][0]
        left_repo.pop("target_branch")
        right_repo.pop("target_branch")
        left["jira_metadata"].pop("ticket_id")
        right["jira_metadata"].pop("ticket_id")
        left["jira_metadata"]["triggering_comment"].pop("comment_id")
        right["jira_metadata"]["triggering_comment"].pop("comment_id")
        assert left == right


def test_t3_agreement_rejects_remote_source_drift(tmp_path: Path) -> None:
    try:
        MODULE.build(
            tmp_path / "agreement",
            source_ref=MODULE.DEFAULT_SOURCE_REF,
            source_commit="a" * 40,
            image_digest="sha256:" + "b" * 64,
            remote_heads=_remote_heads("c" * 40),
        )
    except RuntimeError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("expected source drift to fail closed")
