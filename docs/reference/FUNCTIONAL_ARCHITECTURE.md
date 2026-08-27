# CodeRook 功能架构

本文按当前 `src/code_rook/` 代码组织描述系统，不记录历史实施过程、测试数量或未来计划。跨进程字段的
精确定义以自动生成的 [Wire Protocol](WIRE_PROTOCOL.md) 为准；HTTP 行为以
[Runtime API](RUNTIME_API.md) 为准。

本文的“存在”不等于“v1 稳定承诺”。`runtime.capabilities` / `GET /v1/capabilities` 返回当前
feature level：durable threads/turns、cursor replay、receipts、interrupt/steer、permission response、
Provider Catalog/readiness、Checkpoint、Change Center、有界 Goal、基础子 Agent、Skills、MCP Tools
和 Memory 位于 `stable`；Tool Program、ACP Worker backend、fleet、declarative workflow、Hooks v2、MCP Resources/Prompts 和 VS Code
原型位于 `labs`；Runtime 投影、trace degraded state 和协议生成是 `internal`。
`labs_enabled` 单独报告实验面是否被维护者激活：默认 `false`，只有进程启动前显式设置
`CODEROOK_LABS=1` 才为 true。feature flag 出现在 `labs` 表示代码能力存在，不表示默认可调用。

## 1. 系统边界

CodeRook 是本地优先的双进程 Coding Agent runtime：

```text
coderook / coderook-tui
        |
        | JSON-RPC 2.0 over authenticated NDJSON/TCP
        v
coderook-core
  ├─ agent loop / tools / permissions / provider routes
  ├─ sessions ledger + SQLite runtime projection
  ├─ background processes / MCP / workers / workflows
  └─ HTTP/JSON + SSE Runtime API
        ^
        | HttpOnly Cookie + same-origin HTTP/SSE
CodeRook Web SPA
```

- `coderook-core` 是状态所有者；默认 IPC 为 `127.0.0.1:7437`。
- HTTP Runtime API 默认是 `127.0.0.1:7438`。
- `coderook` 无参数时启动 TUI；`coderook tui` 是显式别名，`coderook web` 打开本机 Web。
- TUI 退出不会删除 daemon 中的 thread、turn、worker 或后台任务状态。

## 2. 进程启动与关闭

`src/code_rook/core/app.py` 的 `CoreApp.run()` 是 daemon 入口，按顺序装配：

1. 配置、日志和脱敏 trace；
2. 权限、按 Labs 开关构造的 HookManager、事件广播；
3. session ledger 与 SQLite runtime；
4. provider route、凭据、MCP、subagent/fleet/workflow；
5. HTTP API 与 IPC SocketServer。

关闭时按反向依赖顺序停止入口、后台工作和持久资源。进程启动前会探测端口，已有 Core 占用时不会
静默启动第二个 daemon。Core 固定服务其启动目录；TUI 启动器通过 `core.ping` 核对 workspace 和活动
run 数：同目录复用，其他目录的空闲受管 Core 有序重启，存在活动 run 时拒绝切换。即使使用
`--no-auto-core`，也必须通过 workspace 一致性校验。

Labs 关闭时 Core 构造空 HookManager，不读取用户/项目 Hook 配置，也不暴露或恢复 Workflow/Fleet
控制面；TUI 同时隐藏 Labs 命令。修改 `CODEROOK_LABS` 必须重启 Core，不能在运行中扩大功能面。

## 3. 协议与传输

### IPC 模型

`src/code_rook/core/bus/` 使用 Pydantic v2 模型定义命令、事件和 JSON-RPC envelope。
`Command`/`Event` 是以 `type` 为判别字段的联合类型。

- `commands.py`：认证、run、session、runtime、权限、任务、worker、workflow 和扩展管理命令。
- `events.py`：run/step、LLM、工具、审批、上下文、计划、subagent 和后台状态事件。
- `task.profiled` 在模型调用前持久化 TaskProfile；画像包含意图、范围、风险、执行策略、上下文策略、
  deliverable、success criteria、置信度和用户可见摘要。默认 `rules_only` 不增加分类调用；显式实验启用
  hybrid 时，风险和范围在合并时仍只能提高。低置信度 Turn 初始只暴露结构化提问；回答入账后用原请求
  和答案重建画像，但保留 `plan_first` 并关闭委派，防止澄清直接越过写入门禁。
- `envelope.py`：请求、成功、错误、事件推送和认证错误码。

`scripts/gen_protocol_doc.py` 从模型生成 `WIRE_PROTOCOL.md`。手写架构文档不复制字段表。

### IPC 传输

`src/code_rook/core/transport/` 实现 loopback TCP NDJSON：

- 客户端第一帧必须是 `core.authenticate`；
- token 默认位于 `~/.coderook/ipc-token`，比较使用恒定时间函数；
- 每行请求独立调度，使长任务不会阻塞审批回复等控制命令；
- 订阅支持 topic、scope、durable replay 和 high-water handoff。

### HTTP/SSE

`src/code_rook/core/api/` 是无 Web framework 的 HTTP/1.1 实现，提供：

- thread 创建、读取、更新、fork、export、delete、context/checkpoint 和 turns；
- turn 读取、interrupt、steer、items 和 receipt；
- thread 事件 SSE 与 cursor replay；
- 权限/Plan/Question 回复，图片 Artifact，受限文件读取与工作区 diff/stage/commit；
- Provider Catalog/Doctor 配置，以及 Goal、Worker、Skills、MCP 和 Memory 稳定控制面；
- 打包在 wheel 内的 React/TypeScript SPA 静态资源。

Runtime API 始终使用 Bearer token；空白环境值不能关闭鉴权。环境未显式提供非空值时，Core 会以
no-follow、普通文件和对象身份检查加载或排他创建用户级 `api-token`；POSIX 额外强制当前 owner 与
`0600`，Windows 使用可执行的重解析点/路径身份边界而不宣称 POSIX chmod。
接口清单和恢复语义见 [Runtime API](RUNTIME_API.md)。

浏览器不接触上述 Bearer token。`web.launch` 只经已认证 IPC 签发 60 秒单次票据，CLI 把票据放在
URL fragment；SPA 以严格 Host/Origin 校验交换 HttpOnly、SameSite=Strict Cookie 与内存 CSRF token，
然后移除 fragment。Cookie 写请求必须同时通过同源和 CSRF header。静态壳启用 CSP、nosniff、
referrer 禁止与 frame-ancestors 禁止；Web 入口拒绝非 loopback API binding，也不提供公网监听参数。

## 4. Agent 执行链

主要代码位于 `core/runner.py`、`core/loop.py`、`core/context.py` 和
`core/interaction.py`：

```text
接收 turn
  → 组装系统提示、会话、仓库上下文和 transient 输入
  → 解析活动 provider route
  → Plan-Act-Observe 循环
  → 工具验证 / Hook / 权限 / 执行 / 输出策略
  → 事件、transcript、runtime projection 和 receipt
  → 完成、失败、取消或等待交互
```

循环支持流式模型输出、工具调用、只读工具并行、步数续段、问题/steer、取消、限流
退避和上下文溢出恢复。三个 wire format 统一使用
`completed / tool_use / length / incomplete / content_filtered / failed / cancelled / transport_error`
终止语义：截断、Responses incomplete 和 SSE 提前 EOF 不会标记成功；长度截断最多自动续写一次，
被截断的工具参数不会以部分 JSON 执行。图片通过 ArtifactStore 以一次性 multimodal 输入交付；永久
transcript 不保存原始 base64。

每次 Provider 调用前，Loop 会把实际消息、分层 System Prompt、完整 Tool Schema、route/model/wire
format、冻结执行契约摘要、图片引用和能力预算写成不可变 `RequestSnapshot`。`llm.request_prepared`
先进入带 checksum 的 Session Ledger，再发布只含摘要和 `ledger_seq` 的公开事件；记录后快照与实际
请求不等价时不会调用 Provider。run/step/tool 公开事件同样在广播前由 Session Ledger Bridge 回填
`ledger_seq`。关键 ledger subscriber 的异常向发布方传播，并触发 `audit_degraded`，不会出现 TUI 已
显示而重连后完全消失的权威事件。

Plan 模式的结果在 `run.finished` 后发布持久 `plan.ready`。`plan.respond` 只解决同一 session 当前 run 的
未决计划，SessionManager 在 Runtime 可回放确认 `plan.resolved` 已落盘后才清除 pending；过期、重复或
持久化失败的决定都失败关闭，新 Turn 在 pending 解决前被阻断。TUI 只在 RPC 成功或收到 durable
`plan.resolved` 后移除 PlanReview；重启按真实 `run.finished → plan.ready → plan.resolved` 顺序恢复，
不会让已批准、修改或取消的旧计划复活。

## 5. 模型路由与凭据

`src/code_rook/core/llm/` 提供三种 wire format：

- `anthropic_messages`；
- `openai_chat`；
- `openai_responses`。

Route 描述 provider kind、URL、模型、凭据引用、上下文窗口和能力。TUI 与 CLI 共用 Provider
Catalog；内置项为 DeepSeek、OpenAI、Anthropic、Gemini、Kimi/Moonshot、OpenRouter、SiliconFlow、
Ollama 和 LM Studio。自定义 route 可选 `openai_chat`、`openai_responses` 或
`anthropic_messages`。Ollama/LM Studio 是仅允许 loopback 探测的免密 route。

每条 Route 保存明确的 `env:`、`keyring:`、`file:` 或 `none:` 凭据引用，解析时只访问该来源。
交互式保存优先写系统 keyring，失败后写权限收紧的用户文件；`env:` 引用中用户进程值优先于显式 env
overlay。旧 `LlmConfig` 迁移在对应环境变量已有值时保留 `env:` 引用，否则引用旧凭据文件，不复制密钥
正文。共享 `ConfigurationService` 返回不含密钥正文的 readiness；本地端点可做轻量端口探测。正常
配置事务在提交前执行脱敏 ProviderDoctor：
它对三种 wire format 验证真实事件流与正常终止，并对 route 声明的工具、并行工具和图片能力做最多
3 次小输出探针。只有全部必需分项通过，才持久化与 route/model 摘要绑定且不含响应正文的收据。
`configure`、route 新增/编辑与活动 route 切换都必须通过 Doctor；公开 CLI 不提供跳过探针的保存
入口。Router 支持 static、rule-based 和 cost-budget 策略；价格未知时成本保持 `unknown`，不会按零
成本记录。每个 Turn 在持久化开始前一次性选择并冻结完整 Route Binding；provider、模型、工具、
图片、并行工具和 thinking 能力在运行中不随配置变化，新的 route 配置只影响下一 Turn。子 Agent
默认使用创建它的冻结 provider，显式 profile route 仍需独立解析并受权限 ceiling 约束。

仓库 `.env` 不参与自动配置。显式 `--env-file` 以禁用插值的方式解析成只读 overlay，用户进程同名值
优先；overlay 不修改 `os.environ`，也不通过 IPC 发送凭据。RouteRegistry、legacy provider、CLI/TUI
readiness/Doctor/Provider 命令与 WebSearch 都使用同一 CredentialStore 语义。TUI 使用显式文件时必须
无活动任务地重启同工作区受管 Core；busy、非受管或 `--no-auto-core` 组合失败关闭。

## 6. 工具与编辑

`src/code_rook/core/tools/` 的调用管线是：

1. 根据 mode、authority、trust 和 profile 生成可见工具；
2. 校验参数 schema 和工具能力，生成 Manifest 驱动的 pending presentation；
3. 先持久化 Tool Call，再运行调用前 Hook；
4. 计算 authority、权限和 sandbox 决定，必要时等待用户；
5. 通过统一 middleware 执行并应用输出长度/Artifact 策略；
6. 运行调用后 Hook，规范化并冻结 Tool Result 与 presentation，再持久化和广播。

主要能力包括：

- 文件读取、搜索、精确编辑、patch 和目录操作；
- Git 状态、diff、历史、blame 与 worktree；
- Bash 和持久 Shell；
- 测试/verifier 运行；
- WebFetch、WebSearch 和本地图片读取；
- Python/TypeScript 编辑后诊断；
- checkpoint、rewind、task、goal 和交互控制工具。

`core/editing/` 与 `core/patching/` 负责事务编辑和 unified diff；`core/checkpoints/` 记录可恢复
文件状态。工具名称同时包含兼容的单工具名称和 action-family 名称，调用方应从 capabilities/catalog
发现，不要硬编码未协商的完整列表。

`ToolActionSpec` 是 action manifest 的唯一声明源：action 参数 schema、capability、authority action、
审批要求、并行策略、权限策略键、是否允许 Tool Program 编排和 `ToolPresentationSpec` 都从同一个
`ResolvedToolCall` 投影。TUI 按 `generic/terminal/diff/read/search/web` 展示类型使用通用卡片；未知
扩展安全降级为 generic，回放不执行仓库 UI 回调，也不重新读取文件。PermissionManager 不维护另一份
family/action alias 表；旧平铺策略名只能由对应 action 的 `permission_policy_aliases` 显式声明，并按
新精确键优先、旧别名回退读取。Catalog 在审批和执行前拒绝未知 action，权限层也会拒绝工具名或
action 与 resolved manifest 不一致的调用，避免策略 alias 扩大到同一 family 的其他 action。

## 7. 权限、安全与进程治理

`core/authority/` 将工作模式、权限姿态、工作区信任和允许动作组合为授权矩阵；
`core/permissions/` 处理策略、会话决定、命令前缀与等待回复。

Runner 在每个 Turn 开始时冻结有效 authority 与完整 Route Binding；工具 catalog、审批、模型能力和
shell sandbox plan 都读取该执行契约。修改 session 权限或 route 配置不能扩大或改变正在执行的 Turn。
Sandbox backend 在构造时也冻结真实探针结果，避免 plan 和 spawn 之间重新探测产生漂移。

`core/sandbox/` 的真实边界：

- Linux：能力探测成功时使用 bubblewrap；
- macOS：能力探测成功时使用 Seatbelt；
- Windows：真实探针成功时使用 Restricted Token + capability SID + NTFS/ReFS ACL 限制写入，报告
  `partial`；探针失败时降级 `windows_none`；
- 域名白名单没有强制后端时 fail closed。

Linux bwrap 不再只读绑定宿主 `/`，而是挂载必要系统运行时、工作区、隔离 Home 和临时目录；macOS
Seatbelt 同样默认拒绝整机读取。shell 子进程使用环境白名单，过滤常见 API key、云、SSH 与 Git
凭据变量。Windows 后端给每个工作区确定性写 SID、给每次执行随机私有临时 SID，`CreateRestrictedToken`
只在两类 capability ACE 下放行写操作；读取、网络、Everyone ACL 与 NTFS hard-link 仍是已知边界，
因此 Shell/Run 无论权限姿态都不能静默批准。

`core/audit.py` 维护进程级审计健康状态。Event ledger 或 Runtime 投影写入异常会触发一次脱敏
`audit.degraded` 事件；PermissionManager 随后拒绝所有非 READ 动作，直到显式修复流程清除状态。
Trace 写入有独立的有界队列和可见降级状态，但不冒充 durable audit ledger。

`ProcessSupervisor` 与 Windows Job Object 负责超时、取消、后代终止和资源采样；Windows ACL/Restricted
Token 才提供部分文件写边界，Job Object 本身不等同于沙箱。详细攻击面见 [威胁模型](THREAT_MODEL.md)。

## 8. 持久化与恢复

CodeRook 使用两类持久状态：

- `~/.coderook/sessions/` 的文件 ledger 是会话运行事实，包含 checksum chain 和规范化 workspace 绑定；
- `~/.coderook/goals/` 保存 session 级持久 Goal、run 引用、预算、状态机、timeline 和完成证据；
- `~/.coderook/runtime.db` 是 thread/turn/item/event 的可查询 SQLite 投影。

`core/session/` 负责 transcript、按 workspace 隔离的列表/恢复/分叉、导出和损坏检测；`coderook --continue`
只选择当前 workspace 最近更新的 session，不存在历史会话时创建新 session。
`core/runtime/` 负责 durable API 投影和启动对账。Turn Receipt 只从已持久化记录重建，不能证明的
字段列为 unavailable。

新写入的 transcript 只使用 `SessionEventEnvelope v2`：输入、模型消息、请求快照以及
run/step/tool/permission/steer/compaction/worker 事实各占一条带统一序号与 checksum 的事件，不再为
同一消息额外写入 `message/block` source row。`derive_messages()` 直接从事件投影模型历史；旧会话中的
legacy `message/block` 前缀保持只读兼容，后续追加立即使用单一 v2 格式，Compaction 也会生成纯事件
账本。SQLite 仍是可重建的查询投影，不是独立事实来源。

Compaction v2 永不改写 `thread.jsonl`。压缩事务依次追加
`context.compaction.started`、候选 `context.compaction.message`、`context.compaction.summary` 和
`context.compaction.committed`；只有最后一条 committed 事件会启用 shadow 投影。daemon 在此前强杀时，
候选摘要仍留作审计事实但不会进入 `derive_messages()`。提交后原始事件继续保留在 checksum 链中，
Runtime/TUI 只消费新的模型上下文投影。

冷启动只在 JSON、消息分组或 checksum 链损坏时归档并修复 Ledger；普通强杀产生的未配对 Tool Call
继续保留为恢复证据。模型投影会临时裁掉孤立 tool use，恢复协调器则据原始调用区分只读可重跑与
修改/命令状态未知，避免为了满足 wire format 先删除最重要的中断证据。

Runtime SQLite 当前 `PRAGMA user_version` 为 4；v4 为 `runtime_session_facades` 增加逐行
`schema_version`。数据库版本和公开记录版本彼此独立：Thread、Turn、Item、Event、Facade 当前逐行
schema 仍为 1。只读 Doctor 不迁移数据库；未来数据库版本、未来逐行版本和外键损坏均失败关闭，显式
repair 也不会把它们降级或覆盖。

`core/upgrade.py` 在 v1 Provider Catalog 迁移前为 config、routes、credentials、sessions、goals、
Runtime、Fleet 和 Workflow 数据创建带目录树/文件 SHA-256 的时间戳备份；daemon 前置备份与 CLI
Provider 写入共用短时
用户状态互斥锁。备份操作幂等，损坏、越界、摘要不一致或与 manifest 不一致的迁移标记会保留原证据并
让写入失败关闭，禁止用迁移后的状态重建所谓迁移前快照。它是迁移基础设施，不是所有历史数据 schema
的通用迁移器；两个真实公开 tag
之间的升级/回滚外部验收仍未完成。

备份 marker `provider-catalog-v1.json` 与完成收据 `provider-catalog-v1.receipt.json` 是独立证据。
收据绑定脱敏旧配置摘要、结果 Catalog 摘要及自身完整性；损坏、未来版本、冲突覆盖，或完成收据与空
Catalog 的非法组合都会拒绝自动重迁移并进入 `audit_degraded`。首次迁移若收据失败，Route Catalog
恢复原字节。`credentials.json` 当前文档版本为 2；未来/损坏文档和不安全路径保留原证据并失败关闭。

其他用户级数据库包括 `fleet.db` 和 `workflow.db`。工作区级状态位于
`<workspace>/.coderook/`，包括 memory、artifacts、worktrees、skills、agents 和 hooks。

## 9. 上下文、仓库理解与记忆

- `core/compact/`：预算估算、结构化压缩、最近窗口保留和质量门禁；
- `core/repository/`：Git-aware 增量仓库地图、符号/引用和 ranked context；
- `core/memory/`：项目级长期记忆、来源和确定性中英文词法召回；
- `core/artifacts/`：内容寻址的大输出与图片存储；
- `core/turn/`：读缓存、重复行为守卫和流看门狗。

压缩保持工具调用/结果配对，不把自动摘要当作原始 transcript 的替代事实。策略支持 `truncate`、
`structured` 和默认候选 `adaptive_evidence`。后者从事实日志固定当前目标、TaskProfile、未决审批与
失败工具，要求摘要逐项携带 `source_event_seqs`；正文或来源序号不一致时拒绝替换上下文。旧窗口内
相同内容哈希的重复工具结果只在模型视图折叠，完整正文仍留在 Ledger。触发判断使用下一请求加输出
预留后的预测占比，而非只等待当前请求超过固定阈值。

默认工具输出预算只做确定性错误提取、头尾裁剪和 Artifact 回查，不再为每个大工具结果静默调用模型。
Task Strategy 默认使用 `rules_only`：`plan_first` 初始目录只允许只读探索、提问和 `update_plan`，计划
事件携带 digest 票据并进入现有 durable Plan Review；当前 Turn 不解锁冻结目录。用户批准票据后，TUI
发起新的 Act Turn 才能按重新分类后的工具目录修改；这不是 System Prompt 中的自律建议。

## 10. 多 Agent 与编排

- `core/task/`：单次 run 的共享任务板和原子认领；
- `core/goal/`：daemon 级目标控制面；`goal.create/get/list/edit/pause/resume/complete/clear` 经 IPC 暴露，
  schema v4 保存 `auto_continue`、默认总计三 Turn/1800 秒上限、硬 token budget、permission ceiling、暂停原因、
  timeline 和证据；单轮成功只记录进度，daemon 把 `verification.completed` 登记为关联 run 的可信引用，
  Agent 的 `update_goal` 只能消费该引用，或由用户显式确认后才完成；自动 Goal 重启后进入
  `paused_needs_confirmation`，普通遗留 Goal 按中断恢复；
- `core/subagent/`：durable worker、写入声明、租约、预算和 handoff 证据；所有可写 Worker 强制进入受管
  Git worktree，完成时从固定 base commit 检查 changed files、diff stat/preview 和检查失败状态；
- `core/worktree/`：并行修改的 Git worktree 生命周期；
- `core/fleet/`：跨进程 worker 调度；
- `core/workflow/`：声明式 IR、事件溯源账本和执行器。

并行 worker 不共享未声明的写权限；写入隔离依赖强制 worktree 和 write claim，而不是模型自律。
模型只有在冻结 TaskProfile 允许委派时才能看到 `agent` 工具；启动前可用 `agent.validate_plan` 校验
最多三个 Worker 的 DAG、总预算、验收条件和 Write Claim。依赖环、父级路径逃逸、嵌套委派或任意
写入交集都会失败关闭。多个已审查 handoff 只有在 base commit 相同、digest 未漂移且文件集合互斥
时，才可通过组合补丁预检后一次应用，避免逐个应用造成基线漂移。
模型可见 `agent.start` 必须携带校验返回的随机 Delegation Ticket 和受覆盖 task ID。Core 从已验证
DAG 恢复 prompt、角色、Write Claim、预算和验收条件，忽略 start 时试图扩大的字段；同一 task 最多
启动一次，依赖 Worker 成功前后继 task 不能启动。未知角色只映射到内置 planner/executor/reviewer
profile，可写 Worker 仍自动创建独立 worktree。
当前 Labs Fleet scheduler 还不会自动创建受管 worktree，因此任何带写 claim 的 workflow worker 都在
host 进程启动前失败关闭；只读 Fleet 节点不受此限制。
稳定基础 Worker 由 daemon-owned `WorkerController` 通过统一 Route Catalog 真正启动或重试；route
readiness、模型、预算、profile digest、authority/Goal ceiling、write claim 和 worktree 都冻结进持久
记录。list/status/events/followup/retry/review/cancel/apply 必须携带并校验 parent session，不能跨 session
观察、纠偏或应用 Worker。
`worker.review` 和 TUI `/workers review <id> approve|reject --yes` 只把人工结论记录为
`reviewed_not_applied` 或 `changes_rejected`，不自动执行 apply/merge。只有 daemon typed
verification 已通过、文件 claim 可穷举、Diff 未截断、人工已批准且审查 digest 重验一致时，
`worker.apply` 才能在独占 workspace mutation 门闩中将完整补丁应用为未暂存改动。它要求主仓库
HEAD 仍是 Worker 基线且工作区干净，先用临时 index 预检，冲突、越界、过期 digest 或部分写入都
fail closed。该路径不执行 commit、push 或自动 merge。

Labs ACP backend 通过统一 `WorkerBackend/WorkerHandle` 契约接入同一控制中心。Backend 必须在启动前
声明 one-shot、continuation、structured output、persona、tool restriction、read-only guarantee、resume
和 live events 能力；调用方要求了不支持的能力会明确失败。ACP 只从用户显式 `CODEROOK_ACP_COMMAND`
装配，进程环境以脱敏基础加显式 extension overlay 构造，永远在独立受管 worktree 运行；只读声明发生
改动会失败，写入结果仍必须经过既有 Diff、review、verification 和 apply。Windows 标记为 partial
enforcement，不宣传为 OS 沙箱。首发不承诺 continuation 或跨 daemon resume。

`SessionManager` 在 Goal run 结束后持久化并发布 typed continue decision；显式
`auto_continue=true` 且决策允许时，会在 session 锁释放后重新读取 Goal 真值并自动创建下一 Turn。
`max_auto_turns` 包含首轮而不是额外续轮数；每一 Turn 都受自动窗口剩余墙钟的 `asyncio.timeout`
硬 deadline 约束，超时会取消 runner/进程树并持久化为需要确认的暂停。
transient transport/stream 超时采用最长 30 秒的有限退避；认证、配置、安全、验证与未分类 LLM 故障
不会自动重试；意外调度异常把 Goal fail closed 为
blocked。新 `goal.create`、GoalService 和 TUI 创建默认 `auto_continue=true`；旧持久记录缺失该字段时
`GoalRecord` 仍按 false 读取，避免升级后静默自动化。有界 Goal 属于 stable capability；
重启后仍不会在用户不知情时自动继续。没有 completion criteria 的 Goal 不会立刻暂停，仍受默认三
Turn/1800 秒和可选 token budget 限制；声明的 criteria 全被 daemon 验证证据覆盖时则暂停等待验证引用
或用户显式验收，模型自然语言自报不能完成 Goal。

## 11. 扩展

`core/capabilities.py` 提供轻量、内部的 Capability Kernel，而不重写 daemon：Provider Catalog、MCP
Manager、Hook Manager 和外部 Worker Backend 在 workspace scope 注册，单次 Turn 的 Tool Registry 在
session scope 注册并在结束时撤销。贡献按 `global → workspace → session → worker` 作用域解析，最近
作用域优先，同层重复 ID 失败，注册返回幂等 disposer，daemon shutdown 会清空 workspace 及后代贡献。
安全相关调用方应读取完整 contribution chain 并取权限、sandbox 和环境交集，不能用近层覆盖扩大权限。

Session Header 冻结 `AgentPreset` ID 与摘要。`standard` 和 `minimal` 是稳定组合；`tool-program` 是
Labs。非空 Session 不原地切换 Preset，TUI `/preset` 会创建保留来源关系的 fork。Tool Program 只支持
`call/sequence/parallel/if` 和受限 `$ref`，限制 16 节点、6 层、并发 4、120 秒；不支持 eval、import、
循环、递归、嵌套 Program 或控制类动作，每个子调用仍进入完整工具管线。

- Skills（stable）：用户级或项目级指令包，带严格 schema、来源、digest 和信任检查；
- Hooks v2（Labs）：生命周期子进程扩展，支持 blocking/fail-closed，rerun 继续执行 workspace trust gate；
- MCP：Tools 是 stable capability；Resources/Prompts 与动态配置属于 Labs。CodeRook 作为 client
  管理 stdio、TCP、legacy SSE 和 Streamable HTTP server；
- Agents：planner、executor、reviewer 等角色 profile，带来源、digest 和严格字段校验；
- Background：daemon 生命周期内可查询和取消的后台任务。

第三方扩展不自动成为可信代码。MCP 的已验证范围见
[MCP 互操作合同](MCP_COMPATIBILITY.md)。

## 12. 客户端

- `src/code_rook/tui/`：主产品界面；先创建/恢复 session，再以 `thread_id + after_seq` 订阅 durable
  任务事件。每个 session 独立保存 cursor，切换时用 typed `event.unsubscribe` 只撤销当前 writer 拥有的
  旧 thread 订阅；全局 daemon 事件与 thread timeline 分通道。活动 Turn 导致 resume busy 时只读附着
  权威 thread，重新读取 transcript 后按原 cursor 订阅并恢复未决交互。激活准备或交付失败会设置
  `requires_replay` fence，后续高 `seq` 不能跨缺口确认；失败的新订阅单独清理，重试从最后确认游标补交。
  统一 reducer 处理 payload、route/retry/reasoning、active run 和未决交互；Goal 读取独立投影，composer
  读取 session-scoped 本地快照。结束后优先从
  Turn Receipt 生成结果卡；`tool_use/length/incomplete` 显示为不完整，cancelled 显示为中断，
  content-filtered/transport-error 保持独立；readiness 卡不会强制弹出配置界面；`/language` 保存用户级
  `zh-CN`/`en-US` 偏好，
  stable shell、命令、选择器、审批、管理面板、事件和结果卡使用集中式文案，Labs Workflow 图、协议值、
  日志与第三方动态文本保留技术原文。默认布局是单一专注时间线：顶栏只展示仓库、模型和
  `run.phase_changed` 阶段，底栏展示 mode、权限、Sandbox、上下文、成本和 queue；`Ctrl+O` 渐进展开
  推理与工具详情，`@file` 仅建立有界路径引用，`!command` 仍进入权限和 Sandbox 管线；
- `web/` + `src/code_rook/web/static/`：React/Vite 源码与打包静态 SPA。Web 使用与 TUI 相同的 durable
  thread/event/receipt、Plan、权限、Recovery、Provider 与 Change Center 语义；页面刷新后从最后
  `seq` 重放。浏览器只调用 Core API，不读取 runtime.db、Ledger、凭据文件或未经过 WorkspaceBoundary
  的路径。API Key 只作为一次配置请求 body 交给 Core，不写入 Web Storage。PlatformBridge 隔离通知、
  剪贴板和外部打开能力，便于后续嵌入桌面壳；当前不包含 Electron/Tauri；
- `core/change_center.py`：`state_digest` 是绑定 scope、canonical visible payload、精确 symbolic ref/commit、
  index、tracked worktree 与 untracked 内容的审查令牌。stage 只消费 `all` 令牌，在真实 `index.lock` 下从
  原 index 字节构造私有 index，只发布用户点名且 `review_complete=true` 的词法路径，并验证未选路径的
  sparse/split、skip-worktree 与 assume-unchanged 语义不变；成功后返回新的 `staged` 令牌。commit 只消费
  staged 令牌，以 exact-ref CAS 提交已审查 tree，并从真实 commit 对象回读文件列表；取消或异常后只在
  ref 仍指向候选 commit 时 CAS 回滚。未出生/orphan 分支可创建首次提交，detached HEAD 拒绝提交；子目录
  workspace 存在边界外 staged 内容时失败关闭；
- `tui/panels/changes.py`：全屏 overlay 合并当前 diff 与最近 durable receipt，支持文件/hunk 以及
  rename/copy、mode-only、binary/opaque metadata 导航；tracked 不透明内容用 old/new blob 长度和
  SHA-256 形成可见证据，untracked mode 也绑定审查摘要。GitDiff 的 subdirectory/特殊路径输出与
  `files[].path`、hunk、metadata 精确对应；无法安全归属时 `review_complete=false`，POSIX 字面反斜杠名称
  保持身份后由 stage 门禁拒绝。展示层不会代替 stage/commit 的 typed 显式确认。stage 成功后 TUI 强制
  展示 Core 返回的最终 staged payload（包括已有 index 内容），commit 仍要求第二次独立确认；
- Change Center typed handler 在任何 Git mutation 前要求整个 workspace 无活动 Turn、健康审计投影、
  可信 workspace 和显式确认；冲突、截断、路径越界、ref/index/worktree 竞态或令牌漂移全部失败关闭，
  不运行 hook/signing/push；
- `src/code_rook/cli/`：ping、配置、provider、session、headless run/review、诊断和 Core 管理；
- `src/code_rook/sdk/`：同步/异步 Runtime API 客户端；
- `editors/vscode/`：只使用公开 Runtime API 的 VS Code 原型，不拥有独立 daemon 状态。

## 13. 配置和数据位置

配置优先级从低到高：

```text
内置默认值
  → ~/.coderook/config.toml
  → <workspace>/.coderook/config.toml
  → 调用方显式指定的 env 文件
  → 用户进程显式 CODEROOK_* 环境变量
```

仓库 `.env` 不自动加载；`coderook`/`coderook-core --env-file <path>` 只读取用户显式选择的文件，
自动启动 daemon 时会转发同一绝对路径。显式 env 文件禁用变量插值、不改变进程环境，也不能设置
`CODEROOK_CONFIG`；进程环境同名值优先。由于未持久化 daemon 配置指纹，显式 env 会安全重启空闲受管
Core，无法证明 overlay 身份时 fail closed。
`CODEROOK_CONFIG` 只接受
用户进程环境中的值并指定单一 TOML。未知配置键导致启动失败；项目 TOML 禁止写入 route 安全字段，
即使被显式指定也不能绕过。
完整用户操作见 [使用说明](../guides/USER_GUIDE.md)。

## 14. 验证边界

测试覆盖协议、loop、工具、权限、路由、持久化、SDK、TUI/CLI、benchmark 和发行脚本。集成测试使用
隔离的 HOME/USERPROFILE、随机端口和占位模型配置，不读取开发者真实密钥。

日常 CI 定义为单个 Ubuntu required job；跨平台安全、恢复、互操作和分发矩阵只接受手动 dispatch 或
release tag。workflow 文件存在不证明远端启用、运行或通过；当前发布结论和未完成项见
[发布评分卡](../status/RELEASE_SCORECARD.md)。
