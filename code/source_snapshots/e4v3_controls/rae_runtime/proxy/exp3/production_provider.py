"""Fail-closed model-level provider boundary for formal Experiment 3 runs.

The Agents SDK runner can make more than one model request during one run, so
wrapping ``Runner.run`` would merge calls and violate the registered budget.
This module instead wraps the model object's single-request ``get_response``
method.  Each request passes isolation preflight, reserves shared/role capacity,
invokes the provider once, and closes accounting on success or failure.

No Agents SDK, LiteLLM, GitHub or network module is imported at module load.
Offline tests use duck-typed fake models and injected ref probes.  The formal
environment loader is fail-closed and performs remote ref reads only when an E3
run explicitly calls ``start`` without an injected probe.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import fields as dataclass_fields, is_dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from exp3.architecture import (
    MANAGER_STAR,
    SINGLE_AGENT,
    explicit_rag_enabled,
    validate_explicit_e3_request,
)
from exp3.attempt_control import attempt_number, validate_two_attempt_controls
from exp3.budget import (
    BudgetExceeded,
    BudgetValidationError,
    RoleBudgetLedger,
    sha256_text,
)
from exp3.contracts import canonical_json
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.policy import DEVELOPER, validate_role
from provider_config import (
    ProviderConfig,
    resolve_provider_config,
    validate_provider_identity,
)


MANIFEST_PATH_ENV = "E3_NEGATIVE_REF_MANIFEST_PATH"
MANIFEST_HASH_ENV = "E3_NEGATIVE_REF_MANIFEST_SHA256"
DENY_SET_HASH_ENV = "E3_NEGATIVE_REF_SET_SHA256"
RUN_IDENTITY_HASH_ENV = "E3_RUN_IDENTITY_SHA256"
_PHASE_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class ProductionProviderBoundaryError(RuntimeError):
    """The formal E3 provider boundary is absent, inconsistent or bypassed."""


class ProductionProviderBoundaryUnavailable(ProductionProviderBoundaryError):
    """Required formal manifest/boundary configuration was not supplied."""


class ProviderUsageError(ProductionProviderBoundaryError):
    """One completed provider request did not report exact per-call usage."""


class StreamingProviderCallForbidden(ProductionProviderBoundaryError):
    """Streaming is disabled because its usage cannot be closed before output."""


def _safe_hash(value: object, *, name: str) -> str:
    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise ProductionProviderBoundaryUnavailable(
            f"{name} must be a lowercase SHA-256"
        )
    return value


def _jsonable(value: Any, *, _seen: set[int] | None = None) -> Any:
    """Create a deterministic hash input without retaining it in telemetry."""

    if value is None or type(value) in {str, int, bool}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ProductionProviderBoundaryError(
                "provider hash input contains a non-finite number"
            )
        return value
    if type(value) is bytes:
        return {
            "type": "bytes",
            "length": len(value),
            "sha256": sha256_text(value.hex()),
        }
    if isinstance(value, asyncio.Future):
        # MCP SDK tool wrappers may retain live session Futures. They are
        # transport state, not model-request content, and cannot be deep-copied.
        return {
            "runtime_object": "asyncio_future",
            "type": f"{type(value).__module__}.{type(value).__qualname__}",
        }
    if callable(value):
        # Tool callback identity is represented without serialising closures or
        # bound runtime state. Tool name/schema remain available in their
        # surrounding dataclass fields.
        return {
            "callable": True,
            "module": str(getattr(value, "__module__", type(value).__module__)),
            "qualname": str(
                getattr(value, "__qualname__", type(value).__qualname__)
            ),
        }

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        raise ProductionProviderBoundaryError(
            "provider hash input contains a reference cycle"
        )
    seen.add(identity)
    try:
        if isinstance(value, Mapping):
            if any(type(key) is not str for key in value):
                raise ProductionProviderBoundaryError(
                    "provider hash mappings require string keys"
                )
            return {
                key: _jsonable(value[key], _seen=seen)
                for key in sorted(value)
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return [_jsonable(item, _seen=seen) for item in value]
        if is_dataclass(value) and not isinstance(value, type):
            # dataclasses.asdict performs copy.deepcopy and fails on live MCP
            # session objects such as asyncio.Future. Traverse fields directly.
            return {
                item.name: _jsonable(getattr(value, item.name), _seen=seen)
                for item in dataclass_fields(value)
            }
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _jsonable(model_dump(mode="json"), _seen=seen)
        public = getattr(value, "__dict__", None)
        if isinstance(public, Mapping):
            safe_public = {
                key: item
                for key, item in public.items()
                if type(key) is str
                and not key.startswith("_")
                and not callable(item)
            }
            return {
                "type": f"{type(value).__module__}.{type(value).__qualname__}",
                "value": _jsonable(safe_public, _seen=seen),
            }
    finally:
        seen.remove(identity)

    raise ProductionProviderBoundaryError(
        "provider request/response contains an unsupported hash value"
    )


def _payload_hash(value: Any) -> str:
    return sha256_text(canonical_json(_jsonable(value)))


def _usage_value(usage: object, name: str) -> object:
    if isinstance(usage, Mapping):
        return usage.get(name)
    return getattr(usage, name, None)


def _extract_usage(response: object) -> tuple[int, int, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, Mapping):
        usage = response.get("usage")
    if usage is None:
        raise ProviderUsageError("provider response omitted per-call usage")

    requests = _usage_value(usage, "requests")
    prompt = _usage_value(usage, "input_tokens")
    completion = _usage_value(usage, "output_tokens")
    total = _usage_value(usage, "total_tokens")
    if requests != 1:
        raise ProviderUsageError(
            "provider usage must report exactly one request; retries are forbidden"
        )
    for name, value in (
        ("input_tokens", prompt),
        ("output_tokens", completion),
        ("total_tokens", total),
    ):
        if type(value) is not int or value < 0:
            raise ProviderUsageError(
                f"provider usage {name} must be a non-negative integer"
            )
    if total != prompt + completion:
        raise ProviderUsageError(
            "provider total_tokens must equal input_tokens plus output_tokens"
        )
    return prompt, completion, total


def _failure_usage(exc: BaseException) -> tuple[int, int, int]:
    prompt = getattr(exc, "input_tokens", getattr(exc, "prompt_tokens", 0))
    completion = getattr(
        exc, "output_tokens", getattr(exc, "completion_tokens", 0)
    )
    total = getattr(exc, "total_tokens", None)
    if type(prompt) is not int or prompt < 0:
        prompt = 0
    if type(completion) is not int or completion < 0:
        completion = 0
    if type(total) is not int or total != prompt + completion:
        total = prompt + completion
    return prompt, completion, total


class AccountedAgentsModel:
    """Duck-typed Agents SDK model proxy with one-call accounting."""

    def __init__(
        self,
        *,
        model: object,
        boundary: "Experiment3ProductionProviderBoundary",
        role: str,
        phase: str,
    ) -> None:
        if not callable(getattr(model, "get_response", None)):
            raise ProductionProviderBoundaryError(
                "Agents model must expose async get_response"
            )
        self._model = model
        self._boundary = boundary
        self._role = role
        self._phase = phase

    def __getattr__(self, name: str) -> Any:
        return getattr(self._model, name)

    async def close(self) -> None:
        close = getattr(self._model, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "__await__"):
                await result

    def get_retry_advice(self, request: object) -> object | None:
        advice = getattr(self._model, "get_retry_advice", None)
        return advice(request) if callable(advice) else None

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        return await self._boundary.invoke_model_call(
            model=self._model,
            role=self._role,
            phase=self._phase,
            args=args,
            kwargs=kwargs,
        )

    async def stream_response(self, *args: Any, **kwargs: Any):
        del args, kwargs
        # Fail before touching the wrapped model. Buffering SDK streams would
        # change event semantics, while yielding early would let unaccounted
        # output escape before final usage exists. Formal E3 therefore uses the
        # non-streaming get_response path only.
        raise StreamingProviderCallForbidden(
            "Experiment 3 forbids streaming provider calls"
        )
        yield  # pragma: no cover - keeps this an async generator for the SDK.


class Experiment3ProductionProviderBoundary:
    """One run-scoped boundary shared by every model object in an E3 run."""

    def __init__(
        self,
        *,
        architecture_mode: str,
        preflight: NegativeRefPreflight,
        ledger: RoleBudgetLedger,
        provider_config: ProviderConfig,
        rag_enabled: bool = False,
        identity_sha256: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if architecture_mode not in {SINGLE_AGENT, MANAGER_STAR}:
            raise ProductionProviderBoundaryError("unsupported E3 architecture mode")
        if not isinstance(preflight, NegativeRefPreflight):
            raise TypeError("preflight must be a NegativeRefPreflight")
        if not isinstance(ledger, RoleBudgetLedger):
            raise TypeError("ledger must be a RoleBudgetLedger")
        if not isinstance(provider_config, ProviderConfig):
            raise TypeError("provider_config must be a ProviderConfig")
        if type(rag_enabled) is not bool:
            raise TypeError("rag_enabled must be a boolean")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.architecture_mode = architecture_mode
        self.preflight = preflight
        self.ledger = ledger
        self.provider_config = provider_config
        self.rag_enabled = rag_enabled
        self.identity_sha256 = (
            _safe_hash(identity_sha256, name=RUN_IDENTITY_HASH_ENV)
            if identity_sha256 is not None
            else None
        )
        self._clock = clock
        self._records: list[dict[str, Any]] = []
        self._lock = RLock()
        self._started_payload_sha256: str | None = None

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started_payload_sha256 is not None

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Validate identity and run negative-ref preflight before orchestration."""

        mode = validate_explicit_e3_request(payload)
        if mode != self.architecture_mode:
            raise ProductionProviderBoundaryError(
                "provider boundary architecture does not match request"
            )
        if explicit_rag_enabled(payload) is not self.rag_enabled:
            raise ProductionProviderBoundaryError(
                "provider boundary RAG state does not match request"
            )
        validate_provider_identity(self.provider_config, payload)
        if self.preflight.manifest.run_id != payload.get("run_id"):
            raise ProductionProviderBoundaryError(
                "negative-ref manifest run_id does not match request"
            )
        repository_items = [
            item
            for item in payload.get("repositories") or []
            if isinstance(item, Mapping)
        ]
        request_repositories = {
            item.get("repo_full_name"): (
                item.get("source_branch"),
                item.get("target_branch"),
            )
            for item in repository_items
        }
        if len(request_repositories) != len(repository_items):
            raise ProductionProviderBoundaryError(
                "request repository scope contains duplicates or invalid names"
            )
        manifest_repositories = {
            item.repo_full_name: (item.source_ref, item.current_target_ref)
            for item in self.preflight.manifest.repositories
        }
        if request_repositories != manifest_repositories:
            raise ProductionProviderBoundaryError(
                "negative-ref manifest repository scope does not match request"
            )
        payload_sha256 = _payload_hash(payload)
        with self._lock:
            if self._started_payload_sha256 is not None:
                if self._started_payload_sha256 != payload_sha256:
                    raise ProductionProviderBoundaryError(
                        "provider boundary cannot be reused across requests"
                    )
                evidence = self.preflight.evidence()
                return copy.deepcopy(evidence[0])
        evidence = self.preflight.pre_orchestration()
        with self._lock:
            self._started_payload_sha256 = payload_sha256
        return evidence

    def wrap_agents_model(self, model: object, *, role: str, phase: str) -> object:
        if not self.started:
            raise ProductionProviderBoundaryError(
                "provider boundary must pass pre-orchestration before model wrapping"
            )
        known_role = validate_role(role)
        if self.architecture_mode == SINGLE_AGENT and known_role != DEVELOPER:
            raise ProductionProviderBoundaryError(
                "M0 provider boundary permits only the developer role"
            )
        if type(phase) is not str or _PHASE_RE.fullmatch(phase) is None:
            raise ProductionProviderBoundaryError(
                "provider phase must be a bounded content-free code"
            )
        proxy_type: type[AccountedAgentsModel] = AccountedAgentsModel
        # openai-agents 0.17.7 validates Agent.model with isinstance(Model).
        # Keep imports lazy for dependency-light/offline tests, while making the
        # production proxy a real SDK Model subclass when the SDK model is real.
        try:
            from agents.models.interface import Model as AgentsModel
        except ImportError:
            AgentsModel = None
        if AgentsModel is not None and isinstance(model, AgentsModel):
            proxy_type = type(
                "SdkAccountedAgentsModel",
                (AccountedAgentsModel, AgentsModel),
                {},
            )
        return proxy_type(
            model=model,
            boundary=self,
            role=known_role,
            phase=phase,
        )

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(
                sorted(self._records, key=lambda item: item["call_id"])
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "architecture_mode": self.architecture_mode,
            "rag_enabled": self.rag_enabled,
            "provider_identity": self.provider_config.identity,
            "identity_sha256": self.identity_sha256,
            "budget": self.ledger.snapshot(),
            "provider_calls": self.records(),
            "isolation_preflight": self.preflight.evidence(),
        }

    def reconcile_runner_usage(
        self,
        usage: Mapping[str, Any],
        *,
        before: Mapping[str, Any] | None = None,
    ) -> None:
        """Reconcile one Runner pass against its provider-accounting delta."""

        if not isinstance(usage, Mapping):
            raise ProviderUsageError("runner usage must be a mapping")
        current = self.ledger.snapshot()["shared"]
        baseline = before or {
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        fields = {
            "calls": "calls",
            "prompt_tokens": "prompt_tokens",
            "completion_tokens": "completion_tokens",
            "total_tokens": "total_tokens",
        }
        for observed_name, expected_name in fields.items():
            observed = usage.get(observed_name)
            if type(observed) is not int or observed < 0:
                raise ProviderUsageError(
                    f"runner usage {observed_name} must be a non-negative integer"
                )
            prior = baseline.get(expected_name)
            if type(prior) is not int or prior < 0 or prior > current[expected_name]:
                raise ProviderUsageError("runner usage baseline is invalid")
            expected_delta = current[expected_name] - prior
            if observed != expected_delta:
                raise ProviderUsageError(
                    "runner aggregate usage does not reconcile with provider calls"
                )
        if current["pending_calls"] != 0:
            raise ProviderUsageError("provider accounting contains pending calls")

    def _append(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._records.append(copy.deepcopy(dict(record)))

    def _failed_record(
        self,
        *,
        call_id: str,
        role: str,
        phase: str,
        input_hash: str,
        latency: float,
        failure_type: str,
        prompt: int,
        completion: int,
        total: int,
    ) -> None:
        self._append(
            {
                "call_id": call_id,
                "role": role,
                "phase": phase,
                "status": "failed",
                "failure_type": failure_type,
                "input_sha256": input_hash,
                "output_sha256": None,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "latency_seconds": round(latency, 6),
            }
        )

    def _close_failure(
        self,
        *,
        call_id: str,
        role: str,
        phase: str,
        input_hash: str,
        latency: float,
        failure: BaseException,
        prompt: int = 0,
        completion: int = 0,
        total: int = 0,
    ) -> None:
        try:
            accounting = self.ledger.finish(
                call_id,
                role=role,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                latency_seconds=latency,
                failure=type(failure).__name__,
                input_hash=input_hash,
            )
        except BudgetExceeded as budget_error:
            self._failed_record(
                call_id=call_id,
                role=role,
                phase=phase,
                input_hash=input_hash,
                latency=latency,
                failure_type=type(budget_error).__name__,
                prompt=prompt,
                completion=completion,
                total=total,
            )
            raise
        self._failed_record(
            call_id=call_id,
            role=role,
            phase=phase,
            input_hash=input_hash,
            latency=accounting["latency_seconds"],
            failure_type=type(failure).__name__,
            prompt=accounting["prompt_tokens"],
            completion=accounting["completion_tokens"],
            total=accounting["total_tokens"],
        )

    async def invoke_model_call(
        self,
        *,
        model: object,
        role: str,
        phase: str,
        args: tuple[Any, ...],
        kwargs: Mapping[str, Any],
    ) -> Any:
        if not self.started:
            raise ProductionProviderBoundaryError(
                "provider call attempted before pre-orchestration"
            )
        request_hash = _payload_hash({"args": args, "kwargs": kwargs})
        # Serialize the read-only capacity gate, negative-ref recheck and actual
        # reservation. An already exhausted attempt must not add a pre-model
        # evidence record, while an isolation failure must not consume a model
        # call. The following begin remains the authoritative atomic mutation.
        with self._lock:
            self.ledger.ensure_capacity(role)
            self.preflight.before_provider_call(role=role, phase=phase)
            call_id = self.ledger.begin(role, input_hash=request_hash)
        started = self._clock()
        try:
            response = await model.get_response(*args, **dict(kwargs))
        except BaseException as exc:
            latency = max(0.0, float(self._clock() - started))
            prompt, completion, total = _failure_usage(exc)
            self._close_failure(
                call_id=call_id,
                role=role,
                phase=phase,
                input_hash=request_hash,
                latency=latency,
                failure=exc,
                prompt=prompt,
                completion=completion,
                total=total,
            )
            raise

        latency = max(0.0, float(self._clock() - started))
        prompt = completion = total = 0
        try:
            prompt, completion, total = _extract_usage(response)
            output_hash = _payload_hash(response)
            accounting = self.ledger.finish(
                call_id,
                role=role,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                latency_seconds=latency,
                input_hash=request_hash,
                output_hash=output_hash,
            )
        except BudgetExceeded:
            self._failed_record(
                call_id=call_id,
                role=role,
                phase=phase,
                input_hash=request_hash,
                latency=latency,
                failure_type="BudgetExceeded",
                prompt=prompt,
                completion=completion,
                total=total,
            )
            raise
        except (ProviderUsageError, ProductionProviderBoundaryError) as exc:
            self._close_failure(
                call_id=call_id,
                role=role,
                phase=phase,
                input_hash=request_hash,
                latency=latency,
                failure=exc,
                prompt=prompt,
                completion=completion,
                total=total,
            )
            raise
        except BudgetValidationError as exc:
            self._close_failure(
                call_id=call_id,
                role=role,
                phase=phase,
                input_hash=request_hash,
                latency=latency,
                failure=exc,
                prompt=prompt,
                completion=completion,
                total=total,
            )
            raise ProviderUsageError(
                "provider returned invalid per-call usage"
            ) from exc

        self._append(
            {
                "call_id": call_id,
                "role": role,
                "phase": phase,
                "status": "succeeded",
                "failure_type": None,
                "input_sha256": request_hash,
                "output_sha256": output_hash,
                "prompt_tokens": accounting["prompt_tokens"],
                "completion_tokens": accounting["completion_tokens"],
                "total_tokens": accounting["total_tokens"],
                "latency_seconds": accounting["latency_seconds"],
            }
        )
        return response


def _github_ref_exists(repo_full_name: str, ref: str) -> bool:
    """Production-only exact branch probe; only HTTP 404 means absent."""

    if os.getenv("RAE_OFFLINE") == "1":
        raise ProductionProviderBoundaryUnavailable(
            "offline E3 must inject a fake ref-existence probe"
        )
    from github_client import get_github_client

    repository = get_github_client().get_repo(repo_full_name)
    try:
        repository.get_branch(ref)
    except Exception as exc:
        if getattr(exc, "status", None) == 404:
            return False
        raise
    return True


def boundary_from_environment(
    payload: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    ref_exists: Callable[[str, str], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Experiment3ProductionProviderBoundary:
    """Build, but do not start, the formal run-scoped boundary from env hashes."""

    source = os.environ if environment is None else environment
    manifest_path = source.get(MANIFEST_PATH_ENV)
    if type(manifest_path) is not str or not manifest_path:
        raise ProductionProviderBoundaryUnavailable(
            f"{MANIFEST_PATH_ENV} is required for an explicit E3 run"
        )
    path = Path(manifest_path)
    if not path.is_absolute() or not path.is_file():
        raise ProductionProviderBoundaryUnavailable(
            "negative-ref manifest path must be an existing absolute file"
        )
    try:
        manifest_value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionProviderBoundaryUnavailable(
            "negative-ref manifest could not be loaded"
        ) from exc
    manifest = NegativeRefManifest.from_dict(manifest_value)
    expected_manifest = _safe_hash(
        source.get(MANIFEST_HASH_ENV), name=MANIFEST_HASH_ENV
    )
    expected_deny = _safe_hash(
        source.get(DENY_SET_HASH_ENV), name=DENY_SET_HASH_ENV
    )
    identity_sha256 = _safe_hash(
        source.get(RUN_IDENTITY_HASH_ENV), name=RUN_IDENTITY_HASH_ENV
    )
    if manifest.sha256 != expected_manifest:
        raise ProductionProviderBoundaryUnavailable(
            "negative-ref manifest hash does not match the frozen environment"
        )
    if manifest.deny_ref_set_sha256 != expected_deny:
        raise ProductionProviderBoundaryUnavailable(
            "negative-ref deny-set hash does not match the frozen environment"
        )

    mode = validate_explicit_e3_request(payload)
    rag_enabled = explicit_rag_enabled(payload)
    current_attempt = attempt_number(payload)
    continuation_attempt = current_attempt == 2
    if current_attempt is not None:
        validate_two_attempt_controls(payload)
    if continuation_attempt:
        context = payload.get("_experiment_attempt") or {}
        if context.get("prior_status") not in {
            "required_commit_missing",
            "retryable_workflow_failure",
        }:
            raise ProductionProviderBoundaryUnavailable(
                "attempt 2 requires a valid prior attempt status"
            )
    provider_config = resolve_provider_config(
        source,
        require_explicit_provider=True,
    )
    ledger = (
        RoleBudgetLedger.for_single_agent()
        if mode == SINGLE_AGENT
        else RoleBudgetLedger()
    )
    return Experiment3ProductionProviderBoundary(
        architecture_mode=mode,
        preflight=NegativeRefPreflight(
            manifest,
            ref_exists=ref_exists or _github_ref_exists,
            # The target-absence invariant belongs to the observation start.
            # Attempt 2 is created only inside the same process after attempt 1
            # returned an answer-blind missing-commit/retryable status, so it
            # must be allowed to reuse target/checkpoint state left by attempt 1.
            require_current_target_absent=not continuation_attempt,
        ),
        ledger=ledger,
        provider_config=provider_config,
        rag_enabled=rag_enabled,
        identity_sha256=identity_sha256,
        clock=clock,
    )
