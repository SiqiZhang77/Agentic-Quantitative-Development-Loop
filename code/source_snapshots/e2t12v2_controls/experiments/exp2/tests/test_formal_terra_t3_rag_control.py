from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


FORMAL_DIR = Path(__file__).resolve().parents[1] / "formal-terra-t3-rag-v1"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, FORMAL_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_module("test_exp2_t3_rag_builder", "build_control_package.py")
RUNNER = load_module("test_exp2_t3_rag_runner", "run_formal_pair.py")


def fake_retrieval(_request: dict) -> dict:
    return {
        "enabled": True,
        "status": "empty",
        "query": "frozen synthetic portfolio analytics task",
        "index_id": BUILDER.INDEX_ID,
        "index_sha256": BUILDER.INDEX_SHA256,
        "query_sha256": "1" * 64,
        "retriever_name": "jira_bm25",
        "retriever_version": "v1",
        "corpus_id": "e2-jira-corpus-v1",
        "corpus_sha256": "2" * 64,
        "exclusion_list_id": "e2-exclusions-v1",
        "exclusion_list_sha256": "3" * 64,
        "cutoff_at": "2026-08-01T00:00:00Z",
        "requested_top_k": 5,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 0,
        "memories": [],
    }


def build_private_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    retrieve=fake_retrieval,
) -> Path:
    output = tmp_path / "private-control" / "control-v1"
    monkeypatch.setattr(BUILDER, "require_outside_git", lambda *_args: None)
    result = BUILDER.build_package(
        salt=b"s" * 64,
        output=output,
        heads={
            BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT,
            "main": "a" * 40,
            "quant/older-observation": "b" * 40,
        },
        retrieve=retrieve,
    )
    assert result["formal_run_count"] == 6
    assert result["pair_count"] == 3
    assert result["provider_call_count"] == 0
    return output


def test_public_control_task_is_byte_identical_to_model_visible_task() -> None:
    assert BUILDER.CANONICAL_TASK_PATH.read_bytes() == (
        BUILDER.T3_ROOT / "TASK.md"
    ).read_bytes()


def test_schedule_is_three_counterbalanced_c0_c1_pairs() -> None:
    assert BUILDER.execution_schedule() == [
        (1, "C0"),
        (1, "C1"),
        (2, "C1"),
        (2, "C0"),
        (3, "C0"),
        (3, "C1"),
    ]


def test_private_package_freezes_six_equal_two_attempt_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    agreement = json.loads((output / "agreement.json").read_text(encoding="utf-8"))
    private_map = json.loads(
        (output / "blinding" / "private_condition_map.json").read_text(
            encoding="utf-8"
        )
    )["submissions"]

    assert agreement["formal_run_count"] == 6
    assert agreement["pair_count"] == 3
    assert agreement["architecture_mode"] == "single_agent"
    assert agreement["iteration_controls"] == BUILDER.ITERATION_CONTROLS
    assert agreement["shared_budget"] == BUILDER.SHARED_BUDGET
    assert all("condition" not in record for record in agreement["runs"])
    assert len(list((output / "runs").glob("*/*.json"))) == 18

    requests_by_pair: dict[int, dict[str, dict]] = {}
    c1_hashes = set()
    for record in agreement["runs"]:
        submission_id = record["submission_id"]
        private = private_map[submission_id]
        request = json.loads(
            (output / record["files"]["request"]).read_text(encoding="utf-8")
        )
        parameters = request["execution_objectives"]["parsed_task_parameters"]
        condition = private["condition"]
        assert request["architecture_mode"] == "single_agent"
        assert request["iteration_controls"] == BUILDER.ITERATION_CONTROLS
        assert parameters["model"] == BUILDER.MODEL_ALIAS
        assert parameters["quant_calculator_enabled"] is True
        assert parameters["quant_calculator_schema_path"] == BUILDER.T3_SCHEMA_PATH
        assert parameters["rag_enabled"] is (condition == "C1")
        assert ("retrieval_context" in request) is (condition == "C1")
        requests_by_pair.setdefault(private["replicate"], {})[condition] = request
        if condition == "C1":
            c1_hashes.add(
                BUILDER.sha256_bytes(
                    BUILDER.canonical_bytes(request["retrieval_context"])
                )
            )

    assert c1_hashes == {agreement["retrieval_context_sha256"]}
    for requests in requests_by_pair.values():
        assert BUILDER.pair_projection(requests["C0"]) == BUILDER.pair_projection(
            requests["C1"]
        )


def test_builder_rejects_retrieval_drift_between_c1_replicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def drifting_retrieval(request: dict) -> dict:
        nonlocal calls
        calls += 1
        value = fake_retrieval(request)
        value["query_sha256"] = f"{calls:x}" * 64
        return value

    with pytest.raises(RuntimeError, match="one identical retrieval"):
        build_private_package(
            tmp_path,
            monkeypatch,
            retrieve=drifting_retrieval,
        )


def test_runner_accepts_verified_package_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    state = RUNNER.load_control(
        output,
        heads={BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT},
        image_check=lambda _digest: None,
    )
    assert len(state["runs"]) == 6
    assert {run["condition"] for run in state["runs"]} == {"C0", "C1"}
    assert state["agreement"]["private_t3_scorer_sha256"] == (
        BUILDER.PRIVATE_SCORER_SHA256
    )


def test_runner_rejects_tampered_private_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    with (output / "agreement.json").open("a", encoding="utf-8") as handle:
        handle.write(" ")
    with pytest.raises(RUNNER.FormalLaunchError, match="checksum mismatch"):
        RUNNER.load_control(
            output,
            heads={BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT},
            image_check=lambda _digest: None,
        )


def test_treatment_evidence_requires_the_expected_delivery() -> None:
    c0 = {
        "telemetry": {
            "retrieval": {
                "enabled": False,
                "delivery_mode": "disabled",
                "prompt_injected": False,
            }
        }
    }
    c1 = {
        "telemetry": {
            "retrieval": {
                "enabled": True,
                "delivery_mode": "generator_prompt",
                "prompt_injected": True,
                "status": "ok",
                "index_id": BUILDER.INDEX_ID,
                "index_sha256": BUILDER.INDEX_SHA256,
            }
        }
    }
    assert RUNNER._treatment_evidence(c0, "C0")
    assert RUNNER._treatment_evidence(c1, "C1")
    c1["telemetry"]["retrieval"]["prompt_injected"] = False
    assert not RUNNER._treatment_evidence(c1, "C1")


def test_docker_command_binds_identity_and_negative_ref_hashes(tmp_path: Path) -> None:
    run = {
        "identity_sha256": "1" * 64,
        "negative_ref_manifest_sha256": "2" * 64,
        "deny_ref_set_sha256": "3" * 64,
    }
    command = RUNNER._docker_command(
        run=run,
        input_dir=tmp_path / "input",
        result_dir=tmp_path / "output",
        environment_names=["OPENAI_API_KEY"],
    )
    joined = " ".join(command)
    assert "E3_RUN_IDENTITY_SHA256=" + "1" * 64 in joined
    assert "E3_NEGATIVE_REF_MANIFEST_SHA256=" + "2" * 64 in joined
    assert "E3_NEGATIVE_REF_SET_SHA256=" + "3" * 64 in joined
    assert BUILDER.IMAGE_DIGEST in command


def test_private_scorer_preflight_reports_25_items_without_provider_call() -> None:
    value = RUNNER._preflight_scorer(
        {"private_t3_scorer_sha256": BUILDER.PRIVATE_SCORER_SHA256}
    )
    assert value == {"status": "ready", "provider_call_count": 0, "total_items": 25}


def test_second_pair_accepts_complete_prior_evidence_and_existing_prior_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agreement_sha256 = "a" * 64
    runs = []
    heads = {}
    for replicate in (1, 2, 3):
        for index, condition in enumerate(BUILDER.PAIR_ORDER[replicate], start=1):
            submission_id = f"E2T3S-R{replicate}{index}"
            target = f"quant/test-{submission_id}"
            runs.append(
                {
                    "submission_id": submission_id,
                    "replicate": replicate,
                    "pair_id": f"T3-R{replicate}",
                    "condition": condition,
                    "target_ref": target,
                }
            )
            if replicate == 1:
                evidence = tmp_path / f"{submission_id}-evidence"
                evidence.mkdir(parents=True)
                (evidence / "formal_start_record.json").write_text(
                    json.dumps(
                        {
                            "agreement_sha256": agreement_sha256,
                            "submission_id": submission_id,
                        }
                    )
                )
                (evidence / "launch_result.json").write_text(
                    json.dumps(
                        {
                            "submission_id": submission_id,
                            "complete_observation": True,
                        }
                    )
                )
                heads[target] = "b" * 40

    monkeypatch.setattr(RUNNER, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(RUNNER.BUILDER, "remote_heads", lambda: heads)
    monkeypatch.setattr(
        RUNNER,
        "load_control",
        lambda *_args, **_kwargs: {
            "agreement": {},
            "agreement_sha256": agreement_sha256,
            "runs": runs,
            "heads": heads,
        },
    )
    monkeypatch.setattr(
        RUNNER,
        "_preflight_scorer",
        lambda _agreement: {
            "status": "ready",
            "provider_call_count": 0,
            "total_items": 25,
        },
    )

    state = RUNNER.preflight(tmp_path / "control", replicate=2)

    assert state["preflight_summary"]["preflight_count"] == 2
    assert {run["replicate"] for run in state["selected"]} == {2}
