"""Shared data types.

The field names follow the original pickle keys (CurTaskID, Round, Iteration,
TaskList) so the two codebases can be read side by side.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Literal, TypedDict


class TaskKind(IntEnum):
    """Values are the original TaskList codes and must not be renumbered."""

    CONTRACT = 1
    INNER_LOOP = 2
    OUTER_LOOP = 3


Mode = Literal["loopinv_gen", "loopinv_infill", "contract_gen", "contract_infill"]


class ProgramTask(TypedDict):
    task_id: int
    kind: int
    function_name: str
    source_line: int


class FunctionLocation(TypedDict):
    function_name: str
    source_file: str
    function_line: int
    loop_lines: list[str]


class PromptMessage(TypedDict):
    role: str
    content: str


Verdict = Literal["pass", "fail", "invalid", "unknown"]


@dataclass(frozen=True)
class FramacOutcome:
    """One Frama-C run.

    `proved` and `total` are the two numbers Frama-C prints in
    "[wp] Proved goals: proved / total". A goal that times out or is
    unknown counts towards `total` but not towards `proved`.
    """

    verdict: Verdict
    proved: int
    total: int
    timed_out_requires: tuple[str, ...] = ()
    command: tuple[str, ...] = ()
    stdout_path: str = ""
    stderr_path: str = ""
    seconds: float = 0.0
    process_timed_out: bool = False

    @property
    def passed(self) -> bool:
        """The FB4C control-flow pass rule, including requires timeouts."""
        return self.upstream_passed

    @property
    def strict_passed(self) -> bool:
        """Diagnostic only: does not change FB4C control flow."""
        return self.verdict == "pass" and self.proved == self.total

    @property
    def result_type(self) -> str:
        if self.verdict in ("pass", "fail"):
            return f"{self.verdict.title()}_{self.proved}_{self.total}"
        return "Invalid" if self.verdict == "invalid" else "UK"

    @property
    def upstream_passed(self) -> bool:
        """Original FB4C success rule; also used by passed for graph/CLI decisions."""
        return (
            self.verdict == "pass"
            and self.proved + len(self.timed_out_requires) == self.total
        )

    def as_dict(self, *, compact: bool = False) -> dict:
        result = {
            "verdict": self.verdict,
            "proved": self.proved,
            "total": self.total,
            "passed": self.passed,
            "strict_passed": self.strict_passed,
            "result_type": self.result_type,
            "upstream_passed": self.upstream_passed,
            "timed_out_requires": list(self.timed_out_requires),
            "command": list(self.command),
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "seconds": self.seconds,
            "process_timed_out": self.process_timed_out,
        }
        if compact:
            for key in ('command', 'stdout_path', 'stderr_path', 'seconds', 'upstream_passed'):
                result.pop(key)
        return result


class RunState(TypedDict, total=False):
    # settings, fixed for the whole run
    source_path: str
    source_name: str
    run_dir: str
    model: str
    model_config: str
    analyzer_config: str
    frama_c: str
    max_retries: int
    max_repairs: int | None
    max_rounds: int
    api_retries: int
    enable_mutation: bool

    # analyze and plan nodes
    spec_locations_text: str
    call_graph: list[dict]
    source_text: str
    tasks: list[ProgramTask]
    marked_lines: list[str]

    # algorithm state; mirrors CurTaskID / Round / Iteration / pickle[str(id)]
    current_task_id: int
    round_number: int
    iteration_number: int
    specs: dict[str, list[str]]

    # generate node
    mode: Mode
    reply: str
    generate_error: str
    token_usage: int

    # embed node
    attempt_clauses: list[str]
    candidate: str
    assume_behavior: bool

    # verify / decide / repair nodes
    repair_count: int
    outcome: FramacOutcome
    verify_counts: dict[str, int]
    verify_seconds: float

    # routing and terminal state
    stage: str
    stop_reason: str
    step_number: int
    status: int
    question: str
    replies: list[str]
    clean_replies: list[str]
    reply_files: list[str]
    merged_file: str
    probe_clauses: list[str]
    probe_queue: list[list[str]]
    probe_index: int
    probe_accepted: list[str]
    probe_phase: str
    choice_index: int
    best_candidate: str
    max_pass_goal: int
    last_completed_task_id: int
    final_outcome: FramacOutcome
