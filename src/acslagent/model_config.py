"""Read config/models_config.yaml.

The file format is the original one: `ConfigTemplates` holds the transport
settings, `ModelMap` maps a model name to a template plus per-model overrides,
and the API key comes from the environment variable named in `api_key_env`.

Reference: LLM4Veri/src/config_manager.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ModelConfig:
    name: str
    platform: str
    model_name: str
    base_url: str | None
    api_key: str | None = field(repr=False)
    api_key_env: str | None
    params: dict

    @classmethod
    def load(cls, path: Path, name: str) -> "ModelConfig":
        try:
            document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ValueError(f"model configuration not found: {path}") from None
        except yaml.YAMLError as error:
            raise ValueError(f"model configuration is not valid YAML: {path}") from error
        if not isinstance(document, dict):
            raise ValueError(f"{path} must define ConfigTemplates and ModelMap")

        mapping = (document.get("ModelMap") or {}).get(name)
        if not mapping:
            known = ", ".join(sorted((document.get("ModelMap") or {}))) or "none"
            raise ValueError(f"model {name!r} is not in ModelMap; known models: {known}")
        template = (document.get("ConfigTemplates") or {}).get(mapping.get("template_id"))
        if not template:
            raise ValueError(f"model {name!r} has no matching ConfigTemplates entry")

        # Template first, ModelMap overrides second, exactly as the original
        # merge order. `model_name` is forced to the requested name.
        merged = {**template, "model_name": name}
        merged.update({k: v for k, v in mapping.items() if k != "template_id"})
        key_env = merged.get("api_key_env")
        return cls(
            name=name,
            platform=merged.get("platform", "Unknown"),
            model_name=merged.get("model_name", name),
            base_url=merged.get("base_url"),
            api_key=os.getenv(key_env) if key_env else None,
            api_key_env=key_env,
            params={
                k: v
                for k, v in merged.items()
                if k not in {"platform", "model_name", "base_url", "api_key_env", "api_key"}
            },
        )
