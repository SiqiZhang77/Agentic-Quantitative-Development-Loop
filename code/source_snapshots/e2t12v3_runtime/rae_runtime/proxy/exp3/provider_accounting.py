"""Per-provider-call accounting boundary for Experiment 3.

The wrapper in this module is deliberately provider-agnostic and dependency
injected.  A production Agents SDK model adapter can call :meth:`invoke` for
every provider request, while offline tests supply a plain Python fake.  The
shared/role ledger is reserved immediately before the provider function and is
finished on every terminal path, including provider errors and malformed usage.

Only hashes, counts, latency and content-free failure class names are retained.
Prompt or completion bodies never enter the accounting records.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import Any

from exp3.budget import (
    BudgetExceeded,
    BudgetValidationError,
    RoleBudgetLedger,
    sha256_text,
)
from exp3.contracts import canonical_json
from exp3.policy import validate_role


class ProviderAccountingError(RuntimeError):
    """A provider result could not be safely and completely accounted."""


class ProviderResultError(ProviderAccountingError, TypeError):
    """The injected provider returned an unsupported result shape."""


@dataclass(frozen=True)
class ProviderCallResult:
    """Provider output and authoritative token usage for one request.

    ``latency_seconds`` is retained for adapter diagnostics, but the accounting
    boundary records its own monotonic wall latency around the actual callable.
    """

    output: Any
    prompt_tokens: int
    completion_tokens: int
    latency_seconds: float = 0.0
    total_tokens: int | None = None


ProviderCallable = Callable[..., ProviderCallResult]
PreCallCheck = Callable[..., Any]


def _failure_usage(exc: BaseException) -> tuple[int, int, int | None]:
    """Extract optional usage attached to a failed provider invocation.

    Real provider wrappers may attach non-negative integer token fields to the
    raised exception.  Invalid or absent fields are treated as unavailable (zero)
    rather than copied into telemetry or allowed to leave a pending reservation.
    """

    prompt = getattr(exc, "prompt_tokens", 0)
    completion = getattr(exc, "completion_tokens", 0)
    total = getattr(exc, "total_tokens", None)
    if type(prompt) is not int or prompt < 0:
        prompt = 0
    if type(completion) is not int or completion < 0:
        completion = 0
    if type(total) is not int or total < 0 or total != prompt + completion:
        total = None
    return prompt, completion, total


class ProviderCallAccountingAdapter:
    """Wrap one injected provider and account every invocation atomically."""

    def __init__(
        self,
        *,
        provider: ProviderCallable,
        ledger: RoleBudgetLedger,
        pre_call_check: PreCallCheck | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not callable(provider):
            raise TypeError("provider must be callable")
        if not isinstance(ledger, RoleBudgetLedger):
            raise TypeError("ledger must be a RoleBudgetLedger")
        if pre_call_check is not None and not callable(pre_call_check):
            raise TypeError("pre_call_check must be callable or None")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._provider = provider
        self._ledger = ledger
        self._pre_call_check = pre_call_check
        self._clock = clock
        self._records: list[dict[str, Any]] = []
        self._records_lock = RLock()

    @property
    def ledger(self) -> RoleBudgetLedger:
        return self._ledger

    def records(self) -> list[dict[str, Any]]:
        """Return detached, content-free records in provider-call order."""

        with self._records_lock:
            return copy.deepcopy(self._records)

    def _append(self, record: Mapping[str, Any]) -> None:
        with self._records_lock:
            self._records.append(copy.deepcopy(dict(record)))

    def _close_failed(
        self,
        *,
        call_id: str,
        role: str,
        phase: str,
        input_hash: str,
        latency_seconds: float,
        failure: BaseException,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int | None = None,
    ) -> None:
        """Finish a failed reservation and append only safe metadata."""

        failure_type = type(failure).__name__
        try:
            accounting = self._ledger.finish(
                call_id,
                role=role,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_seconds=latency_seconds,
                failure=failure_type,
                input_hash=input_hash,
                output_hash=None,
            )
        except BudgetExceeded as budget_exc:
            self._append(
                {
                    "call_id": call_id,
                    "role": role,
                    "phase": phase,
                    "status": "failed",
                    "failure_type": type(budget_exc).__name__,
                    "input_sha256": input_hash,
                    "output_sha256": None,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "latency_seconds": latency_seconds,
                }
            )
            raise
        self._append(
            {
                "call_id": call_id,
                "role": role,
                "phase": phase,
                "status": "failed",
                "failure_type": failure_type,
                "input_sha256": input_hash,
                "output_sha256": None,
                "prompt_tokens": accounting["prompt_tokens"],
                "completion_tokens": accounting["completion_tokens"],
                "total_tokens": accounting["total_tokens"],
                "latency_seconds": accounting["latency_seconds"],
            }
        )

    def invoke(
        self,
        *,
        role: str,
        phase: str,
        request: Any,
    ) -> Any:
        """Run one provider request under preflight, shared and role caps.

        The pre-call check runs before reserving a model call.  A failed
        contamination/isolation check therefore stops execution without falsely
        claiming that a provider request occurred.
        """

        known_role = validate_role(role)
        if type(phase) is not str or not phase or len(phase) > 128:
            raise ProviderAccountingError("phase must be a non-empty bounded string")
        try:
            canonical_input = canonical_json(request)
        except (TypeError, ValueError) as exc:
            raise ProviderAccountingError(
                "provider request must be canonical JSON-compatible"
            ) from exc
        input_hash = sha256_text(canonical_input)

        if self._pre_call_check is not None:
            self._pre_call_check(role=known_role, phase=phase)

        call_id = self._ledger.begin(known_role, input_hash=input_hash)
        started = self._clock()
        try:
            result = self._provider(
                role=known_role,
                phase=phase,
                request=copy.deepcopy(request),
            )
        except BaseException as exc:
            elapsed = max(0.0, float(self._clock() - started))
            prompt, completion, total = _failure_usage(exc)
            self._close_failed(
                call_id=call_id,
                role=known_role,
                phase=phase,
                input_hash=input_hash,
                latency_seconds=elapsed,
                failure=exc,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
            )
            raise

        elapsed = max(0.0, float(self._clock() - started))
        if not isinstance(result, ProviderCallResult):
            error = ProviderResultError(
                "provider must return ProviderCallResult"
            )
            self._close_failed(
                call_id=call_id,
                role=known_role,
                phase=phase,
                input_hash=input_hash,
                latency_seconds=elapsed,
                failure=error,
            )
            raise error

        try:
            output_canonical = canonical_json(result.output)
            output_hash = sha256_text(output_canonical)
            accounting = self._ledger.finish(
                call_id,
                role=known_role,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens,
                latency_seconds=elapsed,
                input_hash=input_hash,
                output_hash=output_hash,
            )
        except BudgetValidationError as exc:
            # finish() guarantees a validation error has not mutated the pending
            # record. Close it once with zero usage so accounting stays terminal.
            self._close_failed(
                call_id=call_id,
                role=known_role,
                phase=phase,
                input_hash=input_hash,
                latency_seconds=elapsed,
                failure=exc,
            )
            raise ProviderAccountingError(
                "provider returned invalid usage metadata"
            ) from exc
        except (TypeError, ValueError) as exc:
            self._close_failed(
                call_id=call_id,
                role=known_role,
                phase=phase,
                input_hash=input_hash,
                latency_seconds=elapsed,
                failure=exc,
            )
            raise ProviderAccountingError(
                "provider output must be canonical JSON-compatible"
            ) from exc
        except BudgetExceeded as exc:
            self._append(
                {
                    "call_id": call_id,
                    "role": known_role,
                    "phase": phase,
                    "status": "failed",
                    "failure_type": type(exc).__name__,
                    "input_sha256": input_hash,
                    "output_sha256": output_hash,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                    "total_tokens": (
                        result.total_tokens
                        if result.total_tokens is not None
                        else result.prompt_tokens + result.completion_tokens
                    ),
                    "latency_seconds": elapsed,
                }
            )
            raise

        self._append(
            {
                "call_id": call_id,
                "role": known_role,
                "phase": phase,
                "status": "succeeded",
                "failure_type": None,
                "input_sha256": input_hash,
                "output_sha256": output_hash,
                "prompt_tokens": accounting["prompt_tokens"],
                "completion_tokens": accounting["completion_tokens"],
                "total_tokens": accounting["total_tokens"],
                "latency_seconds": accounting["latency_seconds"],
            }
        )
        return copy.deepcopy(result.output)
