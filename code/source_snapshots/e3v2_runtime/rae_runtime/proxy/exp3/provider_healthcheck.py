"""One-call, non-formal provider health check for Experiment 3.

The default command is configuration-only and cannot contact a provider.  A
real call requires both ``--execute-once`` and an exact, purpose-specific
environment authorization.  The check uses a synthetic prompt, no MCP server,
no repository client and the same provider factory/accounting wrapper used by
M0/M1.  It is never an Experiment 3 observation.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import version as distribution_version
from typing import Any, Final

from exp3.budget import RoleBudgetLedger
from exp3.isolation import NegativeRefManifest, NegativeRefPreflight
from exp3.production_provider import Experiment3ProductionProviderBoundary
from provider_config import (
    EXPECTED_OPENAI_AGENTS_VERSION,
    EXPECTED_OPENAI_VERSION,
    OPENAI,
    OPENAI_CHAT_REASONING_EFFORT,
    ProviderConfig,
    build_agents_model,
    build_agents_model_settings,
    resolve_provider_config,
)


SCHEMA_VERSION: Final = "exp3-nonformal-provider-healthcheck-v1"
AUTHORIZATION_ENV: Final = "EXP3_NONFORMAL_HEALTHCHECK_AUTHORIZATION"
AUTHORIZATION_VALUE: Final = "AUTHORIZE_EXACTLY_ONE_SYNTHETIC_CALL"
EXPECTED_BASE_URL: Final = "https://api.openai.com/v1"
EXPECTED_MODEL: Final = "gpt-5.6-terra"
SYNTHETIC_OUTPUT: Final = "EXP3_HEALTHCHECK_OK"
SYNTHETIC_MAX_TOKENS: Final = 32
SYNTHETIC_RUN_ID: Final = "E3-NONFORMAL-PROVIDER-HEALTHCHECK"
SYNTHETIC_REPOSITORY: Final = "bankingscience/EXP3NonformalHealthcheck"
SYNTHETIC_SOURCE_REF: Final = "healthcheck/source-ref"
SYNTHETIC_TARGET_REF: Final = "healthcheck/current-target-ref"


class ProviderHealthcheckError(RuntimeError):
    """The non-formal health check was unsafe, incompatible or unsuccessful."""


@dataclass(frozen=True)
class SdkCallResult:
    final_output: str
    usage: Mapping[str, int]


SdkCall = Callable[[object, object], Awaitable[SdkCallResult]]
ModelFactory = Callable[[ProviderConfig], object]
SettingsFactory = Callable[..., object]
VersionLookup = Callable[[str], str]


def _manifest() -> NegativeRefManifest:
    return NegativeRefManifest.from_dict(
        {
            "schema_version": "exp3-negative-ref-manifest-v1",
            "experiment_id": "E3-manager-star-v1",
            "run_id": SYNTHETIC_RUN_ID,
            "repositories": [
                {
                    "repo_full_name": SYNTHETIC_REPOSITORY,
                    "source_ref": SYNTHETIC_SOURCE_REF,
                    "current_target_ref": SYNTHETIC_TARGET_REF,
                    "paired_target_ref": "healthcheck/paired-target-ref",
                    "deny_refs": {
                        "v5_refs": ["healthcheck/deny-v5"],
                        "e2_output_refs": ["healthcheck/deny-e2-output"],
                        "earlier_e3_targets": [],
                        "protected_refs": ["main"],
                        "arbitrary_probe_refs": [
                            "healthcheck/arbitrary-a",
                            "healthcheck/arbitrary-b",
                        ],
                    },
                }
            ],
        }
    )


def _payload(config: ProviderConfig) -> dict[str, Any]:
    return {
        "architecture_mode": "single_agent",
        "run_id": SYNTHETIC_RUN_ID,
        "execution_objectives": {
            "parsed_task_parameters": {
                "rag_enabled": False,
                "provider_mode": config.provider_mode,
                "provider_base_url": config.base_url,
                "provider_identity_sha256": config.identity["sha256"],
                "model": config.model_alias,
            }
        },
        "repositories": [
            {
                "repo_full_name": SYNTHETIC_REPOSITORY,
                "source_branch": SYNTHETIC_SOURCE_REF,
                "target_branch": SYNTHETIC_TARGET_REF,
            }
        ],
    }


def _boundary(config: ProviderConfig) -> Experiment3ProductionProviderBoundary:
    manifest = _manifest()

    def local_ref_probe(_repository: str, ref: str) -> bool:
        return ref == SYNTHETIC_SOURCE_REF

    return Experiment3ProductionProviderBoundary(
        architecture_mode="single_agent",
        preflight=NegativeRefPreflight(
            manifest,
            ref_exists=local_ref_probe,
        ),
        ledger=RoleBudgetLedger.for_single_agent(),
        provider_config=config,
    )


def _versions(version_lookup: VersionLookup) -> dict[str, str]:
    observed = {
        "openai-agents": version_lookup("openai-agents"),
        "openai": version_lookup("openai"),
    }
    expected = {
        "openai-agents": EXPECTED_OPENAI_AGENTS_VERSION,
        "openai": EXPECTED_OPENAI_VERSION,
    }
    if observed != expected:
        raise ProviderHealthcheckError(
            "provider dependency versions do not match the approved identity"
        )
    return observed


def _require_approved_config(config: ProviderConfig) -> None:
    if config.provider_mode != OPENAI:
        raise ProviderHealthcheckError("health check requires provider_mode=openai")
    if config.base_url != EXPECTED_BASE_URL:
        raise ProviderHealthcheckError("health check base URL is not approved")
    if config.model_alias != EXPECTED_MODEL:
        raise ProviderHealthcheckError("health check model is not approved")
    if config.transport_model != EXPECTED_MODEL:
        raise ProviderHealthcheckError("health check transport model is inconsistent")


def _safe_identity(config: ProviderConfig) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "formal_observation": False,
        "provider_identity": copy.deepcopy(config.identity),
        "reasoning_effort": OPENAI_CHAT_REASONING_EFFORT,
        "rag_enabled": False,
        "m0_m1_same_provider": True,
        "m0_m1_same_model": True,
        "shared_call_cap": 15,
        "shared_token_cap": 200_000,
        "source_ref": None,
        "source_ref_status": "not_applicable_to_synthetic_healthcheck",
        "external_surfaces": {
            "openai": "configuration_only",
            "jira": False,
            "airflow": False,
            "github": False,
            "mcp": False,
        },
    }


def _safe_failure(exc: Exception) -> dict[str, Any]:
    """Return provider diagnostics that cannot contain request or secret data."""

    failure: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "failed",
        "formal_observation": False,
        "failure_type": type(exc).__name__,
    }
    for output_name, attribute in (
        ("http_status", "status_code"),
        ("provider_error_code", "code"),
        ("provider_error_param", "param"),
        ("provider_request_id", "request_id"),
    ):
        value = getattr(exc, attribute, None)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            failure[output_name] = value
    return failure


async def _default_sdk_call(model: object, model_settings: object) -> SdkCallResult:
    from agents import Agent, RunConfig, Runner

    agent = Agent(
        name="E3 non-formal provider health check",
        instructions=(
            "This is a synthetic connectivity check. Return exactly "
            f"{SYNTHETIC_OUTPUT} and nothing else. Do not call tools."
        ),
        model=model,
        model_settings=model_settings,
        tools=[],
        mcp_servers=[],
    )
    result = await Runner.run(
        agent,
        "Return the required synthetic health-check marker now.",
        max_turns=1,
        run_config=RunConfig(
            tracing_disabled=True,
            trace_include_sensitive_data=False,
            workflow_name="E3 non-formal provider health check",
        ),
    )
    wrapper = getattr(result, "context_wrapper", None)
    usage = getattr(wrapper, "usage", None)
    if usage is None:
        raise ProviderHealthcheckError("Agents SDK omitted aggregate usage")
    return SdkCallResult(
        final_output=str(getattr(result, "final_output", "")),
        usage={
            "calls": getattr(usage, "requests", None),
            "prompt_tokens": getattr(usage, "input_tokens", None),
            "completion_tokens": getattr(usage, "output_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        },
    )


def run_provider_healthcheck(
    environment: Mapping[str, str] | None = None,
    *,
    execute_once: bool = False,
    model_factory: ModelFactory = build_agents_model,
    settings_factory: SettingsFactory = build_agents_model_settings,
    sdk_call: SdkCall = _default_sdk_call,
    version_lookup: VersionLookup = distribution_version,
) -> dict[str, Any]:
    """Validate configuration, and optionally make exactly one synthetic call."""

    source = os.environ if environment is None else environment
    config = resolve_provider_config(source, require_explicit_provider=True)
    _require_approved_config(config)
    safe = _safe_identity(config)
    if not execute_once:
        return {**safe, "status": "ready", "provider_call_count": 0}

    if source.get(AUTHORIZATION_ENV) != AUTHORIZATION_VALUE:
        raise ProviderHealthcheckError(
            f"{AUTHORIZATION_ENV} must exactly authorize one synthetic call"
        )
    if str(source.get("RAE_OFFLINE", "")).strip() == "1":
        raise ProviderHealthcheckError(
            "RAE_OFFLINE=1 forbids the authorized provider health check"
        )
    versions = _versions(version_lookup)
    boundary = _boundary(config)
    payload = _payload(config)
    boundary.start(payload)
    raw_model = model_factory(config)
    accounted_model = boundary.wrap_agents_model(
        raw_model,
        role="developer",
        phase="nonformal_provider_healthcheck",
    )
    settings = settings_factory(config, max_tokens=SYNTHETIC_MAX_TOKENS)
    before = boundary.ledger.snapshot()["shared"]
    result = asyncio.run(sdk_call(accounted_model, settings))
    boundary.reconcile_runner_usage(result.usage, before=before)
    snapshot = boundary.snapshot()
    shared = snapshot["budget"]["shared"]
    records = snapshot["provider_calls"]
    if shared["calls"] != 1 or len(records) != 1:
        raise ProviderHealthcheckError(
            "health check did not account exactly one provider call"
        )
    if shared["pending_calls"] != 0:
        raise ProviderHealthcheckError("health check left a pending provider call")
    if result.final_output.strip() != SYNTHETIC_OUTPUT:
        raise ProviderHealthcheckError("provider returned the wrong synthetic marker")
    record = records[0]
    return {
        **safe,
        "status": "passed",
        "dependency_versions": versions,
        "provider_call_count": shared["calls"],
        "prompt_tokens": shared["prompt_tokens"],
        "completion_tokens": shared["completion_tokens"],
        "total_tokens": shared["total_tokens"],
        "latency_seconds": record["latency_seconds"],
        "request_sha256": record["input_sha256"],
        "response_sha256": record["output_sha256"],
        "output_marker_matched": True,
        "isolation_preflight_records": len(snapshot["isolation_preflight"]),
        "external_surfaces": {
            "openai": "one_synthetic_inference_call",
            "jira": False,
            "airflow": False,
            "github": False,
            "mcp": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed non-formal E3 provider health check"
    )
    parser.add_argument(
        "--execute-once",
        action="store_true",
        help="make one authorized synthetic inference call",
    )
    args = parser.parse_args(argv)
    try:
        result = run_provider_healthcheck(execute_once=args.execute_once)
    except Exception as exc:
        print(json.dumps(_safe_failure(exc), sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
