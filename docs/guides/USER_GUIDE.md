# CodeRook 使用说明

**适用基线**：`0.2.0-beta.1` 候选版

**主要入口**：`coderook`

**产品界面**：TUI 与本地 Web；CLI 用于脚本、诊断和无人值守任务

CodeRook 是本地优先的 Coding Agent。它可以理解仓库、规划与修改代码、运行验证、保留可恢复会话，
并通过事件、Diff、Turn Receipt 和结果卡说明一次执行到底发生了什么。当前尚未发布 PyPI 或 GitHub
Release；本文先以源码安装为准。发布状态只以[发布评分卡](../status/RELEASE_SCORECARD.md)为准。

## 1. 安装与首次启动

需要 Python 3.12、Git 和 [`uv`](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
uv run coderook
```

也可以打开共享同一会话和 Core 的本地 Web 工作区：

```bash
uv run coderook web
uv run coderook web C:\path\to\repo
uv run coderook web --no-open
uv run coderook tui
```

`coderook web` 只绑定本机回环地址，自动启动或复用当前仓库 Core，并打开最近会话。若空闲的
受管 Core 正绑定其他仓库，会安全重启到新仓库；存在活动任务时则拒绝切换。页面无模型时仍可浏览
会话、文件、Diff、设置和帮助，第一次提交任务前才执行 readiness 检查。

浏览器通过 URL fragment 内 60 秒单次票据换取 HttpOnly Cookie；fragment 随即清除。所有写请求
还必须通过同源与 CSRF 校验。Provider API Key 只提交给本地 Core 的配置事务，不进入 URL、日志、
localStorage 或普通响应。刷新页面后以 durable event seq 续接，不会重新执行工具。

无参数 `coderook` 会启动 TUI，并自动复用或启动当前工作区的 `coderook-core`。首次打开时不会强制
配置 API：没有模型也可以查看帮助、历史会话和设置。界面会显示一张非阻塞 readiness 卡。

首次提交普通任务前，TUI 会再次检查：

- 是否存在活动 route；
- 远端 route 的凭据能否解析；
- Ollama/LM Studio 等本地 route 的 loopback 端口是否可达。

检查不通过时，草稿保持在输入框中，也不会创建一个注定失败的 run。按卡片提示使用 `/config`、
`/provider` 或 `/doctor` 修复后再提交。

默认裸启动会选择当前工作区最近的非空会话；没有历史才新建会话。也可以显式指定：

```bash
uv run coderook --continue
uv run coderook --resume SESSION_ID
uv run coderook --new
```

`--new` 或 TUI 内的 `/new` 都会显式创建新会话。

Core 与 session 都绑定工作区。从另一个仓库启动时，空闲的受管 Core 可以有序切换；若旧工作区仍有
活动 run，则拒绝切换，避免 Agent 在错误目录执行。`--no-auto-core` 禁止 TUI 启动或恢复 Core，适合
手动排障。

## 2. 配置 Provider 与模型

### Provider Catalog

TUI `/config` 与 CLI route 管理使用同一份 Provider Catalog：

| 预设 | 凭据 | 协议/说明 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | OpenAI Chat compatible |
| OpenAI | `OPENAI_API_KEY` | OpenAI route，支持图片能力标记 |
| Anthropic | `ANTHROPIC_API_KEY` | Anthropic Messages |
| Gemini | `GEMINI_API_KEY` | Gemini OpenAI-compatible endpoint |
| Kimi / Moonshot | `MOONSHOT_API_KEY` | OpenAI Chat compatible |
| OpenRouter | `OPENROUTER_API_KEY` | OpenAI Chat compatible |
| SiliconFlow | `SILICONFLOW_API_KEY` | OpenAI Chat compatible |
| Ollama | 无 | 默认探测 `127.0.0.1:11434` |
| LM Studio | 无 | 默认探测 `127.0.0.1:1234` |

还可以创建自定义 `openai_chat`、`openai_responses` 或 `anthropic_messages` route。模型选择器允许使用
Provider 返回的模型，也允许手工填写模型 ID；能力以 route 的明确字段为准，不根据模型名称猜测。

### 配置方法

TUI：

```text
/config
/provider
/model
/doctor
```

CLI：

```bash
uv run coderook configure
uv run coderook provider list
uv run coderook provider add local --preset ollama --activate
uv run coderook provider test local
uv run coderook provider use local
uv run coderook model list --route local
uv run coderook config-status
```

新增、编辑或切换 route 默认先运行 ProviderDoctor，再原子提交 route、活动项和凭据。Doctor 使用最多
3 个有界请求：必查真实流式响应和正常终止；route 声明支持工具、并行工具或图片时，相应能力也必须
真实通过。声明不支持的能力显示 `unsupported`，`not_run` 或任何必需项失败都不能生成可提交收据。
公开 CLI 不提供跳过 Doctor 的保存入口；`configure`、route 新增/编辑和 `provider use` 都必须先完成
当前 route 声明能力对应的探针。探针失败时事务回滚，不会留下半写入的活动 route 或凭据引用。

密钥优先写入系统 keyring；不可用时降级到权限受限的 `~/.coderook/credentials.json`。列表、日志、
readiness 与诊断结果只显示凭据来源和脱敏状态，不显示密钥正文。
Doctor 收据只保存 route/model 摘要、时间和分项状态，不保存请求密钥、响应正文或工具参数。
每条 Route 只解析自身明确的 `env:`、`keyring:`、`file:` 或 `none:` 引用，不会遍历其他凭据来源。
`credentials.json` 当前文档版本为 2；v1 会在下一次受管写入时升级。未来版本、未知字段、损坏 JSON、
符号链接或不安全父目录都会保留原证据并失败关闭。

### 配置来源安全

优先级从低到高为：

```text
内建默认值
  → ~/.coderook/config.toml
  → <workspace>/.coderook/config.toml
  → 显式指定的 env 文件
  → 当前用户进程环境变量
```

仓库根目录 `.env` **不再自动读取**。只有用户显式传入 `--env-file <path>`（或集成方显式调用
配置加载 API 的 `env_file` 参数）时，指定文件才参与本次加载；TUI 自动启动的 Core 会收到同一个
绝对路径。该文件不能设置 `CODEROOK_CONFIG`。例如：

```powershell
uv run coderook --env-file C:\secrets\coderook.env
uv run coderook-core --env-file C:\secrets\coderook.env
```

显式文件以 `interpolate=false` 解析，`${NAME}` 不会再从宿主环境展开；读取只形成进程内 overlay，
不修改 `os.environ`，也不通过 IPC 发送密钥。同名用户进程环境值优先，显式空值也会抑制文件值。
TUI/CLI 的 readiness、Doctor、Provider 增删改查、Core route 和 WebSearch 共用这份 overlay。
WebSearch endpoint 可以来自 overlay，但结构化搜索凭据固定使用受管的 `file:web-search` 引用；项目
endpoint 或 overlay 不能指定任意环境变量名来读取其他用户秘密。

为避免误连到使用另一组凭据的 daemon，带 `--env-file` 的 TUI 每次都会先停止当前工作区无活动任务的
受管 Core，再用同一路径重启。Core 正在运行任务、不是 CodeRook 受管进程、停止后端口仍被占用，或
组合使用 `--no-auto-core --env-file` 时都会 fail closed；当前没有把 env 文件身份持久化成 daemon
配置指纹。

项目
`.coderook/config.toml` 永远不能设置 `provider`、`base_url`、`api_key_env` 或
`active_route_id`，即使通过显式配置路径指向它也不能绕过限制。

行为配置可选择 `agent.task_router = "rules_only"`、`agent.delegation_policy = "routed"` 和
`compaction.strategy = "adaptive_evidence"`。默认路由不额外调用分类模型：确定性规则先识别安全边界，
低置信度任务初始只开放一次结构化提问；用户回答后 Core 用“原请求 + 回答”重新生成画像，但仍保留
`plan_first` 安全门禁。复杂或含糊修改只开放只读探索和 `update_plan`；计划票据签发后 Core 展示
计划审阅卡，当前 Turn 仍保持只读。用户批准后 TUI 才创建新的 Act Turn，因此未批准计划不能在原
Turn 内获得修改工具。
`hybrid`、`llm_only`、`single`、`always_delegate`、`truncate` 等值主要
用于可复现实验，不会降低现有权限或沙箱门禁。完整证据入口见
[可靠长任务实验指南](RELIABILITY_EXPERIMENTS.md)。

用户进程中显式设置的 `CODEROOK_*` 环境变量仍是最高优先级。不要在仓库文件中保存真实 key。

## 3. 一次任务的完整闭环

建议按以下顺序工作：

1. **理解**：先让 Agent 读取相关代码、测试和约束。
2. **规划**：复杂任务使用 `/plan <任务>`，在只读模式审查方案。
3. **执行**：切换到 `act`，核对审批卡中的工具、路径和命令。
4. **验证**：要求运行与风险相匹配的测试、静态检查或构建。
5. **结果**：查看 run 结束后的结果卡，不用一段自然语言回答代替证据。
6. **审查**：使用 `/changes`（`/diff` 为兼容别名）、`/review` 与 `/turn` 对照改动和持久收据。
7. **恢复**：方向错误时使用 `/rewind` 预览并确认恢复点。

Plan Turn 完成后会产生持久 `plan.ready` 审批，而不会自动进入写入模式。批准、要求修改或取消都通过
typed `plan.respond` 发送；Core 只接受当前 session/run 的未决计划，并先持久化 `plan.resolved`，TUI 才
清除审批卡。daemon/TUI 重启会从 Runtime 重建最终状态，已解决的旧计划不会复活；决定落盘前也不能在
同一 session 创建新 Turn。只有批准分支会在 readiness 仍通过时启动新的 Act Turn。

### 结果卡

成功、失败和中断都会生成结果卡。TUI 优先读取持久 Turn Receipt；投影短暂未就绪时会有限重试，
再回退到本次事件证据。卡片可以显示：

- 状态、耗时和 step 数；
- route、model 和可得的成本；
- 修改文件或“证据不足”；
- 验证通过/失败/不可用；
- 未验证项与安全失败分类；
- `/changes`、`/review`、`/rewind`、`/turn` 入口。

“任务完成”不等于所有事项已经验证。缺少持久证据时，卡片必须显示 unavailable/unverified，而不是
猜测成功。
`run.finished.status` 是兼容旧客户端的粗粒度状态；结果卡优先使用可选 `outcome`。`tool_use`、
`length` 和 `incomplete` 显示为“不完整”，`cancelled` 显示为“已中断”，`content_filtered` 和
`transport_error` 分别显示，均不会并入成功。

### Change Center

`/changes` 打开可聚焦的全屏改动中心，`/diff` 保留为兼容别名。面板合并当前 `workspace.diff` 与
最近一次 durable Turn Receipt：用 `j/k` 切换文件、`n/p` 切换 hunk，展示验证命令到路径的映射，
并明确标出冲突、验证失败、缺少收据和 diff 截断；按 `Esc` 返回时间线。返回的 `state_digest` 是审查
令牌，不是权限 token：它绑定 scope、规范化后的完整可见 payload、精确 symbolic HEAD/ref 与 commit、
index、tracked worktree 和 untracked 内容。`/stage` 只接受 `scope=all` 的令牌；成功响应产生新的
`scope=staged` 令牌，`/commit` 只接受该 staged 审查。分支/ref、index、worktree、未跟踪内容或可见
payload 任一变化，都要求重新审查。stage 成功后 TUI 会强制打开最终 staged 视图，其中包含 index
原来已有但本次未选择的内容；用户看完并退出该视图后，仍需单独执行 `/commit ... --yes`，不会把
stage 的确认复用为 commit 确认。

每个目标文件必须为 `review_complete=true`。未跟踪 UTF-8 文本展示完整新增补丁，未跟踪二进制展示
长度和 SHA-256，且 100644/100755 mode 进入审查摘要；tracked 二进制或非 UTF-8 内容展示 old/new blob
的长度和 SHA-256。子目录 workspace 与包含空格、Tab、引号、Unicode 或首尾空格的 Git 路径必须与
`files[].path`、hunk 和 metadata 精确对应；无法安全归属就把 `review_complete` 降为 false。rename/copy、
mode-only 与 opaque metadata 也保留在可导航的文件审查中。超过 200,000 字节总可见预算、证据不全、
补丁截断、路径竞态或无法安全读取都会阻断写入。`/review [关注点]` 会在 Plan 模式提交只读审查任务，
`/rewind` 通过两步确认恢复 checkpoint。

`/stage <path...> --yes` 只把用户明确列出的词法路径和已审查内容加入 Git index；私有 index 必须保留
未选择路径的 sparse/split、skip-worktree 与 assume-unchanged 语义，否则失败关闭。`/commit <主题>
--yes` 只从全部可审查的 staged 内容创建本地 commit，并跳过仓库 hook 与签名程序，不会 push。子目录
workspace 若存在边界外 staged 文件会阻断提交；Change Center 支持未出生/orphan 分支的首次提交，但
detached HEAD 必须先切换到分支。两项写操作都要求同一 workspace 内没有任何活动 Turn、审计存储健康
且 workspace 已信任；冲突、越界路径、ref/CAS 竞态和过期令牌均失败关闭，也不会绕过 typed `--yes`。
POSIX 字面反斜杠路径不会被折叠成目录分隔符，但当前 stage 门禁会显式拒绝这类无法跨平台安全表示的
名称。

`/language zh-CN|en-US` 会把界面语言偏好保存在用户目录。稳定 TUI shell、命令、选择器、
审批、管理面板、事件提示和结果卡使用集中式中英文文案，切换后已打开的控件会立即刷新。
Labs `Workflow` 图仍保留部分中英混合的技术标签；协议状态值、日志正文、模型/插件提供的动态文本不翻译。

## 4. TUI 操作

常用键位：

| 输入 | 行为 |
|---|---|
| `Enter` | 发送消息 |
| `Shift+Enter` / `Alt+Enter` / `Ctrl+J` | 插入换行 |
| `Tab` | 循环 plan/act/operate |
| `Shift+Tab` | 循环权限姿态 |
| `Ctrl+C` | 有选择时复制；否则按提示再次取消当前任务 |
| `Ctrl+Shift+C` | 复制选择或上一条回复 |
| `Ctrl+P` | 打开分类命令面板；常用项置顶，Labs 默认隐藏 |
| `Ctrl+O` | 展开或收起推理、工具步骤与完整输出 |
| `Ctrl+Q` | 退出 TUI；会话和 Core 状态不会被删除 |

常用命令：

| 类别 | 命令 |
|---|---|
| 帮助与输入 | `/help`、`/copy`、`/history status\|on\|off\|clear`、`/attachments [remove N\|clear]` |
| 会话 | `/sessions`、`/new`、`/rename`、`/fork`、`/export`、`/delete --yes` |
| 模型 | `/config`、`/provider`、`/model`、`/doctor` |
| 执行 | `/plan`、`/goal`、`/mode`、`/permissions`、`/trust`、`/sandbox` |
| 审查 | `/changes`（`/diff`）、`/review`、`/rewind`、`/turn`、`/context`、`/cost` |
| 扩展 | `/skills`、`/mcp`、`/memory`、`/artifacts`、`/workers`、`/jobs` |
| Labs/高级 | `/preset tool-program`、`/workflow`、`/hooks` |

`/theme auto|dark|light|high-contrast` 可即时切换主题；高对比度会强化固定顶栏与状态栏，主题切换不
改变当前会话、权限或运行状态。

输入历史按工作区保存，可关闭或清空。密钥样式的输入不会写入历史；这是模式脱敏，不是完备的 DLP。
普通输入中的 `@相对路径` 会建立最多 8 个有界文件引用；只把路径和按需读取约束交给 Agent，不自动把
整份文件塞入上下文。以 `!` 开头的输入表示用户明确要求执行其后的原始 Shell 命令，但命令仍经过同一
权限、Sandbox、审计和 Artifact 管线。Agent 运行时提交普通文本默认作为 steer；使用 `queue:` 或
`排队:` 前缀可把消息放到 Core 持久队列，并在当前 Turn 结束、会话锁释放后按提交顺序自动发送。Web
运行中可在 composer 切换“纠偏/排队”；队列由 TUI 与 Web 共享，不依赖任一前端进程内存。正在派发的
消息只能通过停止活动 Turn 处理，不能从队列界面假删除；daemon 在派发结果不确定时会把该消息标为
`blocked`，由用户确认后重试。
`/export [md|json]` 使用 session/title 生成默认目标，目标已存在时拒绝覆盖并显示精确路径；只有
`/export [md|json] --force --yes` 才允许覆盖。该命令不接受自定义输出路径。
粘贴本地图片路径后，TUI 验证格式和尺寸，写入 ArtifactStore，并随下一条消息一次性交付；composer
上方附件条持续显示序号、尺寸和短 hash。发送前可用 `/attachments remove N` 或
`/attachments clear` 管理附件；发送失败会恢复附件。永久 transcript 不保存图片 base64，且当前不保证
读取所有终端的剪贴板位图。

### 会话隔离与重连

TUI 先创建或恢复 session，再订阅该 thread 的 durable 事件流。每个 session 保存最后确认的 `seq`；
切换会话时使用 typed `event.unsubscribe` 只撤销当前连接拥有的旧 thread 订阅，重连后使用 `after_seq`
回放缺失事件。订阅初始化或 replay 失败只清理本次新订阅，不影响同一连接的 daemon/global 通道。
`runtime.event.payload` 由统一 reducer 处理，daemon 全局事件不会直接混入任务时间线。

活动 Turn 仍持锁而使 `session.resume` 返回 busy 时，TUI 会从权威 thread 投影只读附着原 Turn，重新
读取完整 transcript，再建立带 `after_seq` 的订阅。Reducer 对账 active run、未决审批、问题和计划；
Goal 从独立的权威 Goal 投影恢复，session-scoped composer 从本地 workspace/session 快照恢复。已被后续
durable 进度解决的旧控件不会复活。若视图准备或事件交付在激活中途失败，该 session 会进入
`requires_replay` fence；更高 `seq` 不得越过缺口确认，重试从最后成功交付的游标补交。

若恢复的是中断会话，TUI 会显示 `recovery.available` 卡片。daemon 冷启动不再为了配平模型消息而删除
正常强杀留下的 Tool Call；完整事实保留在 Ledger，`derive_messages()` 只对当前模型投影安全裁剪。只读
工具中断标记为可重跑；修改或命令状态不确定时不会自动重复执行，而是要求先查看变更、恢复
Checkpoint、放弃本轮或导出诊断。每次工具调用的 `operation_id` 与原始 tool use ID 一致，已确认完成的
操作不会由恢复流程重放。只有 JSON、消息分组或 checksum 链真正损坏时才归档并修复尾部。

这套设计用于避免 session 间的 token、审批、busy、取消和结果状态污染；发布级 100 次双 session
并发/断线矩阵尚未形成外部证据，因此不要把架构声明当成该门禁已通过。

Web 首次打开会话只读取最近 30 个 Turn 和最多 5,000 条近期事件；“加载更早记录”按游标继续读取，
不会为长会话一次发起无界的逐 Turn 请求。浏览器进程存活期间使用最后确认的 `seq` 续接 SSE；整页刷新
后从近期窗口重新建立时间线，再持续接收所有新事件。结果行从 Turn Receipt 补充修改文件、验证、模型、
成本和失败分类；Change Center 可按文件选择 Stage，提交仍需要独立操作且不会自动 push。
会话删除、Checkpoint 恢复、Worker Apply、Skill 安装、路由/记忆删除等确认都在 CodeRook 自己的
对话框中完成；输入型操作在取消或请求失败时不会丢失已有内容，也不依赖浏览器原生弹窗。
Web 的“设置”抽屉提供 `简体中文` / `English` 界面切换，以及浅色 / 高对比显示模式；偏好只保存在
当前浏览器的 local storage。它们只改变 CodeRook 自身界面，模型回答、终端输出、日志和代码不会被翻译。

## 5. 权限与沙箱

工作模式和权限姿态是独立维度：

| 设置 | 行为 |
|---|---|
| `plan` | 只读规划；结构化提问可用 |
| `act` | 受控编辑与验证 |
| `operate` | 更广操作面，仍经过权限管线 |
| `ask` | 需要权限的动作逐次询问，默认推荐 |
| `auto-review` | 自动接受支持审查的编辑；Shell 自动化仍取决于真实沙箱 |
| `full-access` | 扩大自动执行范围，但不绕过危险命令、安全降级或审计失败关闭 |

每个 Turn 启动时冻结 authority 快照；该 session 后续设置变化不能扩大正在执行的 Turn。审批决策、
工具可见性和 Shell sandbox 计划读取这份有效快照。Goal 还保存创建时的 permission ceiling，后续
轮次不能越过它。

Linux bubblewrap 和 macOS Seatbelt 只有在真实执行探针成功后才视为可用。强制配置只暴露工作区、
必要系统运行时和临时目录，Home 默认不可见。Windows 使用 Restricted Token、工作区 capability SID、
私有临时目录 ACL 和 Job Object：工作区外写入由 OS 拒绝，但读取与网络不隔离，因此始终显示
`PARTIAL WINDOWS SANDBOX`，Shell/Run 每次仍需明确审批。探针、ACL 或进程创建任一步失败都会回退
`windows_none` 并失败关闭，不会静默无约束执行。

Shell 环境采用白名单，并过滤常见 API key、云凭据、SSH 与 Git token 环境变量。按域名的 Shell
出站白名单无法强制时 fail closed。

若 `events.jsonl` 或 Runtime 投影写入失败，Core 会发出脱敏 `audit.degraded` 事件并拒绝所有非 READ
工具。只读诊断和导出仍可用；状态只能由显式修复流程清除，普通 run 不会静默恢复写权限。

## 6. Goal v4（稳定 TUI 产品面）

`/goal` 管理 session 级持久目标：

```text
/goal create [边界参数] -- <目标>
/goal <目标>                         # 兼容简写，使用默认边界
/goal status
/goal list
/goal pause
/goal resume
/goal edit <新目标>
/goal complete [验收说明]
/goal cancel --yes                  # 取消当前 Turn 并终结 Goal
/goal clear --yes                   # cancel 的兼容别名
```

创建命令直接映射到 typed `goal.create`，支持以下参数：

```text
--auto-continue | --no-auto-continue
--max-auto-turns 1..100
--max-wall-seconds 1..86400
--token-budget <正整数>
--criterion "完成标准"              # 可重复
--constraint "执行约束"             # 可重复
```

例如，让 daemon 在本次自动窗口内总共最多运行两个 Turn（首轮加最多一次续轮）、总墙钟不超过十
分钟且 token 不超过 12,000：

```text
/goal create --max-auto-turns 2 --max-wall-seconds 600 --token-budget 12000 \
  --criterion "tests pass" --criterion "docs aligned" -- 修复登录回归并完成验证
```

未知选项、重复的单值选项、非整数、越界值、空目标和未闭合引号都会在 TUI 本地拒绝，不会创建 Goal。
目标文本若确实以 `--` 开头，必须先写位置分隔符 `--`。`/goal cancel` 与 `/goal clear` 都需要显式
`--yes`；`pause` 保留目标并取消当前 Turn，`cancel` 则把目标终结为 `cleared`。

`/goal status` 展示当前总轮次、已用/最大自动轮次、累计 token 与墙钟预算、已有验证证据、尚未覆盖的
completion criteria、活动 run、暂停原因和 `paused_needs_confirmation`。`/goal list` 使用同一权威 Goal
投影的折叠摘要。自动继续/暂停决策会作为可回放 `goal.continue_decision` 卡片进入当前 session 时间线，
断线重连后仍可看到原决策。

Goal schema v4 包含 `auto_continue`、默认 `max_auto_turns=3`（包含首轮）、默认
`max_wall_seconds=1800`、硬 token budget、权限上限、暂停原因、timeline 与完成证据。一次 run 结束后
daemon 会持久化继续/暂停决策，并发布 `goal.continue_decision`：预算耗尽、达到轮次/时长限制、权限越界、
声明的 completion criteria 已全部被验证证据覆盖时会暂停等待验收；没有声明 criteria 时不会因此
立即暂停，而是仍按默认三 Turn/1800 秒或调用方设置的更小边界继续。若显式设置
`auto_continue=true` 且决策允许，SessionManager 会在当前 session 锁释放后自动创建下一 Turn；每个
Turn 还会使用自动窗口的剩余墙钟作为硬 deadline，超时会取消 runner 与其进程树并进入确认暂停。
只有明确的 transport 或 stream 超时使用最长 30 秒的有限退避，认证、配置、安全、验证失败以及未分类
`llm_error` 都不会被自动绕过。

只有引用 daemon 已记录 `verification.completed` 的 `update_goal`，或用户显式 `/goal complete`，才能进入
completed；文件路径、commit 文本或模型自报“测试通过”都不算证据。预算 Goal 会在每次模型请求前收窄
输出上限，真实 usage 达到上限或 provider 不返回 usage 时立即停止，禁止继续执行本次响应中的工具。自动 Goal
在 daemon 重启后进入 `paused_needs_confirmation`，普通遗留 Goal 则按中断状态恢复；不会在用户不知情时
自行恢复。

新建 Goal 的 `goal.create`、GoalService 和 TUI `/goal <目标>` 默认开启 `auto_continue`，默认最多三个
自动 Turn、1800 秒；调用方可显式关闭。为了避免把升级前的持久数据静默变成无人值守任务，
`GoalRecord` 反序列化缺失该字段的旧记录时仍取 `false`。自动 Goal 重启后始终要求确认。该能力虽然
端到端可调度且已具有 TUI 边界编辑、状态审查和显式恢复入口，真正的 `v1.0.0` 发布仍必须通过发布评分卡
列出的外部真实模型、三平台恢复和安装门禁。

## 7. 扩展与多 Agent 边界

Runtime capability 继续把 Tool Program、ACP Worker backend、fleet workers、declarative workflows、Hooks v2、MCP Resources/Prompts 和
VS Code 原型标为 Labs。bounded Goal loop、基础子 Agent、Skills、MCP Tools、Memory、durable
threads/turns、cursor replay、receipts、interrupt/steer、permission response 与 workspace diff 是稳定机器
合同；稳定标签不替代发布评分卡的外部门禁。

Labs 默认关闭，命令面板也会隐藏相应命令。仅在明确接受实验性恢复与权限风险时启用：

```bash
CODEROOK_LABS=1 uv run coderook
```

PowerShell 使用 `$env:CODEROOK_LABS = "1"` 后再启动。修改开关后必须重启 Core；关闭时 Core 不读取
用户/项目 Hook 配置，不暴露或恢复 Workflow/Fleet 控制面。该开关不把 Labs 变成稳定合同，也不降低
权限、工作区信任或审计要求。

Agent Preset 在 Session 创建时冻结：

```text
/new standard
/new minimal
/preset minimal
/preset tool-program     # 仅 Labs；自动 fork，不改写原会话工具历史
```

`standard` 暴露完整稳定工具，`minimal` 用于精简评测，`tool-program` 增加声明式
`RunToolProgram`。Tool Program 不是任意代码执行器，只允许有界 `call/sequence/parallel/if`；每个
子调用仍经过原工具的 Hook、权限、沙箱、Artifact 和审计管线。

ACP 外部 Agent 也是 Labs。启动 Core 前显式设置命令；Windows 推荐 JSON argv，避免路径转义歧义：

```powershell
$env:CODEROOK_LABS = "1"
$env:CODEROOK_ACP_COMMAND = '["C:\\Tools\\agent.exe","--acp"]'
uv run coderook
```

随后在 Worker start 请求中选择 `backend=acp`。ACP 首发是 one-shot，不保证 followup 或重启恢复；它
始终在受管 worktree 内运行，TUI 显示 partial enforcement，改动仍需 review、verification 和 apply。

稳定基础 Worker 不需要 Labs。控制命令为：

```text
/workers start [--backend builtin|acp] [--profile ROLE] [--route ID] [--model ID] [--budget TOKENS] \
  [--file PATH ...] [--write-root PATH ...] <任务>
/workers status <id>
/workers peek <id> [after_cursor]
/workers followup <id> <补充指令>
/workers retry <id> --yes
/workers cancel <id> --yes
/workers review <id>                              # 预览并取得 digest
/workers review <id> approve <digest> --yes
/workers review <id> reject --yes
/workers apply <id> <digest> --yes
```

- MCP Tools 可通过受控 catalog 调用；Resources/Prompts 和 transport 细节以
  [MCP 兼容文档](../reference/MCP_COMPATIBILITY.md)为准。
- 项目 Skill 和 Agent Profile 有严格 schema、来源、digest 与信任检查，但内容仍需人工审查。
- Hook 与本地脚本等价，项目 Hook 受工作区信任控制；rerun 不能绕过 trust。
- 基础子 Agent 由 daemon-owned WorkerController 管理，所有 list/start/status/retry/peek/followup/cancel/
  review/apply 操作都严格绑定当前 session。启动时可以用 `--profile`、`--route`、`--model`、`--budget`、
  `--file` 和 `--write-root` 收窄角色、模型、预算与写入范围；route 必须通过 readiness，权限不能高于
  父 Turn/Goal ceiling。可写 Worker 会被强制放入受管 Git worktree；完成后 Core 从固定 base commit
  检查 changed files、diff 和 handoff 状态。`/workers` 可查看这些证据；`/workers review <id>` 记录批准并
  返回绑定当前审查状态的 64 位 digest，但不会修改主工作区。确认摘要无误后，必须显式运行
  `/workers apply <id> <digest> --yes`；Core 会在应用前重新核对 session、审批状态、base commit、
  changed files 和 digest，任何漂移或冲突都会失败关闭。apply 只修改当前工作区，不 stage、不创建 commit、
  不 push；拒绝仍可使用 `/workers review <id> reject --yes`。模型报告的测试仅标记为
  `reported_unverified`。Labs Fleet/Workflow 当前没有自动配置独立 worktree，因此写节点会在进程启动前
  失败关闭，只读节点仍可运行；发布级跨平台冲突矩阵仍需外部门禁证据。

VS Code 目录是 experimental 原型，不属于 v1 产品承诺，也不会随当前 release workflow 发布 VSIX。

## 8. CLI 与无人值守运行

```bash
uv run coderook ping
uv run coderook sessions --all
uv run coderook run --goal "分析项目" --output-format stream-json
uv run coderook review --goal "审查当前改动" --output-format json
uv run coderook doctor runtime --json
uv run coderook doctor bundle --output coderook-diagnostics.zip --yes
uv run coderook trace --follow
```

Headless 默认在需要人工审批时 fail-fast。只允许明确工具：

```bash
uv run coderook run --goal "修改并验证代码" \
  --permission-mode allow-list \
  --allow-tool File.write \
  --allow-tool Run.run \
  --allow-tool Bash.run
```

allow-list 仍不能绕过 authority、危险命令规则、工作区边界、Windows Shell 审批和
`audit_degraded`。若任务可能调用结构化提问，还要配置 `--question-mode timeout` 或 `preset`。

## 9. 本地数据与隐私

用户级状态位于 `~/.coderook/`：

- `config.toml`、`routes.json`、credentials fallback、`policy.toml`、`ipc-token`、`api-token`；
- `sessions/`、`goals/`、`runtime.db`、`fleet.db`、`workflow.db`；
- `traces/`、日志和升级备份。

工作区状态位于 `<workspace>/.coderook/`：

- `context.md`、`memory/`、`artifacts/`；
- `worktrees/`、`skills/`、`agents/`、`hooks.toml`。

CodeRook 默认不发送产品遥测。使用远端模型、MCP、Web 或 shell 时，获准调用的数据仍会交给对应
第三方或本地进程；CodeRook 不改变 Provider 的数据保留政策。分享诊断包前仍应人工检查路径、Prompt
和业务数据。

## 10. 分发状态

当前公开使用方式是源码安装。Tag workflow 已准备 PyPI Trusted Publishing、五个自包含平台 archive、
GHCR、checksum、SBOM、provenance 与签名，但这些真实资产尚未产生。

未来 GitHub Release 会附带 Homebrew formula 和 Scoop manifest 文件；仓库目前没有外部 tap 或 bucket，
因此不能写成 `brew install coderook` 或 `scoop install coderook` 已可用。详见
[发行说明](../operations/RELEASING.md)。

## 11. 常见问题

### 一直显示 connecting

```bash
uv run coderook core status
uv run coderook core restart
uv run coderook ping
```

默认受管模式会尝试自动恢复 Core；`--no-auto-core` 只重试连接，不启动进程。

### 模型配置存在但不能提交

```bash
uv run coderook config-status
uv run coderook provider list
uv run coderook provider test
```

`credential_missing` 表示本机找不到 route 引用的凭据；`endpoint_unreachable` 只用于本地端点探测失败；
`configuration_complete` 表示本地前置条件齐全，不等于刚刚完成了在线 Doctor。

### 为什么 Windows 有沙箱仍每次询问 Shell

这是预期安全边界。Windows 后端只对写入位置提供 `partial` 强制力，不能阻止读取用户可读文件或联网；
`auto-review` 和 `full-access` 因此都不能让 Shell/Run 静默通过。权限卡会同时展示命令和这一限制。

### 为什么结果卡显示未验证

只有实际事件或 Turn Receipt 能证明测试、改动和用量。没有证据时显示 unavailable 是设计行为。

### 需要接口、运维或升级细节

- [功能架构](../reference/FUNCTIONAL_ARCHITECTURE.md)
- [Runtime API](../reference/RUNTIME_API.md)
- [运行手册](../operations/RUNBOOK.md)
- [威胁模型](../reference/THREAT_MODEL.md)
- [升级与回滚](UPGRADING.md)
