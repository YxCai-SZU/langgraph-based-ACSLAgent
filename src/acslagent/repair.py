"""FB4C Invalid/Fail repair, including its early-break behavior.

The caller owns clause/state synchronization, as in llmveri.py:557-643.
The diagnostic log is retained for audit rather than deleted after repair.
"""
from dataclasses import dataclass, field
from pathlib import Path
from acslagent._simplify_fb4c import simplify_acsl


@dataclass
class Removal:
    reason: str
    clauses: list[str] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    changed: bool = False
    note: str = ""
    stop: bool = False


def _delete_line(path: Path, number: int) -> str:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not 0 < number <= len(lines):
        return ""
    removed = lines.pop(number - 1).strip()
    path.write_text("".join(lines), encoding="utf-8", newline="")
    return removed


def repair_invalid(stdout_path: str, c_file: Path, generated: list[str], remove_prior) -> Removal:
    """Direct port: FB4C stops at the first unrelated diagnostic line.

    A leading '[kernel] Parsing ...' also stops repair. Do not silently change
    this upstream behavior into a scan of all diagnostics.
    """
    lines = Path(stdout_path).read_text(encoding="utf-8").splitlines(keepends=True)
    target = str(c_file)
    removal = Removal("invalid")
    for i in range(len(lines)):
        if ("[kernel:annot-error]" in lines[i] and target in lines[i]
                or "[kernel]" in lines[i] and target in lines[i]
                and ("syntax error:" in lines[i+1] or "Invalid symbol:" in lines[i+1])):
            if "complete or disjoint behaviors clause in a contract with empty list of behavior" in lines[i+1]:
                if "disjoint behaviors;" in generated:
                    generated.remove("")
                elif "complete behaviors;" in generated:
                    generated.remove("")
                break
            _, right = lines[i].split(target + ":", 1)
            number = int(right.split(":", 1)[0])
            if not generated:
                break
            removed = _delete_line(c_file, number)
            removal.clauses.append(removed)
            removal.lines.append(number)
            if removed not in generated:
                remove_prior(removed)
            else:
                generated.remove(removed)
            break
        elif "[wp] " in lines[i] and target in lines[i] and "User Error:":
            # The constant truthy string is the actual upstream condition.
            _, right = lines[i].split(target + ":", 1)
            number = int(right.split(":", 1)[0])
            if not generated:
                break
            removed = _delete_line(c_file, number)
            removal.clauses.append(removed)
            removal.lines.append(number)
            if removed in generated:
                generated.remove(removed)
        else:
            removal.stop = True
            removal.note = "FB4C ERROR_TAG: first unrelated diagnostic ends repair"
            break
        if i == len(lines)-1:
            removal.note = "I_REACH_MAX"
    removal.changed = bool(removal.clauses)
    removal.stop = removal.stop or not generated
    return removal


def repair_failed(stdout_path: str, c_file: Path) -> Removal:
    before = c_file.read_text(encoding="utf-8")
    removed = simplify_acsl(stdout_path, str(c_file))
    return Removal("fail", clauses=removed if isinstance(removed, list) else [],
                   changed=before != c_file.read_text(encoding="utf-8"))


def remove_from_inner_loop(specs: dict, tasks: list, current: int, removed: str) -> None:
    """baselib.remove_from_inner_loop with its pickle represented by graph state."""
    previous = current - 1
    while previous >= 0:
        if tasks[previous-1]["kind"] == 2:
            if removed in specs[str(previous)]:
                specs[str(previous)].remove(removed)
                break
            if previous == 0:
                raise RuntimeError("FB4C remove_from_inner_loop: no matching clause")
            previous -= 1
            continue
        break
