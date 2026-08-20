# CodeRook Spec-Driven R1–R9 完成度审计

更新时间：2026-08-04

本文以同目录 `SPECDRIVEN_SPEC.md` 为唯一需求基线。判定依据必须同时包含当前实现路径和能命中
对应行为的测试；仅有文件名、绿灯或设计意图不算完成证据。

## P0 基线与架构决策

| 要求 | 当前证据 | 判定 |
|---|---|---|
| 完整 CI、session/event/cancel characterization | `test_session_manager.py`、`test_s4_session_ipc.py`、`test_run_cancellation.py` | 已完成 |
| Runtime 路径、schema、seq、存储边界、导入、boot 与异步策略 | `ADR_RUNTIME_CONTRACT.md`、`runtime/migrations.py`、`runtime/store.py`、`runtime/service.py` | 已完成 |
| 空目录、历史 session、损坏尾部、孤立 tool call、running crash fixture | Runtime/Session/Recovery 单元与真实 daemon 集成测试 | 已完成 |

## R1：统一 Runtime Contract

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| Thread/Turn/Item/Event typed models 与 SQLite migration | `runtime/models.py`、`runtime/store.py`、`test_runtime_models.py`、`test_runtime_store.py` | 已完成 |
| `thread.create/list/get/update/archive` | `bus/commands.py`、Core handlers、`test_runtime_protocol_handlers.py` | 已完成 |
| `turn.start/get/list/interrupt/steer/items`、`runtime.capabilities` | 同上；正式 typed command 与 handler 均有直接测试 | 已完成 |
| 每 Thread transaction 内连续 seq；中断无半提交/空洞 | `test_concurrent_events_receive_contiguous_seq`、`test_transaction_interruption_rolls_back_all_runtime_state` | 已完成 |
| 旧 boot running Turn 恢复为 interrupted | `test_runtime_recovery.py` 真实 daemon 测试 | 已完成 |
| replay + live 无重复/无缺口 | IPC cursor 测试与 `test_sse_reconnect_resumes_after_durable_cursor` | 已完成 |
| session create/resume/fork/export/delete 兼容 | `test_s4_session_ipc.py`、`test_runtime_service.py` | 已完成 |
| terminal 前 tool call/result 一一配对 | RuntimeStore invariant、恢复测试、重复 result/孤立 result 测试 | 已完成 |

## R2：Mode、Authority、Trust 与 Sandbox Truth

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| 四维模型独立、Turn 启动时冻结 | `authority/models.py`、`test_runtime_service.py` | 已完成 |
| Plan action 裁剪、Operate+Ask 仍审批 | `test_authority_matrix.py`、真实 family catalog 测试 | 已完成 |
| Child authority 为 parent/profile/task scope 交集 | `test_child_authority_cannot_elevate_parent`、`test_child_authority_is_narrower_than_parent` | 已完成 |
| Windows 不伪称 sandbox/full access | `test_sandbox_capability.py`、TUI trust/sandbox 独立测试 | 已完成 |
| Header、Tab/Shift+Tab 与四类命令 | `test_tui_app.py` 的 mode/authority/trust/sandbox/header 回归 | 已完成 |

## R3：Tool Surface V2

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| ToolSpec 必需字段、canonical/memoized catalog | `tools/spec.py`、`tools/catalog.py`、`test_tool_catalog.py` | 已完成 |
| 默认 Bash/File/Git/Run/agent/memory/tasks/update_plan/tool_search | `tools/assembly.py`、`tools/families/*`、`test_control_tool_families.py` | 已完成 |
| 旧平铺名仅 internal/replay | File/Git/Run/Bash/Memory/Tasks family adapter 测试 | 已完成 |
| Plan 按 action 裁剪；未知 action/capability/caller fail closed | Catalog、authority、control family 测试 | 已完成 |
| 并行同路径写 claim 自动串行 | `test_loop_parallel.py` | 已完成 |
| deferred discovery、32 项上限、head hash 稳定 | `test_tool_discovery.py`、`test_tool_catalog.py` | 已完成 |
| soft/hard output、Artifact range/hash/missing/corrupt | `test_tool_discovery.py`、`test_artifact_store.py` | 已完成 |

## R4：Turn Loop、LSP 与 Context

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| idle/wall/bytes watchdog；两类 retry 分离 | `test_stream_watchdog.py` | 已完成 |
| 三次相同结果 stuck、重复 read、coalesce、取消边界 | `test_stuck_guard.py` | 已完成 |
| Python LSP 探测、失败/超时/超大降级、diagnostics transient | `test_lsp_diagnostics.py`、`test_working_set.py` | 已完成 |
| Working set、prefix source fingerprint 且无 prompt 正文 | `test_working_set.py`、`test_prefix_fingerprint.py` | 已完成 |
| 结构化 compaction、25% recent、incremental、质量门禁与 tool pair | `test_compactor.py` | 已完成 |
| 半截工具 JSON 断流不执行 | Anthropic retry + OpenAI-compatible strict parser 故障测试 | 已完成 |

## R5：Provider Route Registry 与 Doctor

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| Anthropic/OpenAI/两类 compatible/opencode-zen route | `llm/routes.py`、`test_provider_routes.py` | 已完成 |
| Provider/wire/model/base URL/credential ref 分离，实际 RouteReceipt 入 Turn | Route models/store/registry、`test_runtime_service.py` | 已完成 |
| 项目配置不能注入 provider/URL/key；非 loopback HTTP 拒绝 | Config/route security tests | 已完成 |
| keyring 优先、权限收紧 file fallback、各 route key 隔离 | `llm/credentials.py`、`test_llm_credentials.py`、provider command tests | 已完成 |
| configure/provider/model/doctor CLI 与 TUI | `test_configure_command.py`、`test_provider_commands.py`、`test_tui_app.py` | 已完成 |
| 401/TLS/transport/schema/model 分类且不泄密 | `test_provider_doctor.py`、Provider redaction tests | 已完成 |

## R6：Durable Task、Goal、Hooks 与 Skills

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| Task status/dependency/owner/attempt/acceptance/gate/artifact/timeline/provenance | `task/models.py`、`test_task_manager.py` | 已完成 |
| Goal 与 Task/Plan 分离，预算/耗时/约束/证据 | `goal/*`、`test_goal_service.py` | 已完成 |
| dependency 未满足不可 claim；重启可查 timeline/attempt/artifact | `test_task_manager.py` | 已完成 |
| Hooks V2 生命周期、timeout、blocking policy、scope、队列、进程树 | `hooks/*`、`test_hooks.py`、session lifecycle hook 测试 | 已完成 |
| Hook payload 有界脱敏 | `test_hook_payload_is_redacted_and_bounded`、输出边界测试 | 已完成 |
| Skills manifest/digest/source/install/trust、preview/audit/目录边界 | `skills/*`、CLI/TUI/manager 测试 | 已完成 |

## R7：Durable Subagent V2

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| agent start/status/peek/wait/cancel/followup | `subagent/agent.py`、`test_agent_tool.py` | 已完成 |
| WorkerRecord 全字段、结构化 SUMMARY/CHANGES/EVIDENCE/RISKS/BLOCKERS | `subagent/models.py`、worker result contract tests | 已完成 |
| write claim 冲突、独立 worktree owner/reviewer | `test_worker_registry.py` | 已完成 |
| 深度上限、共享根预算、lease、retry/backoff、restart recovery | Worker registry/tool tests | 已完成 |
| 停止 heartbeat 后 lease 到期 | `test_stopped_heartbeat_expires_worker_lease` | 已完成 |
| Parent 仅收 bounded event 与结构化摘要 | `test_agent_returns_structured_result_and_bounded_events` | 已完成 |

## R8：Workflow、Work Graph 与 Local Fleet

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| JSON/TOML IR 支持 sequence/parallel/branch/retry/review_gate/fan_in，拒绝脚本 | `test_workflow_ir.py` | 已完成 |
| 节点/深度/并发/token/wall-time 上限、fan-in owner、高风险 gate | IR 与 executor 边界测试 | 已完成 |
| Event reducer Work Graph 与 TUI projection | Workflow ledger/reducer/IPC/panel 测试 | 已完成 |
| 退出恢复不重跑 completed node | Workflow executor 与 `test_local_fleet.py` restart 测试 | 已完成 |
| parallel claim/concurrency、fan-in evidence、gate failure | `test_workflow_executor.py`、Fleet integration | 已完成 |
| 本地进程 host、SQLite ledger、heartbeat、固定 profile | `fleet/*`、`test_local_fleet.py` | 已完成 |
| 相同输入/route/profile receipt 可比较 | `test_receipt_is_deterministic_and_comparable` | 已完成 |

## R9：Runtime API、Receipt 与 TUI Inspector

| 规格/验收 | 当前证据 | 判定 |
|---|---|---|
| 规范列出的 HTTP JSON/SSE 路由 | `api/app.py`、`test_runtime_http_api.py` | 已完成 |
| loopback 默认；remote 无 bearer token 启动失败 | API auth 单元测试 | 已完成 |
| SSE cursor 重连无重复/缺口 | `test_sse_reconnect_resumes_after_durable_cursor` | 已完成 |
| API/TUI/IPC 查询同一 durable status/usage | `test_http_and_ipc_share_durable_threads`、Turn inspector tests | 已完成 |
| Receipt 纯函数、字段完整、未知 cost/unavailable | `receipts/*`、`test_turn_receipt.py` | 已完成 |
| File family changed-files、tools/approvals/artifacts/workers/verification/error | Receipt action-family 与完整聚合测试 | 已完成 |
| Tasks/Workers/Workflow/Turn 可切换视图与 `/context` | TUI high-frequency view、workflow panel、turn panel 测试 | 已完成 |
| Compaction trigger/前后 token/窗口/summary path | Compactor event和 TUI render 测试 | 已完成 |
| TUI 退出不取消 daemon-owned Worker | Worker registry 与双进程生命周期边界 | 已完成 |

## 强制故障注入矩阵

| 故障 | 直接证据 | 判定 |
|---|---|---|
| 半个 JSON tool call 后断流 | `test_partial_tool_call_stream_is_discarded_on_retry`、`test_openai_compatible_rejects_partial_tool_arguments` | 已完成 |
| Tool 执行后、result 持久化前 Core 退出 | `test_runtime_recovery.py` | 已完成 |
| SQLite transaction 中断 | `test_transaction_interruption_rolls_back_all_runtime_state` | 已完成 |
| Event client seq N 后断线 | IPC/SSE cursor reconnect 测试 | 已完成 |
| Worker heartbeat 停止 | `test_stopped_heartbeat_expires_worker_lease` | 已完成 |
| dirty worktree | `test_worktree_remove_protects_dirty_changes` | 已完成 |
| LSP 不存在/失败/超时/超大 | `test_lsp_diagnostics.py` | 已完成 |
| Hook 卡死/超大/secret | `test_hooks.py` | 已完成 |
| Artifact 丢失/hash mismatch | `test_artifact_store_reports_missing_and_corrupt` | 已完成 |

## 项目级 Definition of Done

1. TUI、CLI、headless 和 Worker 通过 SessionManager/RuntimeService 与 parent Turn event projection
   使用同一 Thread/Turn/Item/Event ledger。
2. Route、authority、tools、usage、产物和终止原因均在 Turn/Item/Event/Receipt 中可离线解释。
3. Mode/Authority/Trust/Sandbox 独立冻结、查询、展示和测试。
4. 默认工具面有界、action-aware、可发现，旧 alias 不再进入模型目录。
5. Compaction、memory、artifact、working set 与 LSP 均进入上下文治理和观察事件。
6. Worker 持久状态、预算、claim、heartbeat、evidence 与恢复路径有直接测试。
7. Workflow parallel/gate/fan-in/failure/restart 语义有直接测试。
8. TUI 高频视图只通过 typed IPC/runtime projection 查询关键状态。
9. API 默认 loopback；密钥不写项目配置且 redaction tests 覆盖 log/event/receipt/error。
10. Windows 与 Linux Mypy、完整 Pytest、协议、build 和 wheel smoke 结果记录在本轮最终 gate。

本轮最终验证基线：Windows/Linux Mypy 均覆盖 209 个 source files；Pytest 共
`827 passed, 3 skipped`；协议文档同步；sdist/wheel 构建与 wheel smoke 通过。最终提交号以
本轮 Git 提交记录为准。
