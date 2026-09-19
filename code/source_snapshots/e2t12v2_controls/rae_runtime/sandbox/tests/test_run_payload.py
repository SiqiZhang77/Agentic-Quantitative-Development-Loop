"""Unit tests for RAE-02: stdin request validation + normalisation in run.py."""

import json
import sys
from io import StringIO
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, patch
import unittest.mock
import pytest

_SANDBOX = Path(__file__).parent.parent
for _p in (str(_SANDBOX), str(_SANDBOX / "proxy")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# for _mod in ("mcp_client", "pipeline", "github_client", "llm_client"):
#     sys.modules.setdefault(_mod, MagicMock())
# sys.modules.setdefault("dotenv", MagicMock())

_MOCKED_MODULES = ("mcp_client", "pipeline", "github_client", "llm_client", "dotenv")
_patches = unittest.mock.patch.dict(
    "sys.modules",
    {mod: MagicMock() for mod in _MOCKED_MODULES if mod not in sys.modules},
)
_patches.start()

import jsonschema
import run

_patches.stop()

_EXAMPLE = json.loads(
    (Path(__file__).parent / "fixtures" / "runtime_request_example.json").read_text()
)


def _valid():
    return json.loads(json.dumps(_EXAMPLE))  # deep copy per test


def _retrieval_context() -> dict:
    return {
        "enabled": True,
        "status": "ok",
        "query": "Summary: Refactor Jira history",
        "query_sha256": "1" * 64,
        "retriever_name": "jira_lexical_bm25",
        "retriever_version": "1.0.0",
        "corpus_id": "e2-corpus-v1",
        "corpus_sha256": "2" * 64,
        "index_id": "e2-index-v1",
        "index_sha256": "3" * 64,
        "exclusion_list_id": "e2-exclusions-v1",
        "exclusion_list_sha256": "4" * 64,
        "cutoff_at": "2026-06-01T00:00:00+00:00",
        "requested_top_k": 5,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 10,
        "memories": [
            {
                "memory_id": "MEM-01",
                "source_ticket_id": "SCRUM-4",
                "source_type": "comment",
                "source_id": "comment:10001",
                "source_timestamp": "2026-05-01T00:00:00+00:00",
                "rank": 1,
                "score": 2.5,
                "text": "History remains chronological and capped.",
            }
        ],
    }


class TestValidatePayload:
    def test_example_is_valid(self):
        run._validate_payload(_valid())  # IW's own example must pass

    @pytest.mark.parametrize("mode", ["single_agent", "manager_star"])
    def test_supported_architecture_modes_are_valid(self, mode):
        run._validate_payload({**_valid(), "architecture_mode": mode})

    def test_unknown_architecture_mode_is_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload({**_valid(), "architecture_mode": "peer_mesh"})

    def test_missing_schema_version(self):
        p = _valid()
        del p["schema_version"]
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload(p)

    def test_wrong_schema_version(self):
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload({**_valid(), "schema_version": "2.0"})

    def test_missing_jira_metadata(self):
        p = _valid()
        del p["jira_metadata"]
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload(p)

    def test_bad_ticket_id_pattern(self):
        p = _valid()
        p["jira_metadata"]["ticket_id"] = "not a key"
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload(p)

    def test_unknown_top_level_field_rejected(self):
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload({**_valid(), "surprise": 1})

    def test_bad_strategy_type_rejected(self):
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "delete_everything"
        with pytest.raises(jsonschema.ValidationError):
            run._validate_payload(p)

    def test_payload_without_dates_validates(self):
        # Dates are optional now: an empty target_date_range must pass validation
        # (the run inherits the strategy's own window).
        p = _valid()
        p["execution_objectives"]["target_date_range"] = {}
        run._validate_payload(p)


class TestNormaliseRequest:
    def test_ticket_id_maps_to_issue_key(self):
        out = run._normalise_request(_valid())
        assert out["issue_key"] == _EXAMPLE["jira_metadata"]["ticket_id"]

    def test_missing_architecture_mode_preserves_legacy_internal_payload(self):
        request = _valid()
        before = json.loads(json.dumps(request))

        out = run._normalise_request(request)

        assert "architecture_mode" not in out
        assert request == before

    @pytest.mark.parametrize("mode", ["single_agent", "manager_star"])
    def test_explicit_architecture_mode_is_preserved(self, mode):
        request = {**_valid(), "architecture_mode": mode}

        out = run._normalise_request(request)

        assert out["architecture_mode"] == mode

    def test_verified_retrieval_context_is_preserved_without_mutating_input(self):
        p = _valid()
        p["execution_objectives"].setdefault("parsed_task_parameters", {})[
            "rag_enabled"
        ] = True
        p["execution_objectives"]["parsed_task_parameters"]["rag_top_k"] = 5
        p["retrieval_context"] = _retrieval_context()
        before = json.loads(json.dumps(p))

        run._validate_payload(p)
        out = run._normalise_request(p)

        assert out["retrieval_context"] == p["retrieval_context"]
        assert out["retrieval_context"] is not p["retrieval_context"]
        assert p == before

    def test_rag_enabled_requires_gateway_verified_context(self):
        p = _valid()
        p["execution_objectives"].setdefault("parsed_task_parameters", {})[
            "rag_enabled"
        ] = True

        with pytest.raises(ValueError, match="requires a verified retrieval_context"):
            run._normalise_request(p)

    def test_context_is_forbidden_when_rag_is_not_enabled(self):
        p = _valid()
        p["retrieval_context"] = _retrieval_context()

        with pytest.raises(ValueError, match="forbidden unless rag_enabled"):
            run._normalise_request(p)

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda p: p["retrieval_context"].update({"enabled": False}), "enabled"),
            (
                lambda p: p["retrieval_context"].update({"requested_top_k": 4}),
                "must match rag_top_k",
            ),
            (
                lambda p: p["retrieval_context"].update(
                    {"max_memories_per_source_ticket": 3}
                ),
                "max_memories_per_source_ticket must be 2",
            ),
            (
                lambda p: p["retrieval_context"].update({"status": "ok", "memories": []}),
                "requires at least one memory",
            ),
            (
                lambda p: p["retrieval_context"].update({"status": "empty"}),
                "requires no memories",
            ),
            (
                lambda p: p["retrieval_context"]["memories"][0].update({"rank": 2}),
                "ranks must be consecutive",
            ),
            (
                lambda p: p["retrieval_context"]["memories"][0].update(
                    {"score": float("nan")}
                ),
                "scores must be finite",
            ),
        ],
    )
    def test_final_retrieval_context_invariants(self, mutate, message):
        p = _valid()
        p["execution_objectives"].setdefault("parsed_task_parameters", {}).update(
            {"rag_enabled": True, "rag_top_k": 5}
        )
        p["retrieval_context"] = _retrieval_context()
        mutate(p)

        with pytest.raises(ValueError, match=message):
            run._normalise_request(p)

    def test_dates_map_into_args(self):
        out = run._normalise_request(_valid())
        dr = _EXAMPLE["execution_objectives"]["target_date_range"]
        assert out["args"]["start"] == dr["start_date"]
        assert out["args"]["end"] == dr["end_date"]

    def test_missing_dates_are_omitted_so_strategy_window_stands(self):
        p = _valid()
        p["execution_objectives"]["target_date_range"] = {}
        out = run._normalise_request(p)
        assert "start" not in out["args"] and "end" not in out["args"]

    def test_empty_string_dates_are_omitted(self):
        p = _valid()
        p["execution_objectives"]["target_date_range"] = {"start_date": "", "end_date": ""}
        out = run._normalise_request(p)
        assert "start" not in out["args"] and "end" not in out["args"]

    def test_backtest_command_targets_strategy_request(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_PATH", raising=False)
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "backtest"
        out = run._normalise_request(p)
        assert out["strategy"]["path"].endswith("strategy.request")

    def test_resource_path_targets_the_requested_file(self, monkeypatch):
        # Without this, a general ticket silently edits the demo strategy.py.
        monkeypatch.delenv("STRATEGY_PATH", raising=False)
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "refactor"
        p["execution_objectives"]["resource_path"] = "rae_runtime/proxy/github_client.py"
        out = run._normalise_request(p)
        assert out["strategy"]["path"] == "rae_runtime/proxy/github_client.py"

    def test_resource_path_overrides_the_backtest_default(self, monkeypatch):
        monkeypatch.delenv("STRATEGY_PATH", raising=False)
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "backtest"
        p["execution_objectives"]["resource_path"] = "strategies/momentum.request"
        out = run._normalise_request(p)
        assert out["strategy"]["path"] == "strategies/momentum.request"

    def test_general_command_without_resource_path_discovers(self, monkeypatch):
        # A general run that names no file no longer falls back to the demo
        # strategy.py: an empty source path signals resource discovery downstream.
        monkeypatch.delenv("STRATEGY_PATH", raising=False)
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "refactor"
        p["execution_objectives"].pop("resource_path", None)
        out = run._normalise_request(p)
        assert out["strategy"]["path"] == ""
        assert out["strategy"]["source_path"] == ""
        assert out["strategy"]["target_path"] == ""

    def test_backtest_without_resource_path_keeps_request_default(self, monkeypatch):
        # The backtest engine needs a concrete .request, so its default stands.
        monkeypatch.delenv("STRATEGY_PATH", raising=False)
        p = _valid()
        p["execution_objectives"]["strategy_type"] = "backtest"
        p["execution_objectives"].pop("resource_path", None)
        out = run._normalise_request(p)
        assert out["strategy"]["path"].endswith("strategy.request")

    def test_strategy_path_env_pin_still_wins_over_resource_path(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_PATH", "pinned/by/operator.py")
        p = _valid()
        p["execution_objectives"]["resource_path"] = "rae_runtime/proxy/github_client.py"
        out = run._normalise_request(p)
        assert out["strategy"]["path"] == "pinned/by/operator.py"

    def test_allowed_directories_reach_the_strategy_scope(self):
        # pipeline_mcp passes these to the MCP server, which enforces them.
        p = _valid()
        p["repository_details"] = [
            {
                "clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop",
                "allowed_directories": ["rae_runtime/proxy", "strategies"],
            }
        ]
        out = run._normalise_request(p)
        assert out["strategy"]["allowed_directories"] == [
            "rae_runtime/proxy",
            "strategies",
        ]

    def test_absent_allowed_directories_means_unrestricted(self):
        p = _valid()
        p["repository_details"] = [
            {"clone_url": "https://github.com/bankingscience/BSLAgenticQuantDevLoop"}
        ]
        out = run._normalise_request(p)
        assert out["strategy"]["allowed_directories"] == []

    def test_repo_details_map_to_strategy(self):
        out = run._normalise_request(_valid())
        repo = _EXAMPLE.get("repository_details") or [{}]
        if repo and repo[0].get("target_branch"):
            assert out["strategy"]["target_branch"] == repo[0]["target_branch"]
        else:
            assert out["strategy"]["ref"] == "main"

    def test_code_free_run_defaults_repo(self):
        p = _valid()
        p["repository_details"] = None
        out = run._normalise_request(p)
        assert "github.com" in out["strategy"]["repo_url"]
        assert out["strategy"]["ref"] == "main"
        assert out["repositories"][0]["repo_full_name"] == (
            "bankingscience/BSLAgenticQuantDevLoop"
        )

    def test_preserves_all_repository_details_without_backtest_key_mapping(self):
        p = _valid()
        p["repository_details"] = [
            {
                "alias": "ATPConnectorsRepo",
                "repo_full_name": "bankingscience/ATPConnectorsRepo",
                "clone_url": "https://github.com/bankingscience/ATPConnectorsRepo.git",
                "source_branch": "feature/connectors",
                "target_branch": "quant/SCRUM-46",
                "runtime_role": "repository",
                "allowed_directories": ["."],
            },
            {
                "alias": "ATPDataHandlersRepo",
                "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                "clone_url": "https://github.com/bankingscience/ATPDataHandlersRepo.git",
                "source_branch": "develop",
                "target_branch": "quant/SCRUM-46",
                "runtime_role": "repository",
                "allowed_directories": ["."],
            },
        ]

        out = run._normalise_request(p)

        assert [repo["alias"] for repo in out["repositories"]] == [
            "ATPConnectorsRepo",
            "ATPDataHandlersRepo",
        ]
        assert "repository_branch_map" not in out
        assert out["strategy"]["repo_full_name"] == "bankingscience/ATPConnectorsRepo"

    def test_result_path_from_output_paths(self):
        out = run._normalise_request(_valid())
        assert out["result_path"] == _EXAMPLE["output_paths"]["result_path"]

    def test_execution_objectives_are_preserved_for_runtime_loop(self):
        out = run._normalise_request(_valid())
        assert out["execution_objectives"] == _EXAMPLE["execution_objectives"]

    def test_distinct_source_and_target_paths_are_normalised(self):
        p = _valid()
        p["execution_objectives"]["resource_path"] = "docs/source-notes.txt"
        p["execution_objectives"]["target_path"] = "README.md"

        out = run._normalise_request(p)

        assert out["strategy"]["path"] == "docs/source-notes.txt"
        assert out["strategy"]["source_path"] == "docs/source-notes.txt"
        assert out["strategy"]["target_path"] == "README.md"

    def test_target_path_defaults_to_source_path_for_legacy_requests(self):
        p = _valid()
        p["execution_objectives"]["resource_path"] = "src/module.py"
        p["execution_objectives"].pop("target_path", None)

        out = run._normalise_request(p)

        assert out["strategy"]["source_path"] == "src/module.py"
        assert out["strategy"]["target_path"] == "src/module.py"

    def test_complete_jira_context_is_preserved_for_prompt_construction(self):
        out = run._normalise_request(_valid())
        assert out["jira_context"] == _EXAMPLE["jira_metadata"]

    def test_does_not_mutate_input(self):
        p = _valid()
        before = json.dumps(p)
        run._normalise_request(p)
        assert json.dumps(p) == before

    def test_verified_input_dataset_is_preserved(
        self,
        tmp_path,
        monkeypatch,
    ):
        dataset_root = tmp_path / "datasets"
        dataset_root.mkdir()
        dataset_file = dataset_root / "input.csv"
        dataset_file.write_text("date,return\n2024-01-31,0.01\n")
        monkeypatch.setattr(
            run,
            "INPUT_DATASET_ROOT",
            PurePosixPath(str(dataset_root)),
        )
        p = _valid()
        p["input_datasets"] = [
            {
                "dataset_id": "input",
                "source_kind": "hdfs",
                "original_filename": "input.csv",
                "source_uri": "hdfs://namenode:8020/approved/input.csv",
                "container_path": str(dataset_file),
                "format": "csv",
                "read_only": True,
                "size_bytes": dataset_file.stat().st_size,
                "sha256": run._sha256_file(dataset_file),
            }
        ]

        out = run._normalise_request(p)

        assert out["input_datasets"][0]["container_path"] == str(dataset_file)
        assert out["input_datasets"][0]["sha256"] == run._sha256_file(dataset_file)

    def test_verified_jira_attachment_dataset_is_preserved(
        self,
        tmp_path,
        monkeypatch,
    ):
        dataset_root = tmp_path / "datasets"
        dataset_root.mkdir()
        dataset_file = dataset_root / "input.csv"
        dataset_file.write_text("date,return\n2024-01-31,0.01\n")
        monkeypatch.setattr(
            run,
            "INPUT_DATASET_ROOT",
            PurePosixPath(str(dataset_root)),
        )
        p = _valid()
        p["input_datasets"] = [
            {
                "dataset_id": "input",
                "source_kind": "jira_attachment",
                "original_filename": "input.csv",
                "source_uri": "jira-attachment://20001",
                "jira_attachment_id": "20001",
                "container_path": str(dataset_file),
                "format": "csv",
                "read_only": True,
                "size_bytes": dataset_file.stat().st_size,
                "sha256": run._sha256_file(dataset_file),
            }
        ]

        out = run._normalise_request(p)

        assert out["input_datasets"][0]["source_kind"] == "jira_attachment"
        assert out["input_datasets"][0]["jira_attachment_id"] == "20001"

    def test_input_dataset_checksum_mismatch_fails_before_analysis(
        self,
        tmp_path,
        monkeypatch,
    ):
        dataset_root = tmp_path / "datasets"
        dataset_root.mkdir()
        dataset_file = dataset_root / "input.csv"
        dataset_file.write_text("date,return\n2024-01-31,0.01\n")
        monkeypatch.setattr(
            run,
            "INPUT_DATASET_ROOT",
            PurePosixPath(str(dataset_root)),
        )
        p = _valid()
        p["input_datasets"] = [
            {
                "dataset_id": "input",
                "source_kind": "hdfs",
                "original_filename": "input.csv",
                "source_uri": "hdfs://namenode:8020/approved/input.csv",
                "container_path": str(dataset_file),
                "format": "csv",
                "read_only": True,
                "size_bytes": dataset_file.stat().st_size,
                "sha256": "0" * 64,
            }
        ]

        with pytest.raises(ValueError, match="checksum mismatch"):
            run._normalise_request(p)


class TestMainFailedResult:
    def _run(self, stdin_text, tmp_path, monkeypatch):
        monkeypatch.delenv("RAE_LEGACY_INPUT", raising=False)
        rf = tmp_path / "result.json"
        monkeypatch.setattr(run, "DEFAULT_OUTPUT", str(rf))
        with patch("sys.stdin", StringIO(stdin_text)):
            code = run.main()
        return code, json.loads(rf.read_text())

    @pytest.mark.parametrize(
        "stdin_text",
        [
            "",
            "{broken json",
            json.dumps({"schema_version": "1.0"}),
        ],
    )
    def test_bad_input_produces_failed_response(
        self, stdin_text, tmp_path, monkeypatch
    ):
        code, written = self._run(stdin_text, tmp_path, monkeypatch)
        assert code == 1
        assert written["run_id"] == "unknown"
        assert written["execution_summary"]["ticket_id"] == "unknown"
        assert written["execution_summary"]["status"] == "failed"
        assert written["diagnostics"]["error_code"] == "PAYLOAD_VALIDATION_ERROR"

    def test_schema_failure_preserves_parseable_request_identifiers(
        self, tmp_path, monkeypatch
    ):
        payload = _valid()
        payload["iteration_controls"]["unsupported_limit"] = 1

        code, written = self._run(json.dumps(payload), tmp_path, monkeypatch)

        assert code == 1
        assert written["run_id"] == payload["run_id"]
        assert written["execution_summary"]["ticket_id"] == "SCRUM-46"
        assert written["diagnostics"]["error_code"] == "PAYLOAD_VALIDATION_ERROR"

    def test_schema_failure_replaces_invalid_ticket_with_unknown(
        self, tmp_path, monkeypatch
    ):
        payload = _valid()
        payload["jira_metadata"]["ticket_id"] = "not a ticket"

        code, written = self._run(json.dumps(payload), tmp_path, monkeypatch)

        assert code == 1
        assert written["run_id"] == payload["run_id"]
        assert written["execution_summary"]["ticket_id"] == "unknown"
