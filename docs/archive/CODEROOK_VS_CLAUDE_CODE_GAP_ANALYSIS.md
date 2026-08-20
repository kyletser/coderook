# CodeRook 与 Claude Code 能力差距分析

> 历史快照：本文保留早期对标过程，其中功能缺口和比例不代表当前状态。当前体验矩阵见
> [CLAUDE_CODE_EXPERIENCE_PARITY.md](../plans/CLAUDE_CODE_EXPERIENCE_PARITY.md)，发布结论见
> [RELEASE_SCORECARD.md](../status/RELEASE_SCORECARD.md)。

> 本文是初始审计快照，其中部分 P0/P1 缺口已经修复。当前代码证据与未完成项请以
> `IMPLEMENTATION_PROGRESS.md` 和 `LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md` 为准。

> 本文是差距审计快照。2026-07-16 之后的已实现项、测试基线和下一步执行顺序，
> 以 [IMPLEMENTATION_PROGRESS.md](IMPLEMENTATION_PROGRESS.md) 为准；PC 桌面版路线见
> [PC_DESKTOP_MIGRATION_PLAN.md](../plans/PC_DESKTOP_MIGRATION_PLAN.md)。

> 审计日期：2026-07-15
> 审计对象：当前工作区 `C:/Users/Administrator/Desktop/coderook` 的真实代码
> 对标对象：Claude Code 官方公开文档所描述的当前能力
> 结论口径：以代码和测试为准，不把 README、架构文档中的规划项视为已实现

## 1. 执行摘要

CodeRook 已经具备一个“小型本地 Coding Agent Runtime”的主要骨架：

- 有独立 Core daemon、CLI/TUI 客户端和 JSON-RPC 事件协议。
- 有可运行的 ReAct 循环、模型工具调用、结果回填和多步终止逻辑。
- 有工具注册、Pydantic 参数校验、权限询问、事件落盘和 Trace。
- 有多轮 Session、Notes、手动/自动压缩、Skills、Subagents、MCP。
- 有 268 个当前可通过的测试，Mypy 严格模式通过。

但它和 Claude Code 的差距并不主要是“模型不够聪明”，而是 **Agent harness 和产品工程还不完整**。最关键的差距是：

1. **安全边界尚不成立**：文件工具可接受绝对路径，Bash 没有 OS 级沙箱，持久权限粒度只有工具名。
2. **编辑能力太弱**：只有整文件覆盖，没有精确 Edit、冲突检测、Diff 和 Checkpoint。
3. **会话无法真正恢复**：磁盘上虽然有 transcript，daemon 重启后却不会重建 Session；运行中崩溃还可能丢失本轮消息。
4. **交互不可控**：缺少中断、取消、运行中 steering、用户问答、Plan mode 和任务监控。
5. **代码理解工具不足**：缺少专用 Glob、Grep、LSP、Notebook、WebSearch/WebFetch 等工具。
6. **扩展协议只实现了最小子集**：Skills、Subagents 和 MCP 都有骨架，但离成熟生态接口仍有明显距离。
7. **自动化与交付薄弱**：无 SDK、结构化输出、CI 集成、安装升级、容器交付和生产监控。

综合判断：

| 使用目标 | 当前成熟度 | 判断 |
|---|---:|---|
| 学习 Agent 原理和系统设计 | 7.5/10 | 很合适，模块齐全且代码量可控 |
| 秋招项目展示 | 6.5/10 | 可以使用，但必须诚实定位为 mini coding-agent runtime |
| 个人日常编码助手 | 4.0/10 | 简单任务可用，复杂修改与长会话风险较高 |
| 团队长期使用 | 2.5/10 | 缺少恢复、安全、隔离、治理和发布能力 |
| 对标 Claude Code 的产品完整度 | 约 35%–45% | 核心闭环已具备，外围产品层和安全层差距大 |

这里的百分比不是源码数量比较，而是按“核心循环、工具、安全、会话、上下文、扩展、交互、自动化、运维”九类能力加权后的工程判断。

## 2. 审计方法与边界

本报告采用四类证据：

1. **运行时代码**：逐层检查 `src/code_rook` 下的 Core、LLM、Tools、Session、Permission、Compaction、Skills、Subagent、MCP、Trace、Transport、CLI 和 TUI。
2. **测试代码**：检查 46 个 Python 测试文件，并实际执行 Pytest、Mypy 和 Ruff。
3. **项目文档**：对照 `README.md`、`TECH_ARCHITECTURE.md`、`WIRE_PROTOCOL.md`、`RUNBOOK.md`。
4. **Claude Code 官方基线**：仅引用 Anthropic 官方 Claude Code 文档；功能会持续变化，因此本报告以审计日期为准。

本报告不比较模型本身的推理质量，也不把 Claude Code 的云服务规模、商业团队资源当作 CodeRook 必须一次完成的目标。重点是找出：**一个可展示的 mini Agent 与一个可靠的日常 Coding Agent 之间，还缺哪些工程能力。**

评分含义：

| 分数 | 含义 |
|---:|---|
| 0 | 完全没有 |
| 2 | 有接口或概念骨架，尚不可依赖 |
| 4 | 基础路径可用，边界和恢复不足 |
| 6 | 个人场景基本可用，有较完整测试 |
| 8 | 团队级成熟能力 |
| 10 | 形成稳定产品和生态 |

## 3. 当前系统的真实实现

### 3.1 规模与模块

审计时统计：

- 运行时代码：87 个 Python 文件，约 7,568 行。
- 测试代码：46 个 Python 文件，约 5,190 行。
- Python 版本：3.12。
- 主要依赖：Pydantic、Anthropic SDK、HTTPX、Textual、python-dotenv。
- 工程检查：Mypy strict 已配置；Ruff、Pytest 已配置。

### 3.2 主运行链路

真实请求链路是：

1. CLI/TUI 通过 TCP JSON-RPC 向 Core daemon 发送命令。
2. `CoreApp` 创建或取得内存 Session，并异步启动一次 run。
3. `SessionManager` 读取 transcript、解析显式 `/skill`，再通过 factory 新建 `AgentRunner`。
4. `AgentRunner` 组装上下文、模型 Provider、ToolRegistry、PermissionManager、Compactor 和 EventWriter。
5. `AgentLoop` 请求模型；若模型返回 tool calls，则逐个执行并回填 tool results。
6. 事件经 EventBus 同步写入 JSONL，并广播给 CLI/TUI。
7. run 结束后，新消息一次性追加到 Session transcript。

关键代码证据：

- Agent 工具循环与顺序执行：[core/loop.py](../../src/code_rook/core/loop.py#L85)
- 工具注册：[core/runner.py](../../src/code_rook/core/runner.py#L97)
- 上下文文件加载：[core/runner.py](../../src/code_rook/core/runner.py#L163)
- Session 执行入口：[core/session/manager.py](../../src/code_rook/core/session/manager.py#L74)
- Core 对外命令：[core/app.py](../../src/code_rook/core/app.py#L259)
- 事件顺序发布：[core/events/bus.py](../../src/code_rook/core/events/bus.py#L17)

### 3.3 已经做得较好的部分

#### Core 与界面解耦

Core daemon 与 CLI/TUI 分离是正确方向。业务执行不依赖 Textual 组件，事件协议也为以后增加 Web UI、IDE 或 SDK 保留了边界。这比把模型调用、终端输入和工具执行全部写进一个脚本更有系统设计价值。

#### 统一事件模型

Run、Step、LLM、Tool、Permission、Session、Compaction、Subagent 等事件已经被结构化，并可写入 `events.jsonl` 和 Trace。对调试 Agent 这种非确定系统很有价值。

#### 工具参数与错误分类

工具通过 Pydantic 校验参数；调用层区分 schema、timeout、permission、rate limit 和 runtime error。这个抽象可以继续演化成可靠的 Tool Runtime。

#### Session、Notes 与 Compaction 的概念完整

项目已经意识到“聊天历史不等于上下文工程”：有 transcript、notes、global/project context、tool result truncation 和 LLM summary。虽然实现仍有缺陷，但方向是正确的。

#### Subagent 与 MCP 不是空接口

Subagent 有前台/后台执行、角色配置、嵌套深度和结果查询；MCP 有 initialize、tools/list、tools/call、stdio/TCP。它们都是真实可运行的最小实现，不是 README 占位。

#### 类型检查基础不错

`mypy src` 在 strict 配置下通过，说明核心代码的类型边界较清楚。对秋招项目而言，这是可信的工程亮点。

## 4. 文档与代码存在的偏差

下列内容必须以代码为准，否则面试或后续开发容易误判：

| 文档/配置表象 | 真实代码 | 影响 |
|---|---|---|
| 架构文档称自动压缩默认阈值为 0.80 | 配置默认值是 0.0，即默认关闭：[config.py](../../src/code_rook/core/config.py#L58) | 长对话默认不会自动压缩 |
| 配置支持 `llm.router` | 除解析和保存配置外没有路由器消费它：[config.py](../../src/code_rook/core/config.py#L39) | “多模型路由”目前只是配置占位 |
| Agent Profile 支持 `model` | Loader 读取该字段，但 SpawnAgent 未使用它：[agents/loader.py](../../src/code_rook/core/agents/loader.py#L14) | 不同子 Agent 实际共用父 Provider |
| 配置支持 `tool_result_limit/keep` | 读取历史时调用的是模块固定常量 8000/4000：[compact/budget.py](../../src/code_rook/core/compact/budget.py#L5) | 用户配置不生效 |
| Runner 注释称后台任务注册表跨 run 共享 | Session 每次发消息都会 factory 新建 Runner：[session/manager.py](../../src/code_rook/core/session/manager.py#L121)，factory 位于 [app.py](../../src/code_rook/core/app.py#L240) | 后台 Agent 结果不能可靠跨 turn 查询 |
| 架构文档描述 unit/integration/e2e 三层 | 当前只有 `tests/unit` 和 `tests/integration` | 缺少真实终端、daemon 重启和跨平台 E2E |
| 架构文档包含生产部署方向 | `Dockerfile` 当前为空，仓库也无 CI 配置 | 尚无可验证的生产交付路径 |

建议以后为架构文档增加“Implemented / Planned / Deprecated”状态，避免设计目标和当前事实混写。

## 5. 能力成熟度矩阵

| 维度 | CodeRook 当前能力 | Claude Code 官方能力基线 | 当前评分 | 优先级 |
|---|---|---|---:|---|
| Agent loop | 多步 ReAct、工具回填、max steps、基础错误终止 | 可中断、可 steering、工具/子任务调度、计划与验证链路 | 5/10 | P1 |
| 代码检索 | read/list/bash 间接检索 | Read、Glob、Grep、LSP、代码诊断 | 3/10 | P0 |
| 文件编辑 | 整文件 overwrite | 精确 Edit、读后编辑校验、冲突检测、Diff、Checkpoint | 2/10 | P0 |
| Shell 执行 | 超时、合并输出、64 KB 截断 | 前后台任务、监控、输出落盘、权限规则、沙箱 | 3/10 | P0 |
| 权限系统 | allow/ask/deny、一次/永久审批、正则启发式 | 路径/命令/域/MCP/Agent 细粒度规则、模式、组织策略 | 3/10 | P0 |
| OS 隔离 | 无 | 文件系统和网络沙箱，子进程继承约束 | 0/10 | P0/P2 |
| Session | JSONL transcript、chat/one-shot、compact | 连续持久化、列表、搜索、命名、resume、fork、export | 3/10 | P0 |
| 回滚恢复 | compact 前备份 | 文件 checkpoint、代码/对话分别 rewind | 1/10 | P0 |
| Context/Memory | global/project context、notes、summary | CLAUDE.md 层级、rules、imports、nested context、auto memory | 3/10 | P1 |
| LLM Provider | Anthropic 流式、OpenAI-compatible、基础 usage | 模型切换、effort/fast、fallback、成本、SDK 控制 | 4/10 | P1 |
| Skills | 显式 slash、三层查找、allowed tools | 标准 frontmatter、自动发现、支持文件、hooks、fork context | 3/10 | P2 |
| Subagents | 前后台、角色、深度 2、结果轮询 | resume、消息、独立模型/权限/skills/MCP、worktree、teams | 4/10 | P2 |
| MCP | stdio/TCP、tools/list/call | stdio/HTTP/SSE/WS、OAuth、resources/prompts、动态更新、重连 | 3/10 | P2 |
| Hooks/Plugins | 无统一机制 | 生命周期 hooks、插件包、市场、版本与作用域 | 0/10 | P2 |
| Trace/Observability | IPC/Event/LLM JSONL、回放 | 成本统计、OpenTelemetry、指标、组织监控 | 5/10 | P1 |
| TUI/UX | 实时 token、工具块、权限卡片、compact、skill | session picker、diff、tasks、可配快捷键/状态栏、IDE/Desktop/Web | 4/10 | P1 |
| Headless/SDK | 简单 `coderook run` | JSON/stream-json、JSON Schema、Python/TS SDK、审批回调 | 2/10 | P1 |
| CI/交付 | 本地安装和 Makefile | GitHub/GitLab CI、安装升级、诊断、企业部署 | 2/10 | P2 |
| 测试与质量 | 单元/集成较多，Mypy 通过 | 跨平台 E2E、回归、性能、安全、长期兼容 | 5/10 | P0 |

整体约为 **4/10 的可运行产品骨架**。这并不意味着项目价值低；相反，它已经越过“API 包装器”阶段。只是成熟 Coding Agent 的大量工作发生在模型调用之外。

## 6. 逐维度详细差距

### 6.1 Agent Loop 与运行控制

#### 当前实现

- 每一步先调用 LLM。
- 模型返回多个 tool calls 时，通过 `for` 循环依次执行：[loop.py](../../src/code_rook/core/loop.py#L93)。
- 支持 `end_turn`、`max_steps`、LLM 异常和自动压缩检查。
- Core 将每个 session send 作为异步 task 启动，但没有公开 cancel handler。

#### 与 Claude Code 的差距

- 没有用户按键中断正在执行的工具。
- 没有在 Agent 运行期间输入新指令进行 steering；Session lock 忙时直接返回 `SESSION_BUSY`：[session/manager.py](../../src/code_rook/core/session/manager.py#L77)。
- 没有 `run.cancel`、`task.stop` 或进程树取消协议。
- 无 Plan mode、AskUserQuestion、明确的“执行前计划审批”。
- 无依赖感知的并行 tool calls；模型即使一次返回多个独立读取，也会串行执行。
- 异常最终大多折叠成 `llm_error`，难以区分认证、额度、网络、模型协议和内部错误。

#### 建议

引入 `RunController`，持有 run task、cancel token、steering queue 和状态机。把状态从简单的 success/failed 扩展为：

`queued -> running -> waiting_permission/waiting_user -> cancelling -> cancelled/succeeded/failed`

工具执行应支持：

- 只读且无依赖的工具并行。
- 有副作用的工具默认串行。
- 用户取消时终止子进程树、子 Agent 和等待中的权限 Future。
- 新消息可进入 steering queue，在当前安全点注入下一轮上下文。

### 6.2 代码理解与检索工具

#### 当前实现

核心工具只有 ReadFile、ListDir、WriteFile、Bash，加上 Task、Note、Subagent、MCP：[runner.py](../../src/code_rook/core/runner.py#L97)。

模型可以借助 Bash 调 `rg`，但这不等价于稳定的 Grep/Glob 工具：

- 不同操作系统可用命令不同。
- Bash 权限提示增加摩擦。
- 输出格式没有结构化文件名、行号、截断元数据。
- 无法统一应用工作区边界与忽略规则。

#### Claude Code 基线

官方工具包括 Read、Glob、Grep、LSP、Edit、NotebookEdit、Bash/PowerShell、Monitor、WebFetch、WebSearch、MCP Resources 等，详见 [Tools reference](https://code.claude.com/docs/en/tools-reference)。

#### 建议的最小工具集

第一阶段至少增加：

| 工具 | 核心参数 | 必要行为 |
|---|---|---|
| `glob` | pattern, path, limit | 支持 `**`、稳定排序、截断标记、工作区限制 |
| `grep` | pattern, path, glob, output_mode | 基于 ripgrep，返回 path/line/content 结构 |
| `edit_file` | path, old_text, new_text, replace_all | 精确匹配、唯一性检查、并发修改检测 |
| `apply_patch` | patch | 多文件统一 diff、失败时原子回滚 |
| `git_diff` | scope | 面向模型和 TUI 的结构化改动摘要 |

第二阶段再接 LSP、Notebook 和 Web 工具。

### 6.3 文件编辑、Diff 与 Checkpoint

#### 当前风险

`WriteFileTool` 直接调用 `Path.write_text` 覆盖整个文件：[write_file.py](../../src/code_rook/core/tools/builtin/write_file.py#L59)。

缺少：

- 读后编辑约束。
- old/new 精确匹配。
- 文件在读取后被用户修改时的冲突检测。
- 临时文件加 rename 的原子写入。
- 变更前后 diff。
- 每轮 checkpoint 和 rewind。
- 多文件修改失败后的事务式回滚。

整文件覆盖会增加 token 消耗，也容易丢失用户并发修改。它是当前复杂编码任务最直接的质量瓶颈。

#### Claude Code 基线

Claude Code 的 Edit 会做精确替换、匹配唯一性和文件状态校验；Checkpoint 会记录工具编辑前的文件状态，并支持分别恢复代码、对话或两者。参考 [Edit tool behavior](https://code.claude.com/docs/en/tools-reference#edit-tool-behavior) 与 [Checkpointing](https://code.claude.com/docs/en/checkpointing)。

#### 建议

建立 `EditEngine`：

1. Read 返回 `content_hash`。
2. Edit 必须携带 last-seen hash 或精确 old text。
3. 写入临时文件，fsync 后原子替换。
4. 写入前由 `CheckpointStore` 保存原文件。
5. 发布 `file.edit_planned/applied/conflicted` 事件。
6. TUI 渲染统一 diff，并允许逐文件接受/拒绝。

### 6.4 文件系统与 Bash 安全

这是当前最高优先级问题。

#### P0-1：绝对路径绕过工作区

Read/Write/List 工具只检查 `Path(path).parts` 是否包含 `..`：

- [read_file.py](../../src/code_rook/core/tools/builtin/read_file.py#L40)
- [write_file.py](../../src/code_rook/core/tools/builtin/write_file.py#L48)
- [list_dir.py](../../src/code_rook/core/tools/builtin/list_dir.py#L49)

它们没有检查 `is_absolute()`，也没有在 `resolve()` 后验证目标位于 workspace root。Windows 的 `C:/...` 和 Unix 的 `/...` 都可能直接访问工作区外文件。与此同时，read/list 默认自动允许：[permissions/policy.py](../../src/code_rook/core/permissions/policy.py#L43)。

这使工具描述中的“必须是相对路径”没有被真正执行。

#### P0-2：所有 runtime_error 都会重试

调用器把 `runtime_error` 和 `rate_limited` 都标记为可重试，并最多执行三次：[tools/invocation.py](../../src/code_rook/core/tools/invocation.py#L27)、[tools/invocation.py](../../src/code_rook/core/tools/invocation.py#L144)。

这可能重复执行：

- 已产生部分副作用但返回非零码的 Bash。
- 已写入成功、但后续包装异常的文件操作。
- 创建工单、发消息、扣费等 MCP 调用。

重试策略必须由工具声明幂等性，而不能按通用错误类型决定。

#### P0-3：Bash 无 OS 级隔离

`BashTool` 直接使用 `create_subprocess_shell`：[bash.py](../../src/code_rook/core/tools/builtin/bash.py#L48)。目前有超时和 64 KB 输出截断，这是优点；但没有：

- 文件系统沙箱。
- 网络域名限制。
- 环境变量/密钥过滤。
- CPU、内存、进程数限制。
- 子进程树可靠终止。
- 工作目录强制边界。
- 容器、Windows Sandbox 或 WSL2 隔离。

官方 Claude Code 将权限规则与 OS 级文件/网络沙箱分开处理，参考 [Sandboxing](https://code.claude.com/docs/en/sandboxing)。

#### 修复设计

新增单一 `WorkspaceBoundary`，所有 File、Bash、MCP resource、LSP 路径都必须经过它：

1. 拒绝空路径、设备路径、UNC、绝对路径（除非明确位于已授权根）。
2. `resolve(strict=False)` 后使用 `is_relative_to(allowed_root)` 判断。
3. 防御 symlink/junction 逃逸。
4. 区分 read roots 和 write roots。
5. 所有错误返回结构化 `boundary_violation`，不可重试。

工具基类增加：

- `side_effect: none/local_write/external_write`
- `idempotency: safe/conditional/unsafe`
- `retry_policy`
- `required_capabilities`

### 6.5 权限模型

#### 当前实现

- 默认 policy + allow/deny regex + outside-cwd heuristic。
- 支持 allow once、deny once、always allow、always deny。
- session cache key 是 `(session_id, tool_name)`。
- 持久 cache key 只有 `tool_name`：[permissions/manager.py](../../src/code_rook/core/permissions/manager.py#L48)。

#### 问题

- “Always allow bash”接近允许后续所有 Bash，而不是只允许某条命令或命令前缀。
- 没有针对 Read/Edit 的 path glob 规则。
- 没有 Web domain、MCP server/tool、Subagent type 粒度。
- outside-cwd 依赖正则猜测 shell 字符串，不能覆盖脚本、变量、重定向和间接进程。
- 无用户/项目/本地/组织 managed settings 层级。
- 无 workspace trust；克隆项目中的配置可能直接参与运行。
- 无独立的 permission mode，例如 plan、accept edits、deny by default。

Claude Code 官方权限支持工具 specifier、路径、命令、域名、MCP、Agent 等细粒度规则，并按 deny/ask/allow 优先级处理，参考 [Permissions](https://code.claude.com/docs/en/permissions)。

#### 建议规则格式

```toml
[permissions]
default_mode = "ask"

allow = [
  "read:**/*",
  "grep:**/*",
  "bash:git status",
  "bash:pytest *"
]

ask = [
  "edit:src/**",
  "bash:*"
]

deny = [
  "read:**/.env",
  "read:**/*secret*",
  "bash:rm -rf *",
  "network:*"
]
```

规则决策后仍需由 sandbox 执行硬边界，不能把安全完全寄托在字符串匹配上。

### 6.6 Session、持久化与崩溃恢复

#### 当前实现

SessionStore 能写 `meta.json`、`thread.jsonl`、`notes.md`，压缩前也会备份 transcript。这一层是可利用的基础。

#### 关键缺陷

1. `SessionManager` 的索引只在 `_sessions` 内存字典：[session/manager.py](../../src/code_rook/core/session/manager.py#L50)。
2. `_get_session` 找不到内存对象时直接返回 SESSION_NOT_FOUND，不会从 `meta.json` 重建：[session/manager.py](../../src/code_rook/core/session/manager.py#L190)。
3. Core 启动时创建空 SessionManager，没有扫描已有 session。
4. TUI 每次启动都调用 `session.create`：[tui/app.py](../../src/code_rook/tui/app.py#L843)。
5. 没有 list、resume、rename、fork、delete、export RPC。
6. 本轮 assistant/tool messages 在 run 完成后才批量落盘：[runner.py](../../src/code_rook/core/runner.py#L249)。进程崩溃时可能只留下 user message 和 events，thread 不完整。
7. 同一 Session 忙时拒绝新消息，无法把消息排队为 steering。

因此当前的“SessionResumedEvent”只表示同一 daemon 进程中的 waiting session 再次收到消息，不代表退出后可恢复。

#### Claude Code 基线

Claude Code 会持续保存会话，支持 session picker、continue/resume、命名、branch/fork、export，并保留 checkpoint。参考 [Manage sessions](https://code.claude.com/docs/en/sessions)。

#### 建议

实现 `SessionRepository`：

- daemon 启动时扫描 meta，并懒加载 transcript。
- 增加 schema_version 和迁移器。
- 每个 assistant message、tool call、tool result 都立即 append，使用 sequence number。
- run 启动/结束写 WAL 状态；启动时把 running 标记为 interrupted。
- 新增 list/resume/rename/fork/delete/export RPC。
- Fork 使用 copy-on-write transcript 引用，权限 session cache 不继承。
- 增加 transcript retention、归档和损坏恢复。

### 6.7 Context、Memory 与压缩

#### 当前实现

- 只加载 `~/.coderook/context.md` 与项目根 `.coderook/context.md`：[runner.py](../../src/code_rook/core/runner.py#L163)。
- Session notes 会加入 system context。
- tool results 在读取 transcript 时截断。
- 支持手动 `/compact` 和可配置自动阈值。

#### 差距

- 不读取仓库已有的 `CLAUDE.md` 或 `AGENT.md`。
- 没有用户、项目、local、managed 层级和优先级。
- 没有目录嵌套规则、按路径按需加载的 rules。
- 没有 `@import` 或动态上下文。
- 无自动记忆提取、记忆查看/编辑、过期和来源标记。
- 只用字符数除以 4 粗估 token：[compact/compactor.py](../../src/code_rook/core/compact/compactor.py#L113)。
- 自动压缩默认关闭。
- 压缩把整个历史替换为 summary + ack，缺少“保留最近窗口、分层裁剪、去重、压缩抖动保护”。
- 配置中的 tool-result 截断值没有接线。
- system prompt override 会替换基础 prompt，Skill 可能意外丢失核心 Agent 行为约束。

Claude Code 支持多层 CLAUDE.md、目录规则、imports、auto memory 和上下文查看，参考 [Memory](https://code.claude.com/docs/en/memory)。

#### 建议的 ContextAssembler

上下文按稳定顺序构建：

1. Core safety/system contract。
2. 用户级 instructions。
3. repo root instructions。
4. 当前文件路径命中的 scoped rules。
5. Session memory/notes。
6. Skill 附加指令。
7. 最近对话窗口。
8. 压缩摘要。
9. 当前目标和 steering message。

每段携带 source、priority、token estimate、cacheability，并在 TUI 提供 `/context` 检视。

### 6.8 LLM Provider、路由与成本

#### 当前实现

- Anthropic Provider 使用流式接口，有 prompt cache 和网络重试。
- OpenAI-compatible Provider 可接当前第三方 endpoint。
- 有 token usage/context percentage 事件。

#### 差距

- OpenAI-compatible 实际是一次性 HTTP POST，响应完成后才发布整段 token：[openai_compatible.py](../../src/code_rook/core/llm/openai_compatible.py#L89)。
- OpenAI-compatible 无明确重试、退避、Retry-After、流式 tool-call delta 处理。
- 模型 context window 依赖硬编码映射。
- `llm.router` 未实现。
- Subagent profile 的 model 未接线。
- 无运行时 `/model`、effort/thinking、fast mode。
- 无 fallback、熔断、预算上限和 per-run cost。
- Provider 错误没有统一的可诊断分类。
- Trace 默认包含 LLM payload：[config.py](../../src/code_rook/core/config.py#L48)，可能将源码、提示词、tool output 和秘密长期写盘。

#### 建议

定义稳定的 Provider contract：

- streaming text/tool deltas。
- normalized error taxonomy。
- retry-after 和指数退避。
- capability discovery：tools、vision、thinking、context window、streaming。
- per-run model override。
- Router 根据任务类型、上下文长度、预算和失败状态选择模型。
- usage 记录 input/output/cache/cost/latency/TTFT。
- 默认对 Trace 做 secret redaction，payload 默认关闭。

### 6.9 Skills

#### 当前实现

- 支持 builtin、user、project 三层查找。
- 支持 `name`、`description`、`allowed_tools`。
- 仅在用户消息以 `/` 开头时显式触发：[session/manager.py](../../src/code_rook/core/session/manager.py#L103)。

#### 差距

- frontmatter 是手写行解析器，不是 YAML parser：[skills/loader.py](../../src/code_rook/core/skills/loader.py#L27)。
- 任意以 `- ` 开头的 frontmatter list item 都可能被当成 allowed tool：[skills/loader.py](../../src/code_rook/core/skills/loader.py#L53)。
- 不支持自动模型调用、禁用模型调用、是否用户可见、model、context fork、agent、hooks 等字段。
- 不会把 skills 的描述清单暴露给模型做自动选择。
- 缺少 supporting files、scripts、assets、动态 context、命名空间和版本。
- Skill body 作为 system override，而非受约束地追加到稳定系统提示。
- 无 skill lint、eval 和冲突诊断。

Claude Code Skills 支持开放 Agent Skills 结构、自动/显式调用、supporting files 和丰富 frontmatter，参考 [Skills](https://code.claude.com/docs/en/skills)。

### 6.10 Subagents 与多 Agent

#### 当前实现

- 支持 foreground/background。
- 子 Agent 有独立 ExecutionContext。
- 支持 planner/executor/reviewer profile 和 allowed tools。
- 支持最多两层嵌套。
- 后台任务通过 `agent_result` 轮询。

#### 差距

- 子 Agent 不继承 global/project context、notes、skills 或完整父上下文：[subagent/tool.py](../../src/code_rook/core/subagent/tool.py#L123)。
- profile.model 被解析但不生效。
- 子 Agent registry 不包含父级 MCP 和 Note 工具：[subagent/tool.py](../../src/code_rook/core/subagent/tool.py#L235)。
- 无 resume、send message、stop、steer。
- 无共享任务认领、依赖和 mailbox。
- 后台 registry 生命周期绑定 Runner，而 Runner 每个 turn 重建。
- 并发 Agent 共享同一工作区，没有 worktree 隔离，可能相互覆盖文件。
- Registry 没有完成任务清理和持久化恢复。

Claude Code 的 subagents 支持独立模型/权限/skills/MCP、恢复和隔离；更进一步还有 Agent Teams、共享任务和消息，参考 [Subagents](https://code.claude.com/docs/en/sub-agents) 与 [Agent teams](https://code.claude.com/docs/en/agent-teams)。

#### 建议顺序

先把“单个可恢复子 Agent”做好，再做 Teams：

1. daemon 级 AgentRegistry。
2. profile.model/permission/skills/MCP 真正接线。
3. stop/resume/send_message。
4. 子 Agent 状态和结果持久化。
5. 可选 git worktree 隔离。
6. 最后再做共享 task board 和 team messaging。

### 6.11 MCP

#### 当前实现

- 支持 stdio 和自定义 raw TCP。
- 完成 initialize、notifications/initialized、tools/list、tools/call。
- 工具自动注册进 ToolRegistry。

代码证据：[mcp/client.py](../../src/code_rook/core/mcp/client.py#L72)、[mcp/server.py](../../src/code_rook/core/mcp/server.py#L57)。

#### 差距

- 缺少标准 Streamable HTTP、SSE、WebSocket transport。
- 无 OAuth、headers helper、scope 和 workspace trust。
- 只消费 text content，忽略 image/resource/embedded resource。
- 无 resources、prompts、roots、sampling、elicitation。
- 无 list_changed 动态刷新。
- 无断线重连、健康状态、用户可见诊断。
- 无分页、输出 token 限制、per-server timeout。
- 无 tool search/deferred loading；工具多时 schema 会占满 context。
- 无项目/用户/组织多层配置和审批。

Claude Code 官方 MCP 支持 HTTP/SSE/stdio/WebSocket、OAuth、resources/prompts、动态更新、重连、工具搜索和多作用域配置，参考 [MCP](https://code.claude.com/docs/en/mcp)。

### 6.12 Hooks、Plugins 与自动化

CodeRook 当前没有统一生命周期扩展点。虽然 EventBus 可观测，但订阅事件不等于可阻止或修改行为的 Hook。

缺少典型事件：

- SessionStart/SessionEnd。
- UserPromptSubmit。
- PreToolUse/PostToolUse/ToolFailure。
- PermissionRequest。
- PreCompact/PostCompact。
- SubagentStart/Stop。
- Notification/Stop。

成熟 Hook 需要：

- command、HTTP、prompt 或 MCP handler。
- 超时和并发策略。
- 可返回 allow/deny/modified input/additional context。
- 项目 Hook 的 workspace trust。
- 可观测日志和失败隔离。

Claude Code 还可通过 Plugins 打包 Skills、Agents、Hooks、MCP 和配置，参考 [Hooks](https://code.claude.com/docs/en/hooks) 与 [Plugins](https://code.claude.com/docs/en/plugins)。

### 6.13 Transport、并发与背压

#### 当前实现

自定义 TCP JSON-RPC/NDJSON 协议简单清晰，也方便调试。但目前：

- 无连接认证和 TLS。
- host 可配置；若绑定非 loopback，任何可访问端口的进程都可能发命令。
- Socket handler task 用 `create_task` 启动但没有完整生命周期和配额管理。
- EventBus 按订阅顺序逐个 await：[events/bus.py](../../src/code_rook/core/events/bus.py#L17)。
- EventWriter 在 event loop 中同步 write + flush：[events/writer.py](../../src/code_rook/core/events/writer.py#L32)。
- Broadcaster 对每个客户端逐个 `drain`：[transport/ipc_broadcaster.py](../../src/code_rook/core/transport/ipc_broadcaster.py#L59)。

慢磁盘或慢客户端可能拖慢 Agent loop。也缺少队列上限、丢弃策略、断线重放游标和协议版本协商。

#### 建议

- 默认只允许 loopback，并使用启动时生成的本地 bearer token。
- 每个订阅者使用有界 queue 和独立 writer task。
- 关键事件不可丢，token delta 可合并或丢弃。
- EventWriter 批量异步落盘，不在主循环每条 flush。
- 增加 protocol_version、capabilities、heartbeat、last_event_seq。
- 给 command 增加 timeout、request size、rate limit 和 client identity。

### 6.14 TUI、CLI、IDE 与 Headless

#### 当前优点

TUI 已能显示 token、工具块、权限审批、上下文水位、Skill 和 compact；相比纯日志输出更适合作为项目 Demo。

#### 差距

- 每次启动新 Session，无历史选择、恢复和分支。
- 缺少 diff viewer、文件变更列表、任务面板、后台进程面板。
- 无运行中 cancel/steer。
- 快捷键和主题不可配置。
- 无模型、权限模式、成本、git branch 等状态栏。
- 无图片/文件预览和链接导航。
- CLI 子命令仅 ping/chat/run/core/trace：[cli/main.py](../../src/code_rook/cli/main.py#L22)。
- `coderook run` 不订阅 permission 事件，也没有批准回调：[cli/commands/run.py](../../src/code_rook/cli/commands/run.py#L89)。当工具需要审批时，一次性运行可能一直等到权限超时。
- 无 `--resume`、`--model`、`--permission-mode`、`--output-format json`、`--json-schema`。
- 无 Python/TypeScript SDK、IDE、Desktop、Web 或 CI 原生接口。

Claude Code 的 headless 模式支持 JSON/stream-json、JSON Schema、continue/resume、allowed tools，并提供 Agent SDK，参考 [Programmatic usage](https://code.claude.com/docs/en/headless) 与 [Agent SDK](https://code.claude.com/docs/en/agent-sdk/overview)。

### 6.15 Trace、隐私与生产可观测性

#### 当前优点

同时记录 IPC、Event、LLM 层，且能按 run 回放，这是本项目较鲜明的设计亮点。

#### 差距

- payload 默认开启，隐私默认值不安全。
- 无 API key、token、cookie、`.env` 内容的 redaction。
- 无日志轮转、保留期和磁盘配额。
- 无 trace schema version。
- 无 token cost、TTFT、tool success rate、compaction rate、permission wait 等指标。
- 无 OpenTelemetry exporter。
- 无跨 session/run 的聚合诊断。

Claude Code 官方提供 usage/cost 与 OpenTelemetry 监控能力，参考 [Monitoring](https://code.claude.com/docs/en/monitoring-usage)。

### 6.16 测试、跨平台与交付

审计实测：

| 检查 | 结果 |
|---|---|
| Mypy | 87 个 source files，0 errors |
| Pytest | 268 passed，1 skipped，7 failed |
| Ruff | 27 个问题 |
| Docker | `Dockerfile` 为空 |
| CI | 未发现 `.github/workflows` |

7 个测试失败中：

- 1 个 Bash timeout 用例在 Windows 上把 `sleep 5` 解析为普通命令失败，因此得到 runtime_error 而非 timeout。
- 6 个 Compactor 用例依赖 `asyncio.get_event_loop()` 在无当前 event loop 时的旧行为，与 Python 3.12/Windows 运行方式不兼容。

这说明单元测试数量不错，但跨平台约束尚未稳定。还缺少：

- daemon 重启后恢复 session 的 E2E。
- TUI 与真实 Core 的端到端测试。
- 文件系统边界、安全规则和 symlink/junction 测试。
- 进程树 timeout/cancel 测试。
- 并发 session、慢客户端和背压测试。
- MCP 断线/重连和协议兼容测试。
- 大 transcript、压缩抖动、损坏 JSONL 恢复测试。
- 基准测试与长时间 soak test。

## 7. 上线前必须处理的风险清单

| ID | 风险 | 严重度 | 直接后果 | 首选修复 |
|---|---|---|---|---|
| SEC-01 | File tools 接受绝对路径 | P0 | 读取/覆盖工作区外文件 | 统一 WorkspaceBoundary + 路径测试 |
| REL-01 | runtime_error 自动重试有副作用工具 | P0 | 重复命令、重复写入、重复外部操作 | 工具声明幂等性，仅安全操作重试 |
| SES-01 | daemon 重启无法加载 Session | P0 | 已有会话无法继续 | SessionRepository 扫描/懒加载 |
| SES-02 | run 完成后才写本轮 transcript | P0 | 崩溃丢上下文、留下 orphan tool use | 消息和工具结果增量持久化 |
| CLI-01 | `coderook run` 无审批处理 | P0 | 默认 ASK 工具会被动等待到超时 | permission mode / callback / fail-fast |
| SEC-02 | Bash 无硬沙箱 | P0/P2 | 提示注入可执行高风险操作 | 先收紧能力，后上 OS sandbox |
| SEC-03 | Trace 默认记录完整 payload | P0 | 源码与秘密落盘 | 默认关闭 payload + redaction |
| SEC-04 | TCP 无认证 | P1 | 非 loopback 暴露时可被调用 | loopback 强制 + 本地 token |
| EDIT-01 | 整文件覆盖且无冲突检测 | P0 | 丢失用户修改、难以回滚 | EditEngine + checkpoint |
| SUB-01 | 后台 Agent registry 随 Runner 重建 | P1 | 跨 turn 结果丢失 | daemon 级持久 registry |
| PERF-01 | EventBus/Writer/Broadcaster 串行阻塞 | P1 | 慢客户端拖慢主循环 | 有界队列与独立 writer |
| DOC-01 | 文档把规划项写成现状 | P1 | 误导开发与面试陈述 | 状态标签 + 自动文档校验 |

## 8. 分阶段优化路线

### Phase 0：先建立可信边界（1–2 周）

目标：不增加花哨功能，先让现有闭环“不会越界、不会重复副作用、不会轻易丢会话”。

任务：

1. 实现 WorkspaceBoundary，修复绝对路径、symlink/junction 和 read/write root。
2. 将 Tool 重试改为显式幂等策略；Bash、Write、MCP external write 默认不重试。
3. Session 启动扫描和按 ID 懒加载；消息/tool result 增量落盘。
4. 增加 `run.cancel`，并可靠终止 Bash 进程树和 Subagent。
5. 修复 `coderook run` 权限等待：支持 `--permission-mode` 或遇 ASK 立即非零退出。
6. Trace payload 默认 false，并增加 secret redaction、轮转和 retention。
7. 默认拒绝非 loopback Core 监听；加入本地认证 token。
8. 修复 7 个测试失败和 Ruff 基线；建立 Windows/Linux CI。

验收：

- 任意绝对路径和逃逸路径均被拒绝。
- 同一有副作用工具失败时只执行一次。
- 强杀 daemon 后可恢复 Session，并能识别 interrupted run。
- Headless 模式不会被动等待完整的权限超时。
- `pytest`、`mypy`、`ruff` 在 Windows/Linux 全绿。

### Phase 1：成为可用的 Coding Agent（3–6 周）

目标：提高真实代码任务成功率，而非继续堆外围概念。

任务：

1. 增加 Glob、Grep、Edit、ApplyPatch、GitDiff。
2. 增加 read hash、并发修改检测、原子写、checkpoint 和 rewind。
3. 实现 session list/resume/rename/fork/export 与 TUI session picker。
4. 实现 run steering、AskUserQuestion、Plan mode 和任务停止。
5. OpenAI-compatible 真流式、统一错误分类、退避与 Retry-After。
6. 接通 profile.model 和 llm router；增加 per-run model/effort/cost。
7. ContextAssembler 支持 CLAUDE.md/AGENT.md、层级规则和 `/context`。
8. TUI 增加 diff、changed files、tasks/background、model/cost 状态。

验收：

- 可独立完成“检索 -> 多文件精确修改 -> 测试 -> 展示 diff -> 用户回滚”。
- TUI 退出、Core 重启后能恢复到同一 Session。
- 用户可在 Bash 或长任务中取消，并且无残留子进程。
- 文件被用户并发修改时 Edit 拒绝覆盖并要求重新读取。

### Phase 2：扩展与自动化（6–12 周）

目标：把现有 Skills/Subagents/MCP 从“最小实现”升级为稳定扩展面。

任务：

1. Skills 使用 YAML parser 和标准 frontmatter，支持 supporting files、自动选择、fork context、hooks。
2. 增加 Hook runtime：Pre/Post Tool、Session、Prompt、Compact、Subagent。
3. MCP 增加 Streamable HTTP、OAuth、resources/prompts、动态更新、重连、tool search。
4. Subagent 增加持久 registry、resume/message/stop、独立 model/permission/skills/MCP。
5. 并发写任务使用可选 git worktree 隔离。
6. 提供 `--output-format json/stream-json`、JSON Schema 和 Python SDK。
7. 提供 GitHub Actions/GitLab CI 示例。
8. 增加 OpenTelemetry、成本面板、安全审计日志。

验收：

- 一个扩展包可同时声明 Skill、Hook、Agent 和 MCP。
- MCP 断线可恢复，失败状态对用户和模型均可见。
- 子 Agent 可跨 turn 查询、停止和恢复，不会覆盖主 Agent 工作区。
- SDK 能处理流式事件、权限回调和结构化结果。

### Phase 3：产品化与生态（长期）

可选方向：

- IDE extension、Desktop/Web UI、远程任务。
- Plugin manifest、版本依赖、签名和 marketplace。
- Agent Teams、共享 task board、消息协议。
- Browser/computer-use、图像和 artifact。
- 企业 managed settings、SSO、审计、策略分发。
- 远程 sandbox worker、资源配额和多租户。

这些不应早于 Phase 0/1，否则会在不可靠的底座上放大复杂度。

## 9. 推荐的目标架构

建议保留当前 Core/TUI 解耦，但把核心职责进一步收敛：

| 组件 | 责任 |
|---|---|
| `RunController` | run 状态机、取消、steering、等待用户、子任务生命周期 |
| `ContextAssembler` | 指令层级、memory、skills、recent window、token budget |
| `ToolRuntime` | schema、权限、workspace boundary、sandbox、幂等重试、事件 |
| `EditEngine` | 精确编辑、hash、原子写、diff、checkpoint |
| `SessionRepository` | 增量 transcript、WAL、resume/fork、schema migration |
| `ProviderRouter` | capability、stream、error、retry、model、budget、fallback |
| `ExtensionManager` | Skills、Hooks、Agents、MCP、Plugins 的发现和作用域 |
| `AgentRegistry` | 前后台 Agent、状态持久化、stop/resume/message |
| `EventPipeline` | 有界队列、序列号、落盘、广播、重放、背压 |
| `Observability` | trace redaction、metrics、cost、OTel、retention |

推荐依赖方向：

- UI/CLI/SDK 只依赖协议，不直接依赖 runtime。
- AgentLoop 只依赖 Provider、Context、ToolRuntime、RunController。
- Tool 实现不能自行绕过 WorkspaceBoundary 和 Permission。
- SessionRepository 是 run 状态的唯一持久化事实源。
- EventPipeline 是可观测输出，不作为唯一恢复数据源。

## 10. 建议 Backlog

| 顺序 | Issue | 预估 | 价值 |
|---:|---|---:|---|
| 1 | 修复 File absolute path / symlink escape | 1–2d | 安全 P0 |
| 2 | Tool 幂等性与重试策略 | 1–2d | 可靠性 P0 |
| 3 | 修复测试、Ruff、Windows shell 适配 | 1–2d | 建立稳定基线 |
| 4 | Session rehydrate + list/resume RPC | 2–4d | 会话可恢复 |
| 5 | transcript 增量写和 interrupted run | 2–3d | 防数据丢失 |
| 6 | run.cancel + process tree kill | 2–4d | 可控执行 |
| 7 | headless permission mode | 1–2d | CLI 真正可用 |
| 8 | Trace redaction/rotation/default | 1–2d | 隐私 |
| 9 | Glob/Grep | 2–3d | 代码理解 |
| 10 | EditEngine + atomic write | 3–5d | 编辑质量 |
| 11 | Diff + checkpoint/rewind | 4–7d | 安全修改 |
| 12 | TUI session picker + diff panel | 4–7d | Demo 和日用 |
| 13 | OpenAI true streaming + normalized errors | 3–5d | 体验和兼容 |
| 14 | ContextAssembler + instruction hierarchy | 4–7d | 长任务稳定 |
| 15 | profile.model/router 接线 | 2–4d | 多模型策略 |
| 16 | Event queues/backpressure | 3–5d | 并发稳定 |
| 17 | Skills 标准化 | 3–5d | 扩展性 |
| 18 | MCP HTTP/OAuth/resources/reconnect | 1–3w | 生态 |
| 19 | Persistent AgentRegistry/worktree | 1–2w | 多 Agent |
| 20 | SDK/JSON output/CI integration | 1–2w | 自动化 |

## 11. 质量指标与验收体系

不要只用“功能是否存在”评价 Agent。建议建立下面的可量化指标：

### 任务成功率

- SWE-bench 风格的小型仓库任务通过率。
- 首次修改后测试通过率。
- 平均需要用户纠正次数。
- 多文件任务完整率。

### 编辑质量

- 精确 Edit 成功率。
- 错误覆盖/冲突次数。
- 每个任务修改行数与无关改动比例。
- checkpoint 回滚成功率。

### 可靠性

- daemon crash 后恢复成功率。
- orphan tool call 数量。
- 取消后残留进程数。
- MCP/LLM 短暂失败后的恢复率。

### 安全

- 工作区逃逸测试通过率。
- 未授权网络请求阻断率。
- secret redaction 覆盖率。
- 高风险 Bash 误放行率。

### 性能与成本

- 首 token 延迟、完整 turn 延迟。
- tool queue 等待时间。
- 每任务 input/output/cache tokens 和成本。
- compaction 前后 token 节省与任务质量变化。

### 工程质量

- Windows/Linux/macOS 测试矩阵。
- 单元、集成、E2E、安全、性能测试分层。
- protocol/session schema 兼容测试。
- Ruff、Mypy、覆盖率和发布 smoke test。

## 12. 作为秋招项目应如何定位

当前项目可以用于 Agent 方向秋招，但推荐定位为：

> 从零实现的本地 Coding Agent Runtime，重点探索 Agent Loop、工具执行安全、事件驱动架构、会话与上下文治理、Subagent 和 MCP 扩展。

不要宣称“复刻 Claude Code”或“生产级替代品”。更可信的表达是：

- 已实现 Claude Code 类产品的核心运行闭环。
- 通过代码审计识别了成熟产品与教学实现之间的安全、恢复和扩展差距。
- 正在以 WorkspaceBoundary、精确 Edit、Session Resume、Run Cancel 和 Checkpoint 为核心推进第二阶段。

最有说服力的 Demo 不是 ASCII Logo，而是：

1. 从历史 Session 恢复。
2. 用 Grep/Glob 定位跨文件问题。
3. 精确修改并实时展示 Diff。
4. 自动运行测试。
5. 用户中途取消或纠正。
6. 一键 rewind。
7. 展示完整事件 Trace、token/cost 和安全审批记录。

完成 Phase 0 和 Phase 1 后，这个项目的秋招说服力可提升到约 8/10，因为它能同时展示 Python 异步、协议设计、安全边界、上下文工程、终端 UI、测试和 Agent 产品判断。

## 13. 官方对标资料

本报告使用的 Claude Code 官方资料：

- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works)
- [Tools reference](https://code.claude.com/docs/en/tools-reference)
- [Manage sessions](https://code.claude.com/docs/en/sessions)
- [Checkpointing](https://code.claude.com/docs/en/checkpointing)
- [Permissions](https://code.claude.com/docs/en/permissions)
- [Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Memory](https://code.claude.com/docs/en/memory)
- [Skills](https://code.claude.com/docs/en/skills)
- [Hooks](https://code.claude.com/docs/en/hooks)
- [Subagents](https://code.claude.com/docs/en/sub-agents)
- [Agent teams](https://code.claude.com/docs/en/agent-teams)
- [MCP](https://code.claude.com/docs/en/mcp)
- [Plugins](https://code.claude.com/docs/en/plugins)
- [Programmatic usage](https://code.claude.com/docs/en/headless)
- [Agent SDK overview](https://code.claude.com/docs/en/agent-sdk/overview)
- [Monitoring](https://code.claude.com/docs/en/monitoring-usage)
- [VS Code integration](https://code.claude.com/docs/en/ide-integrations)

## 14. 最终判断

CodeRook 已经完成了最难讲清楚的第一步：它不是单纯调用 Chat API，而是有 Agent loop、工具协议、权限、事件、会话、压缩和扩展边界的完整教学型 Runtime。

下一阶段不应继续追求“功能名字更多”，而应优先完成五件事：

1. 工作区硬边界。
2. 精确编辑和 checkpoint。
3. 可恢复 Session 与增量持久化。
4. 可取消、可 steering 的运行控制。
5. 可验证的跨平台测试和安全基线。

这五项完成后，CodeRook 才会从“结构完整的 mini Agent”真正迈入“可以长期使用的 Coding Agent”。
