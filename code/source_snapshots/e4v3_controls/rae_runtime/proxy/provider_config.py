"""Central, fail-closed model-provider configuration and model factories.

The legacy runtime historically exposed several overlapping environment names
and constructed LiteLLM models in multiple modules.  This module keeps the
omitted-provider compatibility path, while requiring explicit, non-mixed
configuration for Experiment 3 and direct OpenAI use.

No provider SDK is imported at module import time.  Tests inject constructors,
and production imports the pinned SDK classes only when the selected provider
factory is actually used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit


COMPANY_LITELLM: Final = "company_litellm"
OPENAI: Final = "openai"
SUPPORTED_PROVIDERS: Final = frozenset({COMPANY_LITELLM, OPENAI})
DEFAULT_COMPANY_BASE_URL: Final = "http://weles.cs.ucl.ac.uk:4000"
DEFAULT_COMPANY_MODEL: Final = "nova-micro"
DEFAULT_OPENAI_BASE_URL: Final = "https://api.openai.com/v1"
OPENAI_CHAT_REASONING_EFFORT: Final = "none"
EXPECTED_OPENAI_AGENTS_VERSION: Final = "0.17.7"
EXPECTED_OPENAI_VERSION: Final = "2.44.0"
PROVIDER_IDENTITY_VERSION: Final = "llm-provider-identity-v1"
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class ProviderConfigurationError(RuntimeError):
    """Provider configuration is absent, ambiguous, unsafe or inconsistent."""


class ProviderFactoryError(ProviderConfigurationError):
    """The selected provider SDK model cannot be constructed safely."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _identity_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _nonempty(source: Mapping[str, str], name: str) -> str | None:
    value = source.get(name)
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        raise ProviderConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _first(
    source: Mapping[str, str],
    names: tuple[str, ...],
) -> tuple[str | None, str | None]:
    for name in names:
        value = _nonempty(source, name)
        if value is not None:
            return value, name
    return None, None


def _normalise_base_url(value: str, *, name: str) -> str:
    if len(value) > 500:
        raise ProviderConfigurationError(f"{name} is too long")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderConfigurationError(
            f"{name} must be an absolute HTTP(S) URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ProviderConfigurationError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProviderConfigurationError(
            f"{name} must not contain a query string or fragment"
        )
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def _model(value: str, *, name: str) -> str:
    if _MODEL_RE.fullmatch(value) is None:
        raise ProviderConfigurationError(
            f"{name} must be a bounded provider model identifier"
        )
    return value


@dataclass(frozen=True)
class ProviderConfig:
    """Resolved provider configuration; repr and safe identity omit the key."""

    provider_mode: str
    base_url: str
    model_alias: str
    transport_model: str
    adapter: str
    api_key: str = field(repr=False, compare=False)
    api_key_source: str = field(repr=False, compare=False)
    explicit_provider: bool = False

    def __post_init__(self) -> None:
        if self.provider_mode not in SUPPORTED_PROVIDERS:
            raise ProviderConfigurationError("unsupported provider mode")
        if (
            _normalise_base_url(self.base_url, name="provider base URL")
            != self.base_url
        ):
            raise ProviderConfigurationError("provider base URL must be normalized")
        _model(self.model_alias, name="provider model alias")
        if type(self.api_key) is not str or not self.api_key:
            raise ProviderConfigurationError("provider API key is required")
        if type(self.api_key_source) is not str or not self.api_key_source:
            raise ProviderConfigurationError("provider API key source is required")
        if type(self.explicit_provider) is not bool:
            raise ProviderConfigurationError("explicit_provider must be boolean")
        expected = {
            COMPANY_LITELLM: (
                f"litellm_proxy/{self.model_alias}",
                "litellm_chat_completions",
            ),
            OPENAI: (self.model_alias, "openai_chat_completions"),
        }[self.provider_mode]
        if (self.transport_model, self.adapter) != expected:
            raise ProviderConfigurationError(
                "provider transport model or adapter is inconsistent"
            )

    @property
    def identity(self) -> dict[str, str]:
        value = {
            "schema_version": PROVIDER_IDENTITY_VERSION,
            "provider_mode": self.provider_mode,
            "base_url": self.base_url,
            "model_alias": self.model_alias,
            "transport_model": self.transport_model,
            "adapter": self.adapter,
        }
        return {**value, "sha256": _identity_hash(value)}


def resolve_provider_config(
    environment: Mapping[str, str] | None = None,
    *,
    require_explicit_provider: bool = False,
) -> ProviderConfig:
    """Resolve one provider without allowing cross-provider secret fallback.

    When ``LLM_PROVIDER`` is omitted, the historical company-LiteLLM path and
    its old environment precedence are retained.  Explicit OpenAI configuration
    accepts only ``OPENAI_API_KEY``; an old company key can never silently become
    an OpenAI credential.  Explicit company configuration similarly never falls
    through to ``OPENAI_API_KEY``.
    """

    source = os.environ if environment is None else environment
    requested = _nonempty(source, "LLM_PROVIDER")
    if requested is None:
        if require_explicit_provider:
            raise ProviderConfigurationError(
                "LLM_PROVIDER is required for an explicit Experiment 3 run"
            )
        provider_mode = COMPANY_LITELLM
        explicit = False
    else:
        provider_mode = requested.lower()
        explicit = True
        if provider_mode not in SUPPORTED_PROVIDERS:
            raise ProviderConfigurationError(
                "LLM_PROVIDER must be company_litellm or openai"
            )

    if provider_mode == OPENAI:
        base = _normalise_base_url(
            _nonempty(source, "OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL,
            name="OPENAI_BASE_URL",
        )
        alias_value = _nonempty(source, "OPENAI_MODEL")
        if alias_value is None:
            raise ProviderConfigurationError(
                "OPENAI_MODEL is required when LLM_PROVIDER=openai"
            )
        alias = _model(alias_value, name="OPENAI_MODEL")
        if alias.startswith("litellm_proxy"):
            raise ProviderConfigurationError(
                "OpenAI model identifiers must not use the LiteLLM proxy prefix"
            )
        key = _nonempty(source, "OPENAI_API_KEY")
        if key is None:
            raise ProviderConfigurationError(
                "OPENAI_API_KEY is required when LLM_PROVIDER=openai"
            )
        return ProviderConfig(
            provider_mode=OPENAI,
            base_url=base,
            model_alias=alias,
            transport_model=alias,
            adapter="openai_chat_completions",
            api_key=key,
            api_key_source="OPENAI_API_KEY",
            explicit_provider=explicit,
        )

    base_source = (
        ("LITELLM_BASE_URL", "OPENAI_BASE_URL")
        if not explicit
        else ("LITELLM_BASE_URL",)
    )
    base_value, _ = _first(source, base_source)
    base = _normalise_base_url(
        base_value or DEFAULT_COMPANY_BASE_URL,
        name="LITELLM_BASE_URL",
    )
    model_names = (
        ("MODEL", "LITELLM_MODEL")
        if not explicit
        else ("LITELLM_MODEL", "MODEL")
    )
    alias_value, _ = _first(source, model_names)
    alias = _model(alias_value or DEFAULT_COMPANY_MODEL, name="LITELLM_MODEL")
    if alias.startswith("litellm_proxy/"):
        raise ProviderConfigurationError(
            "LITELLM_MODEL must be the raw alias without litellm_proxy/"
        )
    key_names = (
        ("API_KEY", "LITELLM_PROXY_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY")
        if not explicit
        else ("LITELLM_PROXY_API_KEY", "LITELLM_API_KEY", "API_KEY")
    )
    key, key_source = _first(source, key_names)
    if key is None or key_source is None:
        raise ProviderConfigurationError(
            "a company LiteLLM API key is required"
        )
    return ProviderConfig(
        provider_mode=COMPANY_LITELLM,
        base_url=base,
        model_alias=alias,
        transport_model=f"litellm_proxy/{alias}",
        adapter="litellm_chat_completions",
        api_key=key,
        api_key_source=key_source,
        explicit_provider=explicit,
    )


def validate_provider_identity(
    config: ProviderConfig,
    payload: Mapping[str, Any],
) -> None:
    """Bind an explicit E3 request's public provider identity to its secret config."""

    if not isinstance(config, ProviderConfig) or not isinstance(payload, Mapping):
        raise TypeError("ProviderConfig and payload are required")
    parameters = (
        (payload.get("execution_objectives") or {}).get("parsed_task_parameters")
        or (payload.get("args") or {})
    )
    if not isinstance(parameters, Mapping):
        raise ProviderConfigurationError("E3 provider identity parameters are missing")
    expected = {
        "provider_mode": config.provider_mode,
        "provider_base_url": config.base_url,
        "model": config.model_alias,
        "provider_identity_sha256": config.identity["sha256"],
    }
    for name, value in expected.items():
        if parameters.get(name) != value:
            raise ProviderConfigurationError(
                f"E3 request {name} does not match resolved provider configuration"
            )


def build_agents_model(
    config: ProviderConfig,
    *,
    litellm_model_cls: Any | None = None,
    openai_client_cls: Any | None = None,
    openai_chat_model_cls: Any | None = None,
) -> object:
    """Construct the selected Agents SDK model with lazy dependency imports."""

    if not isinstance(config, ProviderConfig):
        raise TypeError("config must be a ProviderConfig")
    try:
        if config.provider_mode == COMPANY_LITELLM:
            if litellm_model_cls is None:
                from agents.extensions.models.litellm_model import (
                    LitellmModel as litellm_model_cls,
                )
            return litellm_model_cls(
                model=config.transport_model,
                api_key=config.api_key,
                base_url=config.base_url,
            )

        if openai_client_cls is None:
            from openai import AsyncOpenAI as openai_client_cls
        if openai_chat_model_cls is None:
            from agents import OpenAIChatCompletionsModel as openai_chat_model_cls
        client = openai_client_cls(
            api_key=config.api_key,
            base_url=config.base_url,
            # A logical E3 model call must be one physical provider attempt.
            # SDK retries would otherwise escape the shared call ledger.
            max_retries=0,
        )
        model = openai_chat_model_cls(
            model=config.transport_model,
            openai_client=client,
        )
        # The pinned Agents SDK model exposes a no-op close() even though the
        # AsyncOpenAI client it owns holds HTTP connections. Keep an explicit,
        # runtime-private ownership link so the E3 role runner can await that
        # client before closing its event loop.
        setattr(model, "_rae_owned_async_client", client)
        return model
    except ImportError as exc:
        raise ProviderFactoryError(
            "pinned provider dependencies are unavailable"
        ) from exc
    except ProviderConfigurationError:
        raise
    except Exception as exc:
        raise ProviderFactoryError(
            "selected provider model factory is incompatible"
        ) from exc


def build_agents_model_settings(
    config: ProviderConfig,
    *,
    model_settings_cls: Any | None = None,
    max_tokens: int | None = None,
    tool_choice: str | None = None,
) -> object:
    """Return transport-compatible Agents SDK settings for one provider.

    GPT-5.6 Chat Completions with function/MCP tools must not inherit a
    reasoning default that changes tool compatibility.  Direct OpenAI therefore
    fixes reasoning to ``none`` for both E3 arms.  The company compatibility
    route keeps its historical settings.  A bounded output setting is used only
    by the separately authorized non-formal health check; formal runs remain
    governed by the shared and per-role ledgers.  The OpenAI Chat Completions
    API receives the current ``max_completion_tokens`` field through
    ``extra_args`` because openai-agents 0.17.7 exposes only the deprecated
    ``max_tokens`` ModelSettings field.  The company compatibility route keeps
    the historical field name.
    """

    if not isinstance(config, ProviderConfig):
        raise TypeError("config must be a ProviderConfig")
    if max_tokens is not None and (
        type(max_tokens) is not int or max_tokens <= 0
    ):
        raise ProviderConfigurationError(
            "max_tokens must be a positive integer when supplied"
        )
    if tool_choice not in {None, "none"}:
        raise ProviderConfigurationError(
            "tool_choice must be None or 'none' for the shared provider factory"
        )
    try:
        if model_settings_cls is None:
            from agents import ModelSettings as model_settings_cls
        kwargs: dict[str, Any] = {}
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        if config.provider_mode == OPENAI:
            kwargs.update(
                {
                    "reasoning": {"effort": OPENAI_CHAT_REASONING_EFFORT},
                    "include_usage": True,
                }
            )
        if max_tokens is not None:
            if config.provider_mode == OPENAI:
                kwargs["extra_args"] = {
                    "max_completion_tokens": max_tokens,
                }
            else:
                kwargs["max_tokens"] = max_tokens
        return model_settings_cls(**kwargs)
    except ImportError as exc:
        raise ProviderFactoryError(
            "pinned provider dependencies are unavailable"
        ) from exc
    except ProviderConfigurationError:
        raise
    except Exception as exc:
        raise ProviderFactoryError(
            "selected provider model settings are incompatible"
        ) from exc


def require_identity_hash(value: object) -> str:
    """Validate a provider identity hash without ever accepting secret material."""

    if type(value) is not str or _HASH_RE.fullmatch(value) is None:
        raise ProviderConfigurationError(
            "provider identity hash must be a lowercase SHA-256"
        )
    return value
