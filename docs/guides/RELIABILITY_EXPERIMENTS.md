# 可靠长任务实验指南

本文只描述可执行实验入口和证据口径，不预填任何成绩。所有真实模型实验使用当前已经通过
Provider Doctor 的活动 Route，固定 `temperature=0`；模型、Route ID 和 wire format 从候选
配置读取，不再硬编码历史值。需要防止长实验期间模型漂移时，使用
`--expected-model <model>` 显式锁定。报告绑定完整 Git commit、Route digest 和数据集指纹。

## 1. 无模型检查

先生成套件计划，不会读取凭据或调用模型：

```bash
uv run python scripts/run_reliability_suite.py
```

默认计划是低成本 `pilot`：6 个压缩场景、3 个多文件任务、3 个 quick 任务和一次 10 题 quick
总评，三个付费阶段硬预算合计 8 USD。计划会记录当前工作树是否干净；只有干净提交才能执行。

三个子实验也支持零调用预检：

```bash
uv run python scripts/run_strategy_router_experiment.py --preflight
uv run python scripts/run_compaction_experiment.py --preflight
uv run python scripts/run_multiagent_strategy_experiment.py --preflight
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

确认 Pilot 计划、任务清单和限额后执行：

```bash
uv run python scripts/run_reliability_suite.py --execute
```

只有 Pilot 的报告完整、没有基础设施失败、各对照组实际执行且成本账本正常时，才生成完整计划：

```bash
uv run python scripts/run_reliability_suite.py --profile full
```

完整矩阵必须再显式增加 `--execute`，不会由 Pilot 自动升级。

公开 Polyglot 切片需要额外传入只读数据集和完整 commit：

```bash
uv run python scripts/run_reliability_suite.py --profile full --execute \
  --polyglot-dataset /datasets/polyglot \
  --polyglot-commit <40-character-commit>
```

每个已知美元价格的付费阶段使用独立预算账本。Provider 在每次调用前预留 0.25 USD，余额不足时
不发送请求；响应必须返回 usage 才能结算。预算文件只记录 token、成本和匿名调用 ID，不保存
Prompt、响应或凭据。某个阶段失败不会把剩余额度挪给其他阶段。新模型尚未进入美元价格表时，
实验默认拒绝启动；显式增加 `--allow-unknown-pricing` 后可继续记录 input/output token，但报告必须写明
美元硬预算未执行，且不得把未知成本记成 0。该开关适用于路由、压缩和多 Agent 对照脚本。

## 3. 对照实验

- `run_strategy_router_experiment.py`：默认只运行 12 题 rules-only 零成本诊断。其期望值是旧 benchmark
  category proxy，不等同于真实用户意图标签，因此 Macro-F1、风险漏判和委派标签只用于调试，**不作为
  Coding Agent 效果证据，也不进入默认套件**。`llm_only`/`hybrid` 必须显式传入
  `--allow-model-calls`。
- `run_compaction_experiment.py`：默认从声明的 Pilot ID 选择 6 个合成长会话，各策略运行一次；完整
  模式才使用 12 题两次重复。比较 `truncate`、`structured`、`adaptive_evidence`，保存事实召回、上下文
  token、实际模型输入、回退和来源。该结果是长上下文微实验，不冒充真实编码 pass@1。
- `run_multiagent_strategy_experiment.py`：Pilot 使用 3 个多文件任务和 3 个 quick 任务，完整模式使用
  10+10。比较 `single`、
  `always_delegate`、`routed`。实验 Harness 只自动应用已完成、已验证、digest 未漂移且 Write Claim
  合法的 Worker；`single` 组固定为无委派的直接执行，避免无会话 Headless Harness 因等待 Plan 审批
  而天然失败。多个补丁必须同基线且文件互斥，并作为一次批量应用进入临时主工作区。
- `run_benchmark.py --suite release`：完整 50 任务总评；套件执行两次并保留两份完整报告。
- `run_polyglot_benchmark.py`：每种语言按 `SHA256(instance_id + "coderook-v1")` 排序，取前三个
  verifier 可执行且原始基线失败的实例，不接受人工挑题。

## 4. 报告使用规则

原始逐任务 JSON 是唯一数据来源，Markdown 只是聚合视图。不得挑选最好一次，不得把合成长期会话
称为真实用户数据，也不得把不同模型或不同切片的公开榜单数字直接比较。若 adaptive 压缩发生质量
回退，报告必须保留 `fallback_to_original=true`；若 Worker 未经审查写入主工作区，结果必须计入
`unreviewed_workspace_writes`，不能以最终测试通过掩盖。

Pilot 只有同时满足以下条件才允许扩容：所有阶段都有完整原始报告；三种多 Agent 策略均实际完成同一
任务切片；压缩策略没有基础设施异常；模型 usage 与预算账本一致；报告中没有 `runtime_error` 主导的
伪失败。否则先修实验 Harness，不增加预算和重复次数。
