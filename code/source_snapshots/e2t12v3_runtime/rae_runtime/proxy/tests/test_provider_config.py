from __future__ import annotations

import copy

import pytest
from unittest.mock import MagicMock, patch

from provider_config import (
    COMPANY_LITELLM,
    OPENAI,
    OPENAI_CHAT_REASONING_EFFORT,
    ProviderConfigurationError,
    build_agents_model,
    build_agents_model_settings,
    resolve_provider_config,
    validate_provider_identity,
)


def _company_env(**overrides: str) -> dict[str, str]:
    value = {
        "LLM_PROVIDER": COMPANY_LITELLM,
        "LITELLM_BASE_URL": "http://weles.cs.ucl.ac.uk:4000/",
        "LITELLM_MODEL": "qwen3-coder",
        "LITELLM_PROXY_API_KEY": "company-secret",
    }
    value.update(overrides)
    return value


def _openai_env(**overrides: str) -> dict[str, str]:
    value = {
        "LLM_PROVIDER": OPENAI,
        "OPENAI_BASE_URL": "https://api.openai.com/v1/",
        "OPENAI_MODEL": "gpt-5.6-terra",
        "OPENAI_API_KEY": "openai-secret",
    }
    value.update(overrides)
    return value


def _payload(config) -> dict:
    return {
        "execution_objectives": {
            "parsed_task_parameters": {
                "provider_mode": config.provider_mode,
                "provider_base_url": config.base_url,
                "provider_identity_sha256": config.identity["sha256"],
                "model": config.model_alias,
            }
        }
    }


def test_explicit_company_config_uses_proxy_prefix_and_company_secret() -> None:
    config = resolve_provider_config(_company_env(OPENAI_API_KEY="wrong-secret"))

    assert config.provider_mode == COMPANY_LITELLM
    assert config.base_url == "http://weles.cs.ucl.ac.uk:4000"
    assert config.model_alias == "qwen3-coder"
    assert config.transport_model == "litellm_proxy/qwen3-coder"
    assert config.api_key == "company-secret"
    assert config.api_key_source == "LITELLM_PROXY_API_KEY"


def test_explicit_openai_config_never_uses_proxy_prefix_or_company_secret() -> None:
    config = resolve_provider_config(
        _openai_env(
            API_KEY="wrong-company-secret",
            LITELLM_PROXY_API_KEY="wrong-company-secret-2",
        )
    )

    assert config.provider_mode == OPENAI
    assert config.base_url == "https://api.openai.com/v1"
    assert config.model_alias == "gpt-5.6-terra"
    assert config.transport_model == "gpt-5.6-terra"
    assert "litellm_proxy/" not in config.transport_model
    assert config.api_key == "openai-secret"
    assert config.api_key_source == "OPENAI_API_KEY"


def test_legacy_omitted_provider_preserves_company_precedence() -> None:
    config = resolve_provider_config(
        {
            "API_KEY": "legacy-first",
            "LITELLM_PROXY_API_KEY": "proxy-second",
            "OPENAI_API_KEY": "openai-last",
            "MODEL": "legacy-model",
            "LITELLM_MODEL": "secondary-model",
            "OPENAI_BASE_URL": "http://legacy-compatible-proxy:4000",
        }
    )

    assert config.provider_mode == COMPANY_LITELLM
    assert config.api_key == "legacy-first"
    assert config.api_key_source == "API_KEY"
    assert config.model_alias == "legacy-model"
    assert config.base_url == "http://legacy-compatible-proxy:4000"
    assert config.explicit_provider is False


@pytest.mark.parametrize(
    "environment, message",
    [
        ({"API_KEY": "secret"}, "LLM_PROVIDER"),
        ({"LLM_PROVIDER": "unknown", "API_KEY": "secret"}, "LLM_PROVIDER"),
        (
            {
                "LLM_PROVIDER": OPENAI,
                "OPENAI_MODEL": "gpt-5.6-terra",
                "API_KEY": "company-only",
            },
            "OPENAI_API_KEY",
        ),
        (
            {
                "LLM_PROVIDER": COMPANY_LITELLM,
                "LITELLM_MODEL": "qwen3-coder",
                "OPENAI_API_KEY": "openai-only",
            },
            "LiteLLM API key",
        ),
        (
            {
                "LLM_PROVIDER": OPENAI,
                "OPENAI_MODEL": "litellm_proxy/gpt-5.6-terra",
                "OPENAI_API_KEY": "secret",
            },
            "model identifier",
        ),
        (
            {
                "LLM_PROVIDER": OPENAI,
                "OPENAI_MODEL": "gpt-5.6-terra",
                "OPENAI_API_KEY": "secret",
                "OPENAI_BASE_URL": "https://user:password@example.com/v1",
            },
            "credentials",
        ),
    ],
)
def test_invalid_or_cross_provider_configuration_fails_closed(environment, message):
    with pytest.raises(ProviderConfigurationError, match=message):
        resolve_provider_config(
            environment,
            require_explicit_provider=environment.get("LLM_PROVIDER") is None,
        )


def test_provider_identity_and_repr_never_contain_secret() -> None:
    config = resolve_provider_config(_openai_env())

    assert "openai-secret" not in repr(config)
    assert "openai-secret" not in repr(config.identity)
    assert config.identity["provider_mode"] == OPENAI
    assert len(config.identity["sha256"]) == 64


def test_explicit_e3_identity_must_match_all_resolved_fields() -> None:
    config = resolve_provider_config(_openai_env())
    payload = _payload(config)
    before = copy.deepcopy(payload)

    validate_provider_identity(config, payload)
    assert payload == before

    for name in ("provider_mode", "provider_base_url", "provider_identity_sha256", "model"):
        changed = copy.deepcopy(payload)
        changed["execution_objectives"]["parsed_task_parameters"][name] = "wrong"
        with pytest.raises(ProviderConfigurationError, match=name):
            validate_provider_identity(config, changed)


def test_company_and_openai_factories_are_transport_specific() -> None:
    calls = []

    class FakeLiteLLM:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(("litellm", kwargs))

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(("client", kwargs))

    class FakeOpenAIModel:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            calls.append(("openai_model", kwargs))

    company = resolve_provider_config(_company_env())
    company_model = build_agents_model(company, litellm_model_cls=FakeLiteLLM)
    assert company_model.kwargs == {
        "model": "litellm_proxy/qwen3-coder",
        "api_key": "company-secret",
        "base_url": "http://weles.cs.ucl.ac.uk:4000",
    }

    openai = resolve_provider_config(_openai_env())
    openai_model = build_agents_model(
        openai,
        openai_client_cls=FakeClient,
        openai_chat_model_cls=FakeOpenAIModel,
    )
    assert openai_model.kwargs["model"] == "gpt-5.6-terra"
    assert openai_model.kwargs["openai_client"].kwargs == {
        "api_key": "openai-secret",
        "base_url": "https://api.openai.com/v1",
        "max_retries": 0,
    }
    assert [name for name, _ in calls] == ["litellm", "client", "openai_model"]


def test_llm_client_uses_direct_openai_endpoint_and_raw_model_with_fake_http(
    monkeypatch,
) -> None:
    from llm_client import call_llm

    monkeypatch.delenv("RAE_OFFLINE", raising=False)
    config = resolve_provider_config(_openai_env())
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    with patch("llm_client.requests.post", return_value=response) as post:
        assert call_llm("health", provider_config=config) == "ok"

    assert post.call_args.args[0] == "https://api.openai.com/v1/chat/completions"
    assert post.call_args.kwargs["json"]["model"] == "gpt-5.6-terra"
    assert post.call_args.kwargs["json"]["reasoning_effort"] == "none"
    assert "litellm_proxy/" not in post.call_args.kwargs["json"]["model"]
    assert post.call_args.kwargs["headers"]["Authorization"] == (
        "Bearer openai-secret"
    )


def test_agents_settings_fix_openai_reasoning_without_changing_company_defaults():
    class FakeSettings:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    openai_settings = build_agents_model_settings(
        resolve_provider_config(_openai_env()),
        model_settings_cls=FakeSettings,
    )
    assert openai_settings.kwargs == {
        "reasoning": {"effort": OPENAI_CHAT_REASONING_EFFORT},
        "include_usage": True,
    }

    bounded = build_agents_model_settings(
        resolve_provider_config(_openai_env()),
        model_settings_cls=FakeSettings,
        max_tokens=32,
    )
    assert bounded.kwargs["extra_args"] == {"max_completion_tokens": 32}

    company_settings = build_agents_model_settings(
        resolve_provider_config(_company_env()),
        model_settings_cls=FakeSettings,
    )
    assert company_settings.kwargs == {}

    company_bounded = build_agents_model_settings(
        resolve_provider_config(_company_env()),
        model_settings_cls=FakeSettings,
        max_tokens=32,
    )
    assert company_bounded.kwargs == {"max_tokens": 32}

    manager_settings = build_agents_model_settings(
        resolve_provider_config(_openai_env()),
        model_settings_cls=FakeSettings,
        tool_choice="none",
    )
    assert manager_settings.kwargs["tool_choice"] == "none"

    with pytest.raises(ProviderConfigurationError, match="max_tokens"):
        build_agents_model_settings(
            resolve_provider_config(_openai_env()),
            model_settings_cls=FakeSettings,
            max_tokens=0,
        )
