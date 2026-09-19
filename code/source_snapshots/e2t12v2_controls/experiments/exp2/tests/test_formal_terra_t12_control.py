from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest


FORMAL_DIR = Path(__file__).resolve().parents[1] / "formal-terra-t12-v1"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, FORMAL_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_module("test_exp2_t12_builder", "build_control_package.py")
RUNNER = load_module("test_exp2_t12_runner", "run_formal_batch.py")


def evaluator_hashes() -> dict[str, str]:
    return {
        "schema_version": BUILDER.EVALUATOR_SCHEMA,
        **{
            field: f"{index:x}" * 64
            for index, field in enumerate(BUILDER.EVALUATOR_HASH_FIELDS, start=1)
        },
    }


def fake_retrieval(_request: dict) -> dict:
    return {
        "enabled": True,
        "status": "empty",
        "index_id": BUILDER.INDEX_ID,
        "index_sha256": BUILDER.INDEX_SHA256,
        "memories": [],
    }


def rewrite_checksums(output: Path) -> None:
    rows = [
        f"{RUNNER.sha256_file(path)}  {path.relative_to(output)}"
        for path in sorted(output.rglob("*"))
        if path.is_file() and path.name != "checksums.sha256"
    ]
    (output / "checksums.sha256").write_text("\n".join(rows) + "\n")


def build_private_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    output = tmp_path / "private-control" / "package"
    monkeypatch.setattr(BUILDER, "require_outside_git", lambda *_args: None)
    result = BUILDER.build_package(
        salt=b"s" * 64,
        evaluator_hashes=evaluator_hashes(),
        output=output,
        heads={
            BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT,
            "main": "a" * 40,
            "quant/older-observation": "b" * 40,
        },
        retrieve=fake_retrieval,
    )
    assert result["formal_run_count"] == 20
    return output


def test_schedule_is_frozen_to_t1_t2_ten_pairs() -> None:
    schedule = BUILDER.execution_schedule()
    assert len(schedule) == 20
    assert len(set(schedule)) == 20
    assert all(item.startswith(("T1-", "T2-")) for item in schedule)
    assert not any(item.startswith("T3-") for item in schedule)


def test_formal_retrieval_ticket_is_valid_and_equal_within_pair() -> None:
    smoke = BUILDER._load_smoke_module()
    requests = [
        BUILDER._formal_request(
            smoke,
            task_id="T1",
            replicate=1,
            condition=condition,
            submission_id=f"E2S-{condition}",
            target_ref=f"quant/test-{condition}",
        )
        for condition in ("C0", "C1")
    ]
    ticket_ids = [request["jira_metadata"]["ticket_id"] for request in requests]
    assert ticket_ids == ["E2T12-11", "E2T12-11"]
    assert all(re.fullmatch(r"[A-Z][A-Z0-9]+-[0-9]+", item) for item in ticket_ids)


def test_evaluator_manifest_v2_requires_private_coordinator_hash() -> None:
    manifest = evaluator_hashes()
    assert BUILDER.validate_evaluator_hashes(manifest) == manifest

    legacy = dict(manifest)
    legacy["schema_version"] = "exp2-terra-t12-private-evaluator-hashes-v1"
    legacy.pop("private_evaluator_coordinator_sha256")
    with pytest.raises(ValueError, match="schema is invalid"):
        BUILDER.validate_evaluator_hashes(legacy)

    missing = dict(manifest)
    missing.pop("private_evaluator_coordinator_sha256")
    with pytest.raises(
        ValueError, match="private_evaluator_coordinator_sha256"
    ):
        BUILDER.validate_evaluator_hashes(missing)


def test_private_package_is_complete_and_blinded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    agreement = json.loads((output / "agreement.json").read_text(encoding="utf-8"))
    private_map = json.loads(
        (output / "blinding" / "private_condition_map.json").read_text(
            encoding="utf-8"
        )
    )["submissions"]

    assert agreement["formal_run_count"] == 20
    assert agreement["pair_count"] == 10
    assert len(agreement["runs"]) == 20
    assert all("condition" not in item for item in agreement["runs"])
    assert len(list((output / "runs").glob("*/*.json"))) == 60

    for record in agreement["runs"]:
        submission_id = record["submission_id"]
        request = json.loads(
            (output / record["files"]["request"]).read_text(encoding="utf-8")
        )
        condition = private_map[submission_id]["condition"]
        assert request["execution_objectives"]["parsed_task_parameters"][
            "rag_enabled"
        ] is (condition == "C1")
        assert ("retrieval_context" in request) is (condition == "C1")


def test_runner_accepts_verified_package_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    index_path = tmp_path / "index.json"
    index_path.write_bytes(b"frozen-index")
    digest = RUNNER.sha256_file(index_path)
    monkeypatch.setattr(BUILDER, "INDEX_SHA256", digest)
    monkeypatch.setattr(RUNNER.BUILDER, "INDEX_SHA256", digest)
    monkeypatch.setattr(RUNNER.SMOKE, "INDEX_PATH", index_path)

    # The package was built with the production digest, so update only the
    # fixture's agreement and checksum inventory before testing load_control.
    agreement_path = output / "agreement.json"
    agreement = json.loads(agreement_path.read_text(encoding="utf-8"))
    agreement["index_sha256"] = digest
    for run in agreement["runs"]:
        identity_path = output / run["files"]["identity"]
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["index_sha256"] = digest
        BUILDER.write_json(identity_path, identity)
        run["file_sha256"]["identity"] = RUNNER.sha256_file(identity_path)
        request_path = output / run["files"]["request"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if "retrieval_context" in request:
            request["retrieval_context"]["index_sha256"] = digest
            BUILDER.write_json(request_path, request)
            run["file_sha256"]["request"] = RUNNER.sha256_file(request_path)
    BUILDER.write_json(agreement_path, agreement)
    BUILDER.write_json(
        output / "agreement.sha256.json",
        {
            "schema_version": BUILDER.AGREEMENT_DIGEST_SCHEMA,
            "agreement_sha256": RUNNER.sha256_file(agreement_path),
        },
    )
    rewrite_checksums(output)

    state = RUNNER.load_control(
        output,
        heads={BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT},
        image_check=lambda _digest: None,
    )
    assert len(state["runs"]) == 20
    assert state["evaluator_hashes_schema"] == BUILDER.EVALUATOR_SCHEMA
    assert state["evaluator_hashes_sha256"] == agreement["evaluator_hashes_sha256"]


def test_runner_selects_exactly_one_complete_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    index_path = tmp_path / "index.json"
    index_path.write_bytes(b"frozen-index")
    digest = RUNNER.sha256_file(index_path)
    monkeypatch.setattr(BUILDER, "INDEX_SHA256", digest)
    monkeypatch.setattr(RUNNER.BUILDER, "INDEX_SHA256", digest)
    monkeypatch.setattr(RUNNER.SMOKE, "INDEX_PATH", index_path)

    agreement_path = output / "agreement.json"
    agreement = json.loads(agreement_path.read_text(encoding="utf-8"))
    agreement["index_sha256"] = digest
    for run in agreement["runs"]:
        identity_path = output / run["files"]["identity"]
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["index_sha256"] = digest
        BUILDER.write_json(identity_path, identity)
        run["file_sha256"]["identity"] = RUNNER.sha256_file(identity_path)
        request_path = output / run["files"]["request"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if "retrieval_context" in request:
            request["retrieval_context"]["index_sha256"] = digest
            BUILDER.write_json(request_path, request)
            run["file_sha256"]["request"] = RUNNER.sha256_file(request_path)
    BUILDER.write_json(agreement_path, agreement)
    BUILDER.write_json(
        output / "agreement.sha256.json",
        {
            "schema_version": BUILDER.AGREEMENT_DIGEST_SCHEMA,
            "agreement_sha256": RUNNER.sha256_file(agreement_path),
        },
    )
    rewrite_checksums(output)

    state = RUNNER.load_control(
        output,
        heads={BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT},
        image_check=lambda _digest: None,
    )
    selected = RUNNER._select_pair(state, "T2", 5)
    assert len(selected["runs"]) == 2
    assert {run["condition"] for run in selected["runs"]} == {"C0", "C1"}
    assert all(run["task_id"] == "T2" for run in selected["runs"])
    assert all(run["replicate"] == 5 for run in selected["runs"])
    assert RUNNER._pair_authorization("T2", 5) == (
        "AUTHORIZE_E2T12V2_T2_R5_TWO_OBSERVATIONS"
    )


def test_runner_rejects_legacy_four_field_evaluator_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    evaluator_path = output / "private_evaluator_hashes.json"
    manifest = json.loads(evaluator_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = "exp2-terra-t12-private-evaluator-hashes-v1"
    manifest.pop("private_evaluator_coordinator_sha256")
    BUILDER.write_json(evaluator_path, manifest)
    rewrite_checksums(output)

    with pytest.raises(RUNNER.FormalLaunchError, match="schema is invalid"):
        RUNNER.load_control(
            output,
            heads={BUILDER.SOURCE_REF: BUILDER.SOURCE_COMMIT},
            image_check=lambda _digest: None,
        )


def test_runner_rejects_tampered_private_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = build_private_package(tmp_path, monkeypatch)
    with (output / "agreement.json").open("a", encoding="utf-8") as handle:
        handle.write(" ")
    with pytest.raises(RUNNER.FormalLaunchError, match="checksum mismatch"):
        RUNNER.load_control(output, heads={}, image_check=lambda _digest: None)


def test_model_call_evidence_requires_positive_tokens() -> None:
    assert RUNNER._model_call_evidence(
        {"telemetry": {"model_usage": [{"total_tokens": 52}]}}
    )
    assert not RUNNER._model_call_evidence(
        {"telemetry": {"model_usage": [{"total_tokens": 0}]}}
    )


def test_post_run_treatment_evidence_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(RUNNER.BUILDER, "INDEX_SHA256", "7" * 64)
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
                "index_id": RUNNER.BUILDER.INDEX_ID,
                "index_sha256": "7" * 64,
            }
        }
    }
    assert RUNNER._treatment_evidence(c0, "C0")
    assert RUNNER._treatment_evidence(c1, "C1")
    c1["telemetry"]["retrieval"]["prompt_injected"] = False
    assert not RUNNER._treatment_evidence(c1, "C1")
