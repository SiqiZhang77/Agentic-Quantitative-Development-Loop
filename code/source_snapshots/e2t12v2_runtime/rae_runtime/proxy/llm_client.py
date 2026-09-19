import requests
import os
import logging
from dotenv import load_dotenv
from provider_config import (
    OPENAI,
    OPENAI_CHAT_REASONING_EFFORT,
    ProviderConfig,
    resolve_provider_config,
)

load_dotenv()

log = logging.getLogger("rae.llm_client")

def call_llm(
    prompt: str,
    budget_guard=None,
    *,
    provider_config: ProviderConfig | None = None,
) -> str:
    if os.getenv("RAE_OFFLINE") == "1":
        return (
            "# strategy.py — offline stub from call_llm (RAE_OFFLINE=1)\n"
            "def generate_signals(prices):\n"
            "    short_ma = prices.rolling(10).mean()\n"
            "    long_ma = prices.rolling(50).mean()\n"
            "    rsi_ok = prices.rolling(14).mean() < 70  # stubbed RSI filter\n"
            "    return ((short_ma > long_ma) & rsi_ok).astype(int)\n"
        )

    config = provider_config or resolve_provider_config()
    log.info(
        "LLM provider resolved — provider=%s model=%s endpoint=%s",
        config.provider_mode,
        config.model_alias,
        config.base_url,
    )

    request_body = {
        "model": config.model_alias,
        "messages": [{"role": "user", "content": prompt}],
    }
    if config.provider_mode == OPENAI:
        request_body["reasoning_effort"] = OPENAI_CHAT_REASONING_EFFORT

    try:
        response = requests.post(
            f"{config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"{config.provider_mode} request failed: {type(e).__name__}"
        ) from e

    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(
            f"Provider returned non-JSON (HTTP {response.status_code}): "
            f"{response.text[:500]}"
        )

    if response.status_code != 200 or "choices" not in body:
        raise RuntimeError(
            f"Provider error (HTTP {response.status_code}): {body}"
        )

    if budget_guard:
        budget_guard.record_llm_response(body, model=config.model_alias)

    log.info(
        "LLM call completed — provider=%s model=%s prompt_len=%d chars",
        config.provider_mode,
        config.model_alias,
        len(prompt),
    )
    return body["choices"][0]["message"]["content"]
