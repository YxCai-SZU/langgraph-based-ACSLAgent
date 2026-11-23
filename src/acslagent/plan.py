"""Turn a C file plus its SpecLoC records into an ordered task list.

Reference: LLM4Veri/mark.py (preprocess, GetSpecLoc, TransformDict,
MartkedSourceCode) and LLM4Veri/src/baselib.py (get_infill_lines).

The original runs veri-clang, reads SpecLoC.txt, then walks the records in the
order the tool emitted them. For each record it marks every loop of the
function first and the function contract last, so task IDs are numbered
bottom-up. A record looks like:

    function_name===file.c:function_line===loop_a,loop_b
    check_ia5===x509_utils.c:42===17,@9

A loop line prefixed with '@' is the outermost loop of that function (code 3),
an un-prefixed one is an inner loop (code 2), and a function contract is
always code 1.
"""

from __future__ import annotations

import re
from pathlib import Path

from acslagent.domain import FunctionLocation, ProgramTask, TaskKind

# mark.py rewrites these datasets into a temporary _m.c before analysing.
DATASET_MARKERS = ("code2inv", "SVCOMP", "multi-phase-loops", "fib_46_")

ASSERT_FORMS = (
    "static_assert(",
    "europa_assert(",
    "__VERIFIER_assert(",
    "__VERIFIER_assert (",
    "assert(",
    "assert (",
)
LINE_DROP_FORMS = (
    "// invariant ",
    "// invarian ",
    "assume(",
    "assume (",
    "europa_assume(",
    "europa_assume (",
    "__VERIFIER_assume(",
    "__VERIFIER_assume (",
    "europa_make_symbolic(",
    "europa_make_symbolic (",
    "europa_invariant(",
    "europa_invariant (",
)

CONTRACT_MARKER = "/* {id}. FUNC CONTRACT */\n"
LOOP_MARKER = "{indent}/* {id}. LOOP INVARIANT */\n"


def preprocess_source(text: str, source_path: str) -> str:
    """mark.py:preprocess, kept for the benchmark families it targets.

    Assertions become ACSL assertions, assumptions and symbolic-input helpers
    are dropped, and unrelated text is untouched. Substring matching is the
    original behaviour.
    """
    if not any(marker in source_path for marker in DATASET_MARKERS):
        return text
    out = []
    for line in text.splitlines(keepends=True):
        rewritten = line
        for form in ASSERT_FORMS:
            if form in rewritten:
                rewritten = rewritten.replace(form, "//@ assert ").replace(");", ";")
                break
        else:
            if "&& __VERIFIER_nondet_int()" in rewritten:
                rewritten = rewritten.replace("&& __VERIFIER_nondet_int()", "")
            elif "= __VERIFIER_nondet_int()" in rewritten:
                rewritten = ""
        if any(form in rewritten for form in LINE_DROP_FORMS):
            rewritten = ""
        out.append(rewritten)
    return "".join(out)


def parse_spec_locations(text: str) -> list[FunctionLocation]:
    """Read SpecLoC.txt.

    Records are kept in file order because the order decides the task order.
    A malformed record raises instead of being skipped: silently dropping a
    function would silently drop proof obligations.
    """
    records: list[FunctionLocation] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        parts = raw.split("===")
        if len(parts) != 3:
            raise ValueError(f"SpecLoC line {number} needs 3 '===' fields: {raw!r}")
        function_name, location, loops = parts
        if ":" not in location:
            raise ValueError(f"SpecLoC line {number} has no file:line in {location!r}")
        source_file, function_line = location.rsplit(":", 1)
        loop_lines = [] if loops.strip() == "0" else [
            item.strip() for item in loops.split(",") if item.strip()
        ]
        records.append(
            FunctionLocation(
                function_name=function_name.strip(),
                source_file=source_file.strip(),
                function_line=int(function_line),
                loop_lines=loop_lines,
            )
        )
    return records


def build_tasks(
    source_text: str, records: list[FunctionLocation]
) -> tuple[list[ProgramTask], list[str]]:
    """Insert task markers into the source and number the tasks.

    Returns the task list and the marked source lines. Loop markers keep the
    indentation of the line they precede; contract markers do not, which is
    the original behaviour.
    """
    marked = source_text.splitlines(keepends=True)
    tasks: list[ProgramTask] = []

    def add(record: FunctionLocation, line_number: int, kind: TaskKind) -> None:
        if not 1 <= line_number <= len(marked):
            raise ValueError(
                f"line {line_number} for {record['function_name']} is outside the file"
            )
        task_id = len(tasks) + 1
        tasks.append(
            ProgramTask(
                task_id=task_id,
                kind=int(kind),
                function_name=record["function_name"],
                source_line=line_number,
            )
        )
        if kind is TaskKind.CONTRACT:
            marker = CONTRACT_MARKER.format(id=task_id)
        else:
            indent = re.match(r"\s*", marked[line_number - 1]).group()
            marker = LOOP_MARKER.format(id=task_id, indent=indent)
        marked[line_number - 1] = marker + marked[line_number - 1]

    for record in records:
        for loop_line in record["loop_lines"]:
            if loop_line.startswith("@"):
                add(record, int(loop_line[1:]), TaskKind.OUTER_LOOP)
            else:
                add(record, int(loop_line), TaskKind.INNER_LOOP)
        add(record, record["function_line"], TaskKind.CONTRACT)
    return tasks, marked


def read_source(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def plan_from_zones(source_text: str, spec_locations_text: str) -> tuple[list[ProgramTask], list[str]]:
    """Plan without running veri-clang, when SpecLoC.txt is already available."""
    return build_tasks(source_text, parse_spec_locations(spec_locations_text))
