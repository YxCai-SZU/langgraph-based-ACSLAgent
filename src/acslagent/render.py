"""Render the C source that is sent to the model and to Frama-C.

Reference: baselib.py:get_infill_lines, write_into_infilled_c_file and
write_into_final_c_file.

Two renderings exist in the original and both are kept here.

`render_infill_source` produces the file the model sees. The active task gets
a hole; other tasks show their already accepted clauses.

`render_final_source` mirrors the original export helper, spelling out saved
clauses without task markers. The graph's final verification instead restores
assertions in the repaired merged file, preserving deletions made during repair.
"""

from __future__ import annotations

import re

from acslagent.domain import ProgramTask, TaskKind

MARKER = "/* @ >>> INFILL <<< */"
HOLE = ">>> INFILL <<<"

# baselib.py matches the marker with an unescaped dot and no end anchor.
# Keeping that regex keeps duplicate-marker handling identical.
_MARKER_RE = re.compile(r"\s*/\* (\d+). (?:FUNC CONTRACT|LOOP INVARIANT) \*/*")


def _split_marker(line: str) -> tuple[int, str] | None:
    match = _MARKER_RE.match(line)
    if not match:
        return None
    return int(match.group(1)), line[line.find("*/") + 3 :]


def render_infill_source(
    marked_lines: list[str],
    specs: dict[str, list[str]],
    current_task_id: int,
) -> str:
    """Place the hole for the active task and restore accepted clauses."""
    out: list[str] = []
    for line in marked_lines:
        split = _split_marker(line)
        if split is None:
            out.append(line)
            continue
        task_id, source_line = split
        clauses = specs.get(str(task_id)) or []
        is_current = task_id == current_task_id
        if not clauses and not is_current:
            out.append(source_line)
            continue
        # Original behaviour, including its inconsistency: for the active task
        # the indentation is read from the marker line, for a dormant task from
        # the source line after the marker was cut off. A contract marker has no
        # indentation of its own, so a dormant contract is emitted flush left.
        indent = re.match(r"\s*", line if is_current else source_line).group()
        if not clauses:
            out.append(f"{indent}{MARKER}\n" + source_line)
            continue
        block = ([HOLE] if is_current else []) + clauses
        annotation = indent + "/*@\n"
        annotation += "".join(f"{indent}{clause}\n" for clause in block)
        annotation += indent + "*/\n"
        out.append(annotation + source_line)
    return "".join(out)


def fill_hole(source: str, clauses: list[str]) -> str:
    """Replace the hole in `source` with `clauses`.

    The prepared source has exactly one hole, the one belonging to the active
    task. The original fills it by splitting on the surrounding marker text,
    which has the same effect. Every occurrence is replaced here so a source
    that happens to repeat the marker can never be sent to Frama-C unchanged.
    """
    prefix = infill_prefix(source)
    merged = "\n" + "".join(prefix + c.strip() + "\n" for c in clauses if c.strip()) + prefix
    return (source.replace(MARKER, "/*@>>> INFILL <<<*/")
            .replace("\n" + prefix + HOLE + "\n" + prefix, merged)
            .replace(HOLE, merged))


def infill_prefix(source: str) -> str:
    for line in source.split("\n"):
        if MARKER in line:
            return line.replace(MARKER, "")
        if HOLE in line:
            return line.replace(HOLE, "")
    return ""


def mask_assertions(source: str) -> str:
    """FB4C masks assertions before both the model request and subtask checks."""
    return source.replace("//@ assert", "// @ assert")


def fill_reply(source: str, reply: str) -> str:
    """Unsorted individual reply file, used for behavior contracts (llmveri:296)."""
    prefix = infill_prefix(source)
    reply_str = "\n" + reply.strip()
    if not reply_str.endswith("\n"):
        reply_str += "\n"
    reply_str = reply_str.replace("\n", "\n" + prefix)
    if HOLE + "\n" in source:
        return source.replace(HOLE + "\n" + prefix, reply_str.replace("\n" + prefix, "", 1))
    return source.replace(MARKER, "/*@>>> INFILL <<<*/").replace(HOLE, reply_str)


def fill_assign_probe(source: str, clauses: list[str], last_single: str) -> str:
    """Exact singleton/pair replacement order, including upstream infill quirk."""
    prefix = infill_prefix(source)
    first = clauses[0] if len(clauses) == 1 else last_single
    return (source.replace(MARKER, "/*@>>> INFILL <<<*/")
            .replace("\n" + prefix + HOLE + "\n" + prefix,
                     "\n" + prefix + first + "\n" + prefix)
            .replace(HOLE, "\n" + "".join(prefix + c + "\n" for c in clauses) + prefix))


def render_final_source(
    marked_lines: list[str],
    specs: dict[str, list[str]],
) -> str:
    """The finished program: no hole, every accepted clause written out.

    Mirrors `write_into_final_c_file` (assertions masked, hole removed) followed
    by the `file_alter(..., "// @ assert", "//@ assert")` call that llmveri.py
    makes just before the last Frama-C run. Order matters: mask first, then
    restore, so the assertion reaches Frama-C unmasked.
    """
    source = render_infill_source(marked_lines, specs, current_task_id=0)
    # `write_into_infilled_c_file` masks assertions on the way to disk.
    source = source.replace("//@ assert", "// @ assert")
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if MARKER in line or HOLE in line:
            lines[index] = line.replace(MARKER + "\n", "").replace(MARKER, "")
            break
    # Then the original restores them before the final verification.
    return "".join(lines).replace("// @ assert", "//@ assert")


def expected_mode(task: ProgramTask, existing: list[str]) -> str:
    """Pick the generation mode exactly as AutoGenInfillFile does."""
    if TaskKind(task["kind"]) is TaskKind.CONTRACT:
        return "contract_infill" if existing else "contract_gen"
    return "loopinv_infill" if existing else "loopinv_gen"


def is_loop_task(task: ProgramTask) -> bool:
    return TaskKind(task["kind"]) in (TaskKind.INNER_LOOP, TaskKind.OUTER_LOOP)
