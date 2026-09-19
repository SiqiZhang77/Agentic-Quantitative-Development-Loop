from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from exp3.budget import RoleBudgetLedger
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.policy import ALL_TOOLS, READ_TOOLS
from exp3.provider_accounting import ProviderCallResult
from exp3.role_adapters import (
    READ_ONLY_MODE,
    SCOPED_WRITE_MODE,
    build_role_adapters,
)


SOURCE = Path(__file__).resolve().parents[1] / "exp3" / "role_adapters.py"


def _payload() -> dict:
    return {
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_branch": "exp3/frozen-source",
                "target_branch": "quant/E3-T1-R1-M1",
                "allowed_directories": ["rae_runtime/proxy"],
            }
        ],
        "iteration_controls": {"max_commits_per_run": 3},
    }


def _preflight() -> NegativeRefPreflight:
    manifest = NegativeRefManifest.from_dict(
        {
            "schema_version": "exp3-negative-ref-manifest-v1",
            "experiment_id": "E3-manager-star-v1",
            "run_id": "E3-T1-R1-M1",
            "repositories": [
                {
                    "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                    "source_ref": "exp3/frozen-source",
                    "current_target_ref": "quant/E3-T1-R1-M1",
                    "paired_target_ref": "quant/E3-T1-R1-M0",
                    "deny_refs": {
                        "v5_refs": ["exp2/v5"],
                        "e2_output_refs": ["quant/E2-output"],
                        "earlier_e3_targets": [],
                        "protected_refs": ["main"],
                        "arbitrary_probe_refs": ["arbitrary/a", "arbitrary/b"],
                    },
                }
            ],
        }
    )
    preflight = NegativeRefPreflight(
        manifest,
        ref_exists=lambda _repo, ref: ref == "exp3/frozen-source",
    )
    preflight.pre_orchestration()
    return preflight


def test_launch_specs_enforce_read_only_and_developer_scoped_write() -> None:
    adapters = build_role_adapters(
        _payload(),
        provider=lambda **_kwargs: ProviderCallResult({}, 1, 1),
        ledger=RoleBudgetLedger(),
        preflight=_preflight(),
        server_path="/app/proxy/github_mcp_server.py",
    )

    for role in ("manager", "architect"):
        spec = adapters.spec_for(role)
        assert spec.tool_mode == READ_ONLY_MODE
        assert set(spec.allowed_tools) == set(READ_TOOLS)
        assert spec.env["MAX_BRANCHES_PER_RUN"] == "0"
        assert spec.env["MAX_COMMITS_PER_RUN"] == "0"
        assert spec.precreate_branch is False

    developer = adapters.spec_for("developer")
    assert developer.tool_mode == SCOPED_WRITE_MODE
    assert set(developer.allowed_tools) == set(ALL_TOOLS)
    assert developer.env["MAX_BRANCHES_PER_RUN"] == "1"
    assert developer.env["MAX_COMMITS_PER_RUN"] == "3"
    assert developer.precreate_branch is False


def test_every_spec_binds_role_scope_and_passing_preflight_hashes() -> None:
    preflight = _preflight()
    adapters = build_role_adapters(
        _payload(),
        provider=lambda **_kwargs: ProviderCallResult({}, 1, 1),
        ledger=RoleBudgetLedger(),
        preflight=preflight,
    )

    for role in ("manager", "architect", "developer"):
        env = adapters.spec_for(role).env
        assert env["RAE_ARCHITECTURE_MODE"] == "manager_star"
        assert env["RAE_AGENT_ROLE"] == role
        assert env["ENFORCE_READ_BRANCH_SCOPE"] == "true"
        assert env["E3_REF_SCOPE_PREFLIGHT_PASSED"] == "true"
        assert env["E3_NEGATIVE_REF_MANIFEST_SHA256"] == preflight.manifest.sha256
        assert json.loads(env["SOURCE_BRANCH_MAP"]) == {
            "bankingscience/BSLAgenticQuantDevLoop": "exp3/frozen-source"
        }


def test_injected_runner_can_make_multiple_accounted_turns_in_one_role_stage() -> None:
    provider_calls = []

    def provider(*, role, phase, request):
        provider_calls.append((role, phase, request))
        return ProviderCallResult({"turn": request["turn"]}, 2, 1)

    def runner(*, role, phase, context, provider_call, mcp_launch_spec):
        assert mcp_launch_spec.role == role
        assert context == {"task": "bounded"}
        first = provider_call(request={"turn": 1})
        second = provider_call(request={"turn": 2, "prior": first})
        return second

    adapters = build_role_adapters(
        _payload(),
        provider=provider,
        ledger=RoleBudgetLedger(),
        preflight=_preflight(),
        runner=runner,
    )

    assert adapters.execute(
        role="architect",
        phase="architect_to_manager",
        context={"task": "bounded"},
    ) == {"turn": 2}
    assert len(provider_calls) == 2
    assert adapters.ledger.snapshot()["roles"]["architect"]["calls"] == 2
    # One pre-orchestration record plus one pre-model-call record per turn.
    assert len(adapters.preflight.evidence()) == 3


def test_role_adapter_module_cannot_enter_legacy_pipeline_or_model_clients() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = {
        "agents",
        "fastmcp",
        "github",
        "litellm",
        "pipeline_mcp",
        "requests",
    }
    assert all(name.split(".", 1)[0] not in forbidden for name in imported)
    source = SOURCE.read_text(encoding="utf-8")
    assert "_prepare_repository_branches" not in source


def test_manifest_request_ref_mismatch_fails_closed() -> None:
    payload = _payload()
    payload["repositories"][0]["source_branch"] = "different/source"
    with pytest.raises(RuntimeError, match="source does not match"):
        build_role_adapters(
            payload,
            provider=lambda **_kwargs: ProviderCallResult({}, 1, 1),
            ledger=RoleBudgetLedger(),
            preflight=_preflight(),
        )
