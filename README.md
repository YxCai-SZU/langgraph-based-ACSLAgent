# langgraph-based-ACSLAgent

A LangGraph-based tool for generating and verifying ACSL specifications for C programs.

The program uses `veri-clang` to identify function and loop subtasks, asks an LLM to generate multiple candidates for each subtask, evaluates the candidates with Frama-C WP, selects the best candidate, and finally performs full-file verification with counterexample-guided repair.

## 1. Download the Source Code

These commands should be executed in an Ubuntu or WSL Bash terminal.

```bash
git clone https://github.com/YxCai-SZU/langgraph-based-ACSLAgent.git
cd langgraph-based-ACSLAgent
```
## 2. Install opam and System Dependencies
Install Ubuntu system packages first:
```bash
sudo apt update
sudo apt install -y \
  build-essential \
  bubblewrap \
  ca-certificates \
  curl \
  file \
  git \
  graphviz \
  libgmp-dev \
  libstdc++6 \
  m4 \
  opam \
  pkg-config \
  python3 \
  unzip \
  xz-utils \
  zlib1g-dev
```
Check the installed opam version:
```bash
opam --version
```
The project has been tested with opam 2.1 or newer.

## 3. Install the Conda Environment
```bash
conda env create -f environment.yml
conda activate acslagent
```
The validated Python environment uses:
| Component | Version |
| --- | --- |
| Python | 3.12.16 |
| pip | 26.2.2 |
| LangGraph | 1.2.19 |
| langchain-core | 1.7.4 |
| langgraph-checkpoint-sqlite | 3.1.3 |
| OpenAI Python SDK | 1.82.1 |
| PyYAML | 6.0.3 |

## 4. Install Frama-C

The validated Frama-C baseline for this project is:

| Component | Version |
| --- | --- |
| Frama-C | **33.0 (Arsenic)** |
| OCaml | 4.14.2 |
| Why3 | 1.8.2 |
| Alt-Ergo | alt-ergo-free 2.4.3 |
| Z3 | 4.13.3 |

Create the default opam switch and install the fixed versions:

```bash
opam init --bare --disable-shell-hook -y

opam switch create default \
  ocaml-base-compiler.4.14.2 \
  --jobs=2 \
  -y

opam install \
  --switch=default \
  --jobs=2 \
  -y \
  frama-c.33.0 \
  why3.1.8.2 \
  alt-ergo-free.2.4.3 \
  z3.4.13.3
```
## 5. Install veri-clang

Activate the Conda and opam environments, then run the installer from the project root:

```bash
conda activate acslagent
eval "$(opam env --switch=default --set-switch)"
python scripts/install.py
```

The installation script:
- prepares the LLVM/Clang 12.0.1 runtime required by veri-clang;
- verifies the downloaded files;
- builds the project-local veri-clang fix;
- writes the local analyzer configuration;
- registers this checkout as the acslagent command.

## 6. Configure the Model

Choose a model from:
```plain text
  config/models_config.yaml
```
The required API-key environment variable is defined by the selected model's api_key_env field.

For example, the configured Alibaba Cloud models use:

```bash
export aliyun_API_KEY='your-api-key'
```

Run a single C file:

```bash
acslagent run examples/single/count.c \
  -m deepseek-v4.1-flash \
  --retries 5 \
  --repairs 5 \
  --candidates 5 \
  --fmparallel 5 \
  -o runs
```

Run all C files in a directory:

```bash
acslagent run -i 'Input_dir' \
  -m deepseek-v4.1-flash \
  --retries 5 \
  --repairs 5 \
  --candidates 5 \
  --fmparallel 5 \
  -o runs
```
## 7. Main Parameters and Workflow

| Parameter | Meaning |
| --- | --- |
| `--retries N` | Additional candidate-generation batches available to each subtask |
| `--repairs M` | Counterexample-guided replacement budget for each subtask |
| `--candidates K` | Number of candidates requested from the model in each generation batch; default is 5 |
| `--fmparallel P` | Maximum number of Frama-C processes running concurrently for one candidate batch; default is 5 |
| `--score-drop-threshold 0.10` | Retry when a repair decreases the score by at least 10 percentage points |
| `--wp-cache none` | Disable the Frama-C proof cache for comparison experiments |
| `-o PATH` | Directory used to save run results |

The workflow processes subtasks in dependency order:
1. Inner loops.
2. Outer loops.
3. Function contracts.
4. Full-file verification.
5. Counterexample-guided repair when the full verification fails.
Each candidate is evaluated using the current subtask and its predecessor subtasks.

If a candidate achieves a valid local score of 100%, it is selected immediately and the remaining candidate verifications are cancelled.

If no candidate achieves 100%, all available candidates are evaluated and the candidate with the highest score is selected.

The --retries and --repairs budgets are tracked independently for every subtask.

If the LLM finds a counterexample, one repair budget is consumed and the current subtask specification is replaced.

If the LLM cannot find a counterexample while Frama-C still reports an unproved goal, one retry budget is consumed and a new candidate batch is generated.

## 8. View the Results

Each run directory contains the generated C files, summaries, task budgets, candidate evaluations, and Frama-C logs.

Typical output files include:

```plain text
runs/<run-name>/
  summary.json
  task_ledger.json
  trace.jsonl
  final.c
  tasks/
  full_checks/
```

View the trace:

```bash
acslagent trace runs/<run-name>
```

Generate a Markdown report:

```bash
acslagent report runs/<run-name>
```

The trace.jsonl file records model requests, candidate generation, Frama-C commands, WP scores, budget changes, repairs, retries, and final verification results.
