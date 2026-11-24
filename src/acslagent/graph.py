"""Five-candidate ranking and per-subtask counterexample repair in LangGraph."""
from __future__ import annotations
import copy
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, CancelledError
from queue import Queue
from threading import Event
from pathlib import Path
from langgraph.graph import END, START, StateGraph
from acslagent import candidates, framac, llm, scope, feedback
from acslagent import plan as planner
from acslagent.analyzer import AnalyzerConfig, analyze, call_graph
from acslagent.domain import RunState
from acslagent.model_config import ModelConfig
from acslagent.trace import Tracer

MAX_RETRIES = 5
MAX_REPAIRS = 8


def current_task(state):
    return state['tasks'][state['current_task_id']-1]


def emit(state, node, event, **details):
    record = Tracer(Path(state['run_dir'])).emit(node, event, task_id=state.get('current_task_id'), **details)
    if not state.get('progress', False):
        return
    message = None
    if event == 'request':
        budget = details.get('budget', {})
        message = (f"LLM {details['phase']} start; retries={budget.get('retries_remaining')} "
                   f"repairs={budget.get('repairs_remaining')}")
    elif event == 'reply':
        message = (f"LLM {details['phase']} done {details.get('seconds', 0):.2f}s; "
                   f"tokens={details.get('prompt_tokens', 0)}+{details.get('completion_tokens', 0)} "
                   f"finish={details.get('finish_reason')}")
    elif event == 'transport_timeout':
        message = 'LLM request timed out; no retry/repair budget charged'
    elif event == 'transport_timeout_retry':
        message = "LLM timeout: resending the same request; task budgets unchanged"
    elif event in ('protocol_error', 'request_failed'):
        message = f"sample skipped: {details.get('error', 'LLM error')}; no additional task budget charged"
    elif node in ('evaluate', 'full_verify') and event == 'start':
        message = f"WP {details['scope']} start {Path(details['source_file']).parent.name}; termination excluded"
    elif node in ('evaluate', 'full_verify') and event == 'done':
        message = (f"WP {details.get('scope', 'scored')}={details['proved']}/{details['total']} ({details['score']:.1%}) "
                   f"own={details['current_proved']}/{details['current_total']} "
                   f"{details.get('seconds', 0):.2f}s raw={details['verdict']} "
                   f"excluded={details.get('excluded_goal_count', 0)}")
    elif event == 'charged':
        message = (f"{node} charged; retries={details.get('retries_remaining')} "
                   f"repairs={details.get('repairs_remaining')}; {details.get('reason', 'counterexample replacement')}")
    elif node == 'fix' and event == 'analysis':
        message = f"counterexample={details['has_counterexample']}; repair charged only for a replacement"
    elif node == 'select' and event == 'selected':
        message = (f"selected candidate {details['index']} score={details['score']:.1%}; "
                   f"retries={details['retries_remaining']} repairs={details['repairs_remaining']}")
    elif node == 'evaluate' and event == 'early_stop':
        message = f"candidate {details['index']} reached 100%; cancelling remaining candidates"
    elif event == 'failed':
        message = details.get('reason', 'failed')
    if message:
        print(f"[{record['timestamp'][11:19]} UTC task {state.get('current_task_id', '-')} {node}] {message}", flush=True)


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=lambda x: x.as_dict())+'\n', encoding='utf-8')
    tmp.replace(path)


def checkpoint(state):
    directory = Path(state['run_dir'])
    save_json(directory/'task_ledger.json', state.get('ledger', {}))
    for key, ledger in state.get('ledger', {}).items():
        save_json(directory/'tasks'/f'task_{int(key):03d}'/'state.json', ledger)
    save_json(directory/'checkpoint.json', state)


def updated_ledger(state):
    ledger = copy.deepcopy(state['ledger'])
    return ledger, ledger[str(state['current_task_id'])]


def failed(state, reason, ledger=None):
    update = dict(stage='failed', status=0, stop_reason=reason)
    ledger = copy.deepcopy(state.get('ledger', {})) if ledger is None else ledger
    if str(state.get('current_task_id')) in ledger:
        ledger[str(state['current_task_id'])].update(status='failed', stop_reason=reason)
    update['ledger'] = ledger
    emit(state, 'budget' if 'budget' in reason else 'controller', 'failed', reason=reason)
    return update


def request_retry(state, reason):
    ledger, task = updated_ledger(state)
    if task['retries_remaining'] <= 0:
        return failed(state, f'task_{state["current_task_id"]}:retry_budget_exhausted:{reason}', ledger)
    task['retries_remaining'] -= 1
    task['retries_used'] += 1
    task['retry_reasons'].append(reason)
    emit(state, 'retry', 'charged', reason=reason, retries_remaining=task['retries_remaining'],
         repairs_remaining=task['repairs_remaining'])
    return dict(ledger=ledger, batch_mode='retry', retry_reason=reason, stage='generate')


def analyze_node(state):
    work = Path(state['run_dir'])/'work'/'analyze'
    text, records, report = analyze(Path(state['source_path']), work, AnalyzerConfig.load(Path(state['analyzer_config'])))
    emit(state, 'analyze', 'done', locations=records, report=report)
    return dict(spec_locations_text=text, call_graph=call_graph(records),
                source_text=planner.preprocess_source(state['source_text'], state['source_path']), stage='plan_tasks')


def plan_tasks(state):
    if state.get('max_retries', MAX_RETRIES) < 0 or state.get('max_repairs', MAX_REPAIRS) < 0:
        raise ValueError('per-task budgets must be nonnegative')
    tasks, marked = planner.plan_from_zones(state['source_text'], state['spec_locations_text'])
    if not tasks:
        raise ValueError('veri-clang returned no tasks')
    ledger = {}
    for task in tasks:
        ledger[str(task['task_id'])] = dict(task=task, retries_remaining=state.get('max_retries', MAX_RETRIES),
            repairs_remaining=state.get('max_repairs', MAX_REPAIRS), retries_used=0, repairs_used=0,
            batches=0, history=[], selected=None, current_score=0.0, best_score=0.0,
            best_by_context={}, evaluations=[], retry_reasons=[], fixes=[], status='pending')
    scopes = scope.extract_scopes(dict(state, tasks=tasks))
    emit(state, 'plan', 'done', tasks=tasks, source_scopes=scopes)
    return dict(tasks=tasks, marked_lines=marked, ledger=ledger, source_scopes=scopes,
                specs={str(t['task_id']): '' for t in tasks}, current_task_id=1,
                initial_complete=False, batch_mode='initial', retry_reason='',
                verification_number=0, stage='generate')


def task_payload(state):
    task = current_task(state)
    entry = state['ledger'][str(task['task_id'])]
    source, _ = scope.render_source(state, limit=task['task_id'], hole=task['task_id'])
    owned = next(s for s in state['source_scopes'] if s['task_id'] == task['task_id'])
    last = entry.get('last_evaluation') or {}
    history, generation_errors = feedback.history_feedback(entry['history'])
    last_brief = {k: last.get(k) for k in ('score', 'proved', 'total', 'current_proved', 'current_total', 'verdict', 'error')}
    goals = {}
    for origin, items in (('full', state.get('failed_goals', [])), ('local', last.get('current_failed', []))):
        for goal in items:
            key = (goal.get('title'), goal.get('diagnostic'))
            if key not in goals:
                goals[key] = dict(feedback.compact_goal(goal), observed_in=[])
            goals[key]['observed_in'].append(origin)
    # Give the code once, with exact task bounds, rather than duplicating a
    # potentially large loop/function a second time in every request.
    return dict(task=task, task_source_range=dict(start=owned['start'], end=owned['end']),
                function_type=owned.get('function_type', ''),
                requested_candidate_count=state.get('candidate_count', candidates.REQUESTED_CANDIDATES),
                source_with_insertion_point=source,
                current_specification=state['specs'].get(str(task['task_id']), ''),
                budget={k: entry[k] for k in ('retries_remaining', 'repairs_remaining', 'retries_used', 'repairs_used')},
                current_score=entry['current_score'], best_score=entry['best_score'],
                best_score_in_current_context=entry['best_by_context'].get(context_key(state), entry['current_score']),
                retry_reason=state.get('retry_reason', ''), candidate_history=history,
                recent_generation_errors=generation_errors,
                diagnostics=list(goals.values())[:12], omitted_goal_count=max(0, len(goals)-12),
                last_local_evaluation=last_brief,
                scope_note='Only current and predecessor tasks are scored. Later goals are excluded. '
                           'Read surrounding assertions to design useful facts, but modify this task only. '
                           'WP Timeout/Unknown gives no counterexample; your own concrete code-based witness is allowed.')



def ask_model(state, phase, payload, directory):
    directory.mkdir(parents=True, exist_ok=True)
    messages = candidates.messages(phase, payload)
    save_json(directory/'request.json', messages)
    emit(state, 'generate' if phase != 'fix' else 'fix', 'request', phase=phase,
         request_file=str(directory/'request.json'), budget=payload['budget'])
    started = time.monotonic()
    node = 'fix' if phase == 'fix' else 'generate'
    def transport_event(event, **detail):
        emit(state, node, 'transport_'+event, phase=phase, **detail)
    try:
        reply = llm.ask(ModelConfig.load(Path(state['model_config']), state['model']),
                        messages, 0.7 if phase == 'initial' else 0.9 if phase == 'retry' else 0.3,
                        attempts=1, on_event=transport_event)
    except llm.ModelError as error:
        detail = dict(error=str(error), error_type=type(error).__name__,
                      seconds=round(time.monotonic()-started, 3), skipped=True)
        save_json(directory/'error.json', detail)
        emit(state, node, 'request_failed', phase=phase, **detail)
        ledger, entry = updated_ledger(state)
        entry.update(status='error', stop_reason=type(error).__name__)
        checkpoint(dict(state, ledger=ledger, stage='error', status=0, stop_reason=type(error).__name__))
        raise
    (directory/'response.txt').write_text(reply.text or '', encoding='utf-8')
    emit(state, 'generate' if phase != 'fix' else 'fix', 'reply',
         phase=phase, response=reply.text, completion_tokens=reply.completion_tokens,
         prompt_tokens=getattr(reply, 'prompt_tokens', 0), request_parameters=reply.parameters,
         seconds=reply.seconds, finish_reason=getattr(reply, 'finish_reason', None),
         request_attempts=getattr(reply, 'request_attempts', 1))
    return reply.text or '', dict(token_usage=state.get('token_usage', 0)+reply.completion_tokens,
                                 prompt_tokens=state.get('prompt_tokens', 0)+getattr(reply, 'prompt_tokens', 0),
                                 model_calls=state.get('model_calls', 0)+1)


def abort_protocol(state, directory, message):
    # A response-envelope error is not an ACSL verification failure. Keep the
    # received token usage and budgets, stop this sample, let CLI continue batch.
    ledger, entry = updated_ledger(state)
    entry.update(status='error', stop_reason='llm_response_parse_error')
    detail = dict(error=message, error_type='ModelParseError', skipped=True)
    save_json(Path(directory)/'error.json', detail)
    emit(state, 'fix' if Path(directory).name.startswith('fix_') else 'generate', 'protocol_error', **detail)
    checkpoint(dict(state, ledger=ledger, stage='error', status=0, stop_reason='llm_response_parse_error'))
    raise llm.ModelParseError(message)


def generate(state):
    ledger, entry = updated_ledger(state)
    entry['batches'] += 1
    entry['status'] = 'generating'
    directory = Path(state['run_dir'])/'tasks'/f'task_{state["current_task_id"]:03d}'/f'batch_{entry["batches"]:03d}'
    # Persist counters before network activity.
    local = dict(state, ledger=ledger)
    checkpoint(local)
    response, usage = ask_model(local, state['batch_mode'], task_payload(local), directory)
    adjustments = []
    batch = candidates.parse_batch(response, current_task(state)['kind'],
                                   {h['fingerprint'] for h in entry['history'] if h.get('fingerprint')},
                                   diagnostics=adjustments)
    if any(item['error'] == 'malformed_batch' for item in batch):
        abort_protocol(dict(local, **usage), directory, batch[0].get('error_detail', 'invalid candidate response'))
    parsing = dict(requested_candidate_count=state.get('candidate_count', candidates.REQUESTED_CANDIDATES),
                   candidate_count=len(batch), eligible_for_wp=sum(item['error'] is None for item in batch),
                   adjustments=adjustments)
    save_json(directory/'parsing.json', parsing)
    emit(local, 'generate', 'parsed', **parsing)
    for item in batch:
        item['directory'] = str(directory/f'candidate_{item["index"]}')
    return dict(usage, ledger=ledger, batch=batch, stage='evaluate')


def context_key(state):
    cutoff = state['current_task_id']
    preceding = {k: v for k, v in state['specs'].items() if int(k) < cutoff}
    return hashlib.sha256(json.dumps(preceding, sort_keys=True).encode()).hexdigest()


def verify_source(state, specs, directory, *, full=False, cancel_event=None, cache_dir_override=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    limit = None if full else state['current_task_id']
    source, mapping = scope.render_source(state, specs, limit=limit)
    path = directory/'candidate.c'
    path.write_text(source, encoding='utf-8')
    save_json(directory/'source_map.json', mapping)
    # -wp-fct omits CFG goal details on Frama-C 33 while still counting them.
    # Later annotations are masked in the source; use complete output and retain
    # only current/predecessor goal owners in the local score below.
    functions = None
    cache_dir = Path(state['run_dir'])/'wp_cache' if state.get('wp_cache', 'update') == 'update' else None
    if cache_dir is not None and cache_dir_override is not None:
        cache_dir = Path(cache_dir_override)
    command = framac.build_command(path, state['frama_c'], functions=functions,
                                   include_dir=str(Path(state['source_path']).resolve().parent), cache_dir=cache_dir)
    emit(state, 'full_verify' if full else 'evaluate', 'start', command=command, source_file=str(path),
         scope='full' if full else 'current_and_predecessors')
    try:
        outcome = framac.run(path, directory, label='wp', executable=state['frama_c'], functions=functions,
                             include_dir=str(Path(state['source_path']).resolve().parent), cache_dir=cache_dir,
                             cancel_event=cancel_event)
    except framac.VerificationCancelled as error:
        save_json(directory/'cancelled.json', dict(reason='batch_cancelled', seconds=error.seconds))
        emit(state, 'evaluate', 'cancelled', seconds=error.seconds, command=command,
             source_file=str(path), evaluation_file=str(directory/'evaluation.json'))
        raise
    goals = scope.attribute_goals(outcome, mapping, state['tasks'], path)
    selected = goals if full else [g for g in goals if g['task_id'] is not None and g['task_id'] <= limit]
    unknown = [g for g in goals if g['task_id'] is None]
    valid = outcome.verdict in ('pass', 'fail') and not unknown
    proved, total = sum(g['passed'] for g in selected), len(selected)
    score = proved/total if valid and total else 0.0
    own = [g for g in selected if g['task_id'] == state['current_task_id']]
    result = dict(score=score, proved=proved if valid else 0, total=total if valid else 0,
                  valid=valid, verdict=outcome.verdict, current_proved=sum(g['passed'] for g in own),
                  current_total=len(own), current_failed=[g for g in own if not g['passed']],
                  failed_goals=[g for g in selected if not g['passed']], unmapped_goals=unknown,
                  excluded_goals=[g for g in goals if g not in selected],
                  goals=selected, source_file=str(path), raw=outcome.as_dict(),
                  context_key=None if full else context_key(state))
    save_json(directory/'evaluation.json', result)
    emit(state, 'full_verify' if full else 'evaluate', 'done', score=score, proved=result['proved'],
         total=result['total'], current_proved=result['current_proved'], current_total=len(own),
         verdict=outcome.verdict, command=list(outcome.command), evaluation_file=str(directory/'evaluation.json'),
         unmapped_goals=unknown, seconds=outcome.seconds, scope='full' if full else 'local',
         excluded_goal_count=len(result['excluded_goals']))
    return result, outcome


def zero_evaluation(error):
    return dict(score=0.0, proved=0, total=0, valid=False, error=error, current_failed=[],
                failed_goals=[], current_proved=0, current_total=0, verdict='invalid')


def remember(entry, candidate, result, origin):
    diagnostics = []
    if not result.get('valid'):
        raw = result.get('raw', {})
        for stream in ('stdout_path', 'stderr_path'):
            if raw.get(stream):
                diagnostics.append(Path(raw[stream]).read_text(encoding='utf-8', errors='replace')[-2000:])
    history = dict(specification=candidate.get('specification', ''), fingerprint=candidate.get('fingerprint'),
                   score=result['score'], error=candidate.get('error'), error_detail=candidate.get('error_detail'), origin=origin,
                   diagnostics='\n'.join(diagnostics),
                   evaluation_file=str(Path(candidate['directory'])/'evaluation.json'))
    entry['history'].append(history)
    entry['evaluations'].append(dict(origin=origin, score=result['score'], proved=result['proved'], total=result['total'],
                                    context_key=result.get('context_key'), evaluation_file=history['evaluation_file']))
    entry['best_score'] = max(entry['best_score'], result['score'])
    context = result.get('context_key')
    if context and result['valid']:
        entry['best_by_context'][context] = max(entry['best_by_context'].get(context, 0.0), result['score'])


def evaluate(state):
    ledger, entry = updated_ledger(state)
    batch = copy.deepcopy(state['batch'])
    parallelism = max(1, state.get('fmparallel', 5))
    started = time.monotonic()
    stop = Event()
    finished = Queue()
    pending, eligible = {}, []
    winner = None

    def record(item, result):
        item['evaluation'] = result
        item['verification_status'] = 'completed'
        remember(entry, item, result, f'batch_{entry["batches"]}:{item["index"]}')
        checkpoint(dict(state, ledger=ledger, batch=batch))

    def verify(item):
        if stop.is_set():
            raise framac.VerificationCancelled()
        specs = dict(state['specs'], **{str(state['current_task_id']): item['specification']})
        # Candidate-specific caches avoid concurrent WP writes. The normal
        # per-run cache is still used by sequential full checks and repairs.
        cache = Path(item['directory'])/'wp_cache' if parallelism > 1 else None
        return verify_source(state, specs, item['directory'], cancel_event=stop, cache_dir_override=cache)[0]

    for item in batch:
        if item['error']:
            result = zero_evaluation(item['error'])
            save_json(Path(item['directory'])/'evaluation.json', result)
            record(item, result)
        else:
            item['verification_status'] = 'not_started'
            eligible.append(item)
    queue = iter(eligible)
    executor = ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix='candidate-wp')

    def submit_next():
        item = next(queue, None)
        if item is not None:
            item['verification_status'] = 'running'
            future = executor.submit(verify, item)
            pending[future] = item
            future.add_done_callback(finished.put)

    try:
        for _ in range(min(parallelism, len(eligible))):
            submit_next()
        while pending:
            future = finished.get()
            item = pending.pop(future)
            result = future.result()
            record(item, result)
            if result['valid'] and result['total'] > 0 and result['proved'] == result['total']:
                winner = item['index']
                stop.set()
                emit(state, 'evaluate', 'early_stop', index=winner)
                break
            submit_next()
    finally:
        stop.set()
        for future in pending:
            future.cancel()
        # Workers kill only their own process groups and reap children before
        # the next task starts; cancellation never consumes retry/repair budget.
        executor.shutdown(wait=True, cancel_futures=True)
    for future, item in pending.items():
        try:
            result = future.result()
        except (CancelledError, framac.VerificationCancelled):
            item['verification_status'] = 'cancelled'
        else:
            record(item, result)
    for item in batch:
        if 'evaluation' not in item:
            item['verification_status'] = ('cancelled' if item['verification_status'] == 'running'
                                           else item['verification_status'])
            save_json(Path(item['directory'])/'scheduling.json', dict(status=item['verification_status'],
                       reason='perfect_candidate_selected', winner=winner))
            # Preserve generated-but-unscored blocks for history deduplication.
            entry['history'].append(dict(specification=item['specification'], fingerprint=item.get('fingerprint'),
                                         score=None, error=None, origin=f'batch_{entry["batches"]}:{item["index"]}',
                                         verification_status=item['verification_status']))
    if winner is not None:
        for item in batch:
            item['early_selected'] = item['index'] == winner
    emit(state, 'evaluate', 'batch_done', seconds=round(time.monotonic()-started, 3),
         parallelism=parallelism, winner=winner, completed=sum('evaluation' in c for c in batch),
         candidate_count=len(batch))
    return dict(ledger=ledger, batch=batch, stage='select')


def select(state):
    evaluated = [c for c in state['batch'] if 'evaluation' in c]
    best = next((c for c in evaluated if c.get('early_selected')), None)
    if best is None:
        best = max(evaluated, key=lambda c: c['evaluation']['score'])  # stable first-on-tie
    if best['evaluation']['score'] <= 0:
        return request_retry(state, 'all_candidates_scored_zero')
    ledger, entry = updated_ledger(state)
    entry.update(selected=best['specification'], current_score=best['evaluation']['score'],
                 last_evaluation=best['evaluation'], status='selected')
    specs = dict(state['specs'], **{str(state['current_task_id']): best['specification']})
    emit(state, 'select', 'selected', index=best['index'], score=entry['current_score'],
         best_score=entry['best_score'], retries_remaining=entry['retries_remaining'],
         repairs_remaining=entry['repairs_remaining'])
    if state.get('initial_complete'):
        # Local failures belong to their owner; return to full attribution.
        return dict(ledger=ledger, specs=specs, stage='full_verify')
    if state['current_task_id'] == len(state['tasks']):
        return dict(ledger=ledger, specs=specs, initial_complete=True, stage='full_verify')
    return dict(ledger=ledger, specs=specs, current_task_id=state['current_task_id']+1,
                batch_mode='initial', retry_reason='', failed_goals=[], stage='generate')


def full_verify(state):
    number = state.get('verification_number', 0)+1
    result, outcome = verify_source(state, state['specs'], Path(state['run_dir'])/'full_checks'/f'check_{number:03d}', full=True)
    source = Path(result['source_file']).read_text(encoding='utf-8')
    (Path(state['run_dir'])/'final.c').write_text(source, encoding='utf-8')
    ledger = copy.deepcopy(state['ledger'])
    for key, entry in ledger.items():
        owned = [g for g in result.get('goals', []) if g['task_id'] == int(key)]
        entry['full_check'] = dict(proved=sum(g['passed'] for g in owned), total=len(owned),
                                  failed=[g for g in owned if not g['passed']], check_number=number)
        entry['status'] = 'verified' if result['valid'] and owned and all(g['passed'] for g in owned) else 'unproved'
    update = dict(full_evaluation=result, final_outcome=outcome, verification_number=number, ledger=ledger)
    if result['valid'] and outcome.strict_passed and result['total'] > 0:
        return dict(update, status=1, stage='success', stop_reason='all_nontermination_goals_proved')
    if not result['valid'] or not result['failed_goals']:
        update.update(failed(state, 'full_verification_invalid_or_unattributable', ledger))
        return update
    return dict(update, stage='locate_failure')


def locate_failure(state):
    failures = state['full_evaluation']['failed_goals']
    if any(g['task_id'] is None for g in failures):
        return failed(state, 'unattributable_failure')
    task_id = min(g['task_id'] for g in failures)
    owned = [g for g in failures if g['task_id'] == task_id]
    emit(dict(state, current_task_id=task_id), 'locate_failure', 'selected', failed_goals=owned)
    return dict(current_task_id=task_id, failed_goals=owned, stage='local_check')


def local_check(state):
    ledger, entry = updated_ledger(state)
    serial = len(entry['evaluations'])+1
    directory = Path(state['run_dir'])/'tasks'/f'task_{state["current_task_id"]:03d}'/f'baseline_{serial:03d}'
    result, _ = verify_source(state, state['specs'], directory)
    entry['current_score'] = result['score']
    entry['last_evaluation'] = result
    if result['valid']:
        key = context_key(state)
        entry['best_by_context'][key] = max(entry['best_by_context'].get(key, 0.0), result['score'])
        entry['best_score'] = max(entry['best_score'], result['score'])
    entry['evaluations'].append(dict(origin='baseline', score=result['score'], evaluation_file=str(directory/'evaluation.json')))
    if not result['valid']:
        return dict(local_evaluation=result, **request_retry(dict(state, ledger=ledger), 'invalid_local_baseline'))
    # Full failures of this task still matter even if its local obligations pass.
    return dict(ledger=ledger, local_evaluation=result, stage='fix')


def fix(state):
    ledger, entry = updated_ledger(state)
    directory = Path(state['run_dir'])/'tasks'/f'task_{state["current_task_id"]:03d}'/f'fix_{len(entry["fixes"])+1:03d}'
    response, usage = ask_model(state, 'fix', task_payload(state), directory)
    adjustments = []
    try:
        answer = candidates.parse_fix(response, current_task(state)['kind'], diagnostics=adjustments)
    except (ValueError, TypeError) as error:
        entry['fixes'].append(dict(error=str(error), charged_repair=False))
        local = dict(state, ledger=ledger, **usage)
        checkpoint(local)
        abort_protocol(local, directory, str(error))
    save_json(directory/'parsing.json', dict(adjustments=adjustments))
    if adjustments:
        emit(state, 'fix', 'parsed', adjustments=adjustments)
    entry['fixes'].append(dict(answer, provenance='unvalidated_llm_analysis', charged_repair=False))
    emit(state, 'fix', 'analysis', has_counterexample=answer['has_counterexample'],
         provenance='unvalidated_llm_analysis', counterexample=answer['counterexample'])
    if not answer['has_counterexample']:
        local = dict(state, ledger=ledger, **usage)
        checkpoint(local)
        return dict(usage, **request_retry(local, 'no_counterexample_found'))
    if entry['repairs_remaining'] <= 0:
        return dict(usage, **failed(state, f'task_{state["current_task_id"]}:repair_budget_exhausted', ledger))
    entry['repairs_remaining'] -= 1
    entry['repairs_used'] += 1
    entry['fixes'][-1]['charged_repair'] = True
    emit(state, 'fix', 'charged', repairs_remaining=entry['repairs_remaining'],
         retries_remaining=entry['retries_remaining'], counterexample=answer['counterexample'],
         provenance='unvalidated_llm_analysis')
    local = dict(state, ledger=ledger, **usage)
    checkpoint(local)
    candidate = dict(specification=answer['specification'], fingerprint=candidates.fingerprint(answer['specification']),
                     error=None, directory=str(directory))
    try:
        spec = candidates.normalize_spec(answer['specification'], current_task(state)['kind'])
        candidate.update(specification=spec, fingerprint=candidates.fingerprint(spec))
        if candidate['fingerprint'] in {h['fingerprint'] for h in entry['history']}:
            raise ValueError('duplicate_repair_candidate')
    except (ValueError, TypeError) as error:
        candidate['error'] = str(error)
    specs = dict(state['specs'], **{str(state['current_task_id']): candidate['specification']})
    if candidate['error']:
        result = zero_evaluation(candidate['error'])
        save_json(directory/'evaluation.json', result)
    else:
        result, _ = verify_source(local, specs, directory)
    comparison_best = entry['best_by_context'].get(context_key(state), entry['current_score'])
    remember(entry, candidate, result, f'fix_{len(entry["fixes"])}')
    local.update(ledger=ledger)
    drop = comparison_best-result['score']
    external_only = bool(result.get('failed_goals')) and not result['current_failed']
    degraded = (not external_only and drop > 1e-12 and
                drop+1e-12 >= state.get('score_drop_threshold', 0.10))
    if not result['valid'] or result['total'] == 0 or degraded:
        entry['fixes'][-1].update(rejected=True, score_drop=drop, comparison_best=comparison_best)
        checkpoint(local)
        # Rejected repairs are saved, but the previously selected block stays.
        return dict(usage, **request_retry(local, 'invalid_or_degraded_repair'))
    entry.update(selected=candidate['specification'], current_score=result['score'], last_evaluation=result)
    entry['fixes'][-1].update(rejected=False, score_drop=drop, comparison_best=comparison_best,
                            external_only_failure=external_only)
    # Do not charge this task for failures in predecessors or outer tasks.
    next_stage = 'fix' if result['current_failed'] else 'full_verify'
    return dict(usage, ledger=ledger, specs=specs, local_evaluation=result,
                failed_goals=result['current_failed'], stage=next_stage)


def build_graph(*, start_at='analyze'):
    graph = StateGraph(RunState)
    nodes = dict(analyze=analyze_node, plan_tasks=plan_tasks, generate=generate, evaluate=evaluate,
                 select=select, full_verify=full_verify, locate_failure=locate_failure,
                 local_check=local_check, fix=fix)
    def wrapped(name, fn):
        def run(state):
            update = fn(state)
            checkpoint(dict(state, **update))
            return update
        return run
    for name, fn in nodes.items():
        graph.add_node(name, wrapped(name, fn))
    graph.add_edge(START, start_at)
    destinations = {name: name for name in nodes}
    destinations.update(success=END, failed=END)
    for name in nodes:
        graph.add_conditional_edges(name, lambda state: state['stage'], destinations)
    return graph.compile()
