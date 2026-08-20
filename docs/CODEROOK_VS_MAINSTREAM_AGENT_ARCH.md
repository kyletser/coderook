# CodeRook 与主流 Agent 架构对比及后续改造方向

> 历史快照：本文保留早期结构性分析，正文中的“当前状态”和评分已经过期。当前架构见
> [FUNCTIONAL_ARCHITECTURE.md](FUNCTIONAL_ARCHITECTURE.md)，开源补全工作见
> [OPEN_SOURCE_COMPLETION_PLAN.md](OPEN_SOURCE_COMPLETION_PLAN.md)。

> Generated Time: 2026-07-21 10:01
>
> 审计对象：当前工作区 `C:/Users/Administrator/Desktop/coderook` 的真实代码
> 对标对象：当前业界主流 Agent 架构范式（Coding-Loop / Graph-State / 对话型多 Agent / Handoff-SDK / 远程持久化）
> 结论口径：以代码证据为准，与 [CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md](./CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md) 互补，本文聚焦"结构性差异"，不重复逐条产品差距清单

本报告不重复公开文档所能给出的"功能清单"对比，而是从**控制流范式、状态分布、扩展协议、执行隔离、可恢复性**五个工程维度，把 CodeRook 与主流架构做结构性对照，并据此提出后续改造方向。

## 0. 实施状态更新

本文主体记录的是 2026-07-21 改造前的差距基线。提交 `bae9d75` 已完成或部分完成其中 Phase A 与 Phase B 的若干项目，阅读后文的“缺失”描述时应以当前代码和下表为准：

| 原缺口 | 当前状态 |
|---|---|
| 工具无副作用能力模型 | 已实现 `ToolSideEffect`、`can_parallel` 和保守默认值 |
| 多个 tool call 总是串行 | 已实现连续只读工具批量并行，副作用工具保持模型顺序 |
| Task 未进入控制流 | 主循环已注入 Todo State，并对未完成 Todo 的 `end_turn` 做有限延迟 |
| `profile.model` 未接线 | Anthropic 与 OpenAI-compatible provider 均支持 per-agent 模型覆盖 |
| reviewer 依赖名字白名单 | 已支持 `restrict = "read_only"` 的能力过滤 |
| Registry 跟随 Runner 销毁 | 已提升为 daemon 级，支持跨 turn 查询，daemon 退出时统一清理 |

尚未完成的关键边界仍包括 durable run 恢复、handoff、Plan/Ask/Steering、Trace 图和 OS 级沙箱。daemon 级后台任务只保证跨 turn，不保证 daemon 重启后继续执行。

## 1. 执行摘要

CodeRook 已经具备了一个"单循环 Coding Agent + Daemon"的可运行实现：ReAct 循环、类型化工具、权限审批、上下文压缩、Subagent 派生、MCP 接入、事件总线。

但与主流架构相比，**差距的主要来源不是"少几个工具"，而是控制流抽象层次**：

1. **单一 ReAct 循环 = 唯一控制流**。主流都至少提供"图/状态机"或"handoff"之一作为第二层编排；CodeRook 只有线性 loop + forward-only 的 `spawn_agent`，没有任务边、没有 handoff、没有 group chat。
2. **Agent 看作"提示词+工具白名单"**，而主流把 Agent 看作"可携带上下文、可路由、可交接的结构体"。`AgentProfile` 只有 5 个字段，`profile.model` 已解析但不接线（见 [loader.py](../src/code_rook/core/agents/loader.py#L14)）。
3. **Daemon 进程模型相对独特**。多数竞品（Claude Code、Codex CLI、Cursor、Aider）都是单进程 CLI 或 IDE 内嵌，CodeRook 的 daemon-IPC 解耦是一个优点，但 CodeRook 的 daemon 本身没有"跨进程的行为市场"——它只是一个本地服务。
4. **运行状态无法跨步恢复**。LangGraph 有 Checkpointer、AutoGen 有终止/接续 API、Codex 有 session resume，CodeRook 的控制态（loop 状态、tool_call 队列、permission future）都在内存，崩溃即丢，文件有 checkpoint 但 run 不可续。
5. **可观测性有事件总线，但没有 trace-as-graph**。主流架构（LangGraph Studio、OpenTelemetry、AGENTS.md-based provenance）能给"决定路径"而不是"事件流"；CodeRook 的 `events.jsonl` 是平面流。

综合判断：

| 评估口径 | 结论 |
|---|---|
| 控制流表达力 | 强于纯聊天 Agent，弱于图/状态机范式；处于 ReAct loop + 子 Agent 派生的中间状态 |
| 工程基础设施 | daemon 解耦、类型化协议、事件持久化三类做得比多数开源 Agent 框架更工程化 |
| 多 Agent 编排 | 仅具备"父派生子"单向委托，缺乏 handoff/group chat/durable orchestration |
| 产品成熟度 | 接近 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md 的 35–45% 估计，且在"长程自主性"和"多 Agent 协作"上额外低于行业前沿 |

## 2. 评估对象与对标集

### 2.1 评估对象

CodeRook 的真实运行链路（代码证据）：

```
coderook-core (daemon)
  └─ SessionManager ─→ per-turn AgentRunner factory
        └─ AgentLoop（plan-act-observe）
              ├─ LLMProvider
              ├─ ToolRegistry（内置 + MCP + spawn_agent + task + memory + background + worktree）
              ├─ PermissionManager（4 层静态 + ASK Future + session/持久 always 缓存）
              ├─ Compactor（V2 结构化摘要 + 反应式压缩）
              └─ EventBus → EventWriter(events.jsonl) → IPC Broadcaster → TUI
```

关键约束：

- `AgentRunner` 每个 turn 重建：[session/manager.py](../src/code_rook/core/session/manager.py)、[app.py](../src/code_rook/core/app.py)。
- 一个 run 只对应一个 `ExecutionContext`、一个 `ToolRegistry`、一个 `AgentLoop` 实例：[loop.py](../src/code_rook/core/loop.py#L112)、[runner.py](../src/code_rook/core/runner.py)、[subagent/tool.py](../src/code_rook/core/subagent/tool.py)。
- 多 Agent 只有 forward委托（父→子），子 Agent 把结果以字符串回填给父循环：[subagent/tool.py](../src/code_rook/core/subagent/tool.py#L63)。

### 2.2 对标主流范式

| 范式 | 代表项目 | 核心抽象 | 控制流来源 |
|---|---|---|---|
| Coding-Loop | Claude Code、OpenAI Codex CLI、Cursor Agent、Aider、OpenCode | ReAct + 工具调用 + Checkpoint | LLM 决定下一步 |
| Graph-State | LangGraph、PydanticAI（图子集）、Microsoft Semantic Kernel Process | 显式节点+边+共享状态 | 图拓扑（开发者预先定义） |
| 对话型多 Agent | AutoGen（AG2）、CrewAI、MetaGPT、ChatDev | Agent = 角色 + 工具 + 回合发言权 | Group Chat Manager / 路由器 |
| Handoff-SDK | OpenAI Agents SDK（Swarm 后继）、Google ADK Agent-to-Agent handoffs | Agent = instructions+tools+handoffs，调用即交接 | 上一个 Agent 选择下一个 |
| 远程持久化多 Agent | Inngest / Temporal Agent、Mastra、LangGraph Cloud | durable workflow + sleep/resume/checkpoint | durable runtime |
| 科研任务 Agent | SWE-agent、OpenDevin/OpenHands | 环境接口（shell/IDE）+ 受限动作空间 | 自定义定长循环 |

CodeRook 明确属于 **Coding-Loop** 范式，因此主要的结构性对标对象是 Coding-Loop 范式 + 主流框架提供的"第二控制流"。

## 3. 主流架构范式速览（结构性要点）

### 3.1 Coding-Loop（CodeRook 直接所在）

**Claude Code / Codex CLI 思路**：一个 bounded loop，模型用工具推进，但引入 `TodoWrite / Task` 工具作为"软状态机"——模型自己写出任务清单并以工具调用形式维护状态，把 ReAct 升级成"有持久计划的循环"。Claude Code 的命令行是单进程，但带 checkpoint、`--resume`、Plan mode、Steering 和 IDE Plan/Context。

**Aider/Cursor 思路**：编辑语义是核心抽象（Repo Map、SEARCH/REPLACE 块、Utu/apply_model），loop 反而很薄。而非编辑类 Agent 工程能力弱。

### 3.2 Graph-State（LangGraph 类）

核心结构：`State(TypedDict)` + `节点函数(State)->State` + `边`，由 `Checkpointer`（Memory/Sqlite/Redis）写入每个 super-step 的状态快照，可在任意节点 `interrupt()` 等待人工，并 `update_state` 注入。可行性：

- 可以表达"planner → executor → reviewer → accept(条件回边)"这种条件环，CodeRook 的 `AgentProfile` 三角色（[agents/builtin/](../src/code_rook/core/agents/builtin/)）本来目标就是这样，但没有"边"把它们组织起来，目前只是靠 `spawn_agent` 的字符串结果隐式回路。
- 可以表达 streaming partial，CodeRook 的 `llm.token` 只到事件层而不到状态层。

### 3.3 对话型多 Agent（AutoGen / CrewAI）

核心：每个 Agent 持有独立系统提示 + 工具，由一个 `GroupChatManager` 控制发言权（RoundRobin / Selector LLM / Auto / Random），并以共享上下文/黑板推进。AutoGen v0.4 把 Agent 拆成 `AssistantAgent` + `UserProxyAgent`，并支持 `RunResponse` 流式 + 取消 + 接续（`load_state`/`save_state`）。CrewAI 把流程写成 `Crew(tasks=[...], process=sequential|hierarchical)`。

与 CodeRook 的差异：CodeRook 的多 Agent 是"父 Agent 用文字描述子任务→子 Agent 独立完成→回字符串"，没有共享黑板、没有发言权仲裁、没有 selector。

### 3.4 Handoff-SDK（OpenAI Agents SDK / Google ADK）

核心：Agent = `instructions + tools + handoffs + guardrails`。`handoffs` 是一个 Agent 列表，表示"由本 Agent 决定把当前会话整体交给谁"。运行时有 `Runner.run()` 产出 `RunResultStreaming`，含 `current_agent` 状态。Guardrails 在主模型之前/之后并行跑校验。Agent SDK 鼓励"无显式图"的多 Agent，因为 handoff 边由 LLM 即时决定。

与 CodeRook 的差异：CodeRook 的 `spawn_agent` 不是 handoff（不交接当前会话上下文），是 fork。结果只能以字符串返回，不能把整个对话的所有权转移。也没有 guardrail 概念。

### 3.5 远程持久化多 Agent（Temporal/Inngest/LangGraph Cloud）

核心：把每一步执行视为一个 durable workflow，可 `sleep(seconds)`、`wait_for_signal`、`checkpoint`。即便进程崩溃，runtime 也按事件日志重放。`agent_step` 永不丢。

与 CodeRook 的差异：CodeRook 的"恢复"只覆盖文件和 transcript；run 状态不可续，后台 subagent 与 Runner 同生命周期（[runner.py](../src/code_rook/core/runner.py#L373) cancel_descendants），等于放弃了 durable orchestration。

## 4. 真实映射：架构现状点评

### 4.1 循环（loop.py）

`AgentLoop.run` 是一个 `while not is_done()` 的 plan-act-observe：[loop.py#L112](../src/code_rook/core/loop.py#L112)。

- 仅有一个"模型→工具"选择点；模型在一轮里返回多个 tool_use 时 **总是串行**：[loop.py#L200](../src/code_rook/core/loop.py#L200)。
- 终止条件只有 `end_turn` / `max_steps` / `llm_error`；没有"目标完成判断"、"plan 不变式"、"verification 工具回边"。
- 上下文溢出触发一次反应式压缩，之后不再触发：[loop.py#L160](../src/code_rook/core/loop.py#L160)。

这定下了 CodeRook 的控制流表达力上限：**等同于早期 ReAct，没有 LangGraph 那种可由开发者加密的环，也没有 Claude Code 的 TodoWrite 软状态机**。

### 4.2 工具系统（tools/）

- 工具是用 dataclass + Pydantic params 注册的，schema 走 JSON Schema，调用器有结构化错误分类（schema/timeout/permission/rate_limit/runtime）：[tools/errors.py](../src/code_rook/core/tools/errors.py)。
- 但**没有工具能力的显式声明**：`side_effect`、`idempotency`、`retry_policy`、`composable`、`requires_permission_signoff` 都隐含在代码里。这导致：
  - `runtime_error` 默认三次重试，可能副作用放大（详见 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.4 P0-2）。
  - 并行执行策略无法确定哪些工具可并行。
  - MCP external_write 工具与本地 read 工具被一视同仁。
- 没有工具「能力探针」，模型无法查询工具是否支持流式/视觉/副作用。

### 4.3 多 Agent / 子 Agent（subagent/）

`SpawnAgentTool` 是 forward-only 委托：[subagent/tool.py#L63](../src/code_rook/core/subagent/tool.py#L63)。

- 子 Agent 用全新 `ExecutionContext`，prompt 是父 Agent 写的字符串（即"模型即模型编排器"，而非 LangGraph 显式边）。
- 不能让子 Agent 在自己循环里 `request_human`、`handoff` 回父、或 Update 共享状态。
- `profile.model` / permissions / skills / MCP 在子 Agent 上没有真正接线，子 Agent 只继承父 provider：[subagent/tool.py](../src/code_rook/core/subagent/tool.py#L123)。
- `BackgroundTaskRegistry` 跟 Runner 同生命周期，而 Runner 每 turn 重建（runner.py / app.py / session/manager.py），等于说后台 subagent 不能跨 turn 被找到。

### 4.4 任务系统（task/ + tools/builtin/task_*）

CodeRook 有 TaskManager 与 `task_create / claim / update / list / get` 工具，且 task_claim 是原子认领：[task/manager.py](../src/code_rook/core/task/manager.py)。这是一个亮点。但它和主流的差距是**任务对象不是 Agent 的状态机**：

- 任务没有 `assigned_agent_id`、`dependencies`、`subtasks`、`status machine`（只有 pending/in_progress/completed）。
- 没有让 loop 根据 task 状态选择下一步——loop 仍然让模型自由 step；任务只是"便签"。
- 没有把任务和 Subagent 绑定（谁 claim 谁跑）。
- 持久化只在 `run_path/.tasks`，daemon 不跨 run 暴露 task list。

对比 Claude Code 的 `TodoWrite`（直接驱动 UI checklist）或 LangGraph 的"图节点即任务"，CodeRook 的 task 没有进入控制流。

### 4.5 权限与人类介入（permissions/）

- 4 层静态 + ASK Future 机制是成熟 Agent 都有的：[permissions/manager.py](../src/code_rook/core/permissions/manager.py)。
- 但只有"工具调用层"的人类介入；主流还有**Plan 审批、目标澄清、Steering**（运行中注入新消息、打断当前 step）。CodeRook 没有 `AskUserQuestion`、`Plan mode`、`session.send_message` 在 run 期间入队机制（manager 在忙时直接拒：CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.1）。
- `permission.respond` 走 IPC 异步回，这是好的；但**没有 permission 的细粒度规则**（路径 glob、命令 prefix、domain、MCP server、Agent type）。

### 4.6 持久化与会话（session/）

- `SessionStore` 写 meta/thread/notes、压缩前备份：[session/store.py](../src/code_rook/core/session/store.py)。
- 但 transcript **批量落盘**：run 结束才写新消息：[runner.py](../src/code_rook/core/runner.py#L249)，崩溃丢本轮。
- run 控制状态（当前 step、tool_use 在等谁、compaction 是否已触发）**完全在内存**。
- 没有 store schema_version、迁移器、损坏 JSONL 修复、retention。
- 与 LangGraph Checkpointer 的最大差异：CodeRook 把"事实"和"状态"混在 transcript，没有独立的"运行状态"快照层。

### 4.7 可观测性（events/ + trace/）

- 事件总线覆盖 Run/Step/Tool/LLM/Permission/Session/Compaction/Subagent/Background/Skill，并写 `events.jsonl` + 脱敏 Trace：[events/bus.py](../src/code_rook/core/events/bus.py)、[trace/](../src/code_rook/core/trace/)。
- 但事件是平面流，**不能重组为执行图**：没有 `decision_path`、没有 `subagent.parent_run_id` 的回放事件树（事件里有 `parent_run_id` 字段但 viewer 没有重建）、没有 OpenTelemetry export、没有 cost/cost 汇总。
- LangGraph Studio / Sentry for Agents / Arize Phoenix 都把 trace 当成"图视图"，CodeRook 没有这一层 UI。

### 4.8 扩展协议（skills + mcp + hooks）

- Skills 显式 `/slash` 触发，frontmatter 是自写行解析（不安全）：[skills/loader.py](../src/code_rook/core/skills/loader.py)，参见 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.9。
- MCP 只到 tools/list + tools/call，stdio/raw tcp，无 HTTP/SSE/OAuth/resources/prompts/sampling：[mcp/client.py](../src/code_rook/core/mcp/client.py)。
- Hooks 有 `UserPromptSubmit / PreToolUse / PostToolUse / Stop`（loop.py 末尾 + runner.py 起点），但没有 `SessionStart/End / PreCompact / SubagentStart / Permission / Notification`，也无法改 prompt 或返回 additional context。

## 5. 逐维度结构性对比矩阵

评分沿用 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md 的 0–10 口径，但**维度换成"结构性维度"**，对标是主流范式中最强的不限项目。

| 维度 | 主流最强者能做到 | CodeRook 现状 | 评分 | 优先级 |
|---|---|---|---:|---|
| 控制流抽象 | 图/状态机 + 节点条件环 + interrupt + 软状态机（LangGraph / Claude Code TodoWrite） | 单一 ReAct loop + task 工具（未驱动 loop） | 3/10 | P0 |
| 任务编排（多 Agent 协作） | handoff / group chat / 共享 task board / durable orchestration | forward-only spawn_agent + 字符串回填 | 3/10 | P1 |
| 运行恢复（durable execution） | 任意 step checkpoint + resume + sleep/wait_for_signal | 文件 checkpoint + transcript 批量写；run 控制态不可续 | 2/10 | P0 |
| 人类介入层级 | Plan 审批 / AskUser / Steering / 取消 / 模式切换 | 工具级 ASK + run.cancel；无 plan/steering/ask | 4/10 | P1 |
| 工具能力模型 | side_effect / idempotency / capability / retry_policy | 单一错误类驱动通用重试 | 2/10 | P0 |
| 状态（State）对象 | 显式 TypedDict/Pydantic State + per-key reduce | ExecutionContext 是 message list 派生 + 私有字段隐式 | 3/10 | P1 |
| 流式语义 | partial json / partial tool input / task status partial | llm.token 文本增量；tool 无 partial | 4/10 | P2 |
| 上下文治理 | 分层 context assembly + 摘要图视图 + auto memory | Compactor V2 + 三层 context 文件 + recalled memory（已有řed），但无层级/规则/import | 5/10 | P1 |
| 工具执行隔离 | OS sandbox / network deny / 资源配额 / 进程树 | loopback + workspace boundary + 64KB 截断；无沙箱 | 2/10 | P0 |
| 可观测性 | trace-as-graph / OTel / cost / decision provenance | 事件流 events.jsonl + IPC 广播 | 4/10 | P1 |
| 扩展协议 | Skills 自动调用 + Hooks 行为改写 + Plugins + MCP resources/prompts/sampling | Skills 仅 slash + 自写 frontmatter + Hooks 4 点只观测 | 3/10 | P2 |
| Provider 抽象 | capability discovery + router + fallback + budget + per-agent model | Anthropic 流式 + OpenAI-compatible 一发一收；router 占位 | 4/10 | P1 |
| 产品形态 | SDK / Headless / IDE / Web / CI / Desktop | daemon + CLI + TUI；无 SDK/JSON 输出/CI | 3/10 | P2 |

结构性差距合计约 **3.3/10**，低于 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md 给出的产品成熟度 4/10，原因是结构性维度更难跳过：即使把所有工具补齐，loop 抽象层不变，复杂任务仍会压跨 ReAct。

## 6. 最关键的结构性断层

下面 5 条是"补足它们之前，堆功能收益递减"的瓶颈：

### F1. 没有第二控制流

CodeRook 把 Agent 等同于"system_prompt + 工具白名单 + 单循环"（AgentProfile 5 字段），无法表达：

- `planner -> executor -> reviewer -> 条件回边 planner`（LangGraph 标准模板）
- `triage agent handoff -> frontend_agent | backend_agent | db_agent`（OpenAI Agents SDK 标准模板）
- `sequential crew with review step`（CrewAI 标准模板）

`spawn_agent` 是"父→子委托回字符串"，子 Agent 无法把当前会话所有权交回或交给别人，无法让 reviewer 修改 executor 的计划再让 executor 重跑。`AgentProfile` 三角色文件已经存在（[agents/builtin/](../src/code_rook/core/agents/builtin/) `executor.toml / planner.toml / reviewer.toml`），但**没有把这些角色编排起来的执行器**——这是一个"差最后一层胶水"的状态。

### F2. 运行不可恢复（durable gap）

`ExecutionContext` 是运行时唯一状态对象，且只在内存：[context.py](../src/code_rook/core/context.py)。

- `step`、`status`、`reason`、`result`、`_reactive_compaction_attempted` 都不持久。
- crash 后只能用 transcript 拼回一段没有控制状态的对话。
- 后台 subagent 与 Runner 同周期 cancel，连"我现在跑到第几步"都不可续。

主流（LangGraph Checkpointer、Temporal、AutoGen save_state）都能在任意 step 续跑。

### F3. 工具能力模型缺失

`BaseTool` 没有 `side_effect / idempotency / retry_policy / needs_permission / can_parallel`：[tools/base.py](../src/code_rook/core/tools/base.py)。

后果：

- 重试策略由错误类（`runtime_error`）决定，而非工具声明——副作用工具被错误重试。
- 无法判断哪些工具可并行（loop 串行执行多个 independent read）。
- 无法在 Subagent 中根据 capability 选择工具子集（only allow pure-read tools to a reviewer）。

### F4. 人机协作只有"权限一票"

人类在 CodeRook 中只能对单个工具调用 say yes/no。但主流 Agent 任务里人类介入集中在更早/更高的位置：

- Claude Code Plan mode：在执行前呈现计划并等待批准。
- LangGraph `interrupt()`：在节点边界等待人工，且可 `Command(update=..., resume=...)` 修改状态再续。
- OpenAI Agents SDK：`input_guardrail` / `output_guardrail` 可在主模型前后阻断。

CodeRook 没有 plan presentation、没有 ask、没有 steering 消息队列（CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.1），会话忙时直接拒，等于把"运行中纠正"这条路堵死。

### F5. Trace 是流不是图

`events.jsonl` 按时间排序，虽然有 `parent_run_id` / `run_id` 字段但 viewer 不重组父子树；没有 decision provenance（"哪个 tool_use 触发哪个 subagent"），没有 cost/materialized view。

对"长程自主 Agent"开发的意义在于：没有图视图就无法 debug 多 Agent 路径，无法知道 reviewer 是否在某些 turn 把 plan 拉回来了，无法 A/B 编排策略。

## 7. 后续改造方向

改造按"先补结构性断层、再上产品功能"的顺序。每个阶段都有明确的 acceptance，避免推到 Phase 3 再回头重做底层。

### Phase A：把"循环"升级到"可被编排的状态机"（核心结构层，2–4 周）

目标：在不动 ReAct 路径的前提下，引入第二控制流和工具能力模型，让复杂任务不再靠模型"自己心里数"。

1. **工具能力模型（Capability）**。给 `BaseTool` 增加 `side_effect / idempotency / retry_policy / can_parallel / requires_signoff`。`invocation.py` 由工具声明的 `retry_policy` 决定重试，不再由通用 error 类决定。
   - 验收：bash / write / MCP external_write 默认不重试；read/glob/grep 可并行。
2. **并行工具执行**。loop 中对 `response.tool_calls` 做 dependency-aware 并行：纯读且无依赖的并发，副作用串行。引入简单的 `tool_use` 之间输入 path / file 依赖分析。
   - 验收：3 个独立 glob 调用一并发回；2 个 edit 同文件按模型顺序串行。
3. **TodoState 作为软状态机**。把现有 `task_*` 工具升级为驱动 loop 的 `TodoState`：当模型 `task_create / task_update -> in_progress` 时，loop 在每次 step 把当前 todo 注入系统提示，并提供 `current_task` 给 TUI。
   - 验收：TUI 显示 todo checklist 跟随 run 演进；plan-ended 但 todo 未完成时 loop 不提前 `end_turn`。
4. **轻量 Graph 编排器（可选，先不做）**。如果未来要做 LangGraph 风格显式图，再引入 `WorkflowGraph` + `Node(State)->State` + `Checkpointer`。短期内 TodoState + 能力模型足够覆盖 Coding Agent 90% 复杂度。

这一阶段不引入 LangGraph 依赖，但因为 ToolCapability 和 TodoState 通用，未来要接图也是平滑的。

### Phase B：把"派生"升级到"协作"（多 Agent 层，3–6 周）

目标：让 `spawn_agent` 之外出现 **handoff** 和 **group collaboration** 两类缺的多 Agent 模式。

1. **AgentProfile 真正生效**。解析 `model / allowed_tools / system_prompt / inherits_parent_context / mcp_scope / permission_scope`，并让 SpawnAgentTool 用它：[agents/loader.py#L14](../src/code_rook/core/agents/loader.py#L14)。
   - 验收：reviewer 用便宜 model；executor 用强 model；reviewer 不能执行 bash。
2. **Handoff 工具**。新增 `agent_handoff(target, carry_context=True)`：把当前 ExecutionContext 的所有权交给目标 Agent，并允许目标 Agent 在结束后把控制权还回。与 `spawn_agent`（fork + 等结果）并列。
   - 验收：triage agent 能 handoff 给 frontend_agent，frontend 完成后会话所有权回到 triage。
3. **共享 TaskBoard + Agent Claim 绑定**。把 `TaskManager` 升级为 daemon 级 `TaskBoard`，每个 task 带 `claimed_by_agent_id / dependencies / subtasks`，loop 在空闲挑未认领 task 并 step。
   - 验收：两个后台 subagent 互不抢同一 task；TUI 显示 task-owner 关系。
4. **BackgroundTaskRegistry daemon 化**。把 `BackgroundTaskRegistry` 从 Runner-owned 改为 `CoreApp`-owned，跨 turn 持有，并加 `agent_stop / agent_message / agent_resume`（durable 由 Phase D 配合）。
   - 验收：TUI 退出再进仍能查到后台 subagent 的结果。

### Phase C：把"运行"升级到"Durable Run"（恢复层，2–4 周）

目标：让任意 step 可中断/恢复，让进程模型对行为市场透明。

1. **RunStateStore（运行态快照）**。把 `ExecutionContext.__dict__` 的运行态字段（step/status/reason/_reactive_compaction_attempted/tool_use_pending/permission_pending）每步写 `run_path/state.jsonl`，append-only。
   - 验收：daemon kill 后重启能识别 interrupted run，并能用命令继续或干净结束。
2. **transcript 增量落盘**。assistant message 与 tool_result 在每个 step 完成（含 sub-step）立即 append 已带 seq number；删除批量落盘路径：[runner.py#L249](../src/code_rook/core/runner.py#L249)。
   - 验收：强杀进程后 thread 不会有 orphan `tool_use`。
3. **后台 subagent 状态持久**。Phase B 的后台 AgentRegistry 用 RunStateStore 做状态表，崩溃后可标记 interrupted 并能被父 Agent 看见。
4. **SessionRepository**。daemon 启动扫描 meta + schema_version 迁移 + 懒加载 transcript（与 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.6 同一建议）。

### Phase D：人机协作三层（交互层，3–6 周）

目标：把人类介入从"权限一票"扩到 plan / ask / steering 三层。

1. **Plan mode**。`AgentRunCommand.permission_mode` 增加 `plan`；loop 在前 N step 不执行副作用工具，先把 plan（带 TodoState）通过 `PlanPresentedEvent` 推给 TUI，等 `session.respond_plan(approve/modify/reject)`：与 PermissionManager 挂起 Future 同机制复用。
   - 验收：模型在 plan 模式下不会写文件，只列出修改意图并由用户确认。
2. **AskUserQuestion 工具**。模型可发起 `ask_user_question(questions=...)` 并 await `session.respond_ask`，与 PermissionManager ASK Future 同机制。
3. **Steering 消息队列**。`SessionManager` 在忙时不再直接拒，把消息入 `steering_queue`，loop 在安全点（step 边界）注入成新的 `user` message。
4. **中断传播**。`run.cancel` 把信号广播到 permission future + bash process tree + subagent loop。
   - 验收：长任务运行时发新消息，模型在下个 step 看见；按 esc 后 bash 子进程全部回收。

### Phase E：把"事件流"升级到"Trace 图"（可观测层，2–4 周）

1. **Trace-Tree 重组器**。利用 `parent_run_id`/`run_id` 在 `events.jsonl` 之上建 materialized view（独立文件或 TUI panel），呈现 subagent 父子树 + 每个 node 的 tool_use/tool_result。
2. **OTel Exporter**。主动事件序列导出为 OTel spans，复用现有 LLM/tool/permission 事件作为 span attributes。
3. **Cost/quality dashboard**。按 run/session 聚合 input/output/cache，并按 AgentProfile 分摊成本。
4. **decision provenance**：每个 `end_turn` 反推该步骤由哪些 tool_use 触发。

### Phase F：扩展协议与产品形态（生态层，6–12 周）

1. **Skills 标准化**（YAML frontmatter + 自动触发 + supporting files + fork context，CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.9）。
2. **Hooks 扩到 SessionStart/End、PreCompact、SubagentStart、Permission、Notification**，且可 `return block / modify_prompt / additional_context`。
3. **MCP 升 HTTP/SSE/OAuth/resources/prompts/sampling/roots/重连**（§6.11）。
4. **Headless JSON / SDK**：`--output-format json/stream-json`、JSON Schema 输出、Python SDK + permission approval callback（§6.14）。
5. **ProviderRouter**：capability discovery + 多模型 + budget + fallback + per-run override，替换占位的 `llm.router`（§6.8）。

Phase F 不会早于 A–D，因为它依赖 Phase A 的 Capability、Phase B 的 AgentProfile、Phase D 的 plan/approval。

### 推荐落地顺序

```
A1 工具能力模型 ──> A2 并行执行 ──> A3 TodoState 状态机
        │                              │
        └─> C1 RunStateStore ──> C2 增量落盘 ──> B3 TaskBoard daemon ──> B4 AgentRegistry 持久
                                            │
        ┌─> D1 Plan mode ──> D2 AskUser ──> D3 Steering ──> D4 Cancel 传播
        │
        └─> E1 Trace 重组 ──> E2 OTel ──> E3 Cost ─→ F 扩展生态
```

A 和 C 可以并行启动；B 依赖 C 的 RunStateStore；D 复用 PermissionManager ASK Future 机制（与 B 并行）。

## 8. 可量化的验收指标

在 Phase A–F 后，建议用下面结构性指标衡量（不是"功能存在"），否则容易做了一堆工具仍走老 ReAct：

| 指标 | 含义 | Phase | 目标 |
|---|---|---|---|
| Loop step 内 tool 并发度 | 平均每 step 真并行执行的 tool 数 | A2 | ≥ 1.5 |
| 结构化状态可恢复 step 数 | daemon kill 后能继续的 step 比例 | C | 100% |
| orphan tool_use 数 | 崩溃后没结果的 tool_use | C2 | 0 |
| Plan-mode 误执行副作用次数 | plan 模式下执行写工具的次数 | D1 | 0 |
| Steering 到下一 step 注入延迟 | 用户发新消息到 loop 看见的 step 数 | D3 | ≤ 1 |
| Sub-agent 跨 turn 可见率 | 后台 subagent 跨 turn 仍能查到 | B4 | 100% |
| Handoff 到目标 Agent 成功率 | 调 `agent_handoff` 后 ownership 转移 | B2 | 100% |
| Trace 树重组覆盖率 | 能重组为父子树的 run 比例 | E1 | 100% |
| 工具被错误重试次数 | runtime_error 触发但有副作用工具被重试 | A1 | 0 |

工程基线沿用 CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS §6.16 的 mypy/ruff/pytest 跨平台要求，本文不再重复。

## 9. 不要做的事（陷阱）

- **不要直接引 LangGraph 依赖**。CodeRook 的 ReAct + daemon 模型与 LangGraph 不互斥，但贸然把 loop 改成 LangGraph 图会丢失 TUI 事件协议和事件流；Phase A 的 TodoState + Capability 已经能覆盖 Coding Agent 90% 场景，图编排留给真正需要复杂工作流时再加。
- **不要在 capability 缺失时同时上多 Agent handoff**。当前 `spawn_agent` 的 forward 委托是可工作的最小集，但要先有 capability（决定子 Agent 能用什么工具）和 AgentProfile 真接线，否则 handoff 后目标 Agent 拿不到正确工具集。
- **不要把"durable"等同于"transcript 持久"**。Phase C 的关键不是把对话存好，而是把运行控制态存好（step / status / pending permission / pending tool_use / reactive compactionAttempted）。否则恢复出来也只是个"卡住的对话"。
- **不要扩 hooks 行为改写之前不做 fail isolation**。Hooks 改 prompt/insert additional_context 是高权限操作，Phase F 之前要先定 hooks 的超时、并发、失败隔离和 workspace trust。
- **不要在 Phase A 之前扩 Skills 自动触发**。让模型自治选 skill 是更危险的人机脱钩，必须有 Plan/D 的人类介入兜底。

## 10. 最终判断与推荐

CodeRook 的"差异化资产"是**类型化协议 + daemon 解耦 + 工具权限事件三条主线**，这是很多街边 ReAct demo 没有、但主流 Coding Agent 工程化也未必做得更整洁的部分。它相对主流的**结构性短板**集中在"控制流、运行恢复、多 Agent 协作、人机协作、能力模型"五处，而这五处恰好互相耦合：没有 capability 模型就不能确定子 Agent 用什么工具；没有 RunStateStore 就做不出 durable handoff；没有 plan/steering 就无法安全让模型自治选 skill。

推荐落地节奏：**Phase A 与 C 并行先行**（结构性基础），**B 紧随**（多 Agent），然后 **D**（人机），**E** 负责把前述成果转成可调试的图视图，**F** 最后扩张生态。

完成 A、B、C、D 之后的 CodeRook，会从一个"小 ReAct loop + 工具"真正演进成"可被编排、可恢复、可协作、可人机干预的 Coding Agent Runtime"——这才是相对当前最重要的一个台阶，比再多写几个 builtin 工具有更陡的边际收益。

## 11. 参考资料维度

主流资料按范式，不一一列具体版本：

- Coding-Loop：Claude Code 官方文档（见 [CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md](./CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md) §13）、OpenAI Codex CLI / Codex agent、Cursor agent docs。
- Graph-State：LangGraph docs（StateGraph / Checkpointer / interrupt / Command）。
- 对话型多 Agent：AutoGen v0.4 / AG2 docs、CrewAI Concepts（Crew/Task/Process）、MetaGPT。
- Handoff-SDK：OpenAI Agents SDK（Agent / Handoff / Guardrail / Runner）、Google Agent Development Kit（Sub-agents / Transfers）。
- 远程持久化：Temporal Workflows、Inngest Functions、Mastra Workflows docs。
- 科研任务 Agent：SWE-agent、OpenHands（原 OpenDevin）。
