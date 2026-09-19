from __future__ import annotations

import json

import pytest

from exp3.budget import BudgetExceeded, RoleBudgetLedger
from exp3.provider_accounting import (
    ProviderAccountingError,
    ProviderCallAccountingAdapter,
    ProviderCallResult,
)


class StepClock:
    def __init__(self, values: list[float]):
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_accounts_every_provider_call_not_only_each_topology_stage() -> None:
    ledger = RoleBudgetLedger()
    calls = []

    def provider(*, role, phase, request):
        calls.append((role, phase, request))
        return ProviderCallResult(
            output={"turn": request["turn"]},
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
        )

    adapter = ProviderCallAccountingAdapter(
        provider=provider,
        ledger=ledger,
        clock=StepClock([0.0, 0.1, 1.0, 1.2, 2.0, 2.3]),
    )

    assert adapter.invoke(
        role="architect", phase="architect_to_manager", request={"turn": 1}
    ) == {"turn": 1}
    assert adapter.invoke(
        role="architect", phase="architect_to_manager", request={"turn": 2}
    ) == {"turn": 2}
    assert adapter.invoke(
        role="architect", phase="architect_to_manager", request={"turn": 3}
    ) == {"turn": 3}

    snapshot = ledger.snapshot()
    assert snapshot["shared"]["calls"] == 3
    assert snapshot["roles"]["architect"]["calls"] == 3
    assert snapshot["roles"]["architect"]["total_tokens"] == 36
    assert snapshot["roles"]["architect"]["latency_seconds"] == pytest.approx(0.6)
    with pytest.raises(BudgetExceeded, match="architect call budget"):
        adapter.invoke(
            role="architect", phase="architect_to_manager", request={"turn": 4}
        )
    assert len(calls) == 3


def test_failed_provider_call_counts_call_tokens_latency_and_no_body() -> None:
    secret = "PRIVATE provider body must not be logged"
    ledger = RoleBudgetLedger()

    class ProviderFailure(RuntimeError):
        prompt_tokens = 7
        completion_tokens = 3
        total_tokens = 10

    def provider(**_kwargs):
        raise ProviderFailure(secret)

    adapter = ProviderCallAccountingAdapter(
        provider=provider,
        ledger=ledger,
        clock=StepClock([4.0, 4.25]),
    )

    with pytest.raises(ProviderFailure, match="PRIVATE"):
        adapter.invoke(role="developer", phase="developer_to_manager", request={"x": secret})

    snapshot = ledger.snapshot()
    developer = snapshot["roles"]["developer"]
    assert developer["calls"] == 1
    assert developer["failures"] == 1
    assert developer["total_tokens"] == 10
    assert developer["latency_seconds"] == 0.25
    safe = json.dumps({"records": adapter.records(), "budget": snapshot})
    assert secret not in safe
    assert adapter.records()[0]["failure_type"] == "ProviderFailure"


def test_pre_call_check_runs_before_every_reservation_and_provider_call() -> None:
    ledger = RoleBudgetLedger()
    checks = []
    provider_calls = []

    def check(*, role, phase):
        checks.append((role, phase))
        if len(checks) == 2:
            raise PermissionError("blocked before provider")

    def provider(**kwargs):
        provider_calls.append(kwargs)
        return ProviderCallResult({}, 1, 1)

    adapter = ProviderCallAccountingAdapter(
        provider=provider,
        ledger=ledger,
        pre_call_check=check,
        clock=StepClock([0.0, 0.1]),
    )
    adapter.invoke(role="manager", phase="manager_to_architect", request={})
    with pytest.raises(PermissionError):
        adapter.invoke(role="manager", phase="manager_to_developer", request={})

    assert len(checks) == 2
    assert len(provider_calls) == 1
    assert ledger.snapshot()["shared"]["calls"] == 1


def test_invalid_provider_usage_closes_pending_call_once() -> None:
    ledger = RoleBudgetLedger()

    def provider(**_kwargs):
        return ProviderCallResult({}, prompt_tokens=True, completion_tokens=0)

    adapter = ProviderCallAccountingAdapter(
        provider=provider,
        ledger=ledger,
        clock=StepClock([0.0, 0.2]),
    )

    with pytest.raises(ProviderAccountingError, match="usage metadata"):
        adapter.invoke(role="manager", phase="manager_final", request={})

    manager = ledger.snapshot()["roles"]["manager"]
    assert manager["calls"] == 1
    assert manager["pending_calls"] == 0
    assert manager["failures"] == 1
    assert adapter.records()[0]["failure_type"] == "BudgetValidationError"


def test_success_records_only_hashes_and_returns_a_detached_output() -> None:
    secret = "PRIVATE result text"
    output = {"answer": secret}
    adapter = ProviderCallAccountingAdapter(
        provider=lambda **_kwargs: ProviderCallResult(output, 2, 1),
        ledger=RoleBudgetLedger(),
        clock=StepClock([0.0, 0.5]),
    )

    returned = adapter.invoke(
        role="developer", phase="developer_to_manager", request={"prompt": secret}
    )
    returned.clear()

    record = adapter.records()[0]
    assert set(record) == {
        "call_id",
        "role",
        "phase",
        "status",
        "failure_type",
        "input_sha256",
        "output_sha256",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_seconds",
    }
    assert secret not in json.dumps(record)
    assert len(record["input_sha256"]) == 64
    assert len(record["output_sha256"]) == 64
    assert output == {"answer": secret}
