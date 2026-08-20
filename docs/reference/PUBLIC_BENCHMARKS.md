# CodeRook 公开 Benchmark 复现指南

本文只描述与第三方任务集兼容的执行协议。发布结论仍以[发布评分卡](../status/RELEASE_SCORECARD.md)为准；
没有真实 artifact 时，不把“适配器通过单测”写成公开榜单成绩。

## 1. 证据层级

| 层级 | 能证明什么 | 当前状态 |
|---|---|---|
| 50 任务内建集 | CodeRook runner、verifier、预算、报告和失败分类契约 | 离线契约已实现；真实模型候选待跑 |
| Aider Polyglot | 六语言 Exercism 任务上的端到端 pass@1 | loader/runner/容器入口已实现；固定真实切片待跑 |
| SWE-bench Lite/Verified | 真实仓库 issue 的标准 patch 与官方 Docker 判分 | prediction 导出已实现；官方 harness smoke 待跑 |
| 基线/候选比较 | 任务、类别、失败聚类、成本与耗时回归 | 已实现，可作为 CI/nightly 门禁 |

“当前状态”必须同时出现在开源补全计划和发布评分卡中。公开数字至少绑定 CodeRook commit、数据集
commit、route/model/wire format、温度、预算、有效样本数、超时/格式错误、成本和原始报告。

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

`benchmark-release.yml` 的四个矩阵 job 使用 `--report-only` 保存原始结果，不要求单次 100% 才产报告；
唯一 aggregate job 随后要求两个不同 wire format、每组两次、相同 commit/task/fixture/budget，并按评分卡
门禁总体 ≥80%、多文件 ≥75%、只读解释 ≥90%、安全负例 100%，同时限制两次 pass@1 差值 ≤10%。
聚合 JSON/Markdown 会列出每组均值/极值、成本/耗时和重复间不稳定任务；aggregate job 的退出码才是
release benchmark 的最终结论。

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
OS4-06 保持外部阻塞，不能把分类器或单测写成模型效果提升。

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
    --language python --limit 5 \
    --output /results
```

API key 应使用仅供 benchmark 的限额凭据，并在运行后轮换。容器隔离宿主机，但不把模型输出视为可信；
不要挂载个人主目录、SSH agent、Docker socket 或写权限源码目录。JavaScript/C++ 任务还需要把固定 commit 的
Aider `benchmark/` 目录只读挂载，并传 `--aider-benchmark-dir`。

## 4. SWE-bench Lite/Verified

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
