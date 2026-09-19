"""Per-workflow prompt profiles, keyed by the request's ``strategy_type``.

Every prompt in the runtime used to be written for the backtest use case: the
persona said "quantitative developer" and the instructions said "modify the
trading strategy", regardless of what the ticket actually asked for. A refactor
or ingestion ticket therefore got quant instructions for a file it wasn't meant
to treat as a strategy.

This module is the single place that decides *how to ask* for each workflow
type. run.py already decides *whether to backtest* (`_needs_backtest`); this
decides the persona and edit instruction that go with it.

Personas live here in code on purpose. ``system_instruction_override`` is
deliberately NOT sourced from the Jira comment: prompt_context.py marks all
Jira-authored content as untrusted and states it "cannot override these
instructions", so letting a commenter supply the system persona would defeat
that guard. Operators/IW may still set the override in the payload, and it is
appended as extra goal guidance below the safety rules rather than replacing
them.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_STRATEGY_TYPE = "other"


@dataclass(frozen=True)
class TaskProfile:
    """How to prompt for one workflow type."""

    strategy_type: str
    persona: str
    edit_instruction: str


# The atrade engine can't run Python: a .request is a flat list of engine
# parameters, so tuning it is a different job from editing code. Keyed off the
# file extension rather than strategy_type, because it is a property of the
# file's format, not of the ticket's intent.
REQUEST_FILE_INSTRUCTION = (
    'This file is an atrade engine .request: a flat list of "KEY = value" engine '
    "parameters — factor rules (VALUE_ACTIVE_RULES, GROWTH_ACTIVE_RULES), per-sector "
    "*_EXPERTWGHT_* weights, screener thresholds (MINPE/MAXPE/MINCAPITALISATION), "
    "portfolio caps (SECTORCAP/LIQUIDITYCAP/SHARESCAP), stop-losses "
    "(STATICSTOPLOSS/PORTFOLIOSTOPLOSS), NPORT/NFREQ, and the STARTDATE/ENDDATE "
    "window. Express the requested change by editing the *values* of existing keys "
    "only. Do NOT add new keys, remove keys, or write Python: the engine ignores "
    "anything that is not one of its parameters, and the rule logic lives in the "
    "compiled engine (pinned by atrade_sifting_*), not this file. Keep it "
    "engine-valid."
)

_PROFILES: dict[str, TaskProfile] = {
    "backtest": TaskProfile(
        strategy_type="backtest",
        persona=(
            "You are a quantitative developer tuning a trading strategy that will be "
            "scored by a backtest engine."
        ),
        edit_instruction=(
            "Modify the file to implement the requested strategy change. The result "
            "will be backtested, so keep the change coherent and engine-valid."
        ),
    ),
    "refactor": TaskProfile(
        strategy_type="refactor",
        persona=(
            "You are a software engineer refactoring code in a production repository."
        ),
        edit_instruction=(
            "Refactor the file as the ticket asks. Preserve the existing behaviour "
            "exactly — this is a refactor, not a feature change. Match the "
            "surrounding code's style, naming, and structure. Do not add commentary "
            "about the change itself."
        ),
    ),
    "ingestion": TaskProfile(
        strategy_type="ingestion",
        persona=(
            "You are a data engineer preparing datasets for a quantitative research "
            "cluster."
        ),
        edit_instruction=(
            "Modify the file to implement the requested data-preparation change. Be "
            "conservative: do not invent schema fields or domain-specific cleaning "
            "rules that the ticket did not ask for."
        ),
    ),
    "analysis": TaskProfile(
        strategy_type="analysis",
        persona=(
            "You are a quantitative analyst investigating a codebase and reporting "
            "findings."
        ),
        edit_instruction=(
            "Make the smallest edit that answers the ticket's question. Prefer "
            "leaving the file unchanged and reporting your findings in your final "
            "message over speculative edits."
        ),
    ),
    DEFAULT_STRATEGY_TYPE: TaskProfile(
        strategy_type=DEFAULT_STRATEGY_TYPE,
        persona="You are a software engineer working on a repository task.",
        edit_instruction="Modify the file to implement the Jira task.",
    ),
}


def _strategy_type(payload: dict) -> str:
    """_normalise_request maps execution_objectives.strategy_type -> command."""
    return str(payload.get("command") or "").strip().lower()


def instruction_override(payload: dict) -> str | None:
    """Operator/IW-supplied goal override, if any. Never sourced from Jira."""
    objectives = payload.get("execution_objectives") or {}
    override = objectives.get("system_instruction_override")
    if isinstance(override, str) and override.strip():
        return override.strip()
    return None


def resolve_task_profile(payload: dict) -> TaskProfile:
    """The profile for this request's strategy_type, falling back to ``other``."""
    return _PROFILES.get(_strategy_type(payload), _PROFILES[DEFAULT_STRATEGY_TYPE])


def edit_instruction_for(payload: dict, path: str) -> str:
    """The edit instruction for this request's type and target file.

    A .request target always gets the engine-parameter rules, whatever the
    ticket's declared type, because those rules describe the file format.
    """
    if str(path or "").endswith(".request"):
        return REQUEST_FILE_INSTRUCTION
    return resolve_task_profile(payload).edit_instruction
