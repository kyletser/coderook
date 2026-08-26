# 可靠长任务实验指南

本文只描述可执行实验入口和证据口径，不预填任何成绩。所有真实模型实验固定使用当前
`legacy-anthropic / deepseek-v4-flash` 路由、`temperature=0`，报告绑定完整 Git commit 和数据集指纹。

## 1. 无模型检查

先生成套件计划，不会读取凭据或调用模型：

```bash
uv run python scripts/run_reliability_suite.py
```

五阶段强杀矩阵完全使用本地假模型和临时工作区：

```bash
uv run python scripts/run_crash_recovery_matrix.py \
  --iterations 100 \
  --output .benchmark-results/reliability/crash-recovery/report.json
```

五个窗口依次覆盖请求快照已持久化、工具调用尚未执行、受管 Shell 进程树、等待审批、工具结果已
持久化但 Turn 未结束。报告包含逐轮状态、Ledger 校验、孤儿调用、重复修改、孤儿高风险进程以及
恢复耗时 P50/P95。它验证恢复契约，不代表模型编码效果。

## 2. 真实模型套件

确认当前 route 和限额凭据后执行：

```bash
uv run python scripts/run_reliability_suite.py --execute
```

公开 Polyglot 切片需要额外传入只读数据集和完整 commit：

```bash
uv run python scripts/run_reliability_suite.py --execute \
  --polyglot-dataset /datasets/polyglot \
  --polyglot-commit <40-character-commit>
```

套件把 35 USD 分成六个独立阶段预算账本。Provider 在每次调用前预留 0.25 USD，余额不足时不发送
请求；响应必须返回 usage 才能结算。预算文件只记录 token、成本和匿名调用 ID，不保存 Prompt、响应
或凭据。阶段预算之和固定为 35 USD；某个阶段失败不会把剩余额度挪给其他阶段。

## 3. 对照实验

- `run_strategy_router_experiment.py`：50 个冻结任务，比较 rules-only、LLM-only、hybrid，保存三次
  重复的 Macro-F1、风险漏判、委派准确率、延迟和分类 token。
- `run_compaction_experiment.py`：12 个明确标注为合成的长会话，比较 `truncate`、`structured`、
  `adaptive_evidence`，保存事实保留、输入 token、回退和探针 pass@1。
- `run_multiagent_strategy_experiment.py`：10 个多文件任务和 10 个 quick 任务，比较 `single`、
  `always_delegate`、`routed`。实验 Harness 只自动应用已完成、已验证、digest 未漂移且 Write Claim
  合法的 Worker；多个补丁必须同基线且文件互斥，并作为一次批量应用进入临时主工作区。
- `run_benchmark.py --suite release`：完整 50 任务总评；套件执行两次并保留两份完整报告。
- `run_polyglot_benchmark.py`：每种语言按 `SHA256(instance_id + "coderook-v1")` 排序，取前三个
  verifier 可执行且原始基线失败的实例，不接受人工挑题。

## 4. 报告使用规则

原始逐任务 JSON 是唯一数据来源，Markdown 只是聚合视图。不得挑选最好一次，不得把合成长期会话
称为真实用户数据，也不得把不同模型或不同切片的公开榜单数字直接比较。若 adaptive 压缩发生质量
回退，报告必须保留 `fallback_to_original=true`；若 Worker 未经审查写入主工作区，结果必须计入
`unreviewed_workspace_writes`，不能以最终测试通过掩盖。
