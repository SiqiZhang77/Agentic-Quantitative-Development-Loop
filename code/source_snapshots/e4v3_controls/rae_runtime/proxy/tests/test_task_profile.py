"""Prompts must follow the request's strategy_type, not assume a backtest."""

import pytest

from task_profile import (
    REQUEST_FILE_INSTRUCTION,
    edit_instruction_for,
    instruction_override,
    resolve_task_profile,
)


def _payload(strategy_type="refactor", **objectives):
    return {
        "issue_key": "SCRUM-90",
        "command": strategy_type,
        "execution_objectives": {"strategy_type": strategy_type, **objectives},
    }


@pytest.mark.parametrize(
    ("strategy_type", "expected_in_persona"),
    [
        ("backtest", "quantitative developer"),
        ("refactor", "software engineer"),
        ("ingestion", "data engineer"),
        ("analysis", "quantitative analyst"),
        ("other", "software engineer"),
    ],
)
def test_each_strategy_type_gets_its_own_persona(strategy_type, expected_in_persona):
    profile = resolve_task_profile(_payload(strategy_type))

    assert profile.strategy_type == strategy_type
    assert expected_in_persona in profile.persona


def test_non_backtest_persona_is_not_about_trading_strategies():
    # Regression: every prompt used to say "quantitative developer"/"trading
    # strategy" regardless of the ticket's actual type.
    profile = resolve_task_profile(_payload("refactor"))

    assert "trading strategy" not in profile.persona.lower()
    assert "quantitative developer" not in profile.persona.lower()


def test_unknown_strategy_type_falls_back_to_other():
    profile = resolve_task_profile(_payload("teleport"))

    assert profile.strategy_type == "other"


def test_missing_command_falls_back_to_other():
    profile = resolve_task_profile({"issue_key": "SCRUM-90"})

    assert profile.strategy_type == "other"


def test_request_files_get_engine_parameter_rules_whatever_the_type():
    # The .request rules describe the file format, so they apply even when the
    # ticket declares a non-backtest type.
    for strategy_type in ("backtest", "refactor", "other"):
        instruction = edit_instruction_for(
            _payload(strategy_type), "rae_runtime/proxy/strategy.request"
        )
        assert instruction == REQUEST_FILE_INSTRUCTION


def test_code_files_get_the_type_instruction_not_the_engine_rules():
    instruction = edit_instruction_for(_payload("refactor"), "proxy/github_client.py")

    assert instruction != REQUEST_FILE_INSTRUCTION
    assert "Preserve the existing behaviour" in instruction


def test_instruction_override_is_read_from_the_payload_when_set():
    payload = _payload("refactor", system_instruction_override="Prefer small diffs.")

    assert instruction_override(payload) == "Prefer small diffs."


@pytest.mark.parametrize("override", [None, "", "   "])
def test_absent_or_blank_instruction_override_is_ignored(override):
    payload = _payload("refactor", system_instruction_override=override)

    assert instruction_override(payload) is None
