# langgraph-based-ACSLAgent

基于 LangGraph 的 C 程序 ACSL 规约生成与验证工具。程序使用 veri-clang 划分函数和循环子任务，
让 LLM 为每个子任务生成多个候选，调用 Frama-C WP 评分并选优，最后进行整体验证和反例引导修复。


## 1. 准备系统并下载源码


下载项目源码：

```bash
git clone https://github.com/YxCai-SZU/langgraph-based-ACSLAgent.git
cd langgraph-based-ACSLAgent
```



## 2. 安装opam


```bash
sudo apt install opam
```


## 3. 安装Conda环境


```bash
conda env create -f environment.yml
conda activate acslagent
```


## 4. 安装 Frama-C


```bash
opam --version
opam init --bare --disable-shell-hook -y
opam switch create default ocaml-base-compiler.4.14.2 --jobs=2 -y
opam install --switch=default --jobs=2 -y \
  frama-c.33.0 why3.1.8.2 alt-ergo-free.2.4.3 z3.4.13.3
eval "$(opam env --switch=default --set-switch)"
hash -r
```

## 5. 安装 veri-clang

在项目根目录执行：

```bash
python scripts/install.py
```


## 6. 配置模型并运行示例

在 `config/models_config.yaml` 中完成模型和API配置


运行单个 C 文件：

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

## 7. 主要参数和流程

| 参数 | 含义 |
| --- | --- |
| `--retries N` | 每个子任务额外重新生成候选批次的预算 |
| `--repairs M` | 每个子任务基于反例替换当前整组规约的预算|
| `--candidates K` | 每次向模型请求的候选数，默认 5 |
| `--fmparallel P` | 一批候选同时启动的 Frama-C 进程上限，默认 5 |
| `--score-drop-threshold 0.10` | 修复后分数相对同一前序上下文最高分下降至少 10 个百分点时触发 retry |
| `--wp-cache none` | 关闭证明缓存，用于对照实验 |
| `-o PATH` | 运行结果保存目录 |

任务按内层循环、外层循环、函数契约的依赖顺序处理。每批候选按当前子任务及前序任务的
WP `proved/total` 评分；首次收到有效的 100% 局部结果即选中并取消其余候选验证。
没有满分时完成本批评估并选择最高分。全部任务获得候选后，再进行全文件验证。

全文件失败时按目标归属进入 fix。LLM 找到反例时消耗一次 repairs 并给出一个替代候选；
找不到反例时消耗一次 retries 重新生成候选。LLM 反例是其分析结果，不代表求解器已验证该反例。
每个子任务的预算独立记录；局部满分不代替全文件 PASS。



## 8. 查看结果

每个运行目录保存 `summary.json`、`task_ledger.json`、`trace.jsonl` 和生成的 C 文件。
`tasks/` 保存每个子任务的模型请求/回复、候选、WP 日志和评分；`full_checks/` 保存完整验证记录。
可以用下面的命令查看某个实际运行目录：

```bash
acslagent trace runs/实际运行目录
acslagent report runs/实际运行目录
```



