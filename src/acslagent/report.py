"""Markdown report for one run.

The report is built only from `trace.jsonl`, `summary.json` and the C files the
run wrote. Nothing is recomputed, so the report and the trace can never
disagree.

It answers the question an experiment log has to answer: for each subtask, how
many proposals were made, what did Frama-C say about each one, which clauses
were thrown away and why, and did the final whole-file check succeed without
any goal being given up on.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from acslagent.trace import read_trace

REPAIR_FLOW = """\
## FB4C execution policy

This run uses the task-0 workflow from the frozen FB4C source. `dispatch`
selects a task in SpecLoC order. `generate` masks assertions and sends the
original few-shot messages. `normalize` cleans and pools the replies, including
FB4C mutation rules. Inner loops in iteration 1 check individual assigns and,
when applicable, pairs with a 3-second WP timeout. Later iterations drop new
loop-assigns candidates. `embed` writes the original ACSL comment format.

`verify` uses an 8-second WP timeout. An inner-loop Fail retains candidates;
outer loops and contracts run the original deletion step. Invalid diagnostics
follow the original branch and early-break order. Outer-loop repairs can also
remove saved inner-loop clauses. Behavior contracts verify individual raw
replies and select a passing candidate. Exhausting the repair budget ends that
attempt and returns to dispatch for the same task. The remaining outer retry
budget determines whether another model call runs; -1 restores unbounded repair.

`commit` accumulates loops and replaces contracts. It increments CurTaskID
BEFORE testing for the final check. The final check unmasks assertions in the
repaired merged file. On failure, the next round retains loops and clears
contracts. The default main.py budget is five task invocations, not five rounds.

Pass follows FB4C: proved + requires-timeouts == total. `strict_passed` is a
separate diagnostic and does not change scheduling. Stored clauses can be
provisional (especially inner-loop Fail); storage alone is not proof of each
clause. Consult the actual final verdict and the verification inputs in trace.

For historical logs produced before the parity fix, use their recorded
fields as evidence; this policy description applies only to corrected runs.
"""


def write_report(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    events = read_trace(run_dir)
    if not events:
        raise ValueError(f"no trace.jsonl in {run_dir}")
    summary_path = run_dir / "summary.json"
    summary = (
        json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    )
    lines: list[str] = []
    lines += _headline(run_dir, events, summary)
    lines += _configuration(events)
    lines += _call_graph(events)
    lines += _tasks(events)
    lines += [REPAIR_FLOW]
    lines += _subtask_cycles(events)
    lines += _final_check(events, summary)
    lines += _clause_survival(events)
    lines += _log_index(events)

    path = run_dir / "report.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _headline(run_dir: Path, events: list[dict], summary: dict) -> list[str]:
    """Everything here comes from the trace, so a run without summary.json still reports."""
    start = next((event for event in events if event["event"] == "start"), None)
    finish = next((event for event in reversed(events) if event["event"] == "finish"), None)
    failure = next((e for e in reversed(events) if e['node'] == 'run' and e['event'] == 'failed'), None)
    result = summary.get('result', (finish or {}).get("detail", {}).get("result", "error" if failure else "incomplete"))
    detail = (start or {}).get("detail", {})
    finals = [event for event in events if event["node"] == "final_verify" and event['event'] == 'done']
    last = finals[-1]["detail"] if finals else {}
    rounds = (finish or {}).get("detail", {}).get("rounds")
    tokens = (finish or {}).get("detail", {}).get("tokens", 0)
    tasks = next(
        (event["detail"].get("task_count") for event in events if event["node"] == "plan"),
        None,
    )
    proved = last.get("proved")
    total = last.get("total")
    lines = [
        f"# ACSLAgent run: {run_dir.name}",
        "",
        f"- source: `{detail.get('source_name', detail.get('source', '?'))}`",
        f"- result: **{result}**",
    ]
    if proved is None:
        lines.append("- final check: not reached")
    else:
        lines.append(
            f"- final check: {proved} of {total} goals proved"
            f" (strict pass: {last.get('strict_passed', last.get('passed'))},"
            f" FB4C pass: {last.get('upstream_passed', last.get('passed'))})"
        )
    lines += [
        f"- rounds run: {rounds if rounds is not None else '?'}",
        f"- subtasks: {tasks if tasks is not None else '?'}",
        f"- completion tokens: {tokens}",
        f"- trace: `trace.jsonl` ({len(events)} events)",
        "",
        "Every statement below is read out of `trace.jsonl`. A run that stopped early",
        "says where, because the last recorded node is the node that failed.",
        "",
    ]
    stop = next((e for e in reversed(events) if e['event'] == 'budget_exhausted'
                 and e['detail'].get('route') != 'dispatch'), None)
    if stop:
        d = stop['detail']
        lines += [f"- stopped: **{d.get('reason', 'task_budget_exhausted')}**", ""]
        if d.get('reason') == 'repair_budget_exhausted':
            lines += [f"- task {stop['task_id']}, step {stop.get('step')}: "
                      f"{d['repairs_used']}/{d['max_repairs']} repairs; current candidate was not committed.", ""]
    if failure:
        lines += [f"- error: {failure['detail'].get('error')}", ""]
    return lines


def _configuration(events: list[dict]) -> list[str]:
    start = next((event for event in events if event["event"] == "start"), None)
    if not start:
        return []
    detail = start["detail"]
    settings = detail.get("settings", {})
    return [
        "## Configuration",
        "",
        "| setting | value |",
        "| --- | --- |",
        f"| model | `{detail.get('model')}` |",
        *[f"| {key} | {value} |" for key, value in settings.items()],
        "",
    ]


def _call_graph(events: list[dict]) -> list[str]:
    analyze = next((event for event in events if event["node"] == "analyze" and event["event"] == "done"), None)
    if not analyze:
        return ["## Call graph", "", "The analysis step did not finish, so there is no call graph.", ""]
    detail = analyze["detail"]
    lines = [
        "## Call graph (veri-clang)",
        "",
        f"`veri-clang` returned {len(detail.get('functions', []))} function records.",
        "",
        "```",
        (detail.get("spec_locations") or '\n'.join(
            f"{r['function_name']}: line {r['function_line']}, loops {r['loop_lines']}"
            for r in detail.get('locations', []))).rstrip(),
        "```",
        "",
    ]
    return lines


def _tasks(events: list[dict]) -> list[str]:
    plan = next((event for event in events if event["node"] == "plan"), None)
    if not plan:
        return []
    tasks = plan["detail"].get("tasks", [])
    kinds = {1: "function contract", 2: "inner loop", 3: "outer loop"}
    lines = [
        "## Subtasks",
        "",
        "Order is the order `SpecLoC.txt` produced: loops of a function first, its",
        "contract last, so callees are specified before callers.",
        "",
        "| # | kind | function | line |",
        "| --- | --- | --- | --- |",
    ]
    for task in tasks:
        lines.append(
            f"| {task['task_id']} | {kinds.get(task['kind'], task['kind'])}"
            f" | `{task['function_name']}` | {task['source_line']} |"
        )
    lines.append("")
    return lines


def _subtask_cycles(events: list[dict]) -> list[str]:
    cycles: dict[tuple, dict] = defaultdict(lambda: {"verify": [], "repair": [], "generate": 0, "decide": []})
    for event in events:
        if event["task_id"] is None:
            continue
        key = (event["task_id"], event["round"], event["iteration"])
        bucket = cycles[key]
        detail = event["detail"]
        if event["node"] == "generate" and event["event"] == "reply":
            bucket["generate"] += 1
        elif event["node"] == "verify" and event['event'] == 'done':
            bucket["verify"].append((event, detail))
        elif event["node"] == "repair":
            bucket["repair"].append((event, detail))
        elif event["node"] == "decide":
            bucket["decide"].append((event, detail))

    if not cycles:
        return []
    lines = ["## Subtask timeline", ""]
    for key in sorted(cycles):
        task_id, round_number, iteration = key
        bucket = cycles[key]
        lines += [
            f"### Subtask {task_id}, round {round_number}, iteration {iteration}",
            "",
            f"- proposals from the model: {bucket['generate']}",
            f"- Frama-C calls: {len(bucket['verify'])}",
            f"- clauses deleted: {sum(len(detail.get('removed_clauses', [])) for _, detail in bucket['repair'])}",
            "",
        ]
        if bucket["verify"]:
            lines += [
                "| seq | choice | repair | verdict | proved/total | timeouts | strict pass | FB4C pass |",
                "| --- | --- | --- | --- | --- | --- | --- | --- |",
            ]
            for index, (event, detail) in enumerate(bucket["verify"], start=1):
                lines.append(
                    f"| {event['seq']} | {event.get('choice', detail.get('attempt', '-'))} | {detail.get('repair', 0)}"
                    f" | {detail.get('verdict', '?')} | {detail.get('proved', 0)}/{detail.get('total', 0)}"
                    f" | {len(detail.get('timed_out_requires', []))}"
                    f" | {detail.get('strict_passed', detail.get('passed'))} | {detail.get('upstream_passed', detail.get('passed'))} |"
                )
            lines.append("")
        for event, detail in bucket["repair"]:
            removed = detail.get("removed_clauses", [])
            if removed:
                lines.append(
                    f"- repair {detail.get('repair', '?')}: `{detail.get('reason')}` removed "
                    + ", ".join(f"`{clause}`" for clause in removed)
                )
        if bucket["repair"]:
            lines.append("")
        for event, detail in bucket['decide']:
            if detail.get('reason') == 'repair_budget_exhausted' and detail.get('route') == 'dispatch':
                lines += [f"- outer attempt {event.get('step')}: {detail['repairs_used']}/{detail['max_repairs']} repairs; "
                          f"failed candidate not committed; returned to dispatch for task {detail['next_task_id']} "
                          f"(outer attempts used {detail['retries_used']}/{detail['max_retries']}).", ""]
    return lines


def _final_check(events: list[dict], summary: dict) -> list[str]:
    finals = [event for event in events if event["node"] == "final_verify" and event['event'] == 'done']
    if not finals:
        return [
            "## Final whole-file check",
            "",
            "The run did not reach the final check, so no whole-file verdict exists.",
            "",
        ]
    lines = [
        "## Final whole-file check",
        "",
        "FB4C checks the repaired merged file after advancing CurTaskID to the last task.",
        "The last task is therefore usually not queried separately.",
        "",
        "| round | verdict | proved/total | timeouts | strict pass | original rule |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for event in finals:
        detail = event["detail"]
        lines.append(
            f"| {event['round']} | {detail.get('verdict')} | {detail.get('proved')}/{detail.get('total')}"
            f" | {len(detail.get('timed_out_requires', []))}"
            f" | {detail.get('strict_passed', detail.get('passed'))} | {detail.get('upstream_passed', detail.get('passed'))} |"
        )
    lines.append("")
    final = summary.get("final_verification") or {}
    command = " ".join(final.get("command") or [])
    if command:
        lines += ["Command of the last attempt:", "", "```", command, "```", ""]
    return lines


def _clause_survival(events: list[dict]) -> list[str]:
    final = next((event for event in reversed(events) if event["node"] == "final_verify" and event['event'] == 'done'), None)
    if not final:
        return []
    clauses = final["detail"].get("clauses") or {}
    if not clauses:
        return [
            "## Accepted clauses",
            "",
            "No saved clause is reported in the final-check event. Consult the final C and repair trace. Every proposal may have been rejected",
            "by Frama-C or deleted during repair.",
            "",
        ]
    lines = ["## Accepted clauses", ""]
    for task_id in sorted(clauses, key=int):
        lines.append(f"**Subtask {task_id}**")
        lines.append("")
        lines += [f"- `{clause}`" for clause in clauses[task_id]]
        lines.append("")
    return lines


def _log_index(events: list[dict]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for event in events:
        counts[f"{event['node']}:{event['event']}"] += 1
    lines = [
        "## Trace index",
        "",
        "| node:event | count |",
        "| --- | --- |",
        *[f"| `{name}` | {count} |" for name, count in sorted(counts.items())],
        "",
        "To follow one subtask:",
        "",
        "```bash",
        "acslagent trace <run_dir>",
        "acslagent trace <run_dir> --json  # expands source/stdout/stderr references",
        "jq 'select(.task_id == 2)' <run_dir>/trace.jsonl",
        "```",
    ]
    return lines
