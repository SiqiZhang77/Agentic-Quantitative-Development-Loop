"""Request-to-response Experiment 3 integration using only injected fakes."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
import types
import unittest.mock
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import jsonschema
import pytest


SANDBOX = Path(__file__).resolve().parents[1]
PROXY = SANDBOX / "proxy"
for entry in (str(SANDBOX), str(PROXY)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MOCKED_MODULES = ("mcp_client", "pipeline", "github_client", "llm_client", "dotenv")
_module_patch = unittest.mock.patch.dict(
    "sys.modules",
    {name: MagicMock() for name in _MOCKED_MODULES if name not in sys.modules},
)
_module_patch.start()
import run
_module_patch.stop()

from exp3.budget import RoleBudgetLedger
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.production_provider import Experiment3ProductionProviderBoundary
from provider_config import ProviderConfig


REQUEST_FIXTURE = Path(__file__).parent / "fixtures" / "runtime_request_example.json"
RESPONSE_SCHEMA = SANDBOX / "schemas" / "runtime_response.schema.json"


@pytest.fixture(autouse=True)
def _mcp_github_credential(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "github-test-token")


def _request(mode: str, *, provider_config: ProviderConfig | None = None) -> dict:
    config = provider_config or _provider_config()
    value = json.loads(REQUEST_FIXTURE.read_text(encoding="utf-8"))
    arm = "M0" if mode == "single_agent" else "M1"
    value["run_id"] = f"E3-T1-R1-{arm}"
    value["architecture_mode"] = mode
    value["repository_details"][0]["source_branch"] = "exp3/frozen-source"
    value["repository_details"][0]["target_branch"] = f"quant/E3-T1-R1-{arm}"
    value["execution_objectives"]["strategy_type"] = "refactor"
    value["execution_objectives"]["resource_path"] = "rae_runtime/proxy/budget_guard.py"
    value["execution_objectives"]["parsed_task_parameters"] = {
        "rag_enabled": False,
        "objective": "Exercise the local E3 integration boundary",
        "provider_mode": config.provider_mode,
        "provider_base_url": config.base_url,
        "provider_identity_sha256": config.identity["sha256"],
        "model": config.model_alias,
    }
    value["iteration_controls"].update(
        {
            "allow_iteration": False,
            "max_iterations": 1,
            "max_failed_iterations": 1,
            "max_agent_turns": 15,
            "max_token_budget_per_run": 200_000,
            "timeout_seconds": 1_800,
            "max_commits_per_run": 3,
        }
    )
    return value


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


def _openai_provider_config() -> ProviderConfig:
    return ProviderConfig(
        provider_mode="openai",
        base_url="https://api.openai.com/v1",
        model_alias="gpt-5.6-terra",
        transport_model="gpt-5.6-terra",
        adapter="openai_chat_completions",
        api_key="test-openai-secret",
        api_key_source="test",
        explicit_provider=True,
    )


class BoundaryFactory:
    def __init__(
        self,
        *,
        source_present: bool = True,
        provider_config: ProviderConfig | None = None,
    ) -> None:
        self.source_present = source_present
        self.provider_config = provider_config or _provider_config()
        self.payloads: list[dict] = []
        self.boundaries: list[Experiment3ProductionProviderBoundary] = []
        self.target_present = False

    def __call__(self, payload: dict) -> Experiment3ProductionProviderBoundary:
        self.payloads.append(copy.deepcopy(payload))
        repository = payload["repositories"][0]
        current = repository["target_branch"]
        paired = current[:-2] + ("M1" if current.endswith("M0") else "M0")
        manifest = NegativeRefManifest.from_dict(
            {
                "schema_version": "exp3-negative-ref-manifest-v1",
                "experiment_id": "E3-manager-star-v1",
                "run_id": payload["run_id"],
                "repositories": [
                    {
                        "repo_full_name": repository["repo_full_name"],
                        "source_ref": repository["source_branch"],
                        "current_target_ref": current,
                        "paired_target_ref": paired,
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
        continuation_attempt = (
            (payload.get("_experiment_attempt") or {}).get("number") == 2
        )
        boundary = Experiment3ProductionProviderBoundary(
            architecture_mode=payload["architecture_mode"],
            preflight=NegativeRefPreflight(
                manifest,
                ref_exists=lambda _repo, ref: (
                    self.source_present and ref == source
                ) or (self.target_present and ref == current),
                require_current_target_absent=not continuation_attempt,
            ),
            ledger=(
                RoleBudgetLedger.for_single_agent()
                if payload["architecture_mode"] == "single_agent"
                else RoleBudgetLedger()
            ),
            provider_config=self.provider_config,
        )
        self.boundaries.append(boundary)
        return boundary


@dataclass
class FakeUsage:
    requests: int = 1
    input_tokens: int = 2
    output_tokens: int = 1
    total_tokens: int = 3


@dataclass
class FakeResponse:
    marker: str
    usage: FakeUsage


class FakeModel:
    def __init__(self, calls: list, *, failure: BaseException | None = None) -> None:
        self.calls = calls
        self.failure = failure

    async def get_response(self, *args, **kwargs):
        self.calls.append((copy.deepcopy(args), copy.deepcopy(kwargs)))
        if self.failure is not None:
            raise self.failure
        return FakeResponse("fake-only", FakeUsage())


def _architect_plan() -> dict:
    return {
        "version": "architect_plan_v1",
        "producer": "architect",
        "risks": ["Keep M0 unchanged"],
        "files": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "purpose": "Implement the one approved change.",
            }
        ],
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "Complete one implementation",
                "planned_evidence": "Injected offline verification",
                "files": ["rae_runtime/proxy/budget_guard.py"],
            }
        ],
    }


def _developer_result() -> dict:
    return {
        "version": "developer_result_v1",
        "producer": "developer",
        "implementation": [
            {
                "path": "rae_runtime/proxy/budget_guard.py",
                "summary": "Completed the approved change.",
            }
        ],
        "verification_evidence": [
            {
                "check": "offline fake integration",
                "status": "passed",
                "evidence": "The injected boundary completed.",
            }
        ],
    }


def _manager_final() -> dict:
    return {
        "version": "manager_final_v1",
        "producer": "manager",
        "decision": "accepted",
        "failure_phase": None,
        "summary": "Local integration complete; blinded evaluation is separate.",
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "Complete one implementation",
                "status": "passed",
                "evidence": "Developer evidence is present.",
            }
        ],
    }


class FakeProductionRoleRunner:
    outputs = {
        "manager_to_architect": {"instruction": "Produce one plan."},
        "architect_to_manager": _architect_plan(),
        "manager_to_developer": {"instruction": "Implement the approved plan once."},
        "developer_to_manager": _developer_result(),
        "manager_final": _manager_final(),
    }

    def __init__(
        self,
        *,
        failure_phase: str | None = None,
        invalid_handoff: bool = False,
        invalid_acceptance: bool = False,
        corrupt_audit: bool = False,
        exhaust_developer: bool = False,
    ) -> None:
        self.failure_phase = failure_phase
        self.invalid_handoff = invalid_handoff
        self.invalid_acceptance = invalid_acceptance
        self.corrupt_audit = corrupt_audit
        self.exhaust_developer = exhaust_developer
        self.model_calls: list = []
        self.phases: list[str] = []

    def _one_call(self, *, boundary, role, phase, context) -> None:
        model = boundary.wrap_agents_model(
            FakeModel(self.model_calls),
            role=role,
            phase=phase,
        )
        asyncio.run(model.get_response(context=context))

    def __call__(self, *, role, phase, context, boundary, mcp_launch_spec):
        self.phases.append(phase)
        call_count = 15 if self.exhaust_developer and role == "developer" else 1
        for _ in range(call_count):
            self._one_call(
                boundary=boundary,
                role=role,
                phase=phase,
                context=context,
            )
        if self.failure_phase == phase:
            raise RuntimeError("PRIVATE injected role failure")
        if phase == "developer_to_manager":
            audit_path = Path(mcp_launch_spec.env["MCP_TOOL_AUDIT_PATH"])
            existing = audit_path.read_text(encoding="utf-8")
            if self.corrupt_audit:
                audit_path.write_text(existing + '{"partial":', encoding="utf-8")
            else:
                record = {
                    "schema_version": "exp3-mcp-tool-audit-v1",
                    "record_type": "tool_call",
                    "architecture_mode": "manager_star",
                    "role": "developer",
                    "tool": "commit_and_push",
                    "status": "success",
                    "path": "rae_runtime/proxy/budget_guard.py",
                }
                audit_path.write_text(
                    existing + json.dumps(record) + "\n",
                    encoding="utf-8",
                )
        if self.invalid_handoff and phase == "architect_to_manager":
            return {"instruction": "not an architect handoff"}
        output = copy.deepcopy(self.outputs[phase])
        if self.invalid_acceptance and phase == "manager_final":
            output["acceptance_mapping"][0]["requirement"] = "Changed requirement text"
        return output


def _accepting_review() -> dict:
    return {
        "evaluation": {
            "met_criteria": True,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "Injected offline review only.",
        },
        "recommended_action": "accept",
    }


def _rejecting_review() -> dict:
    return {
        "evaluation": {
            "met_criteria": False,
            "confidence": 0.0,
            "criteria_results": [],
            "unrecognised_criteria": [],
            "summary": "Injected deterministic validation rejection.",
        },
        "recommended_action": "escalate",
    }


def _m0_pipeline(*, fail: bool, model_calls: list, exhaust_calls: bool = False):
    def execute(payload, tracer=None, exp3_provider_boundary=None):
        del tracer
        boundary = exp3_provider_boundary
        assert boundary is not None and boundary.started
        # Production pipeline_mcp repeats start() defensively immediately before
        # repository work.  Keep the integration fake faithful to that boundary.
        boundary.start(payload)
        failure = RuntimeError("PRIVATE fake provider failure") if fail else None
        model = boundary.wrap_agents_model(
            FakeModel(model_calls, failure=failure),
            role="developer",
            phase="single_agent_developer",
        )
        for _ in range(21 if exhaust_calls else 1):
            asyncio.run(model.get_response(payload=payload))
        accounting = boundary.snapshot()
        accounting["commit_count"] = 0
        return {
            "status": "succeeded",
            "summary": "Injected M0 pipeline completed.",
            "artifacts": {
                "feature_branch": payload["strategy"]["target_branch"],
                "modified_files": ["rae_runtime/proxy/budget_guard.py"],
                "new_files": [],
            },
            "usage": {
                "model": "fake-model",
                "calls": 1,
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "cost_usd": None,
            },
            "experiment3_provider_accounting": accounting,
        }

    return execute


def _run_request(
    request: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    boundary_factory,
    role_runner=None,
    audit_directory: Path | None = None,
    m0_pipeline=None,
    review_result: dict | None = None,
) -> tuple[int, dict, MagicMock, MagicMock]:
    monkeypatch.delenv("RAE_LEGACY_INPUT", raising=False)
    if request["architecture_mode"] == "manager_star":
        monkeypatch.setenv("RAE_ENABLE_MANAGER_STAR", "true")
    emitted: dict = {}
    pipeline_mock = MagicMock(side_effect=m0_pipeline)
    review_mock = MagicMock(return_value=review_result or _accepting_review())
    import iteration_loop

    def loop_once(payload, edit, evaluate, tracer=None):
        del tracer
        return {
            "pipeline_out": edit(payload),
            "backtest": evaluate({**payload, "iteration": 1}),
        }

    with patch.object(run, "run_pipeline_auto", pipeline_mock), patch.object(
        run, "emit_result", lambda response, _path: emitted.update(response=response)
    ), patch.object(run, "emit_progress_event", lambda **_kwargs: None), patch.object(
        run, "get_progress_events_path", lambda _payload: None
    ), patch.object(iteration_loop, "run_iteration_loop", side_effect=loop_once), patch(
        "review_agent.review_general_task", review_mock
    ), patch("sys.stdin", StringIO(json.dumps(request))):
        code = run.main(
            exp3_boundary_factory=boundary_factory,
            exp3_production_role_runner=role_runner,
            exp3_audit_directory=audit_directory,
        )
    return code, emitted["response"], pipeline_mock, review_mock


def _assert_response_schema(response: dict) -> None:
    jsonschema.validate(
        response,
        json.loads(RESPONSE_SCHEMA.read_text(encoding="utf-8")),
    )


@pytest.mark.parametrize("fail", [False, True])
def test_m0_request_to_response_uses_real_boundary_and_fake_model(
    monkeypatch,
    fail,
):
    request = _request("single_agent")
    factory = BoundaryFactory()
    model_calls: list = []
    code, response, pipeline_mock, review_mock = _run_request(
        request,
        monkeypatch,
        boundary_factory=factory,
        m0_pipeline=_m0_pipeline(fail=fail, model_calls=model_calls),
    )

    assert code == (1 if fail else 0)
    assert len(factory.boundaries) == 1
    assert len(model_calls) == 1
    assert pipeline_mock.call_count == 1
    assert review_mock.call_count == (0 if fail else 1)
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["architecture_mode"] == "single_agent"
    assert telemetry["rag_enabled"] is False
    assert telemetry["status"] == ("failed" if fail else "succeeded")
    assert telemetry["shared_budget"]["calls"] == 1
    assert len(telemetry["provider_calls"]) == 1
    assert telemetry["provider_calls"][0]["status"] == (
        "failed" if fail else "succeeded"
    )
    _assert_response_schema(response)


def test_two_attempt_m0_reuses_each_exact_attempt_payload_at_real_boundary(
    monkeypatch,
):
    request = _request("single_agent")
    request["iteration_controls"].update(
        {
            "allow_iteration": True,
            "max_iterations": 2,
            "max_failed_iterations": 2,
            "max_agent_turns": 20,
            "max_token_budget_per_run": 700_000,
            "timeout_seconds": 3_600,
        }
    )
    factory = BoundaryFactory()
    model_calls: list = []
    attempt_numbers: list[int] = []

    def pipeline(payload, tracer=None, exp3_provider_boundary=None):
        del tracer
        boundary = exp3_provider_boundary
        assert boundary is not None and boundary.started
        # This is the second start that raised in the formal E3V4 M0 run when
        # run.py had started the same boundary with the unmarked base payload.
        boundary.start(payload)
        number = payload["_experiment_attempt"]["number"]
        attempt_numbers.append(number)
        if number == 1:
            # Match GitHub: the first complete workflow creates the target
            # branch even when it does not produce a commit.
            factory.target_present = True
        model = boundary.wrap_agents_model(
            FakeModel(model_calls),
            role="developer",
            phase="single_agent_developer",
        )
        asyncio.run(model.get_response(payload=payload))
        committed = number == 2
        target_path = payload["strategy"]["path"]
        accounting = boundary.snapshot()
        accounting["commit_count"] = 1 if committed else 0
        return {
            "status": "succeeded",
            "summary": "committed" if committed else "commit missing",
            "artifacts": {
                "modified_files": [target_path] if committed else [],
                "new_files": [],
            },
            "diagnostics": {
                "retry_safe": {
                    "commit": {"paths": [target_path] if committed else []}
                }
            },
            "usage": {
                "model": "fake-model",
                "calls": 1,
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
                "cost_usd": None,
            },
            "experiment3_provider_accounting": accounting,
        }

    code, response, pipeline_mock, review_mock = _run_request(
        request,
        monkeypatch,
        boundary_factory=factory,
        m0_pipeline=pipeline,
    )

    assert code == 0
    assert pipeline_mock.call_count == 2
    assert len(factory.boundaries) == 2
    assert attempt_numbers == [1, 2]
    assert len(model_calls) == 2
    assert [
        item.preflight.evidence()[0]["current_target_absence_checked"]
        for item in factory.boundaries
    ] == [True, False]
    assert review_mock.call_count == 0
    telemetry = response["telemetry"]["experiment3"]
    assert [item["completion"] for item in telemetry["attempts"]] == [
        "required_commit_missing",
        "complete",
    ]
    assert telemetry["shared_budget"]["calls"] == 2
    _assert_response_schema(response)


def test_m0_quality_rejection_marks_top_level_and_exp3_telemetry_failed(
    monkeypatch,
):
    request = _request("single_agent")
    model_calls: list = []
    code, response, _, _ = _run_request(
        request,
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        m0_pipeline=_m0_pipeline(fail=False, model_calls=model_calls),
        review_result=_rejecting_review(),
    )

    assert code == 1
    assert response["execution_summary"]["status"] == "failed"
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["status"] == "failed"
    assert telemetry["failure_phase"] == "quality_validation"
    assert telemetry["shared_budget"]["calls"] == 1
    _assert_response_schema(response)


def test_m1_request_to_response_runs_real_router_with_fake_provider(
    monkeypatch,
    tmp_path,
):
    request = _request("manager_star")
    factory = BoundaryFactory()
    runner = FakeProductionRoleRunner()
    code, response, pipeline_mock, review_mock = _run_request(
        request,
        monkeypatch,
        boundary_factory=factory,
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 0
    assert pipeline_mock.call_count == 0
    assert review_mock.call_count == 0
    assert runner.phases == [
        "manager_to_architect",
        "architect_to_manager",
        "manager_to_developer",
        "developer_to_manager",
        "manager_final",
    ]
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["architecture_mode"] == "manager_star"
    assert telemetry["status"] == "succeeded"
    assert telemetry["shared_budget"]["calls"] == 5
    assert telemetry["shared_budget"]["total_tokens"] == 15
    assert telemetry["commit_count"] == 1
    assert telemetry["manager_acceptance_map"]["complete"] is True
    _assert_response_schema(response)


def test_m1_late_acceptance_failure_preserves_commit_and_isolation_telemetry(
    monkeypatch,
    tmp_path,
):
    runner = FakeProductionRoleRunner(invalid_acceptance=True)
    code, response, _, _ = _run_request(
        _request("manager_star"),
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "manager_acceptance_mapping_validation"
    assert telemetry["commit_count"] == 1
    assert telemetry["isolation_preflight"]["passed"] is True
    assert telemetry["manager_acceptance_map"]["complete"] is False
    assert telemetry["manager_acceptance_map"]["approved_count"] == 1
    assert telemetry["manager_acceptance_map"]["evaluated_count"] == 1
    assert telemetry["manager_acceptance_map"]["passed_count"] == 1
    _assert_response_schema(response)


@pytest.mark.parametrize("mode", ["single_agent", "manager_star"])
def test_openai_identity_is_shared_without_litellm_prefix(
    monkeypatch,
    tmp_path,
    mode,
):
    config = _openai_provider_config()
    request = _request(mode, provider_config=config)
    factory = BoundaryFactory(provider_config=config)
    kwargs = {
        "role_runner": FakeProductionRoleRunner(),
        "audit_directory": tmp_path,
    } if mode == "manager_star" else {
        "m0_pipeline": _m0_pipeline(fail=False, model_calls=[]),
    }

    code, response, _pipeline, _review = _run_request(
        request,
        monkeypatch,
        boundary_factory=factory,
        **kwargs,
    )

    assert code == 0
    identity = response["telemetry"]["experiment3"]["provider_identity"]
    assert identity["provider_mode"] == "openai"
    assert identity["model_alias"] == "gpt-5.6-terra"
    assert identity["transport_model"] == "gpt-5.6-terra"
    assert "litellm_proxy/" not in identity["transport_model"]
    if mode == "single_agent":
        assert response["telemetry"]["model_usage"][0]["provider"] == "openai"
    _assert_response_schema(response)


def test_m0_shared_call_budget_exhaustion_is_preserved(
    monkeypatch,
):
    factory = BoundaryFactory()
    model_calls: list = []
    code, response, _, review_mock = _run_request(
        _request("single_agent"),
        monkeypatch,
        boundary_factory=factory,
        m0_pipeline=_m0_pipeline(
            fail=False,
            model_calls=model_calls,
            exhaust_calls=True,
        ),
    )

    assert code == 1
    assert len(model_calls) == 20
    assert review_mock.call_count == 0
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "single_agent_runtime"
    assert telemetry["shared_budget"]["calls"] == 20
    assert telemetry["shared_budget"]["call_exhausted"] is True
    assert telemetry["role_usage"]["developer"]["call_exhausted"] is True
    assert telemetry["shared_budget"]["over_budget"] is False
    _assert_response_schema(response)


@pytest.mark.parametrize(
    "failure_phase",
    [
        "manager_to_architect",
        "architect_to_manager",
        "manager_to_developer",
        "developer_to_manager",
        "manager_final",
    ],
)
def test_m1_each_role_stage_failure_survives_response_telemetry(
    monkeypatch,
    tmp_path,
    failure_phase,
):
    request = _request("manager_star")
    runner = FakeProductionRoleRunner(failure_phase=failure_phase)
    code, response, pipeline_mock, _ = _run_request(
        request,
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    assert pipeline_mock.call_count == 0
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["status"] == "failed"
    assert telemetry["failure_phase"] == failure_phase
    assert "PRIVATE" not in json.dumps(telemetry)
    _assert_response_schema(response)


def test_m1_isolation_failure_precedes_fake_provider_and_is_versioned(
    monkeypatch,
    tmp_path,
):
    runner = FakeProductionRoleRunner()
    code, response, _, _ = _run_request(
        _request("manager_star"),
        monkeypatch,
        boundary_factory=BoundaryFactory(source_present=False),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    assert runner.model_calls == []
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "isolation_pre_orchestration"
    assert telemetry["shared_budget"]["calls"] == 0
    _assert_response_schema(response)


def test_m1_handoff_schema_failure_is_counted_in_response(
    monkeypatch,
    tmp_path,
):
    runner = FakeProductionRoleRunner(invalid_handoff=True)
    code, response, _, _ = _run_request(
        _request("manager_star"),
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "architect_plan_validation"
    assert telemetry["handoff_schema_failures"] == 1
    _assert_response_schema(response)


def test_m1_mcp_audit_corruption_has_terminal_failure_phase(
    monkeypatch,
    tmp_path,
):
    runner = FakeProductionRoleRunner(corrupt_audit=True)
    code, response, _, _ = _run_request(
        _request("manager_star"),
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "mcp_audit_validation"
    _assert_response_schema(response)


def test_m1_developer_role_budget_exhaustion_is_preserved(
    monkeypatch,
    tmp_path,
):
    runner = FakeProductionRoleRunner(exhaust_developer=True)
    code, response, _, _ = _run_request(
        _request("manager_star"),
        monkeypatch,
        boundary_factory=BoundaryFactory(),
        role_runner=runner,
        audit_directory=tmp_path,
    )

    assert code == 1
    telemetry = response["telemetry"]["experiment3"]
    assert telemetry["failure_phase"] == "developer_to_manager"
    assert telemetry["role_usage"]["developer"]["calls"] == 14
    assert telemetry["role_usage"]["developer"]["call_exhausted"] is True
    assert telemetry["role_usage"]["developer"]["over_budget"] is False
    assert telemetry["shared_budget"]["calls"] == 17
    _assert_response_schema(response)
