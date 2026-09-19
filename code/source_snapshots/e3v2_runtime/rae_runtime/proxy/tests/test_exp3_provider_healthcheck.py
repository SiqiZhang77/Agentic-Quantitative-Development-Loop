from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from exp3.provider_healthcheck import (
    AUTHORIZATION_ENV,
    AUTHORIZATION_VALUE,
    EXPECTED_BASE_URL,
    EXPECTED_MODEL,
    SYNTHETIC_OUTPUT,
    ProviderHealthcheckError,
    SdkCallResult,
    _safe_failure,
    run_provider_healthcheck,
)


def _environment(**overrides: str) -> dict[str, str]:
    value = {
        "LLM_PROVIDER": "openai",
        "OPENAI_BASE_URL": EXPECTED_BASE_URL,
        "OPENAI_MODEL": EXPECTED_MODEL,
        "OPENAI_API_KEY": "PRIVATE-healthcheck-secret",
    }
    value.update(overrides)
    return value


@dataclass
class Usage:
    requests: int = 1
    input_tokens: int = 5
    output_tokens: int = 2
    total_tokens: int = 7


@dataclass
class Response:
    output: str
    usage: Usage


class FakeModel:
    def __init__(self):
        self.calls = []

    async def get_response(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return Response("PRIVATE response body", Usage())


class FakeSettings:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _version(name: str) -> str:
    return {"openai-agents": "0.17.7", "openai": "2.44.0"}[name]


def test_default_healthcheck_is_configuration_only_and_secret_free():
    result = run_provider_healthcheck(_environment())

    assert result["status"] == "ready"
    assert result["provider_call_count"] == 0
    assert result["formal_observation"] is False
    assert result["source_ref"] is None
    assert result["m0_m1_same_provider"] is True
    assert result["m0_m1_same_model"] is True
    assert result["rag_enabled"] is False
    assert result["shared_call_cap"] == 15
    assert result["shared_token_cap"] == 200_000
    assert "PRIVATE-healthcheck-secret" not in json.dumps(result)


def test_real_mode_requires_exact_purpose_specific_authorization():
    with pytest.raises(ProviderHealthcheckError, match=AUTHORIZATION_ENV):
        run_provider_healthcheck(_environment(), execute_once=True)


def test_authorized_fake_exercises_factory_boundary_and_exactly_one_call():
    environment = _environment(**{AUTHORIZATION_ENV: AUTHORIZATION_VALUE})
    model = FakeModel()
    seen = {}

    def model_factory(config):
        seen["config"] = config
        return model

    def settings_factory(config, *, max_tokens):
        seen["settings_config"] = config
        return FakeSettings(
            reasoning={"effort": "none"},
            include_usage=True,
            extra_args={"max_completion_tokens": max_tokens},
        )

    async def sdk_call(accounted_model, settings):
        seen["settings"] = settings
        await accounted_model.get_response(
            "synthetic-system",
            "synthetic-input",
            settings,
            [],
            None,
            [],
            None,
        )
        return SdkCallResult(
            final_output=SYNTHETIC_OUTPUT,
            usage={
                "calls": 1,
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        )

    result = run_provider_healthcheck(
        environment,
        execute_once=True,
        model_factory=model_factory,
        settings_factory=settings_factory,
        sdk_call=sdk_call,
        version_lookup=_version,
    )

    assert result["status"] == "passed"
    assert result["provider_call_count"] == 1
    assert result["total_tokens"] == 7
    assert result["dependency_versions"] == {
        "openai-agents": "0.17.7",
        "openai": "2.44.0",
    }
    assert result["output_marker_matched"] is True
    assert result["external_surfaces"] == {
        "openai": "one_synthetic_inference_call",
        "jira": False,
        "airflow": False,
        "github": False,
        "mcp": False,
    }
    assert len(model.calls) == 1
    assert seen["settings"].kwargs["extra_args"] == {
        "max_completion_tokens": 32,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "PRIVATE" not in serialized
    assert SYNTHETIC_OUTPUT not in serialized


@pytest.mark.parametrize(
    "overrides",
    [
        {"LLM_PROVIDER": "company_litellm"},
        {"OPENAI_BASE_URL": "https://example.com/v1"},
        {"OPENAI_MODEL": "gpt-5.6-sol"},
    ],
)
def test_unapproved_provider_identity_fails_before_any_call(overrides):
    environment = _environment(**overrides)
    if overrides.get("LLM_PROVIDER") == "company_litellm":
        environment.update(
            {
                "LITELLM_MODEL": "qwen3-coder",
                "LITELLM_PROXY_API_KEY": "company-secret",
            }
        )
    with pytest.raises(ProviderHealthcheckError):
        run_provider_healthcheck(environment)


def test_dependency_version_drift_fails_before_model_factory():
    environment = _environment(**{AUTHORIZATION_ENV: AUTHORIZATION_VALUE})
    calls = []

    def model_factory(_config):
        calls.append("called")
        return FakeModel()

    with pytest.raises(ProviderHealthcheckError, match="versions"):
        run_provider_healthcheck(
            environment,
            execute_once=True,
            model_factory=model_factory,
            version_lookup=lambda name: {
                "openai-agents": "0.17.6",
                "openai": "2.44.0",
            }[name],
        )
    assert calls == []


def test_safe_failure_reports_only_non_sensitive_provider_fields():
    class FakeBadRequest(Exception):
        status_code = 400
        code = "unsupported_parameter"
        param = "max_tokens"
        request_id = "req_safe_identifier"

    exc = FakeBadRequest(
        "secret=sk-private prompt=PRIVATE response=PRIVATE"
    )
    result = _safe_failure(exc)

    assert result == {
        "schema_version": "exp3-nonformal-provider-healthcheck-v1",
        "status": "failed",
        "formal_observation": False,
        "failure_type": "FakeBadRequest",
        "http_status": 400,
        "provider_error_code": "unsupported_parameter",
        "provider_error_param": "max_tokens",
        "provider_request_id": "req_safe_identifier",
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "PRIVATE" not in serialized
    assert "sk-private" not in serialized
