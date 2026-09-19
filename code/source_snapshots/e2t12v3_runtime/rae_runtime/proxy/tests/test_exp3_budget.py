from concurrent.futures import ThreadPoolExecutor

import pytest

from exp3.budget import (
    ROLE_LIMITS,
    SINGLE_AGENT_ROLE_LIMITS,
    SHARED_MAX_CALLS,
    SHARED_MAX_TOKENS,
    BudgetExceeded,
    BudgetValidationError,
    RoleBudgetLedger,
    sha256_text,
)
from exp3.policy import ARCHITECT, DEVELOPER, MANAGER


def _finish_zero(ledger, call_id, **kwargs):
    return ledger.finish(
        call_id,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        **kwargs,
    )


def test_limits_are_the_frozen_shared_and_role_caps():
    assert (SHARED_MAX_CALLS, SHARED_MAX_TOKENS) == (15, 200_000)
    assert dict(ROLE_LIMITS[MANAGER]) == {"max_calls": 3, "max_tokens": 45_000}
    assert dict(ROLE_LIMITS[ARCHITECT]) == {
        "max_calls": 3,
        "max_tokens": 35_000,
    }
    assert dict(ROLE_LIMITS[DEVELOPER]) == {
        "max_calls": 9,
        "max_tokens": 120_000,
    }


def test_single_agent_uses_the_full_shared_budget_without_m1_role_partition():
    assert dict(SINGLE_AGENT_ROLE_LIMITS[MANAGER]) == {
        "max_calls": 0,
        "max_tokens": 0,
    }
    assert dict(SINGLE_AGENT_ROLE_LIMITS[ARCHITECT]) == {
        "max_calls": 0,
        "max_tokens": 0,
    }
    assert dict(SINGLE_AGENT_ROLE_LIMITS[DEVELOPER]) == {
        "max_calls": 15,
        "max_tokens": 200_000,
    }

    ledger = RoleBudgetLedger.for_single_agent()
    for _ in range(15):
        _finish_zero(ledger, ledger.begin(DEVELOPER))

    assert ledger.snapshot()["shared"]["calls"] == 15
    with pytest.raises(BudgetExceeded, match="developer call budget"):
        ledger.begin(DEVELOPER)
    with pytest.raises(BudgetExceeded, match="manager call budget"):
        RoleBudgetLedger.for_single_agent().begin(MANAGER)


def test_capacity_precheck_is_read_only_at_and_below_the_limit():
    ledger = RoleBudgetLedger.for_single_agent()
    before = ledger.snapshot()
    ledger.ensure_capacity(DEVELOPER)
    assert ledger.snapshot() == before

    for _ in range(15):
        _finish_zero(ledger, ledger.begin(DEVELOPER))
    exhausted = ledger.snapshot()
    with pytest.raises(BudgetExceeded, match="developer call budget"):
        ledger.ensure_capacity(DEVELOPER)
    assert ledger.snapshot() == exhausted


@pytest.mark.parametrize(
    "role,call_cap",
    [(MANAGER, 3), (ARCHITECT, 3), (DEVELOPER, 9)],
)
def test_each_role_accepts_exact_call_cap_then_blocks(role, call_cap):
    ledger = RoleBudgetLedger()
    for _ in range(call_cap):
        _finish_zero(ledger, ledger.begin(role))

    before = ledger.snapshot()
    with pytest.raises(BudgetExceeded, match="call budget"):
        ledger.begin(role)
    assert ledger.snapshot() == before


def test_all_role_call_caps_sum_to_exact_shared_cap():
    ledger = RoleBudgetLedger()
    for role, count in ((MANAGER, 3), (ARCHITECT, 3), (DEVELOPER, 9)):
        for _ in range(count):
            _finish_zero(ledger, ledger.begin(role))

    snapshot = ledger.snapshot()
    assert snapshot["shared"]["calls"] == SHARED_MAX_CALLS
    assert snapshot["shared"]["calls"] == sum(
        item["calls"] for item in snapshot["roles"].values()
    )


def test_begin_counts_a_call_before_a_failed_model_invocation_finishes():
    ledger = RoleBudgetLedger()
    call_id = ledger.begin(MANAGER, input_hash=sha256_text("manager prompt"))

    pending = ledger.snapshot()
    assert pending["shared"]["calls"] == 1
    assert pending["roles"][MANAGER]["calls"] == 1
    assert pending["roles"][MANAGER]["pending_calls"] == 1

    record = ledger.finish(
        call_id,
        prompt_tokens=0,
        completion_tokens=0,
        latency_seconds=0.25,
        failure=RuntimeError("provider failed"),
        output_hash=sha256_text(""),
    )
    assert record["failed"] is True
    assert record["failure"] == "RuntimeError"
    finished = ledger.snapshot()
    assert finished["roles"][MANAGER]["calls"] == 1
    assert finished["roles"][MANAGER]["failures"] == 1
    assert finished["roles"][MANAGER]["pending_calls"] == 0


def test_unused_capacity_does_not_transfer_between_roles():
    ledger = RoleBudgetLedger()
    manager_call = ledger.begin(MANAGER)
    ledger.finish(
        manager_call,
        prompt_tokens=40_000,
        completion_tokens=5_000,
        total_tokens=45_000,
    )

    before = ledger.snapshot()
    with pytest.raises(BudgetExceeded, match="manager token budget"):
        ledger.begin(MANAGER)
    assert ledger.snapshot() == before

    # Developer capacity remains independently available; it is not donated to
    # manager, and manager's unused call slots are not donated back either.
    developer_call = ledger.begin(DEVELOPER)
    _finish_zero(ledger, developer_call)
    assert ledger.snapshot()["roles"][DEVELOPER]["calls"] == 1


def test_over_limit_usage_is_fully_recorded_before_exception():
    ledger = RoleBudgetLedger()
    input_hash = sha256_text("developer input")
    output_hash = sha256_text("developer output")
    call_id = ledger.begin(DEVELOPER, input_hash=input_hash)

    with pytest.raises(BudgetExceeded) as raised:
        ledger.finish(
            call_id,
            prompt_tokens=100_000,
            completion_tokens=20_001,
            total_tokens=120_001,
            latency_seconds=1.75,
            failure="HTTP_403",
            input_hash=input_hash,
            output_hash=output_hash,
        )

    assert raised.value.call_id == call_id
    snapshot = ledger.snapshot()
    developer = snapshot["roles"][DEVELOPER]
    assert developer["calls"] == 1
    assert developer["prompt_tokens"] == 100_000
    assert developer["completion_tokens"] == 20_001
    assert developer["total_tokens"] == 120_001
    assert developer["latency_seconds"] == 1.75
    assert developer["failures"] == 1
    assert developer["pending_calls"] == 0
    assert developer["over_budget"] is True
    assert developer["records"][0]["input_hash"] == input_hash
    assert developer["records"][0]["output_hash"] == output_hash
    assert developer["records"][0]["failure"] == "HTTP_403"
    assert snapshot["shared"]["total_tokens"] == 120_001


@pytest.mark.parametrize(
    "finish_kwargs",
    [
        {"prompt_tokens": -1, "completion_tokens": 0},
        {"prompt_tokens": True, "completion_tokens": 0},
        {"prompt_tokens": 0, "completion_tokens": False},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": -1},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": True},
        {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 4},
        {"prompt_tokens": 0, "completion_tokens": 0, "latency_seconds": -0.1},
        {"prompt_tokens": 0, "completion_tokens": 0, "role": "reviewer"},
        {"prompt_tokens": 0, "completion_tokens": 0, "failure": "PRIVATE failure body"},
        {"prompt_tokens": 0, "completion_tokens": 0, "output_hash": "not-a-hash"},
    ],
)
def test_invalid_finish_has_zero_mutation(finish_kwargs):
    ledger = RoleBudgetLedger()
    call_id = ledger.begin(ARCHITECT)
    before = ledger.snapshot()

    with pytest.raises(BudgetValidationError):
        ledger.finish(call_id, **finish_kwargs)

    assert ledger.snapshot() == before


def test_unknown_finish_has_zero_mutation():
    ledger = RoleBudgetLedger()
    before = ledger.snapshot()

    with pytest.raises(BudgetValidationError, match="unknown call_id"):
        ledger.finish(
            "exp3-missing",
            prompt_tokens=1,
            completion_tokens=1,
        )

    assert ledger.snapshot() == before


def test_duplicate_finish_has_zero_mutation():
    ledger = RoleBudgetLedger()
    call_id = ledger.begin(MANAGER)
    _finish_zero(ledger, call_id)
    before = ledger.snapshot()

    with pytest.raises(BudgetValidationError, match="already finished"):
        _finish_zero(ledger, call_id)

    assert ledger.snapshot() == before


def test_unknown_begin_role_has_zero_mutation():
    ledger = RoleBudgetLedger()
    before = ledger.snapshot()
    with pytest.raises(BudgetValidationError):
        ledger.begin("reviewer")
    assert ledger.snapshot() == before


def test_concurrent_updates_have_no_lost_calls_or_usage():
    ledger = RoleBudgetLedger()
    roles = [MANAGER] * 3 + [ARCHITECT] * 3 + [DEVELOPER] * 9

    def invoke(role):
        call_id = ledger.begin(role, input_hash=sha256_text(f"{role}-{id(role)}"))
        ledger.finish(
            call_id,
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            latency_seconds=0.01,
            output_hash=sha256_text(call_id),
        )

    with ThreadPoolExecutor(max_workers=len(roles)) as executor:
        list(executor.map(invoke, roles))

    snapshot = ledger.snapshot()
    assert snapshot["shared"]["calls"] == 15
    assert snapshot["shared"]["prompt_tokens"] == 30
    assert snapshot["shared"]["completion_tokens"] == 15
    assert snapshot["shared"]["total_tokens"] == 45
    assert snapshot["shared"]["pending_calls"] == 0
    assert snapshot["shared"]["calls"] == sum(
        role["calls"] for role in snapshot["roles"].values()
    )
    assert snapshot["shared"]["total_tokens"] == sum(
        role["total_tokens"] for role in snapshot["roles"].values()
    )


def test_snapshot_is_a_deep_copy_and_shared_always_sums_roles():
    ledger = RoleBudgetLedger()
    call_id = ledger.begin(MANAGER)
    ledger.finish(call_id, prompt_tokens=7, completion_tokens=3)

    first = ledger.snapshot()
    first["shared"]["calls"] = 999
    first["roles"][MANAGER]["records"][0]["total_tokens"] = 999

    second = ledger.snapshot()
    assert second["shared"]["calls"] == 1
    assert second["roles"][MANAGER]["records"][0]["total_tokens"] == 10
    for key in (
        "calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "failures",
        "pending_calls",
    ):
        assert second["shared"][key] == sum(
            role[key] for role in second["roles"].values()
        )
