from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import exp3.agents_runtime as agents_runtime
from provider_config import ProviderConfig

from exp3.agents_runtime import (
    AgentsSdkComponents,
    AgentsSdkRoleRunner,
    McpAuditError,
    build_production_role_adapters,
)
from exp3.budget import RoleBudgetLedger
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.policy import ROLE_SEQUENCE
from exp3.production_provider import Experiment3ProductionProviderBoundary
from exp3.router import ManagerStarRunError, run_manager_star_runtime


@pytest.fixture(autouse=True)
def _mcp_github_credential(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-token")


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


def _payload(tmp_path: Path) -> dict:
    config = _provider_config()
    return {
        "architecture_mode": "manager_star",
        "run_id": "E3-M1-T1-R1",
        "issue_key": "SCRUM-390",
        "command": "refactor",
        "execution_objectives": {
            "strategy_type": "refactor",
            "parsed_task_parameters": {
                "rag_enabled": False,
                "objective": "Run the fixed manager-star adapter",
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
                "target_branch": "quant/E3-M1-T1-R1",
                "allowed_directories": ["rae_runtime/proxy"],
            }
        ],
        "strategy": {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "ref": "frozen-source",
            "target_branch": "quant/E3-M1-T1-R1",
            "source_path": "rae_runtime/proxy/budget_guard.py",
            "target_path": "rae_runtime/proxy/budget_guard.py",
        },
        "iteration_controls": {
            "allow_iteration": False,
            "max_iterations": 1,
            "max_failed_iterations": 1,
            "max_commits_per_run": 3,
        },
        "retrieval_context": None,
        "output_paths": {
            "result_path": str(tmp_path / "result.json"),
            "artifact_dir": str(tmp_path / "artifacts"),
        },
    }


def _boundary(payload: dict, *, ref_exists=None):
    repository = payload["repositories"][0]
    manifest = NegativeRefManifest.from_dict(
        {
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
    )
    source = repository["source_branch"]
    return Experiment3ProductionProviderBoundary(
        architecture_mode="manager_star",
        preflight=NegativeRefPreflight(
            manifest,
            ref_exists=ref_exists or (lambda _repo, ref: ref == source),
        ),
        ledger=RoleBudgetLedger(),
        provider_config=_provider_config(),
    )


@dataclass
class Usage:
    requests: int = 1
    input_tokens: int = 8
    output_tokens: int = 4
    total_tokens: int = 12


@dataclass
class ModelResponse:
    content: str
    usage: Usage


class FakeRawModel:
    def __init__(self, calls):
        self.calls = calls

    async def get_response(self, *args, **kwargs):
        self.calls.append((copy.deepcopy(args), copy.deepcopy(kwargs)))
        return ModelResponse("PRIVATE model response", Usage())


class FakeMCPServerStdio:
    opened = []

    def __init__(self, *, name, params, client_session_timeout_seconds):
        self.name = name
        self.params = params
        self.timeout = client_session_timeout_seconds

    async def __aenter__(self):
        type(self).opened.append(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeAgent:
    created = []

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        type(self).created.append(self)


class FakeModelSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeRunConfig:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).created.append(self)


def _architect_plan():
    return {
        "version": "architect_plan_v1",
        "producer": "architect",
        "risks": ["Preserve the frozen M0 behavior"],
        "files": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "purpose": "Implement the approved change.",
            }
        ],
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "One implementation",
                "planned_evidence": "Offline verification",
                "files": ["rae_runtime/proxy/budget_guard.py"],
            }
        ],
    }


def _developer_result():
    return {
        "version": "developer_result_v1",
        "producer": "developer",
        "implementation": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "summary": "Implemented the approved plan.",
            }
        ],
        "verification_evidence": [
            {
                "check": "offline tests",
                "status": "passed",
                "evidence": "All injected checks passed.",
            }
        ],
    }


def _manager_final():
    return {
        "version": "manager_final_v1",
        "producer": "manager",
        "decision": "accepted",
        "failure_phase": None,
        "summary": "Complete.",
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "One implementation",
                "status": "passed",
                "evidence": "Developer verification is present.",
            }
        ],
    }


class FakeRunner:
    phases = []
    outputs = {
        "manager_to_architect": {"instruction": "Produce one plan."},
        "architect_to_manager": _architect_plan(),
        "manager_to_developer": {"instruction": "Implement once."},
        "developer_to_manager": _developer_result(),
        "manager_final": _manager_final(),
    }

    @classmethod
    async def run(cls, agent, prompt, max_turns, run_config):
        assert run_config.kwargs["tracing_disabled"] is True
        assert run_config.kwargs["trace_include_sensitive_data"] is False
        phase = agent.name.rsplit("(", 1)[1].rstrip(")")
        cls.phases.append((phase, max_turns, json.loads(prompt)))
        response = await agent.model.get_response(prompt=prompt)
        if phase == "developer_to_manager":
            audit_path = Path(
                agent.mcp_servers[0].params["env"]["MCP_TOOL_AUDIT_PATH"]
            )
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            records = [
                {
                    "schema_version": "exp3-mcp-tool-audit-v1",
                    "record_type": "tool_call",
                    "architecture_mode": "manager_star",
                    "role": "developer",
                    "tool": tool,
                    "status": "success",
                }
                for tool in ("replace_in_file", "commit_and_push")
            ]
            audit_path.write_text(
                audit_path.read_text(encoding="utf-8")
                + "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
        wrapper = type("ContextWrapper", (), {"usage": response.usage})()
        return type(
            "RunResult",
            (),
            {
                "final_output": json.dumps(cls.outputs[phase]),
                "context_wrapper": wrapper,
            },
        )()


def _sdk_runner(model_calls):
    components = AgentsSdkComponents(
        Agent=FakeAgent,
        Runner=FakeRunner,
        RunConfig=FakeRunConfig,
        MCPServerStdio=FakeMCPServerStdio,
        LitellmModel=object,
        ModelSettings=FakeModelSettings,
    )
    return AgentsSdkRoleRunner(
        components_loader=lambda: components,
        model_factory=lambda _role, _components: FakeRawModel(model_calls),
    )


def test_default_m1_model_factory_uses_boundary_owned_provider_config(monkeypatch, tmp_path):
    payload = _payload(tmp_path)
    boundary = _boundary(payload)
    captured = {}
    sentinel = object()
    components = AgentsSdkComponents(
        Agent=object,
        Runner=object,
        RunConfig=FakeRunConfig,
        MCPServerStdio=object,
        LitellmModel=object,
        ModelSettings=FakeModelSettings,
    )

    def fake_build(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(agents_runtime, "build_agents_model", fake_build)
    runner = AgentsSdkRoleRunner(components_loader=lambda: components)

    assert runner._model("manager", components, boundary) is sentinel
    assert captured["config"] is boundary.provider_config
    assert captured["kwargs"]["litellm_model_cls"] is object


def test_real_m1_adapter_runs_fixed_topology_through_accounted_models(
    monkeypatch,
    tmp_path,
):
    FakeAgent.created.clear()
    FakeMCPServerStdio.opened.clear()
    FakeRunner.phases.clear()
    payload = _payload(tmp_path)
    boundary = _boundary(payload)
    model_calls = []
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")

    result = run_manager_star_runtime(
        payload,
        production_boundary=boundary,
        production_role_runner=_sdk_runner(model_calls),
    )

    assert result["status"] == "succeeded"
    assert [record["role"] for record in result["provider_calls"]] == list(
        ROLE_SEQUENCE
    )
    assert result["budget"]["shared"]["calls"] == 5
    assert result["budget"]["shared"]["total_tokens"] == 60
    assert result["commit_count"] == 2
    assert len(model_calls) == 5
    assert [phase for phase, _turns, _prompt in FakeRunner.phases] == [
        "manager_to_architect",
        "architect_to_manager",
        "manager_to_developer",
        "developer_to_manager",
        "manager_final",
    ]
    assert [turns for _phase, turns, _prompt in FakeRunner.phases] == [1, 3, 1, 9, 1]
    assert len(FakeMCPServerStdio.opened) == 5
    manager_indexes = (0, 2, 4)
    for index, agent in enumerate(FakeAgent.created):
        if index in manager_indexes:
            assert agent.model_settings.kwargs["tool_choice"] == "none"
            assert "Do not call repository tools" in agent.instructions
        else:
            assert "tool_choice" not in agent.model_settings.kwargs

    for server in FakeMCPServerStdio.opened:
        env = server.params["env"]
        assert env["GITHUB_TOKEN"] == "github-test-token"
        assert "OPENAI_API_KEY" not in env
        assert env["E3_REF_SCOPE_PREFLIGHT_PASSED"] == "true"
        assert env["E3_NEGATIVE_REF_MANIFEST_SHA256"] == boundary.preflight.manifest.sha256
        assert env["E3_NEGATIVE_REF_SET_SHA256"] == (
            boundary.preflight.manifest.deny_ref_set_sha256
        )
        role = env["RAE_AGENT_ROLE"]
        if role in {"manager", "architect"}:
            assert env["MAX_BRANCHES_PER_RUN"] == "0"
            assert env["MAX_COMMITS_PER_RUN"] == "0"
        else:
            assert env["MAX_BRANCHES_PER_RUN"] == "1"
            assert env["MAX_COMMITS_PER_RUN"] == "3"
    assert "github-test-token" not in repr(model_calls)


def test_m1_missing_mcp_github_token_fails_before_sdk_or_model(
    monkeypatch,
    tmp_path,
):
    payload = _payload(tmp_path)
    boundary = _boundary(payload)
    sdk_calls = []
    runner = AgentsSdkRoleRunner(
        components_loader=lambda: sdk_calls.append(True),
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    monkeypatch.delenv("GITHUB_TOKEN")

    with pytest.raises(ManagerStarRunError) as raised:
        run_manager_star_runtime(
            payload,
            production_boundary=boundary,
            production_role_runner=runner,
        )

    assert raised.value.failure_phase == "role_adapter_initialization"
    assert raised.value.budget["shared"]["calls"] == 0
    assert sdk_calls == []


def test_m1_isolation_failure_happens_before_sdk_or_mcp_session(monkeypatch, tmp_path):
    payload = _payload(tmp_path)
    boundary = _boundary(payload, ref_exists=lambda _repo, _ref: False)
    sdk_calls = []
    runner = AgentsSdkRoleRunner(
        components_loader=lambda: sdk_calls.append(True),
    )
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")

    with pytest.raises(ManagerStarRunError) as raised:
        run_manager_star_runtime(
            payload,
            production_boundary=boundary,
            production_role_runner=runner,
        )

    assert raised.value.failure_phase == "isolation_pre_orchestration"
    assert raised.value.budget["shared"]["calls"] == 0
    assert sdk_calls == []


def test_invalid_agent_json_stops_before_next_role(monkeypatch, tmp_path):
    payload = _payload(tmp_path)
    boundary = _boundary(payload)
    model_calls = []
    original = FakeRunner.outputs["manager_to_architect"]
    FakeRunner.outputs["manager_to_architect"] = ["not", "an", "object"]
    monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    try:
        with pytest.raises(ManagerStarRunError) as raised:
            run_manager_star_runtime(
                payload,
                production_boundary=boundary,
                production_role_runner=_sdk_runner(model_calls),
            )
    finally:
        FakeRunner.outputs["manager_to_architect"] = original

    assert raised.value.failure_phase == "manager_to_architect"
    assert raised.value.failure_type == "AgentOutputError"
    assert boundary.ledger.snapshot()["shared"]["calls"] == 1
    assert len(model_calls) == 1


def _audit_adapters(tmp_path: Path):
    payload = _payload(tmp_path)
    boundary = _boundary(payload)
    boundary.start(payload)
    adapters = build_production_role_adapters(
        payload,
        boundary=boundary,
        runner=lambda **_kwargs: {},
    )
    return adapters, tmp_path / "artifacts" / "exp3_mcp_tool_audit.jsonl"


def test_m1_audit_missing_after_initialization_fails_closed(tmp_path):
    adapters, audit_path = _audit_adapters(tmp_path)
    audit_path.unlink()

    with pytest.raises(McpAuditError, match="missing"):
        adapters.commit_count()


@pytest.mark.parametrize(
    "corruption",
    [
        '{"partial":',
        "\n",
        json.dumps(
            {
                "schema_version": "exp3-mcp-tool-audit-v1",
                "record_type": "audit_initialized",
                "architecture_mode": "manager_star",
                "manifest_sha256": "0" * 64,
                "deny_ref_set_sha256": "0" * 64,
            }
        )
        + "\n",
    ],
)
def test_m1_audit_malformed_partial_or_identity_drift_fails_closed(
    tmp_path,
    corruption,
):
    adapters, audit_path = _audit_adapters(tmp_path)
    audit_path.write_text(corruption, encoding="utf-8")

    with pytest.raises(McpAuditError):
        adapters.commit_count()


def test_m1_audit_rejects_content_or_unknown_tool_records(tmp_path):
    adapters, audit_path = _audit_adapters(tmp_path)
    header = audit_path.read_text(encoding="utf-8")
    invalid = {
        "schema_version": "exp3-mcp-tool-audit-v1",
        "record_type": "tool_call",
        "architecture_mode": "manager_star",
        "role": "developer",
        "tool": "dynamic_write_tool",
        "status": "success",
        "content": "PRIVATE",
    }
    audit_path.write_text(header + json.dumps(invalid) + "\n", encoding="utf-8")

    with pytest.raises(McpAuditError):
        adapters.commit_count()
