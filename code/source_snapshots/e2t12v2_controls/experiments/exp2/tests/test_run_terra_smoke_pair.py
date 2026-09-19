from __future__ import annotations

import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "run_terra_smoke_pair.py"
SPEC = importlib.util.spec_from_file_location("run_terra_smoke_pair", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pair_has_same_common_identity_and_only_c1_enables_rag() -> None:
    c0 = MODULE._request("PAIR1", "C0")
    c1 = MODULE._request("PAIR1", "C1")

    assert MODULE._verify_pair(c0, c1) == MODULE._canonical_sha256(
        MODULE._common_pair_projection(c0)
    )
    assert c0["execution_objectives"]["parsed_task_parameters"]["rag_enabled"] is False
    assert c1["execution_objectives"]["parsed_task_parameters"]["rag_enabled"] is True
    assert "retrieval_context" not in c0
    assert "retrieval_context" not in c1


@pytest.mark.parametrize(
    ("task_id", "resource_path", "target_path", "allowed_directories"),
    [
        (
            "T1",
            "rae_runtime/sandbox/result_io.py",
            "experiments/exp2/formal-v2/submissions/result_emission_analysis.md",
            ["rae_runtime/sandbox", "experiments/exp2/formal-v2/submissions"],
        ),
        (
            "T2",
            "rae_runtime/proxy/budget_guard.py",
            "rae_runtime/proxy/budget_guard.py",
            ["rae_runtime/proxy", "rae_runtime/sandbox/tests"],
        ),
        (
            "T3",
            "experiments/exp2/formal-v2/task_inputs/financial_returns_v1.csv",
            "experiments/exp2/formal-v2/submissions/cumulative_returns.json",
            [
                "experiments/exp2/formal-v2/task_inputs",
                "experiments/exp2/formal-v2/submissions",
            ],
        ),
    ],
)
def test_public_task_registry_drives_each_smoke_request(
    task_id: str,
    resource_path: str,
    target_path: str,
    allowed_directories: list[str],
) -> None:
    request = MODULE._request(f"{task_id}-R0", "C0", task_id=task_id)

    assert request["execution_objectives"]["resource_path"] == resource_path
    assert request["execution_objectives"]["target_path"] == target_path
    assert request["repository_details"][0]["allowed_directories"] == allowed_directories
    assert request["run_id"] == f"E2-{task_id}-R0-C0"


def test_c0_projection_does_not_need_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("C0 must not enter the retriever")

    monkeypatch.setattr(MODULE, "_retrieve_for_c1", forbidden)
    c0 = MODULE._request("PAIR2", "C0")

    assert c0["execution_objectives"]["parsed_task_parameters"]["rag_enabled"] is False
    assert "retrieval_context" not in c0


def test_pair_verification_fails_on_non_treatment_drift() -> None:
    c0 = MODULE._request("PAIR3", "C0")
    c1 = MODULE._request("PAIR3", "C1")
    c1["iteration_controls"]["max_agent_turns"] = 14

    with pytest.raises(MODULE.SmokeLaunchError, match="differ beyond"):
        MODULE._verify_pair(c0, c1)


def test_openai_identity_has_no_litellm_prefix() -> None:
    identity = MODULE._provider_identity()

    assert identity["provider_mode"] == "openai"
    assert identity["model_alias"] == "gpt-5.6-terra"
    assert identity["transport_model"] == "gpt-5.6-terra"
    assert "litellm_proxy/" not in identity["transport_model"]


def test_t1_target_parent_is_present_but_target_is_not_prepopulated() -> None:
    target = (
        MODULE.REPO_ROOT
        / "experiments"
        / "exp2"
        / "formal-v2"
        / "submissions"
        / "result_emission_analysis.md"
    )

    assert target.parent.is_dir()
    assert (target.parent / ".gitkeep").is_file()
    assert not target.exists()


def test_c1_context_is_the_only_extra_top_level_field() -> None:
    c0 = MODULE._request("PAIR4", "C0")
    c1 = MODULE._request("PAIR4", "C1")
    c1["retrieval_context"] = {
        "enabled": True,
        "status": "empty",
        "query": "query",
        "query_sha256": "1" * 64,
        "retriever_name": "jira_lexical_bm25",
        "retriever_version": "1.1.0",
        "corpus_id": "corpus",
        "corpus_sha256": "2" * 64,
        "index_id": "index",
        "index_sha256": "3" * 64,
        "exclusion_list_id": "exclusions",
        "exclusion_list_sha256": "4" * 64,
        "cutoff_at": "2026-01-01T00:00:00Z",
        "requested_top_k": 5,
        "max_memories_per_source_ticket": 2,
        "candidate_count": 0,
        "memories": [],
    }

    projected = copy.deepcopy(c1)
    projected.pop("retrieval_context")
    assert MODULE._verify_pair(c0, projected)


def test_image_preflight_reports_missing_image_precisely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr=f"Error response from daemon: No such image: {MODULE.IMAGE_REF}",
        ),
    )

    with pytest.raises(MODULE.SmokeLaunchError, match="pinned local image is unavailable"):
        MODULE._image_available()


def test_image_preflight_distinguishes_unreachable_docker_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon. Is the docker daemon running?",
        ),
    )

    with pytest.raises(MODULE.SmokeLaunchError, match="engine/context is unavailable"):
        MODULE._image_available()
