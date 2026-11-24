"""Up to ten bounded LLM calls; only timeouts trigger budget-free resends."""

from __future__ import annotations

import time
import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path
from dataclasses import asdict, dataclass, replace
from typing import Any

from acslagent.domain import PromptMessage
from acslagent.model_config import ModelConfig

MAX_REQUEST_ATTEMPTS = 10  # Initial request plus at most nine timeout-only resends.


class ModelError(RuntimeError):
    """The model could not be reached, or answered nothing usable."""


class ModelTimeoutError(ModelError):
    """No completion before the per-attempt deadline."""


class ModelParseError(ModelError):
    """The model response protocol is unusable; skip this sample."""


@dataclass(frozen=True)
class Reply:
    text: str
    completion_tokens: int
    seconds: float
    parameters: dict[str, Any]
    choices: tuple[str, ...] | None = None
    prompt_tokens: int = 0
    finish_reason: str | None = None
    request_attempts: int = 1


def request_parameters(config: ModelConfig, temperature: float) -> dict[str, Any]:
    """Shared parameters plus explicit per-model compatibility overrides."""
    parameters = {
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
    for name in config.params.get('omit_parameters', []):
        parameters.pop(name, None)
    parameters.update(config.params.get('request_overrides', {}))
    return parameters


def _positive_seconds(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


def hard_timeout_seconds(config):
    # This parent-process deadline includes DNS, imports, connect and reading.
    # The HTTP SDK timeout alone cannot bound synchronous getaddrinfo().
    return _positive_seconds(config.params.get('hard_timeout', 60), 'hard_timeout')


def _stop_worker(process):
    if process.poll() is None:
        try:
            if os.name == 'posix':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def _run_worker(config, messages, temperature, timeout, attempt, on_event):
    # Secrets only cross an anonymous pipe, never argv, disk or progress logs.
    payload = json.dumps(dict(config=asdict(config), messages=messages, temperature=temperature)).encode()
    env = dict(os.environ)
    source_root = str(Path(__file__).resolve().parents[1])
    env['PYTHONPATH'] = source_root + (os.pathsep+env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    interval = _positive_seconds(config.params.get('progress_interval', 30), 'progress_interval')
    started = time.monotonic()
    with subprocess.Popen([sys.executable, '-m', 'acslagent.llm_worker'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=env, start_new_session=os.name == 'posix',
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0) as process:
        pending_input = payload
        try:
            while True:
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    raise ModelTimeoutError(f'LLM request exceeded hard deadline ({timeout:g}s), including DNS')
                try:
                    stdout, stderr = process.communicate(input=pending_input, timeout=min(interval, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None  # communicate resumes its buffered input/output.
                    elapsed = time.monotonic()-started
                    if elapsed >= timeout:
                        raise ModelTimeoutError(f'LLM request exceeded hard deadline ({timeout:g}s), including DNS') from None
                    if on_event:
                        on_event('waiting', attempt=attempt, elapsed_seconds=round(elapsed, 3), timeout_seconds=timeout)
            if process.returncode != 0:
                # Do not echo arbitrary stderr; it may contain headers/secrets.
                raise ModelError(f'LLM worker exited with code {process.returncode}')
            try:
                message = json.loads(stdout)
                if not isinstance(message, dict):
                    raise ValueError('worker protocol is not an object')
            except (ValueError, UnicodeDecodeError):
                raise ModelParseError('LLM worker returned an unreadable response envelope') from None
            kind = message.get('kind')
            if kind == 'timeout':
                raise ModelTimeoutError(message['error'])
            if kind == 'parse_error':
                raise ModelParseError(message['error'])
            if kind == 'error':
                raise ModelError(message['error'])
            if kind != 'reply' or not isinstance(message.get('reply'), dict):
                raise ModelParseError('LLM worker response is missing the reply')
            return Reply(**message['reply'])
        finally:
            # Also runs on Ctrl+C, malformed worker output or callback failure.
            # Kill a DNS-stuck child before another request is dispatched.
            _stop_worker(process)


def ask(config: ModelConfig, messages: list[PromptMessage], temperature: float, *,
        attempts: int = 1, on_event=None) -> Reply:
    """One logical request: at most ten calls, retrying ONLY timeouts for free.

    Each attempt runs in a disposable process so DNS cannot evade the deadline.
    HTTP/provider errors and reply parsing errors are never transport-retried.
    """
    if not config.api_key:
        raise ModelError(f'environment variable {config.api_key_env or "<unset>"} has no value; export the API key before running')
    if attempts != 1:
        raise ValueError('SDK retries are disabled; the controller permits at most ten timeout-only calls')
    timeout = hard_timeout_seconds(config)
    started = time.monotonic()
    for attempt in range(1, MAX_REQUEST_ATTEMPTS+1):
        if on_event:
            on_event('attempt_start', attempt=attempt, timeout_seconds=timeout)
        try:
            reply = _run_worker(config, messages, temperature, timeout, attempt, on_event)
        except ModelTimeoutError as error:
            if on_event:
                on_event('timeout', attempt=attempt, timeout_seconds=timeout, error=str(error))
            if attempt == MAX_REQUEST_ATTEMPTS:
                raise ModelTimeoutError(f'all {MAX_REQUEST_ATTEMPTS} LLM requests timed out; sample skipped; task budgets unchanged') from None
            if on_event:
                on_event('timeout_retry', attempt=attempt+1, budget_cost=0)
            continue
        return replace(reply, seconds=round(time.monotonic()-started, 3), request_attempts=attempt)


def _ask_once(config, messages, temperature):
    """Worker implementation: exactly one SDK call, no retry and no recursion."""
    from openai import OpenAI, OpenAIError, APITimeoutError, APIResponseValidationError
    parameters = request_parameters(config, temperature)
    started = time.monotonic()
    try:
        with OpenAI(api_key=config.api_key, base_url=config.base_url, max_retries=0) as client:
            response = client.chat.completions.create(messages=[dict(m) for m in messages], **parameters)
    except APITimeoutError:
        raise ModelTimeoutError('HTTP SDK request timed out') from None
    except json.JSONDecodeError:
        raise ModelParseError('model API returned invalid response JSON') from None
    except APIResponseValidationError:
        raise ModelParseError('model API response does not match the completion protocol') from None
    except (OpenAIError, OSError) as error:
        code, status = getattr(error, 'code', None), getattr(error, 'status_code', None)
        body = getattr(error, 'body', {})
        detail = body.get('message', '') if isinstance(body, dict) else ''
        if isinstance(body, dict) and isinstance(body.get('error'), dict):
            detail = body['error'].get('message', detail)
        message = f'model request failed: {type(error).__name__}; HTTP={status}; code={code}; {detail}'
        raise ModelError(message.replace(config.api_key, '[REDACTED]')[:500]) from None
    try:
        choices = tuple(choice.message.content for choice in response.choices)
        if not choices:
            raise ValueError('no choices')
        if choices[0] is not None and not isinstance(choices[0], str):
            raise ValueError('content is not text')
        usage = response.usage
        return Reply(text=choices[0] or '', choices=choices,
                     completion_tokens=usage.completion_tokens if usage else 0,
                     seconds=round(time.monotonic()-started, 3), parameters=parameters,
                     prompt_tokens=usage.prompt_tokens if usage else 0,
                     finish_reason=response.choices[0].finish_reason)
    except (AttributeError, TypeError, ValueError):
        raise ModelParseError('model API returned no usable completion envelope') from None
