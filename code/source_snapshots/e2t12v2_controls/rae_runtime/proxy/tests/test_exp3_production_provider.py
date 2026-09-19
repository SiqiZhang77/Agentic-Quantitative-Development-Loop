from __future__ import annotations

import asyncio
import copy
import json
import sys
import types
from dataclasses import dataclass

import pytest
from provider_config import ProviderConfig

from exp3.budget import BudgetExceeded, RoleBudgetLedger
from exp3.isolation import (
    IsolationPreflightError,
    NegativeRefManifest,
    NegativeRefPreflight,
)
from exp3.production_provider import (
    DENY_SET_HASH_ENV,
    MANIFEST_HASH_ENV,
    MANIFEST_PATH_ENV,
    RUN_IDENTITY_HASH_ENV,
    Experiment3ProductionProviderBoundary,
    ProductionProviderBoundaryError,
    ProductionProviderBoundaryUnavailable,
    ProviderUsageError,
    StreamingProviderCallForbidden,
    _payload_hash,
    boundary_from_environment,
)


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider_mode="company_litellm",
        base_url="http://weles.cs.ucl.ac.uk:4000",
        model_alias="qwen3-coder",
        transport_model="litellm_proxy/qwen3-coder",
        adapter="litellm_chat_completions",
        api_key="test-secret",
        api_key_source="test",
        explicit_provider=True,
    )


def _payload(mode: str = "single_agent") -> dict:
    config = _provider_config()
    return {
        "architecture_mode": mode,
        "run_id": "E3-M0-T1-R1",
        "issue_key": "SCRUM-390",
        "command": "refactor",
        "execution_objectives": {
            "strategy_type": "refactor",
            "parsed_task_parameters": {
                "rag_enabled": False,
                "objective": "Exercise the production boundary offline",
                "provider_mode": config.provider_mode,
                "provider_base_url": config.base_url,
                "provider_identity_sha256": config.identity["sha256"],
                "model": config.model_alias,
            },
        },
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "frozen-source",
                "target_branch": "quant/E3-M0-T1-R1",
                "allowed_directories": ["rae_runtime/proxy"],
            }
        ],
        "strategy": {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "ref": "frozen-source",
            "target_branch": "quant/E3-M0-T1-R1",
        },
        "retrieval_context": None,
    }


def _manifest_value(payload: dict) -> dict:
    repository = payload["repositories"][0]
    return {
        "schema_version": "exp3-negative-ref-manifest-v1",
        "experiment_id": "E3-manager-star-v1",
        "run_id": payload["run_id"],
        "repositories": [
            {
                "repo_full_name": repository["repo_full_name"],
                "source_ref": repository["source_branch"],
                "current_target_ref": repository["target_branch"],
                "paired_target_ref": repository["target_branch"] + "-paired",
                "deny_refs": {
                    "v5_refs": ["exp2/v5"],
                    "e2_output_refs": ["quant/E2-output"],
                    "earlier_e3_targets": ["quant/E3-earlier"],
                    "protected_refs": ["main"],
                    "arbitrary_probe_refs": ["arbitrary/a", "arbitrary/b"],
                },
            }
        ],
    }


def _boundary(
    payload: dict,
    *,
    ref_exists=None,
) -> Experiment3ProductionProviderBoundary:
    source = payload["repositories"][0]["source_branch"]
    preflight = NegativeRefPreflight(
        NegativeRefManifest.from_dict(_manifest_value(payload)),
        ref_exists=ref_exists or (lambda _repo, ref: ref == source),
    )
    ledger = (
        RoleBudgetLedger.for_single_agent()
        if payload["architecture_mode"] == "single_agent"
        else RoleBudgetLedger()
    )
    return Experiment3ProductionProviderBoundary(
        architecture_mode=payload["architecture_mode"],
        preflight=preflight,
        ledger=ledger,
        provider_config=_provider_config(),
    )


@dataclass
class Usage:
    requests: int = 1
    input_tokens: int = 7
    output_tokens: int = 3
    total_tokens: int = 10


@dataclass
class Response:
    output: str
    usage: Usage


@dataclass
class McpToolHashProbe:
    name: str
    schema: dict
    callback: object
    session_future: object


def test_provider_hash_handles_live_mcp_dataclass_state_without_deepcopy():
    async def callback():
        return None

    loop = asyncio.new_event_loop()
    try:
        first = McpToolHashProbe(
            name="read_file",
            schema={"type": "object"},
            callback=callback,
            session_future=loop.create_future(),
        )
        second = McpToolHashProbe(
            name="read_file",
            schema={"type": "object"},
            callback=callback,
            session_future=loop.create_future(),
        )

        assert _payload_hash({"tools": [first]}) == _payload_hash(
            {"tools": [second]}
        )
    finally:
        loop.close()


class FakeModel:
    model = "fake-model"

    def __init__(self, *, response=None, failure=None):
        self.response = response or Response("PRIVATE output", Usage())
        self.failure = failure
        self.calls = []
        self.stream_calls = 0

    async def get_response(self, *args, **kwargs):
        self.calls.append((copy.deepcopy(args), copy.deepcopy(kwargs)))
        if self.failure is not None:
            raise self.failure
        return copy.deepcopy(self.response)

    async def stream_response(self, *args, **kwargs):
        self.stream_calls += 1
        yield self.response


def _call(proxy, marker: str = "PRIVATE prompt"):
    return asyncio.run(
        proxy.get_response(
            marker,
            model_settings={"temperature": 0},
        )
    )


def test_m0_accounts_each_model_get_response_and_keeps_bodies_out_of_records():
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    model = FakeModel()
    proxy = boundary.wrap_agents_model(
        model,
        role="developer",
        phase="single_agent_developer",
    )

    assert _call(proxy).output == "PRIVATE output"
    assert _call(proxy, "SECOND PRIVATE prompt").usage.total_tokens == 10

    snapshot = boundary.snapshot()
    assert len(model.calls) == 2
    assert snapshot["budget"]["shared"]["calls"] == 2
    assert snapshot["budget"]["shared"]["total_tokens"] == 20
    assert len(snapshot["provider_calls"]) == 2
    assert len(snapshot["isolation_preflight"]) == 3
    serialized = json.dumps(snapshot, sort_keys=True)
    assert "PRIVATE prompt" not in serialized
    assert "PRIVATE output" not in serialized


def test_real_sdk_model_wrapper_preserves_agents_model_runtime_type(monkeypatch):
    class SdkModel:
        pass

    agents = types.ModuleType("agents")
    models = types.ModuleType("agents.models")
    interface = types.ModuleType("agents.models.interface")
    interface.Model = SdkModel
    monkeypatch.setitem(sys.modules, "agents", agents)
    monkeypatch.setitem(sys.modules, "agents.models", models)
    monkeypatch.setitem(sys.modules, "agents.models.interface", interface)

    class RealishModel(FakeModel, SdkModel):
        pass

    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    proxy = boundary.wrap_agents_model(
        RealishModel(),
        role="developer",
        phase="single_agent_developer",
    )

    assert isinstance(proxy, SdkModel)
    assert _call(proxy).usage.total_tokens == 10


def test_preflight_rechecks_source_before_each_call_and_blocks_before_reservation():
    payload = _payload()
    state = {"source_exists": True}
    source = payload["repositories"][0]["source_branch"]

    def ref_exists(_repo, ref):
        return state["source_exists"] and ref == source

    boundary = _boundary(payload, ref_exists=ref_exists)
    boundary.start(payload)
    state["source_exists"] = False
    model = FakeModel()
    proxy = boundary.wrap_agents_model(
        model,
        role="developer",
        phase="single_agent_developer",
    )

    with pytest.raises(IsolationPreflightError, match="FROZEN_SOURCE_REF_ABSENT"):
        _call(proxy)

    assert model.calls == []
    assert boundary.ledger.snapshot()["shared"]["calls"] == 0


def test_failed_provider_call_is_closed_with_attached_usage_and_no_body():
    payload = _payload()

    class ProviderFailure(RuntimeError):
        input_tokens = 11
        output_tokens = 2
        total_tokens = 13

    boundary = _boundary(payload)
    boundary.start(payload)
    model = FakeModel(failure=ProviderFailure("PRIVATE provider failure"))
    proxy = boundary.wrap_agents_model(
        model,
        role="developer",
        phase="single_agent_developer",
    )

    with pytest.raises(ProviderFailure, match="PRIVATE"):
        _call(proxy)

    snapshot = boundary.snapshot()
    assert snapshot["budget"]["shared"]["calls"] == 1
    assert snapshot["budget"]["shared"]["total_tokens"] == 13
    assert snapshot["budget"]["shared"]["failures"] == 1
    assert snapshot["provider_calls"][0]["failure_type"] == "ProviderFailure"
    assert "PRIVATE provider failure" not in json.dumps(snapshot)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        Usage(requests=2),
        Usage(total_tokens=11),
    ],
)
def test_missing_retrying_or_inconsistent_usage_fails_closed(usage):
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    response = {"output": "value"} if usage is None else Response("value", usage)
    proxy = boundary.wrap_agents_model(
        FakeModel(response=response),
        role="developer",
        phase="single_agent_developer",
    )

    with pytest.raises(ProviderUsageError):
        _call(proxy)

    snapshot = boundary.ledger.snapshot()["shared"]
    assert snapshot["calls"] == 1
    assert snapshot["failures"] == 1
    assert snapshot["pending_calls"] == 0


def test_m0_receives_15_calls_not_the_m1_developer_cap():
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    model = FakeModel(response=Response("ok", Usage(1, 0, 0, 0)))
    proxy = boundary.wrap_agents_model(
        model,
        role="developer",
        phase="single_agent_developer",
    )

    for index in range(15):
        _call(proxy, f"turn-{index}")
    with pytest.raises(BudgetExceeded, match="developer call budget"):
        _call(proxy, "turn-16")

    assert len(model.calls) == 15
    assert boundary.ledger.snapshot()["shared"]["calls"] == 15
    # One pre-orchestration record plus exactly one pre-model check per admitted
    # call. The rejected 16th attempt is stopped by the capacity precheck first.
    assert len(boundary.preflight.evidence()) == 16


def test_m1_boundary_retains_fixed_role_caps():
    payload = _payload("manager_star")
    boundary = _boundary(payload)
    boundary.start(payload)
    model = FakeModel(response=Response("ok", Usage(1, 0, 0, 0)))
    proxy = boundary.wrap_agents_model(
        model,
        role="architect",
        phase="architect_to_manager",
    )

    for index in range(3):
        _call(proxy, f"architect-{index}")
    with pytest.raises(BudgetExceeded, match="architect call budget"):
        _call(proxy, "architect-4")

    assert len(model.calls) == 3


def test_streaming_is_blocked_before_the_wrapped_model():
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    model = FakeModel()
    proxy = boundary.wrap_agents_model(
        model,
        role="developer",
        phase="single_agent_developer",
    )

    async def consume():
        async for _event in proxy.stream_response("PRIVATE prompt"):
            pass

    with pytest.raises(StreamingProviderCallForbidden):
        asyncio.run(consume())

    assert model.stream_calls == 0
    assert boundary.ledger.snapshot()["shared"]["calls"] == 0


def test_boundary_rejects_wrong_role_reuse_and_runner_usage_drift():
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)

    with pytest.raises(ProductionProviderBoundaryError, match="developer role"):
        boundary.wrap_agents_model(
            FakeModel(), role="manager", phase="manager_to_architect"
        )
    changed = copy.deepcopy(payload)
    changed["issue_key"] = "SCRUM-391"
    with pytest.raises(ProductionProviderBoundaryError, match="reused"):
        boundary.start(changed)
    with pytest.raises(ProviderUsageError, match="reconcile"):
        boundary.reconcile_runner_usage(
            {
                "calls": 1,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        )


def test_runner_usage_reconciles_each_iteration_delta_not_cumulative_total():
    payload = _payload()
    boundary = _boundary(payload)
    boundary.start(payload)
    proxy = boundary.wrap_agents_model(
        FakeModel(),
        role="developer",
        phase="single_agent_developer",
    )

    before_first = boundary.ledger.snapshot()["shared"]
    _call(proxy, "iteration-1")
    boundary.reconcile_runner_usage(
        {
            "calls": 1,
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
        before=before_first,
    )
    before_second = boundary.ledger.snapshot()["shared"]
    _call(proxy, "iteration-2")
    boundary.reconcile_runner_usage(
        {
            "calls": 1,
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
        before=before_second,
    )

    assert boundary.ledger.snapshot()["shared"]["calls"] == 2


def test_environment_loader_requires_frozen_hashes_and_uses_injected_probe(tmp_path):
    payload = _payload()
    value = _manifest_value(payload)
    manifest = NegativeRefManifest.from_dict(value)
    path = tmp_path / "negative-ref.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    environment = {
        MANIFEST_PATH_ENV: str(path),
        MANIFEST_HASH_ENV: manifest.sha256,
        DENY_SET_HASH_ENV: manifest.deny_ref_set_sha256,
        RUN_IDENTITY_HASH_ENV: "9" * 64,
        "LLM_PROVIDER": "company_litellm",
        "LITELLM_BASE_URL": "http://weles.cs.ucl.ac.uk:4000",
        "LITELLM_MODEL": "qwen3-coder",
        "LITELLM_PROXY_API_KEY": "test-secret",
    }
    source = payload["repositories"][0]["source_branch"]

    boundary = boundary_from_environment(
        payload,
        environment=environment,
        ref_exists=lambda _repo, ref: ref == source,
    )
    boundary.start(payload)

    assert boundary.architecture_mode == "single_agent"
    assert boundary.ledger.snapshot()["roles"]["developer"]["max_calls"] == 15
    corrupted = {**environment, MANIFEST_HASH_ENV: "0" * 64}
    with pytest.raises(ProductionProviderBoundaryUnavailable, match="hash"):
        boundary_from_environment(
            payload,
            environment=corrupted,
            ref_exists=lambda _repo, ref: ref == source,
        )
