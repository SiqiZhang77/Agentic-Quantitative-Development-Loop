from __future__ import annotations

import copy

import pytest

from exp3.architecture import (
    MANAGER_STAR,
    SINGLE_AGENT,
    ArchitectureModeError,
    manager_star_enabled,
    require_manager_star_enabled,
    resolve_architecture_mode,
    validate_explicit_e3_request,
    validate_manager_star_request,
)


def test_missing_mode_resolves_to_m0_without_mutating_request():
    payload = {"execution_objectives": {"strategy_type": "other"}}
    before = copy.deepcopy(payload)

    assert resolve_architecture_mode(payload) == SINGLE_AGENT
    assert payload == before


@pytest.mark.parametrize("mode", [SINGLE_AGENT, MANAGER_STAR])
def test_supported_modes_are_exact(mode):
    assert resolve_architecture_mode({"architecture_mode": mode}) == mode


@pytest.mark.parametrize("mode", [None, "", "manager", "M1", 1, True])
def test_unknown_or_non_string_mode_fails_closed(mode):
    with pytest.raises(ArchitectureModeError):
        resolve_architecture_mode({"architecture_mode": mode})


def test_manager_star_kill_switch_is_explicit():
    assert not manager_star_enabled({})
    assert manager_star_enabled({"RAE_ENABLE_MANAGER_STAR": "true"})
    with pytest.raises(ArchitectureModeError, match="disabled"):
        require_manager_star_enabled({})


def test_explicit_e3_m0_and_m1_both_require_rag_off():
    for mode in (SINGLE_AGENT, MANAGER_STAR):
        payload = _manager_star_request(architecture_mode=mode)
        assert validate_explicit_e3_request(payload) == mode

        payload["execution_objectives"]["parsed_task_parameters"][
            "rag_enabled"
        ] = True
        with pytest.raises(ArchitectureModeError, match="explicit E3"):
            validate_explicit_e3_request(payload)


@pytest.mark.parametrize("bad_parameters", [{}, {"rag_top_k": 5}])
def test_explicit_e3_rejects_missing_rag_false_or_any_retrieval_parameter(
    bad_parameters,
):
    payload = _manager_star_request()
    payload["execution_objectives"]["parsed_task_parameters"] = bad_parameters
    with pytest.raises(ArchitectureModeError):
        validate_explicit_e3_request(payload)


def test_explicit_e3_contract_does_not_reclassify_omitted_legacy_request():
    with pytest.raises(ArchitectureModeError, match="explicitly declare"):
        validate_explicit_e3_request(
            {"execution_objectives": {"parsed_task_parameters": {}}}
        )


def _manager_star_request(**overrides):
    payload = {
        "architecture_mode": MANAGER_STAR,
        "execution_objectives": {
            "parsed_task_parameters": {"rag_enabled": False},
        },
        "iteration_controls": {
            "allow_iteration": False,
            "max_iterations": 1,
            "max_failed_iterations": 1,
        },
    }
    payload.update(overrides)
    return payload


def test_manager_star_accepts_rag_off_single_pass_contract():
    validate_manager_star_request(_manager_star_request())


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda p: p["execution_objectives"]["parsed_task_parameters"].update(rag_enabled=True), "RAG"),
        (lambda p: p.update(retrieval_context={"enabled": True}), "RAG"),
        (lambda p: p["iteration_controls"].update(allow_iteration=True), "iteration"),
        (lambda p: p["iteration_controls"].update(max_iterations=2), "max_iterations"),
        (lambda p: p["iteration_controls"].update(max_failed_iterations=2), "max_failed_iterations"),
    ],
)
def test_manager_star_rejects_treatment_contamination(mutation, message):
    payload = _manager_star_request()
    mutation(payload)
    with pytest.raises(ArchitectureModeError, match=message):
        validate_manager_star_request(payload)


def test_manager_star_validator_is_not_applied_to_legacy_m0():
    with pytest.raises(ArchitectureModeError, match="requires manager_star"):
        validate_manager_star_request(
            {
                "execution_objectives": {
                    "parsed_task_parameters": {"rag_enabled": True},
                }
            }
        )
