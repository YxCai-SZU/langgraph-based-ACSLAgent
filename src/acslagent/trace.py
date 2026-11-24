"""Compact, append-only intermediate results and task states.

seq orders events; task_id/round/iteration/step/choice identify the task.
Repeated source/stdout/stderr payloads use <field>_ref: <seq>, referring to an
earlier event's detail.<field>. read_trace(resolve_refs=True) expands them.
Verifier events include commands and the loaded verifier module for auditing.
UTC timestamps and measured durations allow timing attribution without file-mtime estimates.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock

TRACE_NAME = "trace.jsonl"
_CONTENT_FIELDS = ("source", "stdout", "stderr")


@dataclass
class _Writer:
    sequence: int = 0
    signature: tuple = ()
    contents: dict = field(default_factory=dict)


def _signature(path: Path) -> tuple:
    if not path.exists():
        return ()
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _last_sequence(path: Path) -> int:
    """Read backwards from the tail once, rather than rescan a growing trace."""
    if not path.exists():
        return 0
    with path.open('rb') as stream:
        stream.seek(0, 2)
        position, remaining = stream.tell(), b''
        while position:
            count = min(position, 8192)
            position -= count
            stream.seek(position)
            parts = (stream.read(count) + remaining).split(b'\n')
            remaining = parts.pop(0) if position else b''
            for line in reversed(parts):
                try:
                    return int(json.loads(line)['seq'])
                except (ValueError, KeyError, TypeError):
                    continue
    return 0


class Tracer:
    """One writer per run, shared by graph nodes in this process.

    The bounded cache retains only the last payload per content field. A new
    process can append without loading the history; it simply starts new content
    references. Separate processes must not write to the same run concurrently.
    """
    _writers: OrderedDict = OrderedDict()
    _lock = RLock()

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / TRACE_NAME

    def emit(self, node: str, event: str, *, task_id: int | None = None,
             round_number: int | None = None, iteration_number: int | None = None,
             step: int | None = None, choice: int | None = None, **detail: object) -> dict:
        with self._lock:
            key = self.path.resolve()
            signature = _signature(self.path)
            writer = self._writers.get(key)
            if writer is None or writer.signature != signature:
                writer = _Writer(sequence=_last_sequence(self.path), signature=signature)
                self._writers[key] = writer
            sequence = writer.sequence + 1
            updates = {}
            for name in _CONTENT_FIELDS:
                if name not in detail or not detail[name]:
                    continue
                prior = writer.contents.get(name)
                if prior and prior[0] == detail[name]:
                    detail.pop(name)
                    detail[name + '_ref'] = prior[1]
                else:
                    updates[name] = (detail[name], sequence)
            record = dict(seq=sequence, timestamp=datetime.now(timezone.utc).isoformat(),
                          node=node, event=event, task_id=task_id,
                          round=round_number, iteration=iteration_number,
                          step=step, choice=choice, detail=detail)
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False, default=str,
                                        separators=(',', ':')) + '\n')
            writer.sequence, writer.signature = sequence, _signature(self.path)
            writer.contents.update(updates)
            self._writers.move_to_end(key)
            while len(self._writers) > 32:
                self._writers.popitem(last=False)
            return record

    def header(self, **fields: object) -> dict:
        return self.emit('run', 'start', **fields)

    def footer(self, **fields: object) -> dict:
        return self.emit('run', 'finish', **fields)


def read_trace(run_dir: Path, *, resolve_refs: bool = False) -> list[dict]:
    path = Path(run_dir) / TRACE_NAME
    if not path.exists():
        return []
    with path.open(encoding='utf-8') as stream:
        events = [json.loads(line) for line in stream if line.strip()]
    if resolve_refs:
        by_sequence = {}
        for event in events:
            detail = event['detail']
            for name in _CONTENT_FIELDS:
                reference = detail.get(name + '_ref')
                if reference is not None:
                    detail[name] = by_sequence[reference]['detail'][name]
                    del detail[name + '_ref']
            by_sequence[event['seq']] = event
    return events
