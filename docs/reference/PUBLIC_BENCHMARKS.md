# CodeRook 公开 Benchmark 复现指南

本文只描述与第三方任务集兼容的执行协议。发布结论仍以[发布评分卡](../status/RELEASE_SCORECARD.md)为准；
没有真实 artifact 时，不把“适配器通过单测”写成公开榜单成绩。

## 1. 证据层级

| 层级 | 能证明什么 | 当前状态 |
|---|---|---|
| 50 任务内建集 | CodeRook runner、verifier、预算、报告和失败分类契约 | 离线契约已实现；真实模型候选待跑 |
| Aider Polyglot | 六语言 Exercism 任务上的端到端 pass@1 | loader/runner/容器入口已实现；固定真实切片待跑 |
| SWE-bench Lite/Verified | 真实仓库 issue 的标准 patch 与官方 Docker 判分 | [首次五题 Pilot](../../benchmarks/results/2026-09-swebench-lite-pilot/README.md) 与[修复后开发回归](../../benchmarks/results/2026-09-swebench-lite-retest/README.md) 均官方通过 3/5、正常结束且通过 2/5；缺 action 错误消除，但收尾、传输与环境问题仍在；独立 20 题未运行，不是完整榜单成绩 |
| 基线/候选比较 | 任务、类别、失败聚类、成本与耗时回归 | 已实现；只在手动 benchmark/release 证据流程运行 |

“当前状态”必须与发布评分卡一致。公开数字至少绑定 CodeRook commit、数据集 commit、
route/model/wire format、温度、预算、有效样本数、超时/格式错误、成本和原始报告。

## 2. 内建 50 任务与回归比较

```bash
# 不调用模型
uv run python scripts/run_benchmark.py --validate
uv run python scripts/run_benchmark.py --suite quick --validate-baseline

# 会调用模型并产生费用
uv run python scripts/run_benchmark.py --suite nightly \
  --output .benchmark-results/candidate

# 不调用模型；默认拒绝任务回退、安全负例失败和显著 P95 成本/耗时上涨
uv run python scripts/compare_benchmark_reports.py \
  .benchmark-results/baseline/report.json \
  .benchmark-results/candidate/report.json \
  --output .benchmark-results/comparison
```

报告中的 ProcessSupervisor 指标包括 wall/CPU、峰值内存、进程数和采样完整性。`unknown` 表示平台或事件
没有提供可信测量，不能按零计算。

runner 在写出真实报告前调用 candidate contract：完整 Git SHA、route/model/wire/config 缺一即失败；
JSON 的 `task_contracts` 保存每项预算、允许工具、task/fixture hash，`run_config` 保存聚合的
task/fixture/budget/candidate 指纹。比较器默认要求这些合同与基线一致；只有明确使用
`--allow-contract-change` 才能生成非同题比较，且不应据此宣传效果提升。

手动触发的 `benchmark-release.yml` 用四个矩阵 job（两种 wire format 各重复两次）配合
`--report-only` 保存原始结果，不要求单次 100% 才产报告；
唯一 aggregate job 随后要求两个不同 wire format、每组两次、相同 commit/task/fixture/budget，并按评分卡
门禁总体 ≥80%、多文件 ≥75%、只读解释 ≥90%、安全负例 100%，同时限制两次 pass@1 差值 ≤10%。
聚合 JSON/Markdown 会列出每组均值/极值、成本/耗时和重复间不稳定任务；aggregate job 的退出码才是
release benchmark 的最终结论。

日常 `ci.yml` 不调用真实模型，也没有 cron/nightly benchmark。workflow 定义、secret 占位或离线
fixture 都不等于四份真实报告已经产生；当前缺口以发布评分卡为准。

四份原始报告下载后，workflow 会先按 `retrieval`、`editing`、`verification`、`permission`、
`budget`、`model_error` 六个效果域生成优化队列；没有失败时队列为空，不能凭空创建“优化成果”：

```bash
uv run python scripts/benchmark_optimization.py plan \
  --input-root .benchmark-results/raw \
  --output .benchmark-results/aggregate
```

实现优化后，必须用相同 suite/route/model/wire/config/task/fixture/budget 的前后报告记录实验；命令拒绝相同
commit、合同漂移和缺失任务。只有目标任务改善、无回归且比较门禁通过时才标记 `accepted`：

```bash
uv run python scripts/benchmark_optimization.py record \
  baseline/report.json candidate/report.json \
  --category editing \
  --hypothesis "PatchPlan hunk 选择降低非目标编辑" \
  --task multi_file_01 \
  --output .benchmark-results/optimization/multi_file_01
```

实验 JSON 保存前后完整 commit、两份报告 SHA-256、假设、目标任务、回归比较和客观结论。没有真实报告时，
不能把分类器或单测写成模型效果提升。

## 3. Aider Polyglot pass@1

CodeRook 复刻官方 harness 的以下输入契约：从 `.meta/config.json` 读取 solution/test/example 文件；只允许
修改 solution；按 `introduction.md → instructions.md → instructions.append.md` 拼接问题；使用官方语言测试
命令。数据集必须是无本地改动的精确 commit。

模型生成的代码可能有害，因此 runner 在宿主机上会直接拒绝，必须使用一次性容器。先固定两个源码版本：

```bash
git clone https://github.com/Aider-AI/polyglot-benchmark.git /tmp/polyglot-benchmark
git -C /tmp/polyglot-benchmark rev-parse HEAD
git rev-parse HEAD
```

构建包含 Python、C++、Go、Java、JavaScript 与 Rust 工具链的隔离镜像：

```bash
docker build -f benchmarks/public/Dockerfile \
  --build-arg CODEROOK_SOURCE_COMMIT=$(git rev-parse HEAD) \
  -t coderook-public-benchmark .
```

先跑 1–5 个固定 smoke，再扩大样本；`<POLYGLOT_COMMIT>` 必须替换为上一步完整 commit：

```bash
docker run --rm \
  -e CODEROOK_LLM_PROVIDER \
  -e CODEROOK_LLM_DEFAULT_MODEL \
  -e CODEROOK_LLM_BASE_URL \
  -e CODEROOK_LLM_API_KEY_ENV \
  -e CODEROOK_BENCHMARK_API_KEY \
  -v /tmp/polyglot-benchmark:/datasets/polyglot:ro \
  -v "$PWD/.benchmark-results/polyglot":/results \
  coderook-public-benchmark \
  python scripts/run_polyglot_benchmark.py \
    --dataset /datasets/polyglot \
    --expected-commit <POLYGLOT_COMMIT> \
    --fixed-slice-per-language 0 --language python --limit 5 \
    --output /results
```

API key 应使用仅供 benchmark 的限额凭据，并在运行后轮换。容器隔离宿主机，但不把模型输出视为可信；
不要挂载个人主目录、SSH agent、Docker socket 或写权限源码目录。JavaScript/C++ 任务还需要把固定 commit 的
Aider `benchmark/` 目录只读挂载，并传 `--aider-benchmark-dir`。

正式固定切片不按目录顺序取题：每种语言对 `instance_id + "coderook-v1"` 计算 SHA-256 并排序，
逐个运行原始 verifier，取前三个“verifier 可执行且未修改基线失败”的任务。`--limit` 不能与固定
切片同时使用。选择过程本身不调用模型。

可靠长任务的内部消融、五阶段强杀矩阵和统一 35 USD 预算入口见
[可靠长任务实验指南](../guides/RELIABILITY_EXPERIMENTS.md)。这些报告未实际生成前，README 和简历
不得引用计划阈值作为已取得成绩。

## 4. SWE-bench Lite/Verified

### Lite 固定切片与真实解题入口

以下入口使用真实 `AgentRunner`、独立官方实例容器及官方 `swebench==5.0.2` Harness。
选题按固定哈希跨仓库轮转：5 题 Pilot 与另外 20 题正式切片不重叠，不依据答案或通过率选题。
尚未执行的步骤或缺少官方 `report.json` 的实例不构成已通过的成绩。

```powershell
uv run --with pyarrow==21.0.0 python scripts/prepare_swebench_slice.py --output .benchmark-results/swebench-lite/frozen
docker build -f benchmarks/swebench/Dockerfile -t coderook-swebench:20260907 .
uv run python scripts/run_swebench_benchmark.py --root .benchmark-results/swebench-lite --stage controls
uv run python scripts/run_swebench_benchmark.py --root .benchmark-results/swebench-lite --stage preflight
uv run python scripts/run_swebench_benchmark.py --root .benchmark-results/swebench-lite --stage run --cohort pilot
uv run python scripts/run_swebench_benchmark.py --root .benchmark-results/swebench-lite --stage evaluate --cohort pilot
```

`controls` 用不改变实现的补丁和官方参考补丁检查判分；`preflight` 使用假模型检查真实工具链。
真实解题默认单 Agent、40 步、每题 900 秒，每题只尝试一次。当前入口需要已通过 Doctor 的
`qwen3.8-flash` 路由；可用 `--model` 显式指定另一已配置模型，但同一计分切片不得中途换模型。
解题费用由用户承担：记录实际 usage，价格未知时美元费用为未知，不声称存在美元硬限额。

控制器持有 Docker socket 和评测数据，只运行可信官方 Harness；Agent 容器不挂载二者，凭据仅
通过 stdin 进入运行时内存。Agent 只收到实例 ID、仓库、基线提交和 Issue，不收到标准补丁、
隐藏测试或 hints。结果目录保留完整评测原文和运行日志，不能未经隐私检查直接发布。

容器 `coderook-swebench-controller-20260907` 绑定一个实验根目录。完成并导出所需证据后可用
`docker rm -f coderook-swebench-controller-20260907` 关闭控制器；主机结果目录不会被删除。
实例容器在任务退出后自动删除，官方镜像保留为下载缓存。该入口不更改默认 CI。

运行时代码或 adapter 修改后必须重新构建实验镜像，并使用新的实验根目录重新执行 preflight；
不要覆盖旧 Pilot 的日志、预测补丁或执行契约。原 5 题复测属于开发回归，独立 20 题才属于后续
未参与调试的切片验证。`preflight` 的假模型故意省略 `Bash.run` 的 action，以检查真实调用兼容路径；
固定工具白名单使用大小写准确的 `Repository`。这些修正不改变已发布 Pilot 的分数。

### 从已有工作区导出补丁

官方判分输入是 JSON/JSONL，每条只有 `instance_id`、`model_name_or_path` 和 `model_patch`。CodeRook 导出器
要求每个工作区的 `HEAD` 精确等于实例 `base_commit`，并使用临时 Git index 把 tracked 与 untracked 变更
都写入 patch，而不污染真实 staging area。

准备一个由官方数据集 JSON/JSONL 对应的工作区目录：

```text
/workspaces/
└── django__django-11099/   # Git root，HEAD == base_commit，含 CodeRook 修改
```

导出并打印官方 harness 命令：

```bash
uv run python scripts/prepare_swebench_predictions.py \
  --instances swebench-smoke.jsonl \
  --workspaces /workspaces \
  --model-name coderook/<route-and-model> \
  --output .benchmark-results/swebench/predictions.jsonl \
  --dataset-name princeton-nlp/SWE-bench_Lite \
  --run-id coderook-smoke \
  --print-harness-command
```

随后在 SWE-bench 官方环境执行打印出的 `python -m swebench.harness.run_evaluation ...`。只有官方 harness
生成的 `results.json`、实例日志和 predictions 一起归档后，才能称为 SWE-bench smoke；固定小样本结果不能
冒充 Lite/Verified 完整榜单成绩。

## 5. 上游契约

- [SWE-bench 官方 Evaluation Guide](https://github.com/SWE-bench/SWE-bench/blob/main/docs/guides/evaluation.md)
- [SWE-bench 官方 Harness](https://github.com/SWE-bench/SWE-bench)
- [Aider 官方 Benchmark Harness](https://github.com/Aider-AI/aider/blob/main/benchmark/README.md)
- [Aider Polyglot 数据集](https://github.com/Aider-AI/polyglot-benchmark)

上游可能改变格式或命令。升级适配器时必须固定上游 commit、更新本页，并先跑格式 smoke 和一项真实官方
harness，而不是只修改链接。
