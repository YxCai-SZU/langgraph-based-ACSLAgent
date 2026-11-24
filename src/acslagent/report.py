"""Reports for the Improve workflow, derived only from saved evidence."""
import json
from pathlib import Path
from acslagent.trace import read_trace


def write_report(run_dir):
    root = Path(run_dir)
    def read(name):
        path = root/name
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    summary = read('summary.json')
    ledger = read('task_ledger.json')
    events = read_trace(root)
    result = summary.get('result', 'incomplete')
    timing = summary.get('timing', {})
    lines = [f'# Improve run: {root.name}', '', f'Result: **{result}**', '',
             f'Stop reason: {summary.get("stop_reason", summary.get("error", "not finished"))}', '',
             'Strict PASS requires proved == total and total > 0. Timeouts are not proved.',
             'Every WP call excludes @terminates, @decreases and @variant.', '',
             'Retries and repairs are independent lifetime budgets for each subtask. Initial generation is free.',
             'Best scores are historical; degradation compares scores only with the same predecessor context.',
             'LLM counterexamples are unvalidated analysis, not solver certificates.', '',
             f"Measured time: LLM={timing.get('model_seconds', 'not recorded')}s, WP process sum={timing.get('wp_seconds', 'not recorded')}s, WP wall={timing.get('wp_wall_seconds', 'not recorded')}s.",
             f"Tokens: prompt={summary.get('prompt_tokens', 0)}, completion={summary.get('tokens', 0)}, total={summary.get('total_tokens', 'not recorded')}.",
             f"WP cache: {summary.get('wp_cache', 'not recorded')}; cached timeouts are still unproved.", '',
             '| task | function | kind | batches | retry left | repair left | current | historical best |',
             '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for key, task in ledger.items():
        meta = task['task']
        lines.append(f'| {key} | {meta["function_name"]} | {meta["kind"]} | {task["batches"]} | '
                     f'{task["retries_remaining"]} | {task["repairs_remaining"]} | '
                     f'{task["current_score"]:.2%} | {task["best_score"]:.2%} |')
    lines += ['', '## WP timing by phase', '']
    for phase, value in timing.get('wp_by_phase', {}).items():
        lines.append(f"- {phase}: {value['seconds']:.3f}s, {value['calls']} calls")
    lines += ['', '## Candidate evaluations', '',
              '| task | origin | proved/total | score | evidence |', '| --- | --- | --- | --- | --- |']
    for key, task in ledger.items():
        for ev in task['evaluations']:
            lines.append(f'| {key} | {ev["origin"]} | {ev.get("proved","?")}/{ev.get("total","?")} | '
                         f'{ev["score"]:.2%} | {ev["evaluation_file"]} |')
    lines += ['', '## Whole-file checks', '']
    for event in events:
        if event['node'] == 'full_verify' and event['event'] == 'done':
            d = event['detail']
            lines += [f'- {d["proved"]}/{d["total"]}, score {d["score"]:.2%}, verdict {d["verdict"]}',
                      '', '    ' + ' '.join(d['command']), '']
    lines += ['', '## Selected annotations', '']
    for key, task in ledger.items():
        lines += [f'### Task {key}', '', '~~~acsl', task.get('selected') or '(none)', '~~~', '']
    lines += ['Artifacts: task_ledger.json, checkpoint.json, trace.jsonl, tasks/, full_checks/, final.c.', '']
    path = root/'report.md'
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path
