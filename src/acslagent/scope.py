"""Source ownership and insertion; C structure comes from Clang, never the LLM."""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from acslagent.analyzer import AnalyzerConfig

# Scan comments together with literals so comment-like text in a C string is
# never mistaken for an annotation. Ordinary comments are consumed as a whole.
_C_LEXEME = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*.*?\*/|//[^\n]*', re.S)


def annotation_matches(source):
    return [m for m in _C_LEXEME.finditer(source) if m.group().startswith(('/*@', '//@'))]


def rewrite_annotations(source, transform):
    def replace(match):
        return transform(match) if match.group().startswith(('/*@', '//@')) else match.group()
    return _C_LEXEME.sub(replace, source)


def extract_scopes(state):
    config = AnalyzerConfig.load(Path(state['analyzer_config']))
    directory = Path(state['run_dir']) / 'work' / 'structure'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'structure.c'
    path.write_text(state['source_text'], encoding='utf-8')
    env = dict(os.environ)
    env['LD_LIBRARY_PATH'] = config.library_path
    command = [str(Path(config.compiler_directory)/'clang'), '-fsyntax-only',
               '-Wno-everything', '-Xclang', '-ast-dump=json', '-I',
               str(Path(state['source_path']).resolve().parent), str(path.resolve())]
    result = subprocess.run(command, env=env, capture_output=True, timeout=config.timeout_seconds)
    (directory/'clang.stderr.txt').write_bytes(result.stderr)
    if result.returncode:
        raise ValueError('Clang could not determine reliable task ranges; see work/structure/clang.stderr.txt')
    ast = json.loads(result.stdout)
    data = state['source_text'].encode()
    scopes = []
    contracts = {t['function_name']: t for t in state['tasks'] if t['kind'] == 1}

    def location(item):
        if 'expansionLoc' in item or 'spellingLoc' in item:
            raise ValueError('macro-expanded function/loop locations cannot be inserted safely')
        return item.get('offset')

    def visit(node, function=None):
        kind = node.get('kind')
        children = node.get('inner', [])
        if kind == 'FunctionDecl':
            if node.get('name') not in contracts or not any(x.get('kind') == 'CompoundStmt' for x in children):
                return
            function = node['name']
        if function and kind in ('FunctionDecl', 'ForStmt', 'WhileStmt', 'DoStmt'):
            area = node.get('range', {})
            start = location(area.get('begin', {}))
            end = location(area.get('end', {}))
            if start is None or end is None:
                raise ValueError('missing source range for a task')
            first, last = data[:start].count(b'\n')+1, data[:end].count(b'\n')+1
            matching = [t for t in state['tasks'] if t['function_name'] == function and
                        ((kind == 'FunctionDecl' and t['kind'] == 1) or
                         (kind != 'FunctionDecl' and t['kind'] != 1 and t['source_line'] == first))]
            if len(matching) != 1:
                raise ValueError(f'cannot uniquely match {kind} at {first} to veri-clang task')
            task = matching[0]
            scopes.append(dict(task_id=task['task_id'], function=function, start=first, end=last,
                               kind=task['kind'], offset=start))
            if kind == 'FunctionDecl':
                scopes[-1]['function_type'] = node.get('type', {}).get('desugaredQualType', node.get('type', {}).get('qualType', ''))
        for child in children:
            visit(child, function)
    visit(ast)
    if {s['task_id'] for s in scopes} != {t['task_id'] for t in state['tasks']}:
        raise ValueError('Clang and veri-clang task sets disagree')
    if len({t['source_line'] for t in state['tasks']}) != len(state['tasks']):
        raise ValueError('multiple tasks on the same source line; put function and loops on separate lines')
    return scopes


def owner_at(scopes, line):
    inside = [s for s in scopes if s['start'] <= line <= s['end']]
    if not inside:
        return None
    # Containment width handles nested loops and single-statement loop bodies.
    return min(inside, key=lambda s: (s['end']-s['start'], -s['offset']))['task_id']


def render_source(state, specs=None, limit=None, hole=None):
    specs = state['specs'] if specs is None else specs
    scopes = state['source_scopes']
    by_line = {t['source_line']: t for t in state['tasks']}
    original = state['source_text']
    annotation_owners = {}
    earlier_insertions = {}
    for match in annotation_matches(original):
        first = original[:match.start()].count('\n')+1
        last = original[:match.end()].count('\n')+1
        text = match.group()
        owner = owner_at(scopes, first)
        if re.search(r'\bloop\s+(?:invariant|assigns|variant)', text):
            later = [t for t in state['tasks'] if t['kind'] != 1 and t['source_line'] >= last]
            if later:
                owner = min(later, key=lambda t: t['source_line'])['task_id']
                earlier_insertions.setdefault(owner, first)
        elif re.search(r'\b(?:requires|ensures|assigns|terminates|decreases|behavior)\b', text):
            later = [t for t in state['tasks'] if t['kind'] == 1 and t['source_line'] >= last]
            if later:
                owner = min(later, key=lambda t: t['source_line'])['task_id']
                earlier_insertions.setdefault(owner, first)
        for number in range(first, last+1):
            annotation_owners[number] = owner
    for task in state['tasks']:
        first = earlier_insertions.get(task['task_id'])
        if first is not None and first != task['source_line']:
            by_line.pop(task['source_line'], None)
            if first in by_line:
                raise ValueError('overlapping existing annotations cannot be inserted safely')
            by_line[first] = task
    # Suppress later-task original ACSL blocks locally. Keep line counts stable.
    if limit is not None:
        def mask(match):
            line = original[:match.start()].count('\n')+1
            owner = annotation_owners.get(line, owner_at(scopes, line))
            if owner is not None and owner > limit:
                return match.group().replace('/*@', '/* ', 1).replace('//@', '// ', 1)
            return match.group()
        original = rewrite_annotations(original, mask)
    out, mapping = [], {}
    def append(text, owner, line, annotation=False):
        for part in text.splitlines(keepends=True):
            out.append(part if part.endswith('\n') else part+'\n')
            mapping[len(out)] = dict(task_id=owner, original_line=line, annotation=annotation)
    for line, content in enumerate(original.splitlines(keepends=True), 1):
        task = by_line.get(line)
        if task:
            task_id = task['task_id']
            if task_id == hole:
                kind = 'FUNCTION CONTRACT: requires/ensures/assigns' if task['kind'] == 1 else 'LOOP: loop invariant/loop assigns'
                append(f'/* TARGET_TASK {task_id} {kind}: insert one block here */\n', task_id, line, True)
            elif limit is None or task_id <= limit:
                spec = specs.get(str(task_id), '')
                if spec:
                    append('/*@\n'+spec+'\n*/\n', task_id, line, True)
        # A line annotation can be merged with our block without shifting lines.
        content = rewrite_annotations(content, lambda m: '/*@'+m.group()[3:]+' */' if m.group().startswith('//@') else m.group())
        append(content, annotation_owners.get(line, owner_at(scopes, line)), line)
    source = ''.join(out)
    spans = annotation_matches(source)
    changes = []
    def block_kind(match):
        body = match.group()[3:].lstrip()
        if re.match(r'loop\s+(?:invariant|assigns|variant|allocates|frees)\b', body):
            return 'loop'
        if re.match(r'(?:requires|ensures|assigns|terminates|decreases|behavior|complete|disjoint)\b', body):
            return 'contract'
        return None
    for left, right in zip(spans, spans[1:]):
        # Statement annotations (especially adjacent asserts) each need their
        # own comment. Merge only compatible contracts owned by the same task.
        left_owner = mapping[source[:left.start()].count('\n')+1]['task_id']
        right_owner = mapping[source[:right.start()].count('\n')+1]['task_id']
        if (left.group().startswith('/*@') and right.group().startswith('/*@')
                and block_kind(left) is not None and block_kind(left) == block_kind(right)
                and left_owner is not None and left_owner == right_owner
                and not source[left.end():right.start()].strip()):
            changes += [(left.end()-2, left.end(), '  '), (right.start(), right.start()+3, '   ')]
    for start, end, replacement in sorted(changes, reverse=True):
        source = source[:start]+replacement+source[end:]
    return source, mapping


def attribute_goals(outcome, mapping, tasks, candidate_path):
    contracts = {t['function_name']: t['task_id'] for t in tasks if t['kind'] == 1}
    result = []
    for goal in outcome.goals:
        owner = None
        entry = None
        if goal.get('file') and goal.get('line'):
            path = Path(goal['file'])
            # Reject header locations; do not apply this file's line map to them.
            if path.resolve() == candidate_path.resolve() or (not path.is_absolute() and path.name == candidate_path.name):
                entry = mapping.get(goal['line'])
        if entry:
            owner = entry['task_id']
        if owner is None and not goal.get('file') and goal.get('function'):
            owner = contracts.get(goal['function'])
        result.append(dict(goal, task_id=owner,
                           original_line=entry['original_line'] if entry else None))
    return result
