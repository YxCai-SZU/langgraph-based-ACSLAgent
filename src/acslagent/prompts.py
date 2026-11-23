"""Prompt assembly.

The text itself lives in prompt_templates.json, copied verbatim from the
original LLM4Veri/src/prompt/*.py files. Nothing here rewrites them: an edited
prompt would silently change what the experiment measures.

Reference: src/prompt/prompt.py:get_incontext_learning_contents.
"""

from __future__ import annotations

import json
from functools import cache
from importlib.resources import files

from acslagent.domain import Mode, PromptMessage, ProgramTask, TaskKind


@cache
def load_templates() -> dict:
    resource = files("acslagent").joinpath("prompt_templates.json")
    return json.loads(resource.read_text(encoding="utf-8"))


def mode_for(task: ProgramTask, existing: list[str]) -> Mode:
    """Choose the generation mode for a task, as AutoGenInfillFile does.

    A task with nothing accepted yet needs a fresh proposal; a task that
    already has accepted clauses needs more clauses that fit them.
    """
    if TaskKind(task["kind"]) is TaskKind.CONTRACT:
        return "contract_infill" if existing else "contract_gen"
    return "loopinv_infill" if existing else "loopinv_gen"


def build_messages(mode: Mode, source_text: str) -> list[PromptMessage]:
    """Few-shot context for `mode`, then the C source as the final user turn.

    The original appends the whole prepared C file as the question, with the
    hole marked by `/* @ >>> INFILL <<< */`. The model answers with only the
    clauses that belong in that hole.
    """
    templates = load_templates()
    messages: list[PromptMessage] = [
        PromptMessage(role=entry["role"], content=templates["texts"][entry["text"]])
        for entry in templates["profiles"][mode]
    ]
    messages.append(PromptMessage(role="user", content=source_text))
    return messages


def system_prompt(mode: Mode) -> str:
    """The system message of a mode, for logging without dumping the examples."""
    templates = load_templates()
    first = templates["profiles"][mode][0]
    return templates["texts"][first["text"]]
