# langgraph-based-ACSLAgent

基于 LangGraph 的 C 程序 ACSL 规约生成与验证工具。程序使用 veri-clang 划分函数和循环子任务，
让 LLM 为每个子任务生成多个候选，调用 Frama-C WP 评分并选优，最后进行整体验证和反例引导修复。

本文是面向首次使用者的安装初版。按下面顺序从源码安装；除第一节的 Windows 命令外，
所有命令均在 **Ubuntu / WSL 的 Bash 终端**中执行。

## 1. 准备系统并下载源码

需要 Linux x86_64（Intel/AMD 64 位）。Windows 用户使用 WSL 2；尚未安装 WSL 时，
先在管理员 PowerShell 中执行，并按系统提示完成重启和 Ubuntu 用户创建：

```powershell
wsl --install -d Ubuntu
```

打开 Ubuntu 终端，安装下载工具：

```bash
sudo apt update
sudo apt install -y git curl ca-certificates
```

下载项目源码：

```bash
git clone https://github.com/YxCai-SZU/langgraph-based-ACSLAgent.git
cd langgraph-based-ACSLAgent
```

也可以在仓库页面下载源码 ZIP，解压后进入包含 `README.md`、`environment.yml` 和 `scripts/` 的项目根目录。
后续步骤都从该目录执行。请保留 `vendor/`，安装 veri-clang 时需要其中的源码、工具和校验清单。

## 2. 兼容版本和依赖文件

当前验证基线如下。建议首次安装使用这些固定版本：

| 组件 | 版本 | 定义位置 |
| --- | --- | --- |
| Python | 3.12.16；项目要求 3.12.x | `environment.yml`、`pyproject.toml` |
| pip | 26.2.2 | `environment.yml` |
| LangGraph | 1.2.19 | `pyproject.toml`、`requirements.txt` |
| langchain-core | 1.7.4 | `pyproject.toml`、`requirements.txt` |
| langgraph-checkpoint-sqlite | 3.1.3 | `pyproject.toml`、`requirements.txt` |
| OpenAI Python SDK | 1.82.1 | `pyproject.toml`、`requirements.txt` |
| PyYAML | 6.0.3 | `pyproject.toml`、`requirements.txt` |
| 构建后端 hatchling | 1.33.0 | `pyproject.toml` |
| Frama-C | **33.0 (Arsenic)** | `pyproject.toml` 的原生版本表 |
| OCaml | 4.14.2 | `pyproject.toml` 的原生版本表 |
| Why3 | 1.8.2 | `pyproject.toml` 的原生版本表 |
| Alt-Ergo | alt-ergo-free 2.4.3 | `pyproject.toml` 的原生版本表 |
| Z3 | 4.13.3 | `pyproject.toml` 的原生版本表 |
| LLVM / Clang | 12.0.1 | `scripts/setup_native.py` 中的版本和 SHA-256 |
| veri-clang | 随仓库保存的版本及本地修复 | `vendor/acslagent/manifest.json`、`scripts/build_analyzer.py` |

依赖文件的作用：

- `environment.yml`：创建固定 Python/pip 版本的 Conda 环境，并调用 `requirements.txt` 安装项目。
- `requirements.txt`：统一固定 Python 直接和间接运行依赖（共 44 个包），并以 editable 方式安装当前源码。
- `pyproject.toml`：项目元数据、Python 版本要求、直接依赖、构建后端及已验证的原生工具版本。
  原生版本表用于记录基线和安装检查，不会让 pip 安装 Frama-C 或求解器；对应命令在第五节。

本机实际验证环境为 **WSL 2 / Ubuntu 26.04.1 / x86_64**。
Ubuntu 22.04、24.04 可按这些步骤准备，但尚未完成独立全新环境复测；原生 Windows、macOS 和 ARM
不属于当前自动安装路径。版本锁定和现有环境验证不等同于所有系统的兼容认证。

Frama-C **29.0 已发现与当前 WP 结果解析格式不兼容**，可能出现 `unknown / 0/0`。
33.0 是已验证基线，不代表更高版本一定不能用；更高版本需要先完成本项目回归验证。

## 3. 安装系统依赖

在项目根目录执行：

```bash
sudo apt update
sudo apt install -y build-essential bubblewrap ca-certificates curl dpkg file git \
  graphviz libgmp-dev libstdc++6 m4 opam pkg-config python3 unzip xz-utils zlib1g-dev
```

系统依赖包括 C/C++ 编译工具、opam、GMP、zlib、Graphviz、解压工具等。
后续 opam 安装和 Z3 编译可能需要较长时间，完成后无需在每次运行任务时重复安装。

## 4. 安装 Conda 和 Python 环境

如果还没有 Conda，使用 Miniforge 的 Linux x86_64 安装器。下载后先校验，再安装到用户目录：

```bash
curl -fL https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh \
  -o /tmp/Miniforge3-Linux-x86_64.sh
curl -fL https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh.sha256 \
  -o /tmp/Miniforge3-Linux-x86_64.sh.sha256
(cd /tmp && sha256sum -c Miniforge3-Linux-x86_64.sh.sha256) && \
  bash /tmp/Miniforge3-Linux-x86_64.sh -b -p "$HOME/miniforge3"
source "$HOME/miniforge3/etc/profile.d/conda.sh"
```

已有可用 Conda 时跳过安装器，直接创建项目环境：

```bash
conda env create -f environment.yml
conda activate acslagent
python --version
python -m pip check
```

预期 Python 为 `3.12.16`，`pip check` 输出 `No broken requirements found.`。
Conda 创建过程会调用 pip 安装 `requirements.txt`，同时注册 `acslagent` 命令。

如果已经有 Python 3.12 环境，可以在该环境中使用 `python -m pip install -r requirements.txt`；
不要只运行 `pip install -e .` 来代替锁定依赖安装。

## 5. 安装 Frama-C 和求解器

新机器初始化 opam，创建标准 `default` 环境，然后安装已验证的工具链版本。
建议使用 opam 2.1 或更新版本：

```bash
opam --version
opam init --bare --disable-shell-hook -y
opam switch create default ocaml-base-compiler.4.14.2 --jobs=2 -y
opam install --switch=default --jobs=2 -y \
  frama-c.33.0 why3.1.8.2 alt-ergo-free.2.4.3 z3.4.13.3
eval "$(opam env --switch=default --set-switch)"
hash -r
```

这会安装 Frama-C 33.0、Why3 1.8.2、Alt-Ergo 2.4.3 和 Z3 4.13.3。
请使用上述固定版本，不使用 Ubuntu 的 `apt install frama-c` 代替。

检查实际使用的程序：

```bash
command -v frama-c
frama-c -version
ocamlc -version
why3 --version
alt-ergo --version
z3 --version
frama-c -wp-list-provers
```

Frama-C 应显示 `33.0 (Arsenic)`，路径通常为 `~/.opam/default/bin/frama-c`，
求解器列表应包含 Alt-Ergo 和 Z3。**33.0 查询求解器用 `-wp-list-provers`，不使用旧版 `-wp-detect`。**

已有 `default` 环境的用户可跳过初始化和创建步骤。若旧 OCaml 版本不满足要求，
在没有旧验证任务运行时，执行以下命令在原环境安装基线版本：

```bash
opam install --switch=default --update-invariant --jobs=2 -y \
  ocaml-base-compiler.4.14.2 frama-c.33.0 why3.1.8.2 alt-ergo-free.2.4.3 z3.4.13.3
```

## 6. 安装 veri-clang 并检查项目

保持 `acslagent` Conda 环境和 opam 环境已激活，在项目根目录执行：

```bash
python scripts/install.py
acslagent --version
```

安装脚本会下载/复用固定版本的 LLVM 12.0.1 和兼容库，校验 SHA-256，构建本项目修复的
veri-clang 插件，并将本目录注册为 `acslagent` 的实际入口。
工具及配置保存在 `.tools/` 中。该脚本不会安装 Frama-C，也不会配置模型密钥；Python 依赖须先完成第四步。
首次执行需要网络下载和本地编译，之后会复用已经校验的文件。

在消耗模型额度前，先运行不调用 LLM 的安装检查：

```bash
python -m pip check
python scripts/check_verifier.py --output work/verifier_check
acslagent plan examples/single/count.c -o runs_plan
```

`check_verifier.py` 应正常退出，输出及 `work/verifier_check/check.json` 中的 `ready` 为 `true`。
它检查 Frama-C 基线版本、求解器发现、真实证明结果解析、循环规约，以及错误断言能否被拒绝。
`acslagent plan` 检查 veri-clang 能否正常分析源码并划分任务。
这些检查不需要 API 密钥，不会发起模型调用。若失败，先检查保存的诊断再运行批量实验。

## 7. 配置模型并运行示例

在 `config/models_config.yaml` 中选择模型。每个模型通过模板的 `api_key_env` 字段指定密钥环境变量；
请按实际服务商配置对应变量，变量名区分大小写。例如使用配置中的阿里云模板时：

```bash
export aliyun_API_KEY='替换为你的实际密钥'
```

程序只读取已设置的环境变量，不自动读取 `.env.local`。不要把实际密钥写入源码或提交到仓库。
确认模型在所选服务商处可用，并选择配置文件里对应的模型名称。

运行单个 C 文件，例如：

```bash
acslagent run examples/single/count.c \
  -m deepseek-v4.1-flash \
  --retries 5 --repairs 5 --candidates 5 --fmparallel 5 \
  -o runs
```

批量运行一个目录中的 C 文件：

```bash
acslagent run examples/Fram-c \
  -m deepseek-v4.1-flash \
  --retries 5 --repairs 5 --candidates 5 --fmparallel 5 \
  -o runs
```

每次打开新的终端，先激活环境，再运行项目：

```bash
source "$HOME/miniforge3/etc/profile.d/conda.sh"
conda activate acslagent
eval "$(opam env --switch=default --set-switch)"
cd langgraph-based-ACSLAgent
```

如果 Conda 安装在其他目录，替换第一行路径；项目目录也按实际位置进入。
纯目录复制不会自动改变 Python 环境中已有的 `acslagent` 入口，复制到最终位置后重新执行 `python scripts/install.py`。

## 8. 主要参数和流程

| 参数 | 含义 |
| --- | --- |
| `--retries N` | 每个子任务额外重新生成候选批次的预算；首次生成不扣除 |
| `--repairs M` | 每个子任务基于反例替换当前整组规约的预算，不随 retry 重置 |
| `--candidates K` | 每次向模型请求的候选数，默认 5；接受任意正数的实际返回候选 |
| `--fmparallel P` | 一批候选同时启动的 Frama-C 进程上限，默认 5；1 为串行 |
| `--score-drop-threshold 0.10` | 修复后分数相对同一前序上下文最高分下降至少 10 个百分点时触发 retry |
| `--wp-cache none` | 关闭证明缓存，用于对照实验 |
| `-o PATH` | 运行结果保存目录 |

任务按内层循环、外层循环、函数契约的依赖顺序处理。每批候选按当前子任务及前序任务的
WP `proved/total` 评分；首次收到有效的 100% 局部结果即选中并取消其余候选验证。
没有满分时完成本批评估并选择最高分。全部任务获得候选后，再进行全文件验证。

全文件失败时按目标归属进入 fix。LLM 找到反例时消耗一次 repairs 并给出一个替代候选；
找不到反例时消耗一次 retries 重新生成候选。LLM 反例是其分析结果，不代表求解器已验证该反例。
每个子任务的预算独立记录；局部满分不代替全文件 PASS。

所有 WP 调用均显式排除终止性目标：

```text
-wp-prop=-@terminates,-@decreases,-@variant
```

包括候选评估、局部基线、fix 和全文件验证。WP 超时或未知目标不会计为已证明。
LLM 请求默认每次总时限 60 秒，超时后免费重发，包含首次请求最多 10 次；不计入算法 retries/repairs。
LLM 协议解析失败会保存诊断并跳过当前样本。更多参数可用 `acslagent run --help` 查看。

## 9. 查看结果

每个运行目录保存 `summary.json`、`task_ledger.json`、`trace.jsonl` 和生成的 C 文件。
`tasks/` 保存每个子任务的模型请求/回复、候选、WP 日志和评分；`full_checks/` 保存完整验证记录。
可以用下面的命令查看某个实际运行目录：

```bash
acslagent trace runs/实际运行目录
acslagent report runs/实际运行目录
```

本仓库不包含开发测试目录。首次运行前，使用第六节的 `check_verifier.py` 和 `acslagent plan`
完成安装检查；它们不需要模型密钥，也不会消耗 API 额度。

## 10. 常见安装问题

- **Frama-C 返回 `unknown / 0/0`**：查看 `wp.stderr.txt`。若包含 `WP goal details disagree with summary`，
  先检查是否实际调用了 33.0；旧版本的输出格式可能与当前解析器不一致。
- **安装了 33.0，终端仍显示旧版本**：重新执行第五步的 `opam env` 和 `hash -r`，检查 `command -v frama-c`；
  同时检查是否使用了指向旧程序的 `--frama-c` 参数。
- **找不到 Alt-Ergo 或 Z3**：确认已按第五节安装依赖并激活相同 opam 环境，
  然后执行 `frama-c -wp-list-provers`。
- **`acslagent --version` 显示其他项目路径**：进入本项目，激活目标 Python 环境，重新运行 `python scripts/install.py`。
- **模型调用报鉴权或额度错误**：核对 `config/models_config.yaml` 中的模型、服务地址和 `api_key_env`。
- **并行验证占用较高**：降低 `--fmparallel`。每个 Frama-C 内部还会并行运行最多 2 个 WP 求解进程。

这是源码安装文档初版。当前已验证的是上述版本组合及现有 WSL 环境；
公开发布前仍需在独立干净系统上完成一次从下载到运行的安装复测。
