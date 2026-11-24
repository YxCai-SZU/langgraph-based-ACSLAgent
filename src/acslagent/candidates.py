"""Validated model protocol, history deduplication and prompt assembly."""
from __future__ import annotations
import hashlib
import json
import re
from importlib.resources import files

TOKEN = re.compile(r'\\[A-Za-z_]\w*|[A-Za-z_]\w*|\d+(?:\.\d+)?|<==>|==>|&&|\|\||==|!=|<=|>=|[^\s]')
REQUESTED_CANDIDATES = 5
JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
# Only known ACSL tokens can be recovered, and only in specification values.
# In particular, bare \n / \r / \t remain JSON whitespace escapes. Matching
# complete tokens also avoids interpreting an unknown escape as a known prefix.
ACSL_ESCAPE = re.compile(r'\\(?:' + '|'.join((
    'result', 'nothing', 'old', 'at', 'forall', 'exists', 'true', 'false',
    'valid', 'valid_read', 'valid_range', 'valid_index', 'initialized',
    'dangling', 'separated', 'base_addr', 'block_length', 'offset', 'null',
    'allocable', 'freeable', 'fresh', 'empty', 'union', 'inter', 'subset',
    'sum', 'product', 'numof', 'min', 'max', 'lambda', 'let', 'sizeof',
    'typeof', 'type',
)) + r')(?![A-Za-z_0-9])')


class _JSONObject(dict):
    """Preserve repeated keys until the appropriate protocol is validated."""
    def __init__(self, pairs):
        super().__init__(pairs)
        self.pairs = pairs
        self.duplicate_keys = {key for key, _ in pairs if sum(k == key for k, _ in pairs) > 1}


def _escape_specification_backslashes(text, diagnostics):
    replacements, keywords = [], []
    for key in JSON_STRING.finditer(text):
        if key.group() != '"specification"':
            continue
        separator = re.match(r'\s*:\s*', text[key.end():])
        if separator is None:
            continue
        value = JSON_STRING.match(text, key.end()+separator.end())
        if value is None:
            continue  # Null, wrong types and unfinished strings are not repaired.
        raw, pieces, position = value.group(), [], 0
        while position < len(raw):
            if raw[position] != '\\':
                pieces.append(raw[position])
                position += 1
                continue
            token = ACSL_ESCAPE.match(raw, position)
            if token:
                pieces.append('\\'+token.group())
                keywords.append(token.group())
                position = token.end()
            else:
                # Consume an existing escape as a pair. This keeps correctly
                # escaped backslashes/quotes/newlines/unicode byte-for-byte.
                pieces.append(raw[position:position+2])
                position += 2
        replacement = ''.join(pieces)
        if replacement != raw:
            replacements.append((value.start(), value.end(), replacement))
    for start, end, replacement in reversed(replacements):
        text = text[:start]+replacement+text[end:]
    if keywords:
        diagnostics.append(dict(kind='escaped_acsl_backslashes', count=len(keywords),
                                keywords=sorted(set(keywords))))
    return text


def _recover_batch_items(text, diagnostics):
    """Keep complete JSON items in a complete batch; never invent missing syntax.

    A damaged but balanced candidate object cannot invalidate its independent
    siblings. Truncated strings/containers and ambiguous envelopes still fail.
    """
    opening = re.match(r'^\{\s*"candidates"\s*:\s*\[', text)
    if not opening:
        return None
    fragments, stack = [], []
    start = position = opening.end()
    quoted = escaped = False
    while position < len(text):
        char = text[position]
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '[{':
            stack.append(char)
        elif char == ']' and not stack:
            if text[position+1:].strip() != '}':
                return None
            fragments.append(text[start:position])
            break
        elif char in ']}':
            if not stack or stack.pop() != {']': '[', '}': '{'}[char]:
                return None
        elif char == ',' and not stack:
            fragments.append(text[start:position])
            start = position+1
        position += 1
    else:
        return None
    if any(not fragment.strip() for fragment in fragments):
        return None
    items, rejected = [], []
    for index, fragment in enumerate(fragments, 1):
        try:
            items.append(json.loads(fragment, object_pairs_hook=_JSONObject))
        except json.JSONDecodeError as error:
            rejected.append(dict(kind='ignored_malformed_candidate', source_index=index,
                                 error=str(error), raw=fragment.strip()))
    if not items or not rejected:
        return None
    diagnostics.extend(rejected)
    return _JSONObject([('candidates', items)])


def json_response(text, expected_keys=None, *, diagnostics=None):
    diagnostics = [] if diagnostics is None else diagnostics
    text = text.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.I)
        text = re.sub(r'\n?```\s*$', '', text)
    # Run before decoding even valid JSON: unescaped ACSL \nothing / \result
    # would otherwise silently turn into newline / carriage-return characters.
    text = _escape_specification_backslashes(text, diagnostics)
    try:
        data = json.loads(text, object_pairs_hook=_JSONObject)
    except json.JSONDecodeError:
        if expected_keys == {'candidates'}:
            recovered = _recover_batch_items(text, diagnostics)
            if recovered is not None:
                return recovered
        # Some compatible APIs prepend prose despite the JSON-only prompt.
        # Recover one complete, unambiguous object; never repair truncated JSON
        # or accept one of several conflicting answers.
        if text.startswith(('{', '[')):
            raise
        decoder = json.JSONDecoder(object_pairs_hook=_JSONObject)
        objects, position = [], 0
        while position < len(text):
            start = text.find('{', position)
            if start < 0:
                break
            try:
                obj, length = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                position = start+1
                continue
            objects.append(obj)
            position = start+length
        if len(objects) != 1 or (expected_keys is not None and set(objects[0]) != expected_keys):
            raise ValueError('response has no single complete protocol object')
        data = objects[0]
    if not isinstance(data, dict):
        raise ValueError('response must be a JSON object')
    if data.duplicate_keys:
        raise ValueError('duplicate protocol fields: ' + ', '.join(sorted(data.duplicate_keys)))
    return data


def _batch_items(obj, diagnostics):
    if set(obj) != {'candidates'} or not isinstance(obj['candidates'], list):
        raise ValueError('response must contain a candidates array')
    items = []
    for source_index, item in enumerate(obj['candidates'], 1):
        if not isinstance(item, dict):
            diagnostics.append(dict(kind='ignored_non_candidate', source_index=source_index))
            continue
        # Recover the observed {specification: A, specification: B} shape without
        # silently losing A. This is batch-only: fix must still return one block.
        specs = [value for key, value in item.pairs if key == 'specification']
        if len(specs) > 1:
            diagnostics.append(dict(kind='expanded_repeated_specification',
                                    source_index=source_index, count=len(specs)))
        extras = sorted(set(item)-{'specification'})
        if extras:
            diagnostics.append(dict(kind='ignored_candidate_metadata',
                                    source_index=source_index, keys=extras))
        usable = [spec for spec in specs if isinstance(spec, str) and spec.strip()]
        if not specs or len(usable) != len(specs):
            diagnostics.append(dict(kind='ignored_non_candidate', source_index=source_index))
        items.extend(dict(specification=spec) for spec in usable)
    if not items:
        raise ValueError('at least one nonempty string specification is required')
    return items


def normalize_spec(text, kind):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('specification must be a nonempty string')
    text = text.strip()
    if text.startswith('/*@') and text.endswith('*/'):
        text = text[3:-2].strip()
    text = re.sub(r'^\s*//@\s?', '', text, flags=re.M)
    if any(x in text for x in ('/*', '*/', '//', '#', '```')):
        raise ValueError('return clauses only, without code/comments/directives')
    if re.search(r'\b(?:terminates|decreases|variant|axiom|axiomatic|ghost|admit|check)\b', text):
        raise ValueError('unsupported or out-of-scope clause')
    if not text.endswith(';'):
        raise ValueError('annotation must end in a semicolon')
    if kind in (2, 3):
        if not re.match(r'loop\s+(invariant|assigns)\b', text) or not re.search(r'\bloop\s+invariant\b', text):
            raise ValueError('loop task requires loop invariant clauses')
        if re.search(r'\b(?:requires|ensures|assumes|behavior|assert)\b', text):
            raise ValueError('non-loop clause in a loop task')
    else:
        if not re.match(r'(requires|ensures|assigns|behavior)\b', text):
            raise ValueError('function contract expected')
        if re.search(r'\bloop\b|\bassert\b', text):
            raise ValueError('non-contract clause in function task')
        if not re.search(r'\bensures\b|\bassigns\b', text):
            raise ValueError('contract must describe a postcondition or frame')
    return text


def fingerprint(spec):
    # Preserve token boundaries: "a b" must not become "ab". Ignore user labels.
    text = re.sub(r'\b(loop\s+invariant|requires|ensures|assumes)\s+[A-Za-z_]\w*\s*:', r'\1 ', spec)
    text = ' '.join(TOKEN.findall(text))
    # Simple independent clauses may be reordered; behavior/quantifier order is kept.
    if not re.search(r'\bbehavior\b|\\forall|\\exists|\\let', text):
        text = ';'.join(sorted(x.strip() for x in text.split(';') if x.strip()))
    return hashlib.sha256(text.encode()).hexdigest()


def parse_batch(text, kind, history, *, diagnostics=None):
    diagnostics = [] if diagnostics is None else diagnostics
    try:
        obj = json_response(text, {'candidates'}, diagnostics=diagnostics)
        items = _batch_items(obj, diagnostics)
    except (ValueError, TypeError) as exc:
        return [dict(index=1, specification='', error='malformed_batch',
                     error_detail=str(exc), fingerprint=None)]
    seen = set(history)
    candidates = []
    for i, item in enumerate(items, 1):
        spec, error, key = '', None, None
        try:
            if not isinstance(item, dict) or set(item) != {'specification'}:
                raise ValueError('candidate must contain only specification')
            if isinstance(item['specification'], str):
                spec = item['specification']
                key = fingerprint(spec)
            spec = normalize_spec(item['specification'], kind)
            key = fingerprint(spec)
            if key in seen:
                error = 'duplicate_candidate'
            seen.add(key)
        except (ValueError, TypeError) as exc:
            error = str(exc)
            if key:
                seen.add(key)
        candidates.append(dict(index=i, specification=spec, fingerprint=key, error=error))
    return candidates


def parse_fix(text, kind, *, diagnostics=None):
    obj = json_response(text, {'has_counterexample', 'counterexample', 'specification'}, diagnostics=diagnostics)
    if set(obj) != {'has_counterexample', 'counterexample', 'specification'}:
        raise ValueError('invalid fix schema')
    if type(obj['has_counterexample']) is not bool or not isinstance(obj['counterexample'], str) or not obj['counterexample'].strip():
        raise ValueError('fix requires a boolean and an explanation')
    if obj['has_counterexample']:
        # Candidate syntax is checked separately, after charging a repair.
        if not isinstance(obj['specification'], str):
            raise ValueError('counterexample=true requires a replacement string')
    elif obj['specification'] is not None:
        raise ValueError('counterexample=false requires specification=null')
    return obj


def messages(phase, payload):
    root = files('acslagent').joinpath('improve_prompts')
    count = payload.get('requested_candidate_count', REQUESTED_CANDIDATES)
    if phase != 'fix':
        payload = dict(payload, requested_candidate_count=count)
    task = payload['task']
    target = (f"ACTIVE TASK {task['task_id']}: FUNCTION CONTRACT for {task['function_name']}. "
              "Return requires/ensures/assigns for this function; DO NOT return loop clauses."
              if task['kind'] == 1 else
              f"ACTIVE TASK {task['task_id']}: LOOP ANNOTATION in {task['function_name']} at original line {task['source_line']}. "
              "Return loop invariant/loop assigns for this loop; DO NOT return a function contract.")
    if task['kind'] == 1 and re.match(r'^void\s*\(', payload.get('function_type', '')):
        target += (' This function returns void: NEVER use \\result. Body-local variables are not '
                   'visible in its contract. Describe observable state or a justified assigns frame; '
                   'keep original body assertions for verification. Do not invent \\valid as a result variable.')
    schema = json.dumps({'candidates': [{'specification': '...'} for _ in range(count)]}, separators=(',', ':'))
    def prompt(name):
        return (root.joinpath(name).read_text(encoding='utf-8')
                .replace('{{candidate_count}}', str(count)).replace('{{batch_schema}}', schema))
    return [
        {'role': 'system', 'content': prompt('system.md')},
        {'role': 'user', 'content': target + '\n\n' + prompt(phase + '.md') +
         '\n\nTASK DATA:\n' + json.dumps(payload, ensure_ascii=False, separators=(',', ':')) +
         '\n\n' + prompt('response_' + ('fix' if phase == 'fix' else 'batch') + '.md')}
    ]
