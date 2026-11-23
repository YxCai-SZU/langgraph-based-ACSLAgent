"""LangGraph execution of FB4C's single-file task-0 workflow.

Each transition corresponds to llmveri.py's actual statement order. LangGraph
owns assignment probes, the verification/repair loop, behavior choice, task
advancement and round reset. See tests/test_differential.py and PARITY.md.
"""
from __future__ import annotations

import shutil
from itertools import combinations
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from acslagent import framac, llm, normalize, render, repair
from acslagent import plan as planner
from acslagent.analyzer import AnalyzerConfig, analyze, call_graph
from acslagent.domain import RunState, TaskKind
from acslagent.model_config import ModelConfig
from acslagent.prompts import build_messages, mode_for
from acslagent.trace import Tracer

MAX_RETRIES = 5  # main.py MAX_ITERATION_TIMES: outer-loop attempts including the first.
MAX_ROUNDS = None
MAX_REPAIRS = 8  # Per dispatched task; -1 explicitly restores upstream's unbounded loop.


def current_task(state):
    return state['tasks'][state['current_task_id']-1]


def context(state):
    return dict(task_id=state.get('current_task_id'), round_number=state.get('round_number'),
                iteration_number=state.get('iteration_number'), step=state.get('step_number', 0),
                choice=state.get('choice_index', 0))


def emit(state, node, event, **detail):
    Tracer(Path(state['run_dir'])).emit(node, event, **(context(state) | detail))


def run_path(state, *parts):
    path = Path(state['run_dir']).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path, text):
    path.write_text(text, encoding='utf-8', newline='')
    return str(path)


def diagnostics(path, state):
    """Keep diagnostic contents in the trace without machine-local path prefixes.

    The original on-disk log remains untouched for FB4C's path-sensitive repair.
    """
    if not path:
        return ''
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    for key in ('candidate', 'merged_file', 'source_path'):
        value = state.get(key)
        if value:
            for name in (str(value), str(Path(value).resolve()), Path(value).resolve().as_posix()):
                text = text.replace(name, Path(value).name)
    for root in (str(Path(state['run_dir']).resolve()), Path(state['run_dir']).resolve().as_posix(),
                 state['run_dir']):
        text = text.replace(root + '/', '').replace(root + '\\', '')
    return text


def analyze_node(state):
    config = AnalyzerConfig.load(Path(state['analyzer_config']))
    work = run_path(state, 'work', 'analyze')
    text, records, report = analyze(Path(state['source_path']), work, config)
    emit(state, 'analyze', 'done', functions=[r['function_name'] for r in records],
         locations=[{k: v for k, v in r.items() if k != 'source_file'} for r in records],
         stderr=diagnostics(report.get('stderr'), state))
    return {'spec_locations_text': text, 'call_graph': call_graph(records),
            'source_text': planner.preprocess_source(state['source_text'], state['source_path'])}


def plan_tasks(state):
    tasks, marked = planner.plan_from_zones(state['source_text'], state['spec_locations_text'])
    emit(state, 'plan', 'done', task_count=len(tasks), tasks=tasks)
    return dict(tasks=tasks, marked_lines=marked, specs={str(t['task_id']): [] for t in tasks},
                current_task_id=1, step_number=0, status=0, stage='dispatch')


def dispatch(state):
    # Transport failures abort; there is no extra proposal budget in FB4C.
    if state.get('api_retries', 1) != 1:
        raise ValueError('FB4C issues one SDK call per subtask; api_retries must be 1')
    if state.get('max_retries', MAX_RETRIES) < 1:
        raise ValueError('outer retry budget must be positive')
    if state.get('max_repairs', MAX_REPAIRS) is not None and state.get('max_repairs', MAX_REPAIRS) < -1:
        raise ValueError('repair budget must be -1 (unlimited) or nonnegative')
    if state.get('step_number', 0) >= state.get('max_retries', MAX_RETRIES):
        emit(state, 'dispatch', 'budget_exhausted', reason='outer_retry_budget_exhausted',
             max_retries=state.get('max_retries', MAX_RETRIES), retries_used=state.get('step_number', 0))
        return {'stage': 'exhausted', 'stop_reason': 'outer_retry_budget_exhausted', 'status': 0}
    task = current_task(state)
    emit(state, 'dispatch', 'selected', kind=task['kind'], function=task['function_name'],
         step=state.get('step_number', 0)+1, choice=0, task_status='running',
         source_line=task['source_line'], accepted_clauses=state['specs'][str(task['task_id'])])
    return dict(step_number=state.get('step_number', 0)+1, repair_count=0, choice_index=0,
                attempt_clauses=[], best_candidate='', max_pass_goal=0,
                stage='generate')


def generate(state):
    task = current_task(state)
    existing = state['specs'][str(task['task_id'])]
    mode = mode_for(task, existing)
    question = render.mask_assertions(render.render_infill_source(
        state['marked_lines'], state['specs'], task['task_id']))
    messages = build_messages(mode, question)
    temperature = min(0.7 + state['iteration_number']*0.1, 1.0)
    emit(state, 'generate', 'request', mode=mode, temperature=temperature,
         source=question,
         accepted_clauses=existing)
    reply = llm.ask(ModelConfig.load(Path(state['model_config']), state['model']),
                    messages, temperature, attempts=1)
    replies = list(reply.choices) if reply.choices is not None else [reply.text]
    emit(state, 'generate', 'reply', choices=replies,
         completion_tokens=reply.completion_tokens,
         request_parameters=reply.parameters)
    return dict(question=question, mode=mode, reply=reply.text, replies=replies,
                token_usage=state.get('token_usage', 0)+reply.completion_tokens)


def normalize_reply(state):
    clean = [normalize.clean_reply(reply) for reply in state['replies']]
    existing = state['specs'][str(state['current_task_id'])]
    clauses, last_spec = normalize.pool_replies(clean, existing, with_context=True)
    # Generated reply files precede mutation/assigns probes in the original.
    directory = run_path(state, 'work', 'candidates')
    # Only the current task needs raw files; earlier results live in the trace.
    for old in directory.glob('gen*.c'):
        old.unlink()
    files = [write(directory / f'gen{i}.c',
                   render.fill_reply(state['question'], reply)) for i, reply in enumerate(clean)]
    clauses = normalize.mutate(clauses, state['iteration_number'], existing,
                               state.get('enable_mutation', False), last_spec=last_spec)
    emit(state, 'normalize', 'done', clauses=clauses, clean_replies=clean)
    return dict(clean_replies=clean, reply_files=files, attempt_clauses=clauses)


def prepare_assigns(state):
    clauses = list(state['attempt_clauses'])
    is_loop = current_task(state)['kind'] in (2, 3)
    probes = [c for c in clauses if 'loop assigns ' in c] if is_loop else []
    if is_loop and state['iteration_number'] > 1:
        for clause in probes:
            clauses.remove(clause)
        emit(state, 'assigns', 'drop_later_round', removed_clauses=probes)
        return dict(attempt_clauses=clauses, stage='embed')
    if is_loop and current_task(state)['kind'] == 2 and state['iteration_number'] == 1:
        return dict(probe_clauses=probes, probe_queue=[[c] for c in probes], probe_index=0,
                    probe_accepted=[], probe_phase='single', stage='probe_next')
    return {'stage': 'embed'}


def probe_next(state):
    if state['probe_index'] < len(state['probe_queue']):
        return {'stage': 'probe_verify'}
    if state['probe_phase'] == 'single' and not state['probe_accepted'] and len(state['probe_clauses']) <= 4:
        pairs = [list(p) for p in combinations(state['probe_clauses'], 2)]
        if pairs:
            return dict(probe_queue=pairs, probe_index=0, probe_phase='pair', stage='probe_verify')
    clauses = list(state['attempt_clauses'])
    if state['probe_accepted']:
        for clause in state['probe_clauses']:
            if clause not in state['probe_accepted']:
                clauses.remove(clause)
    emit(state, 'assigns', 'done', accepted=state['probe_accepted'], clauses=clauses)
    return dict(attempt_clauses=clauses, stage='embed')


def check(state, path, timeout=8, node='verify', **detail):
    # Trace the input BEFORE calling the process, including calls that crash.
    # Identical sources/diagnostics are stored once and referenced by sequence.
    emit(state, node, 'start', repair=state.get('repair_count', 0),
         source=Path(path).read_text(encoding='utf-8'), wp_timeout=timeout)
    outcome = framac.run(Path(path), run_path(state, 'work', 'verifier'), label='latest',
                        executable=state['frama_c'], wp_timeout=timeout)
    emit(state, node, 'done', repair=state.get('repair_count', 0),
         stdout=diagnostics(outcome.stdout_path, state), stderr=diagnostics(outcome.stderr_path, state),
         **outcome.as_dict(compact=True), **detail)
    return outcome


def probe_verify(state):
    proposal = state['probe_queue'][state['probe_index']]
    path = run_path(state, 'work', 'candidates') / 'probe.c'
    write(path, render.fill_assign_probe(state['question'], proposal, state['probe_clauses'][-1]))
    outcome = check(state, path, 3, 'assigns_verify')
    accepted = list(state['probe_accepted'])
    if outcome.verdict == 'pass':
        accepted.extend(proposal)
    elif outcome.verdict not in ('fail', 'invalid'):
        raise RuntimeError('FB4C: unknown result type when checking loop assigns')
    return dict(probe_accepted=accepted, probe_index=state['probe_index']+1,
                verify_counts=counted(state, outcome), stage='probe_next')


def embed(state):
    clauses = normalize.benchmark_adjustments(state['attempt_clauses'], state['source_path'],
                                              state['mode'], current_task(state)['kind'] == 3)
    clauses = sorted(clauses, reverse=True)
    path = run_path(state, 'work', 'candidates') / 'merged.c'
    write(path, render.fill_hole(state['question'], clauses))
    behavior = current_task(state)['kind'] == 1 and normalize.has_behavior(clauses)
    candidate = state['reply_files'][0] if behavior else str(path)
    emit(state, 'embed', 'done', clauses=clauses, assume_behavior=behavior,
         source=Path(candidate).read_text(encoding='utf-8'))
    return dict(attempt_clauses=clauses, merged_file=str(path), candidate=candidate,
                assume_behavior=behavior, stage='verify')


def counted(state, outcome, prefix=''):
    counts = dict(state.get('verify_counts', {}))
    key = prefix + outcome.verdict
    counts[key] = counts.get(key, 0)+1
    return counts


def verify(state):
    outcome = check(state, state['candidate'])
    return dict(outcome=outcome, verify_counts=counted(state, outcome))


def decide(state):
    outcome = state['outcome']
    kind = current_task(state)['kind']
    if outcome.verdict == 'invalid':
        route = 'repair'
    elif outcome.verdict == 'fail':
        route = 'finish_task' if kind == 2 else 'repair'
    elif kind == 1 and state['assume_behavior']:
        route = 'select_behavior'
    else:
        route = 'finish_task'
    limit = state.get('max_repairs', MAX_REPAIRS)
    if route == 'repair' and limit is not None and limit >= 0 and state['repair_count'] >= limit:
        # This ends the INNER repair loop, not the outer main.py attempt loop.
        # Keep CurTaskID and already-saved specs (including prior-loop removals).
        # Discard this failed proposal; dispatch alone owns the remaining budget.
        emit(state, 'decide', 'budget_exhausted', route='dispatch',
             reason='repair_budget_exhausted', repairs_used=state['repair_count'],
             max_repairs=limit, verdict=outcome.verdict, task_status='attempt_failed',
             next_task_id=state['current_task_id'], retries_used=state.get('step_number', 0),
             max_retries=state.get('max_retries', MAX_RETRIES))
        return dict(stage='dispatch', status=0)
    emit(state, 'decide', 'route', route=route, reason=outcome.result_type)
    return {'stage': route}


def repair_node(state):
    clauses = list(state['attempt_clauses'])
    specs = {k: list(v) for k, v in state['specs'].items()}
    kind = current_task(state)['kind']

    def remove_prior(clause):
        if kind == 3:
            repair.remove_from_inner_loop(specs, state['tasks'], state['current_task_id'], clause)

    if state['outcome'].verdict == 'invalid':
        removal = repair.repair_invalid(state['outcome'].stdout_path, Path(state['candidate']),
                                         clauses, remove_prior)
        stop = removal.stop or (kind == 1 and removal.note == 'I_REACH_MAX')
    else:
        removal = repair.repair_failed(state['outcome'].stdout_path, Path(state['candidate']))
        stop = not clauses  # checked BEFORE synchronizing removals in FB4C
        if not stop:
            for clause in removal.clauses:
                if clause == '}':
                    break
                if clause not in clauses:
                    remove_prior(clause)
                else:
                    clauses.remove(clause)
    emit(state, 'repair', 'done', reason=removal.reason, removed_clauses=removal.clauses,
         removed_lines=removal.lines, changed=removal.changed, note=removal.note,
         repair=state['repair_count']+1, stop=stop, resulting_specs=specs,
         remaining_clauses=clauses, source=Path(state['candidate']).read_text(encoding='utf-8'))
    return dict(attempt_clauses=clauses, specs=specs, repair_count=state['repair_count']+1,
                stage='finish_task' if stop else 'verify')


def select_behavior(state):
    outcome = state['outcome']
    best, maximum = state['best_candidate'], state['max_pass_goal']
    clauses = list(state['attempt_clauses'])
    # Upstream never updates max_all_goal (it assigns to all_goal instead).
    if outcome.verdict == 'pass' and outcome.proved > maximum and outcome.total >= 0:
        maximum, best = outcome.proved, state['candidate']
        clauses = state['clean_replies'][state['choice_index']].split('\n')
    index = state['choice_index']+1
    update = dict(max_pass_goal=maximum, best_candidate=best, attempt_clauses=clauses,
                  choice_index=index, stage='finish_task')
    if index < len(state['reply_files']):
        update.update(candidate=state['reply_files'][index], stage='verify')
    return update


def finish_task(state):
    if state['assume_behavior']:
        if not state['best_candidate']:
            raise RuntimeError('FB4C: No useful specification (no passing behavior candidate)')
        shutil.copyfile(state['best_candidate'], state['merged_file'])
    return {'stage': 'commit'}


def commit(state):
    task = current_task(state)
    clauses = list(state['attempt_clauses'])
    # Preserve removal while iterating (including skipped adjacent empties).
    for clause in clauses:
        if clause.startswith('//@ assert ') or clause == '':
            clauses.remove(clause)
    specs = {k: list(v) for k, v in state['specs'].items()}
    key = str(state['current_task_id'])
    if clauses:
        if task['kind'] in (2, 3):
            specs[key].extend(clauses)
            specs[key] = sorted(specs[key])
        elif state['assume_behavior']:
            specs[key] = clauses
        else:
            specs[key] = sorted(clauses)
        specs[key].reverse()
    following = state['current_task_id']
    if len(state['tasks']) > following:
        following += 1
    # FB4C checks AFTER increment, so the final task is generally not queried.
    stage = 'final_verify' if following == len(state['tasks']) else 'dispatch'
    emit(state, 'commit', 'done', accepted_clauses=specs[key],
         task_passed=state['outcome'].passed, task_status='committed',
         next_task_id=following, route=stage)
    return dict(specs=specs, current_task_id=following,
                last_completed_task_id=task['task_id'], stage=stage)


def final_verify(state):
    # The final check uses the repaired candidate, NOT a reconstruction from
    # accepted state. A repair may have changed an earlier annotation.
    path = Path(state['merged_file'])
    source = path.read_text(encoding='utf-8').replace('// @ assert', '//@ assert')
    write(path, source)
    write(Path(state['run_dir'])/'final.c', source)
    outcome = check(state, path, node='final_verify', clauses={k: v for k, v in state['specs'].items() if v})
    return dict(outcome=outcome, final_outcome=outcome, status=int(outcome.passed),
                verify_counts=counted(state, outcome, 'final_'),
                stage='success' if outcome.passed else 'reset_round')


def reset_round(state):
    specs = {k: ([] if state['tasks'][int(k)-1]['kind'] == 1 else list(v))
             for k, v in state['specs'].items()}
    emit(state, 'reset_round', 'done', kept={k: v for k, v in specs.items() if v})
    return dict(specs=specs, current_task_id=1, round_number=state['round_number']+1,
                iteration_number=state['iteration_number']+1, stage='dispatch')


def route(state):
    return state['stage']


def build_graph(*, start_at='analyze'):
    """start_at='dispatch' is used for differential tests with a fixed plan."""
    graph = StateGraph(RunState)
    nodes = dict(analyze=analyze_node, plan_tasks=plan_tasks, dispatch=dispatch, generate=generate,
                 normalize=normalize_reply, prepare_assigns=prepare_assigns, probe_next=probe_next,
                 probe_verify=probe_verify, embed=embed, verify=verify, decide=decide,
                 repair=repair_node, select_behavior=select_behavior, finish_task=finish_task,
                 commit=commit, final_verify=final_verify, reset_round=reset_round)
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, start_at)
    for a, b in [('analyze','plan_tasks'), ('plan_tasks','dispatch'), ('generate','normalize'),
                 ('normalize','prepare_assigns'), ('probe_verify','probe_next'), ('embed','verify'),
                 ('verify','decide'), ('finish_task','commit'), ('reset_round','dispatch')]:
        graph.add_edge(a, b)
    for name, destinations in {
        'dispatch': ['generate','exhausted'], 'prepare_assigns': ['probe_next','embed'],
        'probe_next': ['probe_verify','embed'], 'decide': ['repair','finish_task','select_behavior','dispatch'],
        'repair': ['verify','finish_task'], 'select_behavior': ['verify','finish_task'],
        'commit': ['dispatch','final_verify'], 'final_verify': ['success','reset_round'],
    }.items():
        graph.add_conditional_edges(name, route,
            {d: END if d in ('success','exhausted') else d for d in destinations})
    return graph.compile()
