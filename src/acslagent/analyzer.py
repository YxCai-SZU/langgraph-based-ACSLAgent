"""Step 1: build the call graph with veri-clang.

The original runs `veri-clang -O0 -c file.c -o tmp.o` and reads the
`SpecLoC.txt` it drops next to the object file. That file *is* the call graph:
one line per function, in bottom-up order, naming the functions and loops that
need specifications.

    function_name===file:function_line===loop_line,loop_line

This module runs the analyzer and returns SpecLoC.txt verbatim. It never
interprets a failure as a verification result.

Reference: LLM4Veri/mark.py:RunClangCommand, GetSpecLoc.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from acslagent.domain import FunctionLocation
from acslagent.plan import DATASET_MARKERS, parse_spec_locations, preprocess_source


class AnalyzerError(RuntimeError):
    """veri-clang could not run or did not produce a call graph."""


@dataclass(frozen=True)
class AnalyzerConfig:
    """Where the vendored veri-clang lives.

    Relative paths resolve against the configuration file's directory.
    Absolute paths remain supported. On Linux or inside WSL the analyzer is run
    directly. Only when this Python process runs on Windows is the command
    forwarded through `wsl.exe -d <distribution>`.
    """

    analyzer_root: str
    compiler_directory: str
    library_path: str
    distribution: str = "Ubuntu"
    timeout_seconds: int = 120
    tool_sha256: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "AnalyzerConfig":
        path = Path(path)
        if not path.is_file():
            raise AnalyzerError(f"{path} is missing; run scripts/setup_native.py first")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        for key in ('analyzer_root', 'compiler_directory', 'library_path'):
            value = raw.get(key)
            if value and not value.startswith('/') and not Path(value).is_absolute():
                resolved = (path.resolve().parent/value).resolve()
                raw[key] = (_wsl_path(resolved, raw.get('distribution', 'Ubuntu'))
                            if sys.platform == 'win32' else str(resolved))
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def via_wsl(self) -> bool:
        return sys.platform == "win32"


def _environment(config: AnalyzerConfig) -> list[str]:
    # Unset the optional veri-clang instrumentation switches: the original
    # analyses unoptimised, uninstrumented code.
    cleared = ["VERI_DONT_OPTIMIZE", "VERI_USE_ASAN", "VERI_USE_MSAN", "VERI_HARDEN", "VERI_NO_BUILTIN"]
    command = ["env"]
    for name in cleared:
        command += ["-u", name]
    command += [
        f"PATH={config.compiler_directory}:/usr/local/bin:/usr/bin:/bin",
        "VERI_CC=clang",
        f"VERI_LIB_PATH={config.analyzer_root}",
        f"LD_LIBRARY_PATH={config.library_path}",
        "VERI_QUIET=1",
    ]
    return command


def run_veri_clang(
    source: Path,
    work_dir: Path,
    config: AnalyzerConfig,
    *,
    executable: str = "veri-clang",
) -> tuple[str, dict]:
    """Run the analyzer in `work_dir` and return SpecLoC.txt plus a report.

    The report is what the trace stores: command, exit code, duration and the
    hashes of the inputs and outputs, so a run can be reproduced exactly.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    analyzer = f"{config.analyzer_root}/bin/{executable}"
    if not config.via_wsl:
        _check_paths(config, analyzer)

    analysis_source = source.resolve()
    transformed = any(marker in str(source) for marker in DATASET_MARKERS)
    saved_preprocessed = None
    if transformed:
        # Match mark.py's <source>_m.c in the original directory, so local
        # includes resolve identically. Restore a pre-existing file afterwards.
        analysis_source = Path(str(analysis_source).replace('.c', '_m.c'))
        saved_preprocessed = analysis_source.read_bytes() if analysis_source.exists() else None
        analysis_source.write_text(preprocess_source(source.read_text(encoding='utf-8'), str(source)),
                                   encoding='utf-8', newline='')
    argument_source = _wsl_path(analysis_source, config.distribution) if config.via_wsl else str(analysis_source)
    arguments = [
        "timeout",
        "--kill-after=2s",
        f"{config.timeout_seconds}s",
        analyzer,
        "-O0",
        "-c",
        argument_source,
        "-o",
        "tmp.o",
    ]
    # The analyzer reads the original source location to preserve local include
    # resolution. A copy is retained for audit; SpecLoC and tmp.o live in work_dir.
    shutil.copy2(analysis_source, work_dir / analysis_source.name)
    # A rerun must never reuse a stale analyzer result.
    (work_dir / 'SpecLoC.txt').unlink(missing_ok=True)
    (work_dir / 'tmp.o').unlink(missing_ok=True)

    if config.via_wsl:
        command = [
            "wsl.exe",
            "-d",
            config.distribution,
            "--cd",
            _wsl_path(work_dir, config.distribution),
            "--exec",
            *_environment(config),
            *arguments,
        ]
    else:
        command = [*_environment(config), *arguments]

    started = time.monotonic()
    # Bytes, not text: wsl.exe prints its own errors in UTF-16, and a
    # strict UTF-8 decode of that used to hide the real message.
    try:
        completed = subprocess.run(command, capture_output=True, cwd=work_dir)
    finally:
        if transformed:
            if saved_preprocessed is None:
                analysis_source.unlink(missing_ok=True)
            else:
                analysis_source.write_bytes(saved_preprocessed)
    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    (work_dir / "veri-clang.stdout.txt").write_text(stdout, encoding="utf-8")
    (work_dir / "veri-clang.stderr.txt").write_text(stderr, encoding="utf-8")

    spec_file = work_dir / "SpecLoC.txt"
    report = {
        "command": command,
        "returncode": completed.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "spec_locations_file": str(spec_file),
        "stdout": str(work_dir / "veri-clang.stdout.txt"),
        "stderr": str(work_dir / "veri-clang.stderr.txt"),
    }
    if completed.returncode != 0:
        lines = (stderr or stdout).strip().splitlines()
        detail = [line for line in lines if re.search(r'warning:|error:|Running pass|Segmentation|isIntrinsic', line)][:6]
        detail = detail or lines[-5:]
        raise AnalyzerError(
            f"veri-clang exited with {completed.returncode}: "
            + " | ".join(detail)
            + f" (full log: {work_dir / 'veri-clang.stderr.txt'})"
        )
    if not spec_file.exists():
        raise AnalyzerError(
            "veri-clang produced no SpecLoC.txt; the call graph is unavailable"
        )
    return spec_file.read_text(encoding="utf-8", errors="replace"), report


def _check_paths(config: AnalyzerConfig, analyzer: str) -> None:
    """Fail early, with a useful message, when native.json is stale.

    native.json stores absolute paths. Copying the project to another machine
    or directory leaves them pointing at the old location.
    """
    for label, path in (
        ("veri-clang", analyzer),
        ("clang directory", config.compiler_directory),
        ("library path", config.library_path),
    ):
        if not Path(path).exists():
            raise AnalyzerError(
                f"{label} not found at {path}; .tools/native.json was written on "
                "another machine or directory. Run `python scripts/setup_native.py` here."
            )


def _decode(data: bytes) -> str:
    """Decode subprocess output that may be UTF-8 or (from wsl.exe) UTF-16."""
    for encoding in ("utf-8", "utf-16-le"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _wsl_path(path: Path, distribution: str) -> str:
    completed = subprocess.run(
        ["wsl.exe", "-d", distribution, "--exec", "wslpath", "-a", str(Path(path).resolve())],
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AnalyzerError(
            f"cannot translate {path} to a WSL path: {_decode(completed.stderr).strip()}"
        )
    return _decode(completed.stdout).strip()


def call_graph(records: list[FunctionLocation]) -> list[dict]:
    """Group SpecLoC records into one entry per function, bottom-up."""
    return [
        {
            "function": record["function_name"],
            "source_file": record["source_file"],
            "source_line": record["function_line"],
            "loops": record["loop_lines"],
        }
        for record in records
    ]


def analyze(source: Path, work_dir: Path, config: AnalyzerConfig) -> tuple[str, list[FunctionLocation], dict]:
    """Run the analyzer and parse its call graph."""
    text, report = run_veri_clang(source, work_dir, config)
    records = parse_spec_locations(text)
    if not records:
        raise AnalyzerError("SpecLoC.txt is empty; nothing to specify")
    return text, records, report
