"""Command line entry point.

    acslagent run  <file.c> -m <model>          full pipeline
    acslagent run  <directory> -m <model>       independent C programs, serially
    acslagent run  -i <directory> -m <model>    same directory batch
    acslagent plan <file.c>                     call graph and subtasks only
    acslagent trace <run_dir>                   read a finished run back
    acslagent report <run_dir>                  Markdown report of a run

`run` is what reproduces the paper: analyse, then generate, verify and repair
every subtask, then verify the whole file.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from acslagent.graph import MAX_REPAIRS, MAX_RETRIES, build_graph
from acslagent.llm import ModelError, MAX_REQUEST_ATTEMPTS
from acslagent.framac import VERIFICATION_SCOPE
from acslagent.trace import Tracer, read_trace
from acslagent.runtime import project_root
from acslagent import __version__

API_RETRIES_DEFAULT = 1


def new_run_dir(root: Path, source: Path, run_id: str | None) -> Path:
    """One directory per run, named after the input file plus a sequence."""
    root.mkdir(parents=True, exist_ok=True)
    if run_id:
        directory = root / run_id
        directory.mkdir(parents=True, exist_ok=False)
        return directory
    name = source.resolve().name if source.is_dir() else source.stem
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name) or "input"
    number = 1
    while True:
        directory = root / f"{stem}{number:03d}"
        try:
            directory.mkdir()
            return directory
        except FileExistsError:
            number += 1


def validate_options(args: argparse.Namespace) -> None:
    if args.rounds is not None:
        raise ValueError("--rounds is unsupported; use independent per-task --retries and --repairs")
    if args.retries < 0 or args.repairs < 0:
        raise ValueError("--retries and --repairs must be nonnegative")
    if args.candidates < 1 or args.fmparallel < 1:
        raise ValueError("--candidates and --fmparallel must be positive")
    if not 0 <= args.score_drop_threshold <= 1:
        raise ValueError("--score-drop-threshold must be between 0 and 1")
    if args.enable_mutation:
        raise ValueError("--enable-mutation belongs to the old workflow and is unsupported in Improve")
    if args.api_retries != 1:
        raise ValueError("--api-retries must be 1 (legacy option); up to ten total LLM requests on timeout, SDK retries disabled")
    if args.recursion_limit < 1:
        raise ValueError("--recursion-limit must be positive")


def initial_state(args: argparse.Namespace, run_dir: Path) -> dict:
    validate_options(args)
    source = Path(args.source).resolve()
    return {
        # FB4C's preprocessing and benchmark hooks inspect the caller's path
        # string. Resolving it first can spuriously match an ancestor's name.
        "source_path": str(args.source),
        "source_name": source.name,
        "source_text": source.read_text(encoding="utf-8"),
        "run_dir": str(run_dir),
        "model": args.model or "",
        "model_config": str(Path(args.model_config).resolve()),
        "analyzer_config": str(Path(args.analyzer_config).resolve()),
        "frama_c": args.frama_c,
        "candidate_count": args.candidates,
        "fmparallel": args.fmparallel,
        "wp_cache": args.wp_cache,
        "progress": not args.quiet_progress,
        "max_retries": args.retries,
        "max_repairs": args.repairs,
        "score_drop_threshold": args.score_drop_threshold,
        "max_rounds": args.rounds,
        "api_retries": args.api_retries,
        "enable_mutation": args.enable_mutation,
        "round_number": 1,
        "iteration_number": 1,
        "current_task_id": 1,
        "repair_count": 0,
        "token_usage": 0,
        "stage": "analyze",
        "step_number": 0,
        "status": 0,
    }


def command_run(args: argparse.Namespace) -> int:
    validate_options(args)
    source = Path(args.source)
    if source.is_dir():
        return _run_directory(args)
    if not source.is_file():
        print(f"no such file: {source}", file=sys.stderr)
        return 2
    run_dir = new_run_dir(Path(args.output), source, args.run_id)
    return _run_file(args, run_dir)


def _run_file(args: argparse.Namespace, run_dir: Path) -> int:
    """One complete independent FB4C task-0 run, also used by directory batches."""
    source = Path(args.source)
    log = Tracer(run_dir)
    log.header(
        trace_version=4,
        source_name=source.name,
        model=args.model or None,
        settings={
            "verification_scope": VERIFICATION_SCOPE,
            "wp_cache": args.wp_cache,
            "requested_candidate_count": args.candidates,
            "fmparallel": args.fmparallel,
            "early_select_perfect_candidate": True,
            "max_retries": args.retries,
            "max_repairs": args.repairs,
            "budget_scope": "per_subtask_lifetime",
            "score_drop_threshold": args.score_drop_threshold,
            "max_rounds": args.rounds,
            "api_retries": args.api_retries,
            "model_timeout_policy": "timeout-only free resends; hard deadline includes DNS; skip on exhaustion",
            "model_request_limit": MAX_REQUEST_ATTEMPTS,
            "enable_mutation": args.enable_mutation,
            "stop_after": args.stop_after,
        },
    )
    try:
        state = initial_state(args, run_dir)
        graph = build_graph()
        if args.stop_after == "plan":
            final = _run_until_planned(graph, state, run_dir)
            log.footer(result="planned", stage="planned", tasks=len(final['tasks']))
            print(f"planned: {run_dir / 'plan.json'}")
            print(f"trace:   {run_dir / 'trace.jsonl'}")
            return 0
        final = graph.invoke(state, {"recursion_limit": args.recursion_limit})
    except (Exception, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        result = 'interrupted' if interrupted else 'error'
        skipped = isinstance(error, ModelError)
        log.emit(
            "run",
            "interrupted" if interrupted else "failed",
            error=f"{type(error).__name__}: {error}",
            hint="read trace.jsonl to see the last node that ran",
        )
        # Preserve received usage even when a later tool fails.
        events = read_trace(run_dir)
        tokens = sum(e['detail'].get('completion_tokens', 0) or 0
                     for e in events
                     if e['node'] in ('generate', 'fix') and e['event'] == 'reply')
        prompt_tokens = sum(e['detail'].get('prompt_tokens', 0) for e in events if e['event']=='reply')
        last = json.loads((run_dir/'checkpoint.json').read_text(encoding='utf-8')) if (run_dir/'checkpoint.json').exists() else {}
        (run_dir / 'summary.json').write_text(json.dumps({
            'result': result, 'stage': result, 'tokens': tokens,
            'skipped': skipped, 'error_type': type(error).__name__,
            'stop_reason': last.get('stop_reason') or type(error).__name__,
            'task_ledger': last.get('ledger', {}),
            'model_calls': sum(e['node'] in ('generate','fix') and e['event']=='reply' for e in events),
            'prompt_tokens': prompt_tokens, 'completion_tokens': tokens, 'total_tokens': tokens+prompt_tokens,
            'timing': timing_summary(run_dir),
            'error': f'{type(error).__name__}: {error}',
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        if interrupted:
            raise
        print(f"{'sample skipped (LLM error)' if skipped else 'run failed'}: {type(error).__name__}: {error}", file=sys.stderr)
        print(f"trace: {run_dir / 'trace.jsonl'}", file=sys.stderr)
        return 1

    outcome = final.get("final_outcome")
    passed = bool(final.get("status") == 1 and outcome and outcome.passed)
    log.footer(
        result="pass" if passed else "fail",
        stage=final.get("stage"),
        stop_reason=final.get("stop_reason"),
        rounds=final.get("round_number", 1),
        tokens=final.get("token_usage", 0),
        specs={key: value for key, value in (final.get("specs") or {}).items() if value},
    )
    _write_summary(run_dir, final, passed)
    timing = timing_summary(run_dir)
    print(f"time: LLM={timing['model_seconds']:.2f}s, WP wall={timing['wp_wall_seconds']:.2f}s "
          f"(process sum={timing['wp_seconds']:.2f}s), "
          f"model calls={timing['model_calls']}, WP calls={timing['wp_calls']}")
    print("WP phases: " + ", ".join(f"{name}={value['seconds']:.2f}s/{value['calls']} calls"
                                     for name, value in timing['wp_by_phase'].items()))
    print(f"result: {'pass' if passed else 'fail'}")
    if final.get('stop_reason'):
        print(f"stop reason: {final['stop_reason']}")
    for key, task in final.get('ledger', {}).items():
        print(f"task {key}: local score={task['current_score']:.2%}, best={task['best_score']:.2%}, "
              f"retries used/left={task['retries_used']}/{task['retries_remaining']}, "
              f"repairs used/left={task['repairs_used']}/{task['repairs_remaining']}")
    if (run_dir / "final.c").exists():
        print(f"last whole-file check C: {run_dir / 'final.c'}")
    else:
        print("final assertion check was not reached")
    print(f"trace:   {run_dir / 'trace.jsonl'}")
    return 0 if passed else 1


def _run_directory(args: argparse.Namespace) -> int:
    """FB4C auto_run.py semantics: finish one independent file before the next.

    Snapshot immediate *.c inputs before writing outputs. Sorting stabilizes
    baseline order across hosts; per-file synthesis remains the single-file graph.
    """
    source = Path(args.source)
    inputs = sorted((p for p in source.iterdir() if p.is_file() and p.suffix == '.c'),
                    key=lambda p: (p.name.casefold(), p.name))
    if not inputs:
        print(f"no .c files directly inside: {source}", file=sys.stderr)
        return 2
    batch_dir = new_run_dir(Path(args.output), source, args.run_id)
    results_path = batch_dir / 'results.jsonl'
    results_path.touch()
    counts = dict.fromkeys(('pass', 'fail', 'error', 'planned', 'interrupted'), 0)
    tokens = prompt_tokens = 0
    model_seconds = wp_seconds = wp_wall_seconds = 0.0

    def checkpoint(stage, current_file=None):
        finished = sum(counts.values())
        summary = dict(kind='batch', dataset=source.resolve().name, model=args.model or None,
                       verification_scope=VERIFICATION_SCOPE,
                       stage=stage, total=len(inputs), completed=finished,
                       remaining=len(inputs)-finished, counts=counts, tokens=tokens,
                       completion_tokens=tokens, prompt_tokens=prompt_tokens, total_tokens=tokens+prompt_tokens,
                       timing=dict(model_seconds=round(model_seconds, 3), wp_seconds=round(wp_seconds, 3),
                                   wp_wall_seconds=round(wp_wall_seconds, 3)),
                       requested_candidate_count=args.candidates, fmparallel=args.fmparallel,
                       retry_budget_per_subtask=args.retries, repair_budget_per_subtask=args.repairs,
                       current_file=current_file, results='results.jsonl')
        # Atomic replacement leaves the previous complete summary readable.
        temporary = batch_dir / 'summary.tmp'
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(batch_dir / 'summary.json')

    print(f"batch: {batch_dir} ({len(inputs)} C files)")
    checkpoint('running')
    try:
        for index, path in enumerate(inputs, 1):
            checkpoint('running', path.name)
            print(f"\n[{index}/{len(inputs)}] {path.name}", flush=True)
            child = argparse.Namespace(**(vars(args) | {'source': path, 'input_dir': None, 'run_id': None}))
            child_dir = new_run_dir(batch_dir, path, None)
            interrupted = False
            try:
                code = _run_file(child, child_dir)
            except KeyboardInterrupt:
                interrupted = True
                code = 130
            summary_path = child_dir / 'summary.json'
            summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
            if interrupted:
                result = 'interrupted'
            elif code == 0 and args.stop_after == 'plan':
                result = 'planned'
            else:
                result = summary.get('result', 'error')
            record = dict(filename=path.name, run_dir=child_dir.name, result=result,
                          Status=1 if result == 'pass' else 0 if result == 'fail' else 'Check',
                          retries_used=summary.get('retries_used'), iteration_count=summary.get('iteration'),
                          completion_tokens=summary.get('tokens', 0), prompt_tokens=summary.get('prompt_tokens', 0),
                          total_tokens=summary.get('tokens', 0)+summary.get('prompt_tokens', 0),
                          timing=summary.get('timing', {}))
            for key in ('stop_reason', 'error', 'error_type', 'skipped'):
                if summary.get(key):
                    record[key] = summary[key]
            with results_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            counts[result] += 1
            tokens += record['completion_tokens']
            prompt_tokens += record['prompt_tokens']
            model_seconds += record['timing'].get('model_seconds', 0)
            wp_seconds += record['timing'].get('wp_seconds', 0)
            wp_wall_seconds += record['timing'].get('wp_wall_seconds', record['timing'].get('wp_seconds', 0))
            checkpoint('running')
            if interrupted:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        checkpoint('interrupted')
        print(f"\nbatch interrupted; saved results: {results_path}", file=sys.stderr)
        return 130
    except Exception:
        checkpoint('error')
        raise
    checkpoint('completed')
    print(f"\nbatch finished: {counts['pass']} pass, {counts['fail']} fail, "
          f"{counts['error']} error, {counts['planned']} planned")
    print(f"results: {results_path}")
    print(f"summary: {batch_dir / 'summary.json'}")
    return 0 if counts['fail'] == counts['error'] == 0 else 1


def _run_until_planned(graph, state: dict, run_dir: Path) -> dict:
    """Run analyze and plan_tasks only, for inspecting the task decomposition."""
    from acslagent import graph as graph_module

    snapshot = dict(state)
    snapshot = {**snapshot, **graph_module.analyze_node(snapshot)}
    snapshot = {**snapshot, **graph_module.plan_tasks(snapshot)}
    (run_dir / "plan.json").write_text(
        json.dumps(
            {"call_graph": snapshot["call_graph"], "tasks": snapshot["tasks"]},
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return snapshot


def timing_summary(run_dir):
    events = read_trace(run_dir)
    model = [e['detail'] for e in events if e['node'] in ('generate', 'fix') and e['event'] in ('reply', 'request_failed')]
    attempts = sum(e['event'] == 'transport_attempt_start' for e in events)
    timeout_retries = sum(e['event'] == 'transport_timeout_retry' for e in events)
    wp_events = [e for e in events if e['node'] in ('evaluate', 'full_verify') and e['event'] in ('done', 'cancelled')]
    wp = [e['detail'] for e in wp_events]
    intervals = sorted((datetime.fromisoformat(e['timestamp']).timestamp()-e['detail'].get('seconds', 0),
                        datetime.fromisoformat(e['timestamp']).timestamp()) for e in wp_events)
    wall, end = 0.0, float('-inf')
    for start, finish in intervals:
        wall += max(0.0, finish-max(start, end))
        end = max(end, finish)
    phases = {name: dict(calls=0, seconds=0.0) for name in ('candidate', 'full_check', 'local_baseline', 'repair')}
    for event in events:
        if event['node'] not in ('evaluate', 'full_verify') or event['event'] not in ('done', 'cancelled'):
            continue
        detail = event['detail']
        directory = Path(detail.get('evaluation_file', '')).parent.name
        phase = ('full_check' if event['node'] == 'full_verify' else
                 'local_baseline' if directory.startswith('baseline_') else
                 'repair' if directory.startswith('fix_') else 'candidate')
        phases[phase]['calls'] += 1
        phases[phase]['seconds'] = round(phases[phase]['seconds']+detail.get('seconds', 0), 3)
    return dict(model_seconds=round(sum(e.get('seconds', 0) for e in model), 3),
                wp_seconds=round(sum(e.get('seconds', 0) for e in wp), 3),
                wp_wall_seconds=round(wall, 3),
                wp_cancelled=sum(e['event'] == 'cancelled' for e in wp_events),
                model_calls=len(model), http_attempts=attempts, timeout_retries=timeout_retries,
                wp_calls=len(wp), wp_by_phase=phases)


def _write_summary(run_dir: Path, final: dict, passed: bool) -> None:
    outcome = final.get("final_outcome")
    summary = {
        "verification_scope": VERIFICATION_SCOPE,
        "result": "pass" if passed else "fail",
        "stage": final.get("stage"),
        "stop_reason": final.get("stop_reason"),
        "current_task_id": final.get("current_task_id"),
        "repair_budget_per_subtask": final.get("max_repairs", MAX_REPAIRS),
        "repairs_used": sum(t['repairs_used'] for t in final.get('ledger', {}).values()),
        "rounds": final.get("round_number", 1),
        "iteration": final.get("iteration_number", 1),
        "tasks": len(final.get("tasks") or []),
        "retry_budget_per_subtask": final.get("max_retries", MAX_RETRIES),
        "retries_used": sum(t['retries_used'] for t in final.get('ledger', {}).values()),
        "pass_policy": "strict: proved == total and total > 0; no termination goals",
        "budget_scope": "per_subtask_lifetime",
        "task_ledger": final.get('ledger', {}),
        "model_calls": final.get('model_calls', 0),
        "prompt_tokens": final.get('prompt_tokens', 0),
        "score_drop_threshold": final.get('score_drop_threshold', 0.10),
        "tokens": final.get("token_usage", 0),
        "completion_tokens": final.get("token_usage", 0),
        "total_tokens": final.get("token_usage", 0)+final.get("prompt_tokens", 0),
        "timing": timing_summary(run_dir),
        "wp_cache": final.get("wp_cache", "update"),
        "requested_candidate_count": final.get("candidate_count", 5),
        "fmparallel": final.get("fmparallel", 5),
        "verdicts": final.get("verify_counts") or {},
        "final_verification": outcome.as_dict(compact=True) if outcome else None,
        "specifications": {
            key: value for key, value in (final.get("specs") or {}).items() if value
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def command_trace(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    events = read_trace(run_dir, resolve_refs=args.json)
    if args.json:
        print(json.dumps(events, indent=2, ensure_ascii=False))
        return 0
    if not events:
        print(f"no trace in {run_dir}", file=sys.stderr)
        return 2
    print(f"{'seq':>4}  {'node':<12} {'event':<16} {'task':>4} {'try':>4} {'round':>5} {'iter':>4}  detail")
    for event in events:
        detail = event.get("detail") or {}
        summary = _summarise(event["event"], detail)
        print(
            f"{event['seq']:>4}  {event['node']:<12} {event['event']:<16}"
            f" {str(event['task_id'] or '-'):>4} {str(event.get('step') or '-'):>4} {str(event['round'] or '-'):>5}"
            f" {str(event['iteration'] or '-'):>4}  {summary}"
        )
    return 0


def _summarise(event: str, detail: dict) -> str:
    if event == "budget_exhausted":
        if detail.get('reason') == 'outer_retry_budget_exhausted':
            return f"outer_retry_budget_exhausted retries={detail['retries_used']}/{detail['max_retries']}"
        if detail.get('reason') == 'repair_budget_exhausted' and detail.get('route') == 'dispatch':
            return (f"repair_budget_exhausted repairs={detail['repairs_used']}/{detail['max_repairs']} "
                    f"-> dispatch task={detail['next_task_id']} "
                    f"retries={detail['retries_used']}/{detail['max_retries']}")
        return (f"{detail.get('reason', 'task_budget_exhausted')} "
                f"repairs={detail.get('repairs_used', '-')}/{detail.get('max_repairs', '-')} "
                f"steps={detail.get('max_steps', '-')}")
    if event in {"start", "finish"}:
        keys = ("result", "rounds", "tasks", "spec_locations")
        return " ".join(f"{key}={detail[key]}" for key in keys if key in detail)[:120]
    if "verdict" in detail:
        parts = [f"{detail['verdict']} {detail.get('proved')}/{detail.get('total')}"]
        if detail.get("timed_out_requires"):
            parts.append(f"timeouts={len(detail['timed_out_requires'])}")
        parts.append(f"passed={detail.get('passed')}")
        return " ".join(parts)
    if 'retries_remaining' in detail or 'repairs_remaining' in detail:
        return (f"score={detail.get('score', '-')} retries_left={detail.get('retries_remaining')} "
                f"repairs_left={detail.get('repairs_remaining')} {detail.get('reason','')}")
    if "route" in detail:
        return f"{detail['route']}: {detail.get('reason', '')}"
    if "clauses" in detail:
        return f"{len(detail['clauses'])} clauses"
    if "removed_clauses" in detail:
        return f"removed {len(detail['removed_clauses'])}: {detail.get('reason', '')}"
    if "error" in detail:
        return str(detail["error"])[:120]
    keys = ("attempt", "mode", "task_count", "function", "reason", "accepted_clauses")
    return " ".join(f"{key}={detail[key]}" for key in keys if key in detail)[:120]


def command_report(args: argparse.Namespace) -> int:
    from acslagent.report import write_report

    run_dir = Path(args.run_dir)
    path = write_report(run_dir)
    print(f"report: {path}")
    events = read_trace(run_dir)
    counts = Counter(event["event"] for event in events)
    print("events: " + ", ".join(f"{name}={count}" for name, count in sorted(counts.items())))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acslagent",
        description="ACSLAgent Improve: candidate WP ranking and per-subtask repair",
    )
    parser.add_argument('--version', action='version',
                        version=f'ACSLAgent Improve {__version__} ({project_root()})')
    commands = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("source", type=Path, nargs='?', help="C file or directory of independent .c programs")
        target.add_argument("-i", "--input-dir", type=Path, help="directory of independent .c programs (alternative to source)")
        target.add_argument("-m", "--model", help="ModelMap key in models_config.yaml")
        target.add_argument(
            "--model-config", default=str(project_root()/"config/models_config.yaml"), help="model configuration file"
        )
        target.add_argument(
            "--analyzer-config", default=str(project_root()/".tools/native.json"), help="veri-clang locations"
        )
        target.add_argument("--frama-c", default="frama-c", help="frama-c executable")
        target.add_argument("--candidates", type=int, default=5,
                            help="candidate count requested from the model (default 5); any nonempty batch is accepted")
        target.add_argument("--fmparallel", type=int, default=5,
                            help="maximum concurrent candidate Frama-C processes (default 5); 1 for serial")
        target.add_argument("--wp-cache", choices=("update", "none"), default="update",
                            help="Frama-C proof cache isolated per run (default update); none for cold comparison")
        target.add_argument("--quiet-progress", action="store_true", help="hide live model/WP/budget progress")
        target.add_argument("-o", "--output", default="runs", help="directory that holds run folders")
        target.add_argument("--run-id", help="explicit run folder name (batch folder for directory input)")
        target.add_argument(
            "--retries",
            type=retry_budget,
            default=MAX_RETRIES,
            help="additional candidate batches per subtask (default 5); initial batch is free",
        )
        target.add_argument(
            "--repairs",
            type=repair_budget,
            default=MAX_REPAIRS,
            help="single-candidate repairs over each subtask's lifetime (default 8); regeneration does not reset this",
        )
        target.add_argument("--score-drop-threshold", type=float, default=0.10,
                            help="absolute drop from best score in the same context that triggers retry (default 0.10 = 10 percentage points)")
        target.add_argument("--rounds", type=int, default=None, help=argparse.SUPPRESS)
        target.add_argument(
            "--api-retries", type=int, default=API_RETRIES_DEFAULT, help="legacy option, must be 1; timeout resends allow up to ten total requests, without task budget cost"
        )
        target.add_argument(
            "--enable-mutation", action="store_true", help="enable mutation initially (FB4C forces on at 3/4 and off at 5+)"
        )
        target.add_argument(
            "--recursion-limit", type=int, default=10000, help="LangGraph step limit"
        )

    run = commands.add_parser("run", help="full pipeline: analyse, generate, verify, repair")
    add_common(run)
    run.add_argument("--stop-after", choices=["plan", "full"], default="full")
    run.set_defaults(handler=command_run)

    plan = commands.add_parser("plan", help="call graph and subtask decomposition only")
    add_common(plan)
    plan.set_defaults(handler=lambda args: command_run(_as_plan(args)))

    trace = commands.add_parser("trace", help="print the trace of a finished run")
    trace.add_argument("run_dir", type=Path)
    trace.add_argument("--json", action="store_true")
    trace.set_defaults(handler=command_trace)

    report = commands.add_parser("report", help="write a Markdown report for a finished run")
    report.add_argument("run_dir", type=Path)
    report.set_defaults(handler=command_report)
    return parser


def retry_budget(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative; initial generation is free")
    return number


def repair_budget(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return number


def _as_plan(args: argparse.Namespace) -> argparse.Namespace:
    args.stop_after = "plan"
    return args


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `acslagent file.c` is the paper's default behaviour: a full run with a
    # model. Anything else is a subcommand.
    if argv and ((not argv[0].startswith("-") and argv[0] not in {"run", "plan", "trace", "report"})
                 or argv[0] in {'-i', '--input-dir'}):
        argv.insert(0, "run")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {'run', 'plan'}:
        if args.source is not None and args.input_dir is not None:
            parser.error('choose either source or --input-dir, not both')
        if args.input_dir is not None:
            if not args.input_dir.is_dir():
                parser.error(f'--input-dir is not a directory: {args.input_dir}')
            args.source = args.input_dir
        if args.source is None:
            parser.error('a source file/directory or --input-dir is required')
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        print('run interrupted', file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
