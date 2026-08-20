# CodeRook 面试讲解指南

目标不是把功能列表背一遍，而是让每个结论都能落到设计取舍、代码路径和验证证据。先说明这是个人
项目和当前 NO-GO 状态，再按“问题—机制—取舍—证据—缺口”展开。

## 3 分钟版本

**0:00–0:25，问题。** 我做的是本地 Coding Agent runtime，不是聊天壳。代码任务要跨多轮理解仓库、
执行受控修改、跑验证，并在客户端退出、进程中断或模型失败后恢复。

**0:25–1:00，架构。** 核心是持久 daemon，TUI 是主要前端，SDK/CLI 走 JSON-RPC，外部集成走
HTTP/SSE。daemon 持有 session、permission、worker 和 ledger；客户端重连后通过事件游标恢复，而不是
把状态藏在 UI 进程。

**1:00–1:35，Coding Agent 闭环。** loop 从 repo map/working set 取上下文，模型选择 File/Git/Bash/Run；
每次调用经过 Hook、六层权限、sandbox plan、执行和 receipt。编辑使用事务 PatchPlan 与 preimage hash，
用户修改过文件时拒绝覆盖。

**1:35–2:10，长任务与多 Agent。** task/subagent/fleet/worktree/workflow 共用预算、租约、写声明和事件账本；
文件 ledger 是操作真相，SQLite 是查询投影，启动时 reconcile 中断状态。

**2:10–2:40，证据。** 仓库有 50 个固定任务、1,000+ 自动测试、三平台 CI、公开 benchmark 适配器，
发布链生成 SBOM/checksums/provenance。但离线 verifier 通过不等于模型效果。

**2:40–3:00，诚实边界。** 当前评分卡仍 NO-GO：真实模型 pass@1、Windows 100 次强杀、active ruleset
和首次公开发行证据未完成；Ubuntu/macOS 各 100 次与干净发行矩阵已有报告。这说明我把“代码实现”和
“生产证据”分开管理。

## 10 分钟版本

1. **场景与非目标（1 分钟）**：本地优先、单用户可信边界；不做多租户 SaaS，不用 RAG/Agent 框架
   名称代替仓库理解和安全编辑。
2. **双进程与协议（1.5 分钟）**：解释 daemon 为什么拥有状态、首帧 token、事件 replay、Pydantic
   discriminated union、生成协议和 HTTP 兼容窗口。
3. **一次工具调用（2 分钟）**：从 model decision 到 schema、Hook、permission、sandbox、ProcessSupervisor、
   tool result、verification 和 receipt；重点讲 Windows degraded + ASK。
4. **持久化与恢复（1.5 分钟）**：ledger checksum、SQLite projection、强杀截断、reconcile、幂等；说明
   本机 smoke、Ubuntu/macOS 100/100 与 Windows 探针超时的区别。
5. **多 Agent 约束（1 分钟）**：何时拆任务，write claim 如何防冲突，budget/lease 如何终止失控 worker；
   承认小任务多 Agent 可能只增加成本。
6. **评测与发行（1.5 分钟）**：50 任务 verifier、Aider Polyglot/SWE-bench 适配、pass@1/成本/P95 比较，
   再讲评分卡 GO 才允许 tag、SBOM 与 OIDC attestation。
7. **复盘与下一步（1.5 分钟）**：讲 CI #31 暴露的平台假设，以及 TUI 拆分在收益递减点停止；最后指出
   当前最大缺口是外部证据，不是再加一个框架。

## 推荐现场演示

1. 打开 README 的确定性 TUI SVG，说明它来自真实 Textual 控件但不是在线模型成绩；
2. 本地启动 daemon/TUI，展示无模型状态、sandbox status、一次只读任务与 Turn Inspector；
3. 展示一个受审批的 diff/verification 事件和 receipt；
4. 运行 `scripts/check_release_contract.py --tag v0.1.0`，再说明 `--require-go` 为什么会失败；
5. 打开 `docs/RELEASE_SCORECARD.md`，主动指出尚未完成的真实模型和远端证据。

不在面试现场临时运行收费 benchmark、创建 tag、改评分卡为 GO 或展示个人 API key。

## 常见追问与证据

| 追问 | 回答抓手 | 证据入口 |
|---|---|---|
| 为什么不是单进程 CLI？ | 后台任务、客户端重启、统一权限、事件 replay 的收益与复杂度 | `core/app.py`、transport、runtime integration tests |
| 为什么双存储？ | ledger 易审计/恢复，SQLite 易查询；reconcile 明确主从关系 | session store、runtime reconcile tests |
| 如何防模型覆盖用户修改？ | preimage hash、PatchPlan、逐 hunk 决策、冲突拒绝 | patching engine、`test_apply_patch.py` |
| 沙箱真的安全吗？ | Linux/macOS 后端可强制；Windows 明确 degraded；域白名单无后端就拒绝 | `docs/THREAT_MODEL.md`、sandbox boundary script |
| 多 Agent 如何避免互相写坏？ | task scope、write claims、worktree、lease、预算和 ledger | subagent/fleet/workflow tests |
| 50 个任务是不是刷题自证？ | baseline 50/50 按预期失败只证明 verifier；真实效果必须固定模型和公开 harness | `docs/PUBLIC_BENCHMARKS.md` |
| 有什么失败案例？ | CI #31 的平台假设、CI #34/#35 的闭环与为何仍要求连续 3 次 | `postmortems/2026-08-19-cross-platform-ci.md` |
| 有什么工程优化？ | TUI 4,176 行拆分，未硬追 <500 行，95 个测试守护行为 | `postmortems/2026-08-17-tui-refactor.md` |

## 避免的回答

- “支持多 Agent，所以效果更好”——先给任务类型、基线、成本和冲突数据；当前没有就不下结论。
- “Windows 有沙箱”——当前只有进程治理和审批降级，不是文件/网络强制隔离。
- “通过 1,000+ 测试所以生产可用”——测试证明内部合同，不证明真实模型 pass@1 或公开安装成功。
- “所有功能都是自研”——模型、UI 框架、OS sandbox 和供应链工具必须明确归因。
