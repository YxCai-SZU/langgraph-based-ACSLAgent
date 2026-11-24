"""Strict WP results; every call explicitly excludes termination obligations."""
from __future__ import annotations

import re
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path
from acslagent.domain import FramacOutcome

WP_TIMEOUT_SECONDS = 8
PROCESS_TIMEOUT_SECONDS = 500
PROVERS = 'Alt-Ergo,Z3'
EXCLUDED_GOALS = ('@terminates', '@decreases', '@variant')
PROPERTY_FILTER = '-wp-prop=' + ','.join('-' + goal for goal in EXCLUDED_GOALS)
VERIFICATION_SCOPE = 'partial_correctness'
ABORT_MARKERS = ('Frama-C aborted', 'Plug-in wp aborted', 'invalid preprocessing directive',
                 '[kernel:annot-error]', '[kernel:parser]', 'User Error:')


class VerificationCancelled(RuntimeError):
    """A sibling proved all local goals; this result must not be scored zero."""
    def __init__(self, seconds=0.0):
        super().__init__('candidate verification cancelled')
        self.seconds = seconds


def _kill_verifier(process):
    # Every verifier owns a new process group. This includes its solver children,
    # and never targets an unrelated Frama-C or the user's other terminal jobs.
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
    except ProcessLookupError:
        pass


def classify(stdout: bytes) -> tuple[str, int, int, tuple[str, ...]]:
    text = stdout.decode('utf-8', errors='replace')
    timeouts = tuple(line for line in text.splitlines() if '[Timeout]' in line and '_requires' in line)
    if any(marker in text for marker in ABORT_MARKERS):
        return 'invalid', 0, 0, timeouts
    matches = re.findall(r'\[wp\]\s+Proved goals:\s*(\d+)\s*/\s*(\d+)', text)
    if not matches:
        return 'unknown', 0, 0, timeouts
    proved, total = map(int, matches[-1])
    if not 0 <= proved <= total:
        return 'invalid', 0, 0, timeouts
    return ('pass' if proved == total and total > 0 else 'fail'), proved, total, timeouts


def parse_goals(text: str) -> tuple[dict, ...]:
    """WP print includes CFG/unreachable goals omitted by WP's JSON report.

    Keep each obligation rather than counting solver attempts. The caller checks
    these counts against WP's summary; an unfamiliar format fails closed.
    """
    goals = []
    pattern = r'^Goal (.*?):\s*\n(.*?)(?=^-{10,}|\Z)'
    for title, body in re.findall(pattern, text, re.M | re.S):
        locations = re.findall(r'"([^"]+)", lines? (\d+)', title)
        functions = re.findall(r" in '([^']+)'", title)
        # An instantiated precondition names the callee/header first and the
        # caller/call site last. Attribute it to the actual statement being proved.
        location = locations[-1] if locations else None
        goals.append(dict(title=' '.join(title.split()),
                          file=location[0] if location else None,
                          line=int(location[1]) if location else None,
                          function=functions[-1] if functions else None,
                          declaration_location=({'file': locations[0][0], 'line': int(locations[0][1])}
                                                if len(locations) > 1 else None),
                          passed=bool(re.search(r'^Prover .*\breturns Valid\b', body, re.M)),
                          diagnostic=body.strip()))
    return tuple(goals)


def build_command(c_file: Path, executable: str = 'frama-c', wp_timeout: int = WP_TIMEOUT_SECONDS,
                  *, functions: list[str] | None = None, include_dir: str | None = None,
                  cache_dir: Path | None = None) -> list[str]:
    prefix = [executable]
    if executable == 'frama-c' and shutil.which(executable) is None and shutil.which('opam'):
        prefix = [shutil.which('opam'), 'exec', '--', 'frama-c']
    command = [*prefix, '-wp', PROPERTY_FILTER, '-wp-precond-weakening',
               '-wp-no-callee-precond', '-wp-prover', PROVERS, '-wp-print',
               '-wp-timeout', str(wp_timeout), '-wp-par', '2',
               '-wp-cache', 'update' if cache_dir is not None else 'none']
    if cache_dir is not None:
        command += ['-wp-cache-dir', str(cache_dir.resolve())]
    if functions:
        command += ['-wp-fct', ','.join(functions)]
    if include_dir:
        import shlex
        command += ['-cpp-extra-args=-I' + shlex.quote(include_dir)]
    return command + [str(c_file)]


def run(c_file: Path, output_dir: Path, *, label: str, executable: str = 'frama-c',
        process_timeout: int = PROCESS_TIMEOUT_SECONDS, wp_timeout: int = WP_TIMEOUT_SECONDS,
        functions: list[str] | None = None, include_dir: str | None = None,
        cache_dir: Path | None = None, cancel_event=None) -> FramacOutcome:
    output_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(c_file, executable, wp_timeout, functions=functions, include_dir=include_dir,
                            cache_dir=cache_dir)
    started = time.monotonic()
    timed_out, returncode = False, None
    if cancel_event is not None and cancel_event.is_set():
        raise VerificationCancelled()
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=os.name == 'posix')
    except FileNotFoundError:
        raise RuntimeError('frama-c not found; run under opam exec or use --frama-c') from None
    cancelled = False
    try:
        while True:
            elapsed = time.monotonic()-started
            cancelled = cancel_event is not None and cancel_event.is_set()
            timed_out = elapsed >= process_timeout
            if cancelled or timed_out:
                _kill_verifier(process)
                stdout, stderr = process.communicate()
                break
            try:
                stdout, stderr = process.communicate(timeout=min(.1, process_timeout-elapsed))
                break
            except subprocess.TimeoutExpired:
                continue
        returncode = process.returncode
    except BaseException:
        _kill_verifier(process)
        process.communicate()
        raise
    if cancelled:
        _save(output_dir, label, 'stdout', stdout)
        _save(output_dir, label, 'stderr', stderr)
        raise VerificationCancelled(round(time.monotonic()-started, 3))
    verdict, proved, total, timeouts = classify(stdout + b'\n' + stderr)
    goals = parse_goals(stdout.decode('utf-8', errors='replace'))
    if timed_out:
        verdict, proved, total = 'unknown', 0, 0
    elif returncode != 0:
        verdict, proved, total = 'invalid', 0, 0
    elif verdict in ('pass', 'fail') and (len(goals) != total or sum(g['passed'] for g in goals) != proved):
        verdict, proved, total = 'unknown', 0, 0
        stderr += b'\nImprove: WP goal details disagree with summary; refusing incomplete results.\n'
    return FramacOutcome(verdict=verdict, proved=proved, total=total, timed_out_requires=timeouts,
                         command=tuple(command), stdout_path=_save(output_dir, label, 'stdout', stdout),
                         stderr_path=_save(output_dir, label, 'stderr', stderr),
                         seconds=round(time.monotonic()-started, 3), process_timed_out=timed_out,
                         returncode=returncode, goals=goals)


def _save(directory: Path, label: str, stream: str, content: bytes) -> str:
    path = directory / f'{label}.{stream}.txt'
    path.write_bytes(content)
    return str(path)
