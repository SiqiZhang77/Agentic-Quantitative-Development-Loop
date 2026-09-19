"""Thread-safe shared and per-role accounting for Experiment 3.

Calls are reserved by :meth:`RoleBudgetLedger.begin` before invoking a model, so
provider failures still consume call capacity.  Usage is committed atomically by
``finish``.  If returned usage exceeds a token cap, the complete observation is
recorded first and :class:`BudgetExceeded` is raised afterwards.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections.abc import Mapping
from threading import RLock
from types import MappingProxyType
from typing import Final

from exp3.policy import ARCHITECT, DEVELOPER, MANAGER, ROLES, validate_role


SHARED_MAX_CALLS: Final = 20
SHARED_MAX_TOKENS: Final = 350_000
ROLE_LIMITS: Final = MappingProxyType(
    {
        # Call reservations preserve the fixed topology. Tokens are intentionally
        # pooled: every role sees the same run-wide ceiling, so unused manager or
        # architect capacity is available to the developer without increasing
        # the total resource offered to M1.
        MANAGER: MappingProxyType(
            {"max_calls": 3, "max_tokens": SHARED_MAX_TOKENS}
        ),
        ARCHITECT: MappingProxyType(
            {"max_calls": 3, "max_tokens": SHARED_MAX_TOKENS}
        ),
        DEVELOPER: MappingProxyType(
            {"max_calls": 14, "max_tokens": SHARED_MAX_TOKENS}
        ),
    }
)
SINGLE_AGENT_ROLE_LIMITS: Final = MappingProxyType(
    {
        MANAGER: MappingProxyType({"max_calls": 0, "max_tokens": 0}),
        ARCHITECT: MappingProxyType({"max_calls": 0, "max_tokens": 0}),
        DEVELOPER: MappingProxyType(
            {"max_calls": SHARED_MAX_CALLS, "max_tokens": SHARED_MAX_TOKENS}
        ),
    }
)


class BudgetError(RuntimeError):
    """Base class for Experiment 3 accounting failures."""


class BudgetValidationError(BudgetError, ValueError):
    """An invalid accounting operation that must not mutate the ledger."""


class BudgetExceeded(BudgetError):
    """A shared or role-specific hard cap was reached or exceeded."""

    def __init__(
        self,
        message: str,
        *,
        role: str | None = None,
        call_id: str | None = None,
        snapshot: dict | None = None,
    ) -> None:
        self.role = role
        self.call_id = call_id
        self.snapshot = snapshot
        super().__init__(message)


def sha256_text(value: str) -> str:
    """Return a content hash without retaining the model input or output."""

    if type(value) is not str:
        raise BudgetValidationError("value to hash must be a string")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_hash(name: str, value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or re.fullmatch(r"[A-Fa-f0-9]{64}", value) is None:
        raise BudgetValidationError(f"{name} must be a 64-character SHA256 or None")
    return value.lower()


def _token_count(name: str, value: object) -> int:
    # bool is an int subclass, but accepting True as one token masks caller bugs.
    if type(value) is not int or value < 0:
        raise BudgetValidationError(f"{name} must be a non-negative integer")
    return value


def _latency(value: object) -> float:
    if type(value) not in (int, float):
        raise BudgetValidationError("latency_seconds must be a non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise BudgetValidationError("latency_seconds must be a finite non-negative number")
    return result


def _failure_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, BaseException):
        return type(value).__name__
    if type(value) is str and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
        return value
    raise BudgetValidationError(
        "failure must be a content-free failure code, exception, or None"
    )


def _empty_role_usage() -> dict:
    return {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "failures": 0,
        "latency_seconds": 0.0,
        "pending_calls": 0,
        "records": [],
    }


class RoleBudgetLedger:
    """Atomic shared plus per-role call/token ledger.

    Call capacity remains role-bound so M1 cannot change its topology. Token
    capacity is pooled by setting every active M1 role to the same shared ceiling;
    the shared limit is therefore the binding token guard. The sum of the three
    usage buckets is the shared snapshot, avoiding a second mutable total that
    could drift under concurrency.
    """

    def __init__(
        self,
        *,
        role_limits: Mapping[str, Mapping[str, int]] | None = None,
        shared_max_calls: int = SHARED_MAX_CALLS,
        shared_max_tokens: int = SHARED_MAX_TOKENS,
    ) -> None:
        selected_limits = ROLE_LIMITS if role_limits is None else role_limits
        if set(selected_limits) != set(ROLES):
            raise BudgetValidationError("role limits must define exactly the fixed roles")
        if type(shared_max_calls) is not int or shared_max_calls <= 0:
            raise BudgetValidationError("shared_max_calls must be a positive integer")
        if type(shared_max_tokens) is not int or shared_max_tokens <= 0:
            raise BudgetValidationError("shared_max_tokens must be a positive integer")
        normalized_limits: dict[str, MappingProxyType] = {}
        for role in sorted(ROLES):
            value = selected_limits[role]
            calls = value.get("max_calls")
            tokens = value.get("max_tokens")
            if type(calls) is not int or calls < 0:
                raise BudgetValidationError("role max_calls must be non-negative")
            if type(tokens) is not int or tokens < 0:
                raise BudgetValidationError("role max_tokens must be non-negative")
            normalized_limits[role] = MappingProxyType(
                {"max_calls": calls, "max_tokens": tokens}
            )
        if sum(item["max_calls"] for item in normalized_limits.values()) < shared_max_calls:
            raise BudgetValidationError(
                "role call capacities cannot be smaller than the shared call cap"
            )
        if sum(item["max_tokens"] for item in normalized_limits.values()) < shared_max_tokens:
            raise BudgetValidationError(
                "role token capacities cannot be smaller than the shared token cap"
            )
        self._lock = RLock()
        self._role_limits = MappingProxyType(normalized_limits)
        self._shared_max_calls = shared_max_calls
        self._shared_max_tokens = shared_max_tokens
        self._roles = {role: _empty_role_usage() for role in sorted(ROLES)}
        self._calls: dict[str, dict] = {}
        self._next_call = 1

    @classmethod
    def for_single_agent(cls) -> "RoleBudgetLedger":
        """Create the M0 ledger: one developer owns only the shared capacity.

        M0 and M1 receive the same 20-call, 350,000-token total. Manager and
        architect have zero capacity in this ledger because M0 has no such stages.
        """

        return cls(role_limits=SINGLE_AGENT_ROLE_LIMITS)

    def _shared_totals_locked(self) -> dict[str, int | float]:
        numeric_keys = (
            "calls",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "failures",
            "latency_seconds",
            "pending_calls",
        )
        totals: dict[str, int | float] = {}
        for key in numeric_keys:
            totals[key] = sum(self._roles[role][key] for role in sorted(ROLES))
        totals["latency_seconds"] = round(float(totals["latency_seconds"]), 6)
        return totals

    def begin(self, role: object, *, input_hash: object = None) -> str:
        """Reserve one call atomically before a model invocation."""

        try:
            known_role = validate_role(role)
        except ValueError as exc:
            raise BudgetValidationError(str(exc)) from exc
        checked_input_hash = _optional_hash("input_hash", input_hash)

        with self._lock:
            role_usage = self._roles[known_role]
            role_limit = self._role_limits[known_role]
            shared = self._shared_totals_locked()
            if role_usage["calls"] >= role_limit["max_calls"]:
                raise BudgetExceeded(
                    f"{known_role} call budget ({role_limit['max_calls']}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if shared["calls"] >= self._shared_max_calls:
                raise BudgetExceeded(
                    f"shared call budget ({self._shared_max_calls}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if role_usage["total_tokens"] >= role_limit["max_tokens"]:
                raise BudgetExceeded(
                    f"{known_role} token budget ({role_limit['max_tokens']}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if shared["total_tokens"] >= self._shared_max_tokens:
                raise BudgetExceeded(
                    f"shared token budget ({self._shared_max_tokens}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )

            call_id = f"exp3-{self._next_call:06d}"
            self._next_call += 1
            record = {
                "call_id": call_id,
                "role": known_role,
                "status": "pending",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "latency_seconds": 0.0,
                "failed": False,
                "failure": None,
                "input_hash": checked_input_hash,
                "output_hash": None,
            }
            self._calls[call_id] = record
            role_usage["calls"] += 1
            role_usage["pending_calls"] += 1
            role_usage["records"].append(record)
            return call_id

    begin_call = begin

    def ensure_capacity(self, role: object) -> None:
        """Fail without mutation when no shared/role capacity remains."""

        try:
            known_role = validate_role(role)
        except ValueError as exc:
            raise BudgetValidationError(str(exc)) from exc
        with self._lock:
            role_usage = self._roles[known_role]
            role_limit = self._role_limits[known_role]
            shared = self._shared_totals_locked()
            if role_usage["calls"] >= role_limit["max_calls"]:
                raise BudgetExceeded(
                    f"{known_role} call budget ({role_limit['max_calls']}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if shared["calls"] >= self._shared_max_calls:
                raise BudgetExceeded(
                    f"shared call budget ({self._shared_max_calls}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if role_usage["total_tokens"] >= role_limit["max_tokens"]:
                raise BudgetExceeded(
                    f"{known_role} token budget ({role_limit['max_tokens']}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )
            if shared["total_tokens"] >= self._shared_max_tokens:
                raise BudgetExceeded(
                    f"shared token budget ({self._shared_max_tokens}) exhausted",
                    role=known_role,
                    snapshot=self._snapshot_locked(),
                )

    def finish(
        self,
        call_id: object,
        *,
        prompt_tokens: object,
        completion_tokens: object,
        total_tokens: object = None,
        latency_seconds: object = 0.0,
        failure: object = None,
        failed: object = None,
        input_hash: object = None,
        output_hash: object = None,
        role: object = None,
    ) -> dict:
        """Finish one reservation and return a copy of its accounting record.

        Every argument is validated before mutation.  Unknown/duplicate calls,
        unknown roles, boolean or negative usage, and malformed metadata therefore
        leave the ledger byte-for-byte unchanged.  A valid over-budget response is
        different: it is fully recorded, then ``BudgetExceeded`` is raised.
        """

        if type(call_id) is not str or not call_id:
            raise BudgetValidationError("call_id must be a non-empty string")
        prompt = _token_count("prompt_tokens", prompt_tokens)
        completion = _token_count("completion_tokens", completion_tokens)
        computed_total = prompt + completion
        if total_tokens is not None:
            reported_total = _token_count("total_tokens", total_tokens)
            if reported_total != computed_total:
                raise BudgetValidationError(
                    "total_tokens must equal prompt_tokens + completion_tokens"
                )
        total = computed_total
        latency = _latency(latency_seconds)
        failure_text = _failure_text(failure)
        if failed is not None and type(failed) is not bool:
            raise BudgetValidationError("failed must be a boolean or None")
        if failed is False and failure_text is not None:
            raise BudgetValidationError("failed=False conflicts with a failure value")
        is_failed = bool(failed) if failed is not None else failure_text is not None
        checked_input_hash = _optional_hash("input_hash", input_hash)
        checked_output_hash = _optional_hash("output_hash", output_hash)

        checked_role: str | None = None
        if role is not None:
            try:
                checked_role = validate_role(role)
            except ValueError as exc:
                raise BudgetValidationError(str(exc)) from exc

        violation: str | None = None
        violation_role: str | None = None
        with self._lock:
            record = self._calls.get(call_id)
            if record is None:
                raise BudgetValidationError(f"unknown call_id {call_id!r}")
            if record["status"] != "pending":
                raise BudgetValidationError(f"call_id {call_id!r} was already finished")
            actual_role = record["role"]
            if checked_role is not None and checked_role != actual_role:
                raise BudgetValidationError(
                    f"call_id {call_id!r} belongs to {actual_role!r}, not {checked_role!r}"
                )
            if (
                checked_input_hash is not None
                and record["input_hash"] is not None
                and checked_input_hash != record["input_hash"]
            ):
                raise BudgetValidationError(
                    f"input_hash for call_id {call_id!r} does not match begin()"
                )

            role_usage = self._roles[actual_role]
            record.update(
                {
                    "status": "finished",
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": total,
                    "latency_seconds": latency,
                    "failed": is_failed,
                    "failure": failure_text,
                    "input_hash": checked_input_hash or record["input_hash"],
                    "output_hash": checked_output_hash,
                }
            )
            role_usage["prompt_tokens"] += prompt
            role_usage["completion_tokens"] += completion
            role_usage["total_tokens"] += total
            role_usage["latency_seconds"] = round(
                role_usage["latency_seconds"] + latency, 6
            )
            role_usage["pending_calls"] -= 1
            if is_failed:
                role_usage["failures"] += 1

            shared = self._shared_totals_locked()
            role_limit = self._role_limits[actual_role]
            violations = []
            if role_usage["total_tokens"] > role_limit["max_tokens"]:
                violations.append(
                    f"{actual_role} token budget ({role_limit['max_tokens']}) exceeded "
                    f"with {role_usage['total_tokens']}"
                )
            if shared["total_tokens"] > self._shared_max_tokens:
                violations.append(
                    f"shared token budget ({self._shared_max_tokens}) exceeded "
                    f"with {shared['total_tokens']}"
                )
            if violations:
                violation = "; ".join(violations)
                violation_role = actual_role
            result = copy.deepcopy(record)
            exceeded_snapshot = self._snapshot_locked() if violation else None

        if violation:
            raise BudgetExceeded(
                violation,
                role=violation_role,
                call_id=call_id,
                snapshot=exceeded_snapshot,
            )
        return result

    finish_call = finish

    def _snapshot_locked(self) -> dict:
        shared = self._shared_totals_locked()
        roles = copy.deepcopy(self._roles)
        for role, usage in roles.items():
            usage["max_calls"] = self._role_limits[role]["max_calls"]
            usage["max_tokens"] = self._role_limits[role]["max_tokens"]
            usage["over_budget"] = (
                usage["calls"] > usage["max_calls"]
                or usage["total_tokens"] > usage["max_tokens"]
            )
        shared.update(
            {
                "max_calls": self._shared_max_calls,
                "max_tokens": self._shared_max_tokens,
                "over_budget": (
                    shared["calls"] > self._shared_max_calls
                    or shared["total_tokens"] > self._shared_max_tokens
                ),
            }
        )
        return {
            "shared": shared,
            "roles": roles,
        }

    def snapshot(self) -> dict:
        """Return a deep copy whose shared counters equal the sum of role counters."""

        with self._lock:
            return self._snapshot_locked()


BudgetLedger = RoleBudgetLedger
