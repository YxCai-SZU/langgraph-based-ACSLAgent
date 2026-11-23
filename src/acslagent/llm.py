"""FB4C model transport: n=1, original request parameters, SDK retry policy.

The caller processes every returned choice. An SDK error aborts the workflow;
FB4C's apparent range(3) loop has no exception handler and always breaks once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from acslagent.domain import PromptMessage
from acslagent.model_config import ModelConfig


class ModelError(RuntimeError):
    """The model could not be reached, or answered nothing usable."""


@dataclass(frozen=True)
class Reply:
    text: str
    completion_tokens: int
    seconds: float
    parameters: dict[str, Any]
    choices: tuple[str, ...] | None = None


def request_parameters(config: ModelConfig, temperature: float) -> dict[str, Any]:
    """Exactly what the original sends, including the ones it does not use."""
    return {
        "model": config.model_name,
        "max_tokens": config.params.get("max_tokens", 8192),
        "temperature": temperature,
        "timeout": config.params.get("timeout", 60),
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "top_p": 1,
        "stream": False,
        "stop": None,
        "n": 1,
        "extra_body": {"enable_thinking": False},
    }


def ask(
    config: ModelConfig,
    messages: list[PromptMessage],
    temperature: float,
    *,
    attempts: int = 1,
) -> Reply:
    """Send one chat request and return the first choice.

    Raises ModelError after `attempts` transport failures. The caller records
    the failure and moves on; a model outage is not a verification verdict.
    """
    if not config.api_key:
        raise ModelError(
            f"environment variable {config.api_key_env or '<unset>'} has no value; "
            "export the API key before running"
        )

    from openai import OpenAI, OpenAIError

    if attempts != 1:
        raise ValueError("FB4C makes one SDK call; the SDK retains its default retry policy")
    parameters = request_parameters(config, temperature)
    started = time.monotonic()
    try:
        with OpenAI(api_key=config.api_key, base_url=config.base_url) as client:
            response = client.chat.completions.create(
                messages=[dict(message) for message in messages], **parameters)
    except (OpenAIError, OSError) as error:
        # qwencore's for range(3) has an unconditional break and no exception
        # handler: it does not add three transport retries around the SDK.
        raise ModelError(f"model request failed: {type(error).__name__}") from None
    choices = tuple(choice.message.content for choice in response.choices)
    if not choices:
        raise ModelError("model returned no choices")
    return Reply(text=choices[0], choices=choices,
                 completion_tokens=response.usage.completion_tokens,
                 seconds=round(time.monotonic()-started, 3), parameters=parameters)
