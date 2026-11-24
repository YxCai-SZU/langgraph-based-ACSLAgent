"""Disposable SDK process; parent enforces a total deadline including DNS."""
from __future__ import annotations
import json
import sys
from dataclasses import asdict
from acslagent.llm import _ask_once, ModelError, ModelTimeoutError, ModelParseError
from acslagent.model_config import ModelConfig


def main():
    try:
        payload = json.load(sys.stdin)
        config = ModelConfig(**payload['config'])
        reply = _ask_once(config, payload['messages'], payload['temperature'])
        result = dict(kind='reply', reply=asdict(reply))
    except ModelTimeoutError as error:
        result = dict(kind='timeout', error=str(error))
    except ModelParseError as error:
        result = dict(kind='parse_error', error=str(error))
    except ModelError as error:
        result = dict(kind='error', error=str(error))
    except Exception as error:
        # Never serialize request payloads, credential values or arbitrary tracebacks.
        result = dict(kind='error', error=f'LLM worker failed: {type(error).__name__}')
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()


if __name__ == '__main__':
    main()
