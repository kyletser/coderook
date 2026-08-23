# CodeRook 功能架构

本文按当前 `src/code_rook/` 代码组织描述系统，不记录历史实施过程、测试数量或未来计划。跨进程字段的
精确定义以自动生成的 [Wire Protocol](WIRE_PROTOCOL.md) 为准；HTTP 行为以
[Runtime API](RUNTIME_API.md) 为准。

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
```

- `coderook-core` 是状态所有者；默认 IPC 为 `127.0.0.1:7437`。
- HTTP Runtime API 默认是 `127.0.0.1:7438`。
- `coderook` 无参数时启动 TUI；CLI 子命令主要用于脚本与诊断。
- TUI 退出不会删除 daemon 中的 thread、turn、worker 或后台任务状态。

## 2. 进程启动与关闭

`src/code_rook/core/app.py` 的 `CoreApp.run()` 是 daemon 入口，按顺序装配：

1. 配置、日志和脱敏 trace；
2. 权限、Hook、事件广播；
3. session ledger 与 SQLite runtime；
4. provider route、凭据、MCP、subagent/fleet/workflow；
5. HTTP API 与 IPC SocketServer。

关闭时按反向依赖顺序停止入口、后台工作和持久资源。进程启动前会探测端口，已有 Core 占用时不会
静默启动第二个 daemon。

## 3. 协议与传输

### IPC 模型

`src/code_rook/core/bus/` 使用 Pydantic v2 模型定义命令、事件和 JSON-RPC envelope。
`Command`/`Event` 是以 `type` 为判别字段的联合类型。

- `commands.py`：认证、run、session、runtime、权限、任务、worker、workflow 和扩展管理命令。
- `events.py`：run/step、LLM、工具、审批、上下文、计划、subagent 和后台状态事件。
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

- thread 创建、读取、更新和 turns；
- turn 读取、interrupt、steer、items 和 receipt；
- thread 事件 SSE 与 cursor replay；
- 权限回复、工作区 diff、capabilities 和 usage。

非 loopback 监听必须配置 Bearer token；接口清单和恢复语义见 [Runtime API](RUNTIME_API.md)。

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

循环支持流式模型输出、工具调用、只读工具并行、步数续段、每步 route 刷新、问题/steer、取消、限流
退避和上下文溢出恢复。图片通过 ArtifactStore 以一次性 multimodal 输入交付；永久 transcript 不保存
原始 base64。

## 5. 模型路由与凭据

`src/code_rook/core/llm/` 提供三种 wire format：

- `anthropic_messages`；
- `openai_chat`；
- `openai_responses`。

Route 描述 provider kind、URL、模型、凭据引用、上下文窗口和能力。内置 CLI preset 包括
`anthropic`、`openai`、`openai-compatible`、`anthropic-compatible` 和
`opencode-zen`；TUI 的平台选择器提供 DeepSeek、OpenAI、Anthropic 和硅基流动。

凭据解析顺序为 keyring、权限受限文件和环境变量引用。ProviderDoctor 在提交配置前执行脱敏探测。
Router 支持 static、rule-based 和 cost-budget 策略；价格未知时成本保持 `unknown`，不会按零成本记录。

## 6. 工具与编辑

`src/code_rook/core/tools/` 的调用管线是：

1. 根据 mode、authority、trust 和 profile 生成可见工具；
2. 校验参数 schema 和工具能力；
3. 运行调用前 Hook；
4. 计算权限决定，必要时等待用户；
5. 执行并应用输出长度/Artifact 策略；
6. 运行调用后 Hook，写入事件和 receipt。

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

## 7. 权限、安全与进程治理

`core/authority/` 将工作模式、权限姿态、工作区信任和允许动作组合为授权矩阵；
`core/permissions/` 处理策略、会话决定、命令前缀与等待回复。

`core/sandbox/` 的真实边界：

- Linux：能力探测成功时使用 bubblewrap；
- macOS：能力探测成功时使用 Seatbelt；
- Windows：没有文件系统/网络强制后端，明确降级到审批链和工作区边界；
- 域名白名单没有强制后端时 fail closed。

`ProcessSupervisor` 与 Windows Job Object 负责超时、取消、后代终止和资源采样，不等同于文件系统
沙箱。详细攻击面见 [威胁模型](THREAT_MODEL.md)。

## 8. 持久化与恢复

CodeRook 使用两类持久状态：

- `~/.coderook/sessions/` 的文件 ledger 是会话运行事实，包含 checksum chain；
- `~/.coderook/goals/` 保存 session 级持久 Goal、run 引用、预算、状态机、timeline 和完成证据；
- `~/.coderook/runtime.db` 是 thread/turn/item/event 的可查询 SQLite 投影。

`core/session/` 负责 transcript、恢复、分叉、导出和损坏检测；
`core/runtime/` 负责 durable API 投影和启动对账。Turn Receipt 只从已持久化记录重建，不能证明的
字段列为 unavailable。

其他用户级数据库包括 `fleet.db` 和 `workflow.db`。工作区级状态位于
`<workspace>/.coderook/`，包括 memory、artifacts、worktrees、skills、agents 和 hooks。

## 9. 上下文、仓库理解与记忆

- `core/compact/`：预算估算、结构化压缩、最近窗口保留和质量门禁；
- `core/repository/`：Git-aware 增量仓库地图、符号/引用和 ranked context；
- `core/memory/`：项目级长期记忆、来源和确定性中英文词法召回；
- `core/artifacts/`：内容寻址的大输出与图片存储；
- `core/turn/`：读缓存、重复行为守卫和流看门狗。

压缩保持工具调用/结果配对，不把自动摘要当作原始 transcript 的替代事实。

## 10. 多 Agent 与编排

- `core/task/`：单次 run 的共享任务板和原子认领；
- `core/goal/`：daemon 级目标控制面；`goal.create/get/list/edit/pause/resume/complete/clear` 经 IPC 暴露，
  每轮由 SessionManager 注入持久 Goal 上下文；单轮成功只记录进度，Agent 必须通过 `update_goal` 提交
  具体证据或由用户显式确认后才完成，失败或重启中断进入 blocked 并保留原因；
- `core/subagent/`：durable worker、写入声明、租约和预算；
- `core/worktree/`：并行修改的 Git worktree 生命周期；
- `core/fleet/`：跨进程 worker 调度；
- `core/workflow/`：声明式 IR、事件溯源账本和执行器。

并行 worker 不共享未声明的写权限；写入隔离依赖 worktree 和 write claim，而不是模型自律。

## 11. 扩展

- Skills：用户级或项目级指令包，带来源、digest 和信任检查；
- Hooks：生命周期子进程扩展，支持 blocking/fail-closed；
- MCP：CodeRook 作为 client 管理 stdio、TCP、legacy SSE 和 Streamable HTTP server；
- Agents：planner、executor、reviewer 等角色 profile；
- Background：daemon 生命周期内可查询和取消的后台任务。

第三方扩展不自动成为可信代码。MCP 的已验证范围见
[MCP 互操作合同](MCP_COMPATIBILITY.md)。

## 12. 客户端

- `src/code_rook/tui/`：主产品界面，负责连接恢复、斜杠命令、事件渲染、审批和管理面板；
- `src/code_rook/cli/`：ping、配置、provider、session、headless run/review、诊断和 Core 管理；
- `src/code_rook/sdk/`：同步/异步 Runtime API 客户端；
- `editors/vscode/`：只使用公开 Runtime API 的 VS Code 原型，不拥有独立 daemon 状态。

## 13. 配置和数据位置

配置优先级从低到高：

```text
内置默认值
  → ~/.coderook/config.toml
  → <workspace>/.coderook/config.toml
  → .env
  → CODEROOK_* 环境变量
```

`CODEROOK_CONFIG` 指定单一 TOML。未知配置键导致启动失败；项目 TOML 禁止写入 route 安全字段。
完整用户操作见 [使用说明](../guides/USER_GUIDE.md)。

## 14. 验证边界

测试覆盖协议、loop、工具、权限、路由、持久化、SDK、TUI/CLI、benchmark 和发行脚本。集成测试使用
隔离的 HOME/USERPROFILE、随机端口和占位模型配置，不读取开发者真实密钥。

仓库包含 CI、发行、安全、恢复和互操作 workflow 定义，但 GitHub Actions 当前关闭。历史运行结果
只证明绑定 commit；当前发布结论和未完成项见 [发布评分卡](../status/RELEASE_SCORECARD.md)。
