"""Small, explicit proof context for generation and counterexample analysis."""
from __future__ import annotations
import re


def goal_kind(title):
    title = title.lower()
    for fragment, kind in (('preservation', 'loop_preservation'), ('establishment', 'loop_initiation'),
                           ('pre-condition', 'call_precondition'), ('post-condition', 'postcondition'),
                           ('assert', 'assertion'), ('assigns', 'frame')):
        if fragment in title:
            return kind
    return 'other'


def compact_goal(goal):
    text = goal.get('diagnostic', '')
    # Preserve both hypotheses and the final Prove/prover result. The old
    # prefix-only truncation could discard the actual obligation.
    brief = text if len(text) <= 1800 else text[:650]+'\n[... hypotheses truncated ...]\n'+text[-1100:]
    return dict(title=goal.get('title'), kind=goal_kind(goal.get('title', '')),
                task_id=goal.get('task_id'), original_line=goal.get('original_line'),
                passed=goal.get('passed'), diagnostic=brief, diagnostic_truncated=len(text)>1800)


def history_feedback(history):
    # Preserve every distinct prior block for deduplication. Empty protocol
    # failures and repeated copies add no specifications and need only one hint.
    unique, errors = {}, {}
    for item in history:
        spec = item.get('specification', '')
        if spec:
            summary = dict(specification=spec, score=item.get('score'), error=item.get('error'))
            if item.get('verification_status'):
                summary['verification_status'] = item['verification_status']
            unique[item.get('fingerprint') or spec] = summary
        diagnostic = item.get('diagnostics', '')
        if diagnostic:
            # Compiler errors are more useful than repeatedly printing a long
            # command, standard headers, and the final generic abort line.
            lines = diagnostic.splitlines()
            indices = set()
            for i, line in enumerate(lines):
                if re.search(r'error|unbound|unexpected|syntax|not allowed|Only one|meaningless|Failure', line, re.I):
                    # Frama-C puts the actual cause after a "Failure:" header.
                    indices.update(range(i, min(len(lines), i+3)))
            relevant = [lines[i] for i in sorted(indices)]
            diagnostic = '\n'.join(relevant) or diagnostic[-700:]
        error = item.get('error_detail') or item.get('error') or diagnostic
        if error:
            errors[str(error)[:700]] = True
    return list(unique.values()), list(errors)[-8:]
