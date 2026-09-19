from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest


FORMAL_DIR = (
    Path(__file__).resolve().parents[1] / "formal-terra-t3-rag-v6-draft"
)


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, FORMAL_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_module("test_exp2_t3_rag_v6_preparation", "build_preparation_package.py")


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _resolved_config(tmp_path: Path) -> tuple[Path, str]:
    config = copy.deepcopy(BUILDER.load_json(BUILDER.DEFAULT_CONFIG_PATH))
    source_tree = Path(__file__).resolve().parents[3]
    commit = "a" * 40

    corpus_identity_sha = "1" * 64
    exclusion_sha = "2" * 64
    corpus_manifest = tmp_path / "private" / "corpus_manifest.json"
    _write_json(
        corpus_manifest,
        {
            "corpus_id": "e2-jira-corpus-v6",
            "corpus_sha256": corpus_identity_sha,
            "exclusion_list_id": "e2-exclusions-v6",
            "exclusion_list_sha256": exclusion_sha,
            "cutoff_at": "2026-08-01T00:00:00Z",
        },
    )
    index_path = tmp_path / "private" / "index.json"
    _write_json(index_path, {"index": "offline-test"})
    retrieval = {
        "enabled": True,
        "status": "ok",
        "query": "public T3 task",
        "index_id": "e2-jira-index-v6",
        "index_sha256": BUILDER.sha256_file(index_path),
        "query_sha256": "3" * 64,
        "retriever_name": "jira_bm25",
        "retriever_version": "v1",
        "corpus_id": "e2-jira-corpus-v6",
        "corpus_sha256": corpus_identity_sha,
        "exclusion_list_id": "e2-exclusions-v6",
        "exclusion_list_sha256": exclusion_sha,
        "cutoff_at": "2026-08-01T00:00:00Z",
        "requested_top_k": 5,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 1,
        "memories": [{"memory_id": "synthetic-test-memory"}],
    }
    retrieval_path = tmp_path / "private" / "retrieval_context.json"
    _write_json(retrieval_path, retrieval)

    evaluator = {
        "schema_version": "exp2-terra-t3-private-evaluator-hashes-v3",
        "private_t3_item_scorer_sha256": "4" * 64,
        "private_evaluator_coordinator_sha256": "5" * 64,
        "primary_scorer_commitment_sha256": "6" * 64,
    }
    evaluator_path = tmp_path / "private" / "private_evaluator_hashes.json"
    _write_json(evaluator_path, evaluator)

    config["source"] = {
        "tree": str(source_tree),
        "ref": "exp2/terra-t3-rag-source-v6",
        "commit_sha": commit,
    }
    config["control"] = {
        "tree": str(source_tree),
        "ref": "exp2/terra-t3-rag-control-v6",
        "commit_sha": commit,
    }
    config["image"]["digest"] = "sha256:" + "7" * 64
    config["rag"] = {
        "corpus_manifest_path": str(corpus_manifest),
        "corpus_manifest_sha256": BUILDER.sha256_file(corpus_manifest),
        "corpus_id": "e2-jira-corpus-v6",
        "corpus_sha256": corpus_identity_sha,
        "index_path": str(index_path),
        "index_id": "e2-jira-index-v6",
        "index_sha256": BUILDER.sha256_file(index_path),
        "retrieval_context_path": str(retrieval_path),
        "retrieval_context_sha256": BUILDER.sha256_bytes(
            BUILDER.canonical_bytes(retrieval)
        ),
        "exclusion_list_id": "e2-exclusions-v6",
        "exclusion_list_sha256": exclusion_sha,
        "cutoff_at": "2026-08-01T00:00:00Z",
        "top_k": 5,
    }
    config["private_evaluator"] = {
        "manifest_path": str(evaluator_path),
        "manifest_sha256": BUILDER.sha256_file(evaluator_path),
        **evaluator,
    }
    config["blinding"]["salt_sha256"] = "8" * 64
    for name in ("schema", "validator"):
        path_field = f"scorer_result_{name}_path"
        hash_field = f"scorer_result_{name}_sha256"
        config["public_task"][hash_field] = BUILDER.sha256_file(
            source_tree / config["public_task"][path_field]
        )
    config_path = tmp_path / "resolved-preparation-config.json"
    _write_json(config_path, config)
    return config_path, commit


def test_default_template_is_explicitly_unresolved_and_fails_closed() -> None:
    result = BUILDER.offline_preflight(BUILDER.DEFAULT_CONFIG_PATH)

    assert result["status"] == "blocked"
    assert result["reason"] == "unresolved_bindings"
    assert "source.commit_sha" in result["unresolved_fields"]
    assert "image.digest" in result["unresolved_fields"]
    assert "rag.corpus_sha256" in result["unresolved_fields"]
    assert "rag.index_sha256" in result["unresolved_fields"]
    assert "rag.retrieval_context_sha256" in result["unresolved_fields"]
    assert "private_evaluator.private_t3_item_scorer_sha256" in result[
        "unresolved_fields"
    ]
    assert "public_task.scorer_result_schema_sha256" in result["unresolved_fields"]
    assert "public_task.scorer_result_validator_sha256" in result[
        "unresolved_fields"
    ]
    assert result["provider_call_count"] == result["network_call_count"] == 0
    assert result["agreement_generated"] is False
    assert result["formal_run_command_available"] is False


def test_pair_schedule_is_counterbalanced_and_only_rag_enabled_differs() -> None:
    assert BUILDER.load_json(BUILDER.DEFAULT_CONFIG_PATH)["study"][
        "answer_capture_profile"
    ] == "t3_item_results_v2"
    assert BUILDER.pair_difference_fields() == ["rag_enabled"]
    assert BUILDER.CONDITION_OVERLAYS == {
        "C0": {"rag_enabled": False},
        "C1": {"rag_enabled": True},
    }
    assert [
        (item["replicate"], item["condition"])
        for item in BUILDER.execution_schedule()
    ] == [
        (1, "C0"),
        (1, "C1"),
        (2, "C1"),
        (2, "C0"),
        (3, "C0"),
        (3, "C1"),
    ]


def test_timeout_budget_matches_the_shared_two_attempt_runtime_meaning() -> None:
    assert BUILDER.ITERATION_CONTROLS["timeout_seconds"] == 1_800
    assert BUILDER.SHARED_BUDGET["attempt_timeout_seconds"] == 1_800
    assert BUILDER.SHARED_BUDGET["observation_timeout_seconds"] == 3_600
    assert BUILDER.SHARED_BUDGET["max_attempts"] == 2
    assert (
        BUILDER.SHARED_BUDGET["observation_timeout_seconds"]
        == BUILDER.SHARED_BUDGET["attempt_timeout_seconds"]
        * BUILDER.SHARED_BUDGET["max_attempts"]
    )


def test_resolved_local_inputs_pass_preparation_only_preflight(
    tmp_path: Path,
) -> None:
    config_path, commit = _resolved_config(tmp_path)
    result = BUILDER.offline_preflight(
        config_path,
        git_probe=lambda _path: {"head": commit, "clean": True},
    )

    assert result["status"] == "ready_for_preparation_only"
    assert result["pair_difference_fields"] == ["rag_enabled"]
    assert result["provider_call_count"] == result["network_call_count"] == 0
    assert result["agreement_generated"] is False
    assert result["formal_execution_authorized"] is False
    assert result["remote_ref_check"] == "not_performed_offline"
    assert result["image_presence_check"] == "not_performed_offline"
    assert (
        "experiments/shared/t3-quant-suite-v2/item_submission_schema_v2.json"
        in result["public_source_files_sha256"]
    )
    assert (
        "experiments/shared/t3-quant-suite-v2/scorer_result_schema_v2.json"
        in result["public_source_files_sha256"]
    )


def test_builder_writes_only_non_runnable_preparation_artifacts(
    tmp_path: Path,
) -> None:
    config_path, commit = _resolved_config(tmp_path)
    output = tmp_path / "e2v6-preparation"

    result = BUILDER.build_preparation_package(
        config_path=config_path,
        output=output,
        git_probe=lambda _path: {"head": commit, "clean": True},
    )

    assert result == {
        "status": "prepared_not_frozen_not_authorized",
        "output": str(output),
        "planned_observation_count": 6,
        "provider_call_count": 0,
        "network_call_count": 0,
        "agreement_generated": False,
        "formal_run_command_available": False,
    }
    assert {path.name for path in output.iterdir()} == {
        "offline_preflight.json",
        "pair_template.json",
        "preparation_manifest.json",
        "checksums.sha256",
    }
    assert not list(output.rglob("*agreement*"))
    assert not list(output.rglob("*run*command*"))
    pair = BUILDER.load_json(output / "pair_template.json")
    assert pair["status"] == "not_runnable"
    assert pair["allowed_pair_difference_fields"] == ["rag_enabled"]
    assert pair["condition_overlays"] == BUILDER.CONDITION_OVERLAYS
    assert pair["common_request_fields"]["answer_capture_profile"] == (
        "t3_item_results_v2"
    )
    assert pair["common_request_fields"]["iteration_controls"][
        "timeout_seconds"
    ] == 1_800
    assert pair["common_request_fields"]["shared_budget"][
        "attempt_timeout_seconds"
    ] == 1_800
    assert pair["common_request_fields"]["shared_budget"][
        "observation_timeout_seconds"
    ] == 3_600
    outcome = pair["common_request_fields"]["outcome_contract"]
    assert {
        key: outcome[key]
        for key in (
            "primary_outcome",
            "required_item_count",
            "missing_items_reduce_correct_items_out_of_25",
            "missing_reported_separately_from_submitted_incorrect",
        )
    } == {
        "primary_outcome": "correct_items_out_of_25",
        "required_item_count": 25,
        "missing_items_reduce_correct_items_out_of_25": True,
        "missing_reported_separately_from_submitted_incorrect": True,
    }
    assert outcome["scorer_result_schema_path"].endswith(
        "t3-quant-suite-v2/scorer_result_schema_v2.json"
    )
    assert len(outcome["scorer_result_schema_sha256"]) == 64
    assert outcome["scorer_result_validator_path"].endswith(
        "t3-quant-suite-v2/scorer_result_validator.py"
    )
    assert len(outcome["scorer_result_validator_sha256"]) == 64
    manifest = BUILDER.load_json(output / "preparation_manifest.json")
    assert manifest["agreement_generated"] is False
    assert manifest["formal_execution_authorized"] is False
    assert manifest["formal_run_command_available"] is False
    rendered = (output / "preparation_manifest.json").read_text(encoding="utf-8")
    assert "synthetic-test-memory" not in rendered


def test_builder_rejects_answer_capture_profile_drift(tmp_path: Path) -> None:
    config = BUILDER.load_json(BUILDER.DEFAULT_CONFIG_PATH)
    config["study"]["answer_capture_profile"] = "calculator_enabled"
    path = tmp_path / "profile-drift.json"
    _write_json(path, config)

    with pytest.raises(ValueError, match="study constants drifted"):
        BUILDER.validate_config_shape(BUILDER.load_json(path))


def test_builder_refuses_unresolved_template_without_creating_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "must-not-exist"

    with pytest.raises(BUILDER.PreparationBlockedError, match="preparation is blocked"):
        BUILDER.build_preparation_package(
            config_path=BUILDER.DEFAULT_CONFIG_PATH,
            output=output,
            git_probe=lambda _path: pytest.fail("git probe must not run"),
        )

    assert not output.exists()


def test_rag_hash_drift_blocks_before_any_package_is_written(tmp_path: Path) -> None:
    config_path, commit = _resolved_config(tmp_path)
    config = BUILDER.load_json(config_path)
    Path(config["rag"]["index_path"]).write_text("tampered", encoding="utf-8")

    result = BUILDER.offline_preflight(
        config_path,
        git_probe=lambda _path: {"head": commit, "clean": True},
    )

    assert result["status"] == "blocked"
    assert "rag.index_sha256" in result["errors"][0]
    assert result["provider_call_count"] == result["network_call_count"] == 0


def test_scorer_result_contract_hash_drift_blocks_preparation(tmp_path: Path) -> None:
    config_path, commit = _resolved_config(tmp_path)
    config = BUILDER.load_json(config_path)
    config["public_task"]["scorer_result_schema_sha256"] = "9" * 64
    _write_json(config_path, config)

    result = BUILDER.offline_preflight(
        config_path,
        git_probe=lambda _path: {"head": commit, "clean": True},
    )

    assert result["status"] == "blocked"
    assert "public_task.scorer_result_schema_sha256" in result["errors"][0]
    assert result["provider_call_count"] == result["network_call_count"] == 0


def test_config_rejects_extra_fields_instead_of_silently_ignoring_them() -> None:
    config = BUILDER.load_json(BUILDER.DEFAULT_CONFIG_PATH)
    config["source"]["unexpected"] = "value"

    with pytest.raises(ValueError, match="source fields"):
        BUILDER.validate_config_shape(config)
