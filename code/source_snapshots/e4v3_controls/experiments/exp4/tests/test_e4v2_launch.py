from __future__ import annotations

import json
import hashlib
from pathlib import Path

from experiments.exp4 import build_e4v2_agreement
from experiments.exp4 import build_e4v2_launch_package as launch_builder
from experiments.exp4 import identity_e4v2 as identity, run_e4v2_batch, run_e4v2_observation
from experiments.exp4.tests.test_e4v2_controls import _resolved_config


def _context() -> dict:
    return json.loads(
        (
            identity.REPOSITORY_ROOT
            / "experiments/exp4/tests/fixtures/retrieval_context.json"
        ).read_text(encoding="utf-8")
    )


def test_request_combines_manager_star_and_rag_without_changing_budget() -> None:
    config = _resolved_config()
    run_identity = next(
        row
        for row in identity.build_block_identities(config, "T3", 1)
        if row["condition_code"] == "M1R1"
    )
    request = launch_builder._request(
        run_identity=run_identity,
        sequence=1,
        task_text="frozen task",
        config=config,
        retrieval_context=_context(),
    )
    parameters = request["execution_objectives"]["parsed_task_parameters"]
    assert request["architecture_mode"] == "manager_star"
    assert parameters["rag_enabled"] is True
    assert parameters["formal_execution_contract"] == (
        "factorial_rag_architecture_v1"
    )
    assert parameters["rag_delivery_policy"] == "all_model_stages_v1"
    assert request["retrieval_context"] == _context()
    assert request["iteration_controls"]["max_iterations"] == 2
    assert request["iteration_controls"]["max_agent_turns"] == 20
    assert request["iteration_controls"]["max_token_budget_per_run"] == 1_000_000


def test_rag_off_request_omits_context() -> None:
    config = _resolved_config()
    run_identity = next(
        row
        for row in identity.build_block_identities(config, "T3", 1)
        if row["condition_code"] == "M1R0"
    )
    request = launch_builder._request(
        run_identity=run_identity,
        sequence=1,
        task_text="frozen task",
        config=config,
        retrieval_context=_context(),
    )
    assert request["architecture_mode"] == "manager_star"
    assert request["execution_objectives"]["parsed_task_parameters"][
        "rag_enabled"
    ] is False
    assert "retrieval_context" not in request


def test_negative_manifest_denies_every_prior_shared_runtime_ref() -> None:
    config = _resolved_config()
    run_identity = identity.build_block_identities(config, "T3", 1)[0]
    all_runs = [
        row
        for replicate in range(1, 6)
        for row in identity.build_block_identities(config, "T3", replicate)
    ]

    manifest, _manifest_sha, _deny_sha = launch_builder._negative_manifest(
        run_identity, all_runs
    )

    prior = set(manifest["repositories"][0]["deny_refs"]["prior_study_refs"])
    assert {
        f"exp/shared-t3-runtime-v{version}" for version in range(1, 7)
    }.issubset(prior)
    assert config["source"]["source_ref"] not in prior


def test_batch_records_one_launch_error_and_continues_remaining_runs(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[str] = []
    states = [
        {
            "run_id": f"run-{replicate}",
            "condition_code": "M1R1",
            "rag_enabled": True,
            "architecture_mode": "manager_star",
        }
        for replicate in range(1, 6)
    ]
    monkeypatch.setattr(
        run_e4v2_batch,
        "preflight",
        lambda package, condition: {
            "status": "ready",
            "agreement_sha256": "a" * 64,
            "launch_package_sha256": "b" * 64,
            "run_ids": [state["run_id"] for state in states],
            "states": states,
        },
    )
    monkeypatch.setattr(
        run_e4v2_batch,
        "_execution_environment",
        lambda condition: {"OPENAI_API_KEY": "sk-test"},
    )

    def fake_execute(state):
        calls.append(state["run_id"])
        if state["run_id"] == "run-2":
            raise RuntimeError("recorded failure")
        return {
            "run_id": state["run_id"],
            "result_status": "failed" if state["run_id"] == "run-3" else "succeeded",
            "output_directory": str(tmp_path / state["run_id"]),
        }

    monkeypatch.setattr(run_e4v2_batch.observation, "execute", fake_execute)
    monkeypatch.setattr(
        run_e4v2_batch.shared_scoring,
        "_score",
        lambda state, launch: {
            "run_id": state["run_id"],
            "architecture_mode": "manager_star",
            "correct_items": 10,
            "total_items": 25,
        },
    )
    monkeypatch.setattr(run_e4v2_batch, "RESULT_ROOT", tmp_path / "summaries")
    result = run_e4v2_batch.run_batch(Path("unused"), "M1R1", True)
    assert calls == [f"run-{replicate}" for replicate in range(1, 6)]
    assert result["status"] == "completed_with_recorded_errors"
    assert result["launched_observation_count"] == 4
    assert result["scored_observation_count"] == 4
    assert result["workflow_succeeded_count"] == 3
    assert result["workflow_failed_count"] == 1
    assert [row["run_id"] for row in result["launch_errors"]] == ["run-2"]


def test_agreement_launch_package_and_observation_preflight_connect(
    monkeypatch, tmp_path: Path
) -> None:
    config = _resolved_config()
    config["execution"]["agreement_generation_enabled"] = True
    freeze = tmp_path / "freeze"
    freeze.mkdir()
    context = _context()
    context_bytes = json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    context_path = freeze / "retrieval_context.json"
    context_path.write_bytes(context_bytes)
    binding = config["retrieval_binding"]["task_query_context"]["T3"]
    binding.update(
        retrieval_context_path=str(context_path),
        retrieval_context_sha256=hashlib.sha256(context_bytes).hexdigest(),
        retrieval_context_byte_count=len(context_bytes),
        memory_count=1,
    )
    request_schema = (
        identity.REPOSITORY_ROOT
        / "rae_runtime/sandbox/schemas/runtime_request.schema.json"
    )
    config["runtime"]["runtime_request_schema_sha256"] = hashlib.sha256(
        request_schema.read_bytes()
    ).hexdigest()
    (freeze / "freeze_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert identity.readiness_issues(config) == []
    agreement_dir = tmp_path / "agreement"
    agreement = build_e4v2_agreement.build_agreement(config, agreement_dir)
    assert agreement["observation_count"] == 20
    monkeypatch.setattr(
        launch_builder,
        "_launch_control",
        lambda ref, commit: {
            "repository": "bankingscience/BSLAgenticQuantDevLoop",
            "control_ref": ref,
            "control_commit": commit,
            "files_sha256": {"synthetic": "a" * 64},
            "files_fingerprint_sha256": "b" * 64,
        },
    )
    launch_dir = tmp_path / "launch"
    manifest = launch_builder.build(
        freeze_package=freeze,
        agreement_dir=agreement_dir,
        control_ref=config["control"]["control_ref"],
        control_commit=config["control"]["control_commit"],
        output=launch_dir,
    )
    assert len(manifest["runs"]) == 20
    assert sum(row["condition_code"] == "M1R1" for row in manifest["runs"]) == 5
    monkeypatch.setattr(run_e4v2_observation, "_validate_control", lambda value: None)
    monkeypatch.setattr(run_e4v2_observation, "_image_available", lambda value: None)

    def remote(ref: str):
        if ref == config["source"]["source_ref"]:
            return config["source"]["source_commit"]
        return None

    monkeypatch.setattr(run_e4v2_observation, "_remote_head", remote)
    selected = next(row for row in manifest["runs"] if row["condition_code"] == "M1R1")
    state = run_e4v2_observation.preflight(launch_dir, selected["run_id"])
    assert state["status"] == "ready"
    assert state["architecture_mode"] == "manager_star"
    assert state["rag_enabled"] is True
    assert state["provider_call_count"] == 0
