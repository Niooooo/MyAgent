"""Explicitly bounded network clients; credentials are environment-only."""

import os
from types import SimpleNamespace

from ..deepseek import DEFAULT_DEEPSEEK_BASE_URL, DeepSeekResponsesClient


def create_client(provider, thinking, timeout_seconds):
    variable = "DEEPSEEK_API_KEY" if provider == "deepseek" else "OPENAI_API_KEY"
    key = os.getenv(variable)
    if not key:
        raise ValueError(f"{variable} is required for --provider {provider}")
    from openai import OpenAI

    base_url = (os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
                if provider == "deepseek" else os.getenv("OPENAI_BASE_URL"))
    options = {"api_key": key, "max_retries": 0, "timeout": timeout_seconds}
    if base_url:
        options["base_url"] = base_url
    client = OpenAI(**options)
    responses = (DeepSeekResponsesClient(client.chat.completions, thinking=thinking).responses
                 if provider == "deepseek" else client.responses)
    return SimpleNamespace(responses=responses, close=client.close)
