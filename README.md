# ACSLAgent LangGraph Reproduction

A clean, readable LangGraph reproduction of ACSLAgent-FB4C. Every step of the
original pipeline is one graph node. Every node writes what it received, what it
decided and what it produced to `trace.jsonl` before it returns.

## What this reproduces

ACSLAgent is a neuro-symbolic loop for synthesising ACSL specifications for C
programs. The LLM proposes candidates and Frama-C/WP acts as a deterministic
critic. The loop for one file is:

```
analyze (veri-clang)
    └─▶ plan tasks (bottom-up: loops first, contracts last)
            └─▶ dispatch ─▶ generate ─▶ embed ─▶ verify
                    ▲                        │
                    │                   decide ─▶ repair ─▶ verify …
                    │
                    └─ commit ─▶ dispatch  (next subtask)
                                │
                                └─▶ final_verify ─▶ END  (all goals proved)
                                        │
                                        └─▶ reset_round ─▶ dispatch
```

One deliberate change: the original `get_result_type` adds timed-out
`requires` goals to the proved count and reports `Pass_8_10` for a run where
only 8 of 10 goals were actually proved. This reproduction never credits a
timeout. A run passes only when proved == total. The `upstream_passed` field in
the trace shows what the original rule would have said.

## Setup

Install Miniconda or Anaconda, then in a CMD window inside this directory:

```bat
conda env create -f environment.yml
conda activate acslagent
python scripts\setup_native.py
```

`setup_native.py` downloads LLVM 12 into `.tools\llvm12` and writes
`.tools\native.json`.

Export your API key (name matches the `api_key_env` field in
`config/models_config.yaml` for the model you pick):

```bat
set "aliyun_API_KEY=sk-…"
```

## Run

```bat
acslagent run  examples\nested\count.c   -m qwen3.7-plus
acslagent plan examples\nested\count.c
acslagent trace runs\count001
acslagent report runs\count001
```

`run` is the full pipeline. `plan` stops after the call graph and subtask
decomposition; it does not need an API key. Both subcommands accept:

| flag | default | meaning |
| --- | --- | --- |
| `-m` / `--model` | — | ModelMap key in `config/models_config.yaml` |
| `--attempts` | 1 | independent LLM proposals per subtask |
| `--repairs` | 8 | delete-and-reverify cycles per attempt |
| `--rounds` | 5 | whole-file retries when the final check fails |
| `--frama-c` | `frama-c` | path to the frama-c executable |
| `-o` / `--output` | `runs/` | root directory for run folders |

`acslagent file.c -m model` without a subcommand is a shorthand for `run`.

## Files produced by one run

```
runs/count001/
  trace.jsonl        every node boundary: input, decision, output, timestamp
  final.c            annotated C file sent to the last Frama-C check
  report.md          human-readable walkthrough built from trace.jsonl
  summary.json       machine-readable totals
  manifest.json      (future: populated by the full runtime)
  work/
    analyze/         veri-clang output (SpecLoC.txt, stderr, stdout)
    candidates/      one .c file per attempt
    prompts/         one .json file per model request (full message list)
  framac/            one stdout+stderr pair per Frama-C invocation
```

## Reading a run

```bash
acslagent trace runs/count001           # one line per event
acslagent report runs/count001          # Markdown report

# filter with jq
jq 'select(.task_id == 2)' runs/count001/trace.jsonl
jq 'select(.node == "repair")' runs/count001/trace.jsonl
jq 'select(.node == "final_verify")' runs/count001/trace.jsonl
```

## Module map

| file | responsibility |
| --- | --- |
| `graph.py` | LangGraph nodes and edges; the whole pipeline |
| `trace.py` | append-only `trace.jsonl` writer |
| `plan.py` | parse `SpecLoC.txt`; insert task markers; number tasks |
| `render.py` | render the C file with the active hole and accepted clauses |
| `normalize.py` | turn model replies into ACSL clauses |
| `repair.py` | delete the clauses Frama-C could not prove |
| `framac.py` | run Frama-C once and classify its output |
| `prompts.py` | assemble the original few-shot messages |
| `llm.py` | one model request; records exactly what is sent |
| `analyzer.py` | drive `veri-clang` over WSL or native Linux |
| `model_config.py` | read `config/models_config.yaml` |
| `domain.py` | shared types: `RunState`, `FramacOutcome`, `TaskKind` |
| `cli.py` | `run`, `plan`, `trace`, `report` subcommands |
| `report.py` | Markdown report built entirely from `trace.jsonl` |


