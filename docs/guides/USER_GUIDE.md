# CodeRook 使用说明

**适用基线**：`0.2.0-beta.1` 候选版

**主要入口**：`coderook`

**产品界面**：TUI 与本地 Web；CLI 用于脚本、诊断和无人值守任务

CodeRook 是本地优先的 Coding Agent。它可以理解仓库、规划与修改代码、运行验证、保留可恢复会话，
并通过事件、Diff、Turn Receipt 和结果卡说明一次执行到底发生了什么。当前尚未发布 PyPI 或 GitHub
Release；本文先以源码安装为准。发布状态只以[发布评分卡](../status/RELEASE_SCORECARD.md)为准。

## 1. 安装与首次启动

### 网络重试与重复操作提醒

临时断网、超时、限流、服务端错误及空响应默认由 Core 在同一步最多重试 5 次，
不会因此重新执行已完成的工具。界面显示等待时间，等待期间仍可停止任务；
认证错误和没有有效结束标记的 SSE 响应不会无限重试。失败的部分回答留作本地审计，
不与重试后的正常回答拼接，也不带入后续模型历史。

可在用户配置 `~/.coderook/config.toml` 中调整（修改后重启 Core 生效）：

```toml
[llm.retry]
max_retries = 5       # 设为 0 关闭自动重试；不包括首次请求
initial_delay_s = 0.5
max_delay_s = 10.0
jitter_ratio = 0.1
```

连续同参调用同一工具时，Core 在第 3、5、8 次提醒模型重新分析结果。
提醒不会阻止继续执行，也不会强迫修改代码；发送新的纠偏消息会重置计数。
正常的权限审批和预算上限仍然有效。未显式配置 `agent.max_steps` 时，交互会话不按固定步数截断，脚本和 Worker 默认最多 20 步；显式配置的步数上限对所有入口生效。交互长任务可能消耗更多 Token，可随时停止。

### 图片尺寸与原图

用户附件和工具返回图片默认按最长边 2000 像素、base64 内容 4.5 MiB 的目标处理。
缩放后会向模型提供原尺寸和坐标换算说明，用户附件的原始 Artifact 不会被覆盖。
`read` 支持 PNG、JPEG、WebP、GIF，也可将 BMP/TIFF 转为 PNG。
用户配置 `[agent] image_auto_resize = false` 可关闭缩放，重启 Core 生效；
关闭缩放不会关闭格式转换。浏览器上传限制仍独立生效，不代表任意大小附件都可上传。

### 自定义项目指令与系统提示

项目指令从父目录到当前工作目录加载，每层优先选择 `AGENTS.override.md`，其次为 `AGENTS.md`、`AGENTS.MD`、`CLAUDE.md`、`CLAUDE.MD`，同层只加载第一份。全局指令放在 `~/.coderook/`，原有 `context.md` 仍受支持。

需要定制 Agent 的默认角色时，在 `~/.coderook/SYSTEM.md` 中编写替换提示；只想补充要求，使用 `APPEND_SYSTEM.md`。受信项目的 `.coderook/` 下同名文件优先于全局文件，两种文件分别选择。修改后下一轮任务自动读取，无需重启；正在执行的任务不受影响。替换角色不会移除项目指令，也不会改变实际工具权限。

提示模板默认从 `~/.coderook/prompts/` 和受信项目的 `.coderook/prompts/` 加载。额外的模板文件或目录可在用户 `~/.coderook/config.toml` 中设置：

```toml
[agent]
prompt_paths = ["~/shared-prompts", "C:/prompts/review.md"]
skill_paths = ["~/shared-skills", "C:/skills/inspect/SKILL.md"]
# 可选：只填写你信任的 Python 扩展文件
# extension_paths = ["C:/my-extensions/greeting.py"]
steering_mode = "one-at-a-time"
follow_up_mode = "one-at-a-time"
```

默认目录优先，额外路径按配置顺序加载，同名只使用第一份可读模板。相对路径以当前项目为基准。更新路径配置后重启 Core；模板内容修改后可用 `/reload` 刷新补全，执行时会读取最新正文。

`steering_mode`（纠偏）与 `follow_up_mode`（后续消息）可分别改为 `all`，一次交付当前排队的消息；默认 `one-at-a-time` 每次交付一条。纠偏在下一次模型决策前生效，后续消息等当前回答与工具调用结束后才进入同一运行。TOML 是首次默认值；之后可在 Web 设置中即时切换，或使用 `/delivery steering|follow-up one|all`，选择保存在 `~/.coderook/agent-settings.json` 并在下次启动恢复。

`skill_paths` 支持单个 Markdown Skill、含 `SKILL.md` 的包目录或包含多个 Skill 的目录。用户显式配置的手工维护 Skill 可以直接使用，无需复制安装；受管 Skill 仍保留其安装信任与完整性检查。加载顺序为用户目录、项目目录、显式路径，之后才是内建与旧目录兼容项；同名采用先发现的条目，项目不覆盖用户同名 Skill。使用 `/skill:名称 参数` 调用，其参考文件可通过 `read` 读取。

### Python 扩展与自定义命令

扩展直接使用 Python，不运行 Node/Pi 子进程。将下面的内容保存到你自己的 `greeting.py`，并把完整路径加入用户配置的 `agent.extension_paths`（新增路径配置后重启 Core）：

```python
# 注册会话局部命令，计数在同一会话的连续任务之间保留
def setup(api):
    count = 0

    # 本地返回文字，不调用模型、不创建任务
    def greet(arguments):
        nonlocal count
        count += 1
        return f"Hello {arguments} · 本会话第 {count} 次"

    # 使用同一会话的正常任务入口提交请求
    async def ask(arguments):
        await api.send_user_message(arguments)

    api.register_command("greet", greet, description="本地问候")
    api.register_command("ask-agent", ask, description="提交 Agent 任务")
```

打开会话后，TUI/Web 的输入补全会出现 `/greet` 和 `/ask-agent`。`/greet 小明` 直接显示返回文字；
`/ask-agent 解释当前项目` 才调用模型。扩展命令接收斜杠名称之后的原始参数字符串。
`send_user_message` 默认按普通文本处理，不展开参数里的斜杠命令；需要展开模板时显式设置
`expand_prompt_templates=True`。运行中可选 `deliver_as="steer"` 或 `"follow_up"`，空闲时会启动下一轮。

修改扩展源码后，在空闲会话执行 `/reload`，无需再次重启 Core。重载会清除旧扩展的内存计数，
但不会删除聊天历史。不同会话拥有各自的扩展状态；关闭会话会执行 `api.on_shutdown` 注册的清理函数。
扩展是你显式信任并加载的本地 Python 代码，具有当前用户的进程权限，不是沙箱脚本。
更多 Hook、工具、图片和生命周期接口见 [Python Agent Runtime](../reference/PYTHON_AGENT_RUNTIME.md)。

扩展也可以注册只属于当前会话的兼容 Provider，并从命令中切换模型：

```python
def setup(api):
    api.register_provider("team-proxy", {
        "name": "Team Proxy",
        "baseUrl": "https://proxy.example.com/v1",
        "apiKey": "$TEAM_PROXY_KEY",
        "headers": {"X-Workspace": "$TEAM_WORKSPACE_ID"},
        "api": "openai-completions",
        "models": [{"id": "team-coder", "contextWindow": 128000}],
    })

    async def use_team_model(_arguments):
        await api.set_model("team-proxy", "team-coder")
        return "已为当前会话切换模型"

    api.register_command("team-model", use_team_model)
```

该选择也会出现在 Web 的模型抽屉中；不会覆盖其他会话或修改全局默认 Provider。
若只需要让已有 Provider 经过企业网关，可以省略 `models`：

```python
def setup(api):
    api.register_provider("openai", {
        "baseUrl": "https://gateway.example.com/v1",
        "headers": {"X-Corp-Auth": "$CORP_AUTH_TOKEN"},
    })
```

覆盖只作用于加载该扩展的会话，已有模型能力与用户凭据保持不变。

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
uv run coderook "修复登录问题并运行相关测试"
uv run coderook -p "解释当前项目结构"
```

`coderook web` 只绑定本机回环地址，自动启动或复用当前仓库 Core，并打开最近会话。若空闲的
受管 Core 正绑定其他仓库，会在同一进程内切换工作区；存在活动任务时则拒绝切换。页面无模型时仍可浏览
会话、文件、Diff、设置和帮助，第一次提交任务前才执行 readiness 检查。

长会话可在 Web 的“历史与分支”抽屉中手动整理上下文，并填写需要重点保留的事实；CLI 可使用
`coderook session compact <session-id> --focus "保留失败原因"`。两者都调用同一 append-only 压缩流程，
返回压缩前后 Token 与节省量，不改写原始会话事实。

从 CodeRook 自身源码目录执行无参数 `coderook web` 时，会进入不含源码的欢迎工作区。文件、变更
和任务输入保持关闭，必须先在左上角选择已有项目或创建空白项目。CodeRook 源码目录不会出现在最近
项目中，也不能通过项目选择器或显式路径登记为用户项目；其他 `coderook web <path>` 仍以给定用户目录启动。
即使后台残留了曾经绑定源码目录的旧 Core，空闲时也会自动换成欢迎工作区实例；如果该旧 Core 仍有
活动任务，启动器会先要求结束任务，而不会继续展示源码。

Web 左上角的项目名称是项目入口，不需要先在终端切换目录：

- “新建项目”输入名称即可，默认创建到 `~/CodeRookProjects/<项目名>`，也可修改保存位置；
- “打开文件夹”通过网页目录选择器选择电脑上已有的目录，CodeRook 不复制或移动其中的文件；
- “最近项目”用于切换。切换时当前 Core 在没有活动任务的前提下原地重建工作区运行时，浏览器页面、认证 Cookie、HTTP 与 IPC 监听均不重启；
- 项目就是 Agent 的工作区。切换完成后，文件、Shell、Diff、会话和工作区级 `.coderook/` 都绑定新目录，原项目不会继续作为隐式代码上下文。

删除项目记录只会从最近项目列表移除，绝不删除磁盘目录。当前活动项目必须先切换后才能移除。

浏览器直接打开固定的本机 URL，并自动建立 HttpOnly Cookie，不需要等待或交换一次性启动票据。所有写请求
仍通过同源与 CSRF 校验。Provider API Key 只提交给本地 Core 的配置事务，不进入 URL、日志、
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
uv run coderook --resume
uv run coderook --session SESSION_ID
uv run coderook --new
```

`-c` 是 `--continue` 的短写。`-r`/`--resume` 不带参数时直接打开可搜索的会话选择器；附加
`SESSION_ID` 时精确恢复该会话。打印模式没有交互选择器，因此 `coderook -p -r` 仍需提供 ID。
`--new` 或 TUI 内的 `/new` 都会显式创建新会话。

session 与工作区显式绑定。从另一个仓库启动时，空闲的受管 Core 可以原地切换；若旧工作区仍有
活动 run，则拒绝切换，避免 Agent 在错误目录执行。`--no-auto-core` 禁止 TUI 启动或恢复 Core，适合
手动排障。

## 2. 配置 Provider 与模型

### Provider Catalog

TUI `/config` 与 CLI route 管理使用同一份 Provider Catalog：

| 预设 | 凭据 | 协议/说明 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | OpenAI Chat compatible |
| Alibaba Cloud Bailian (China mainland) | `DASHSCOPE_API_KEY` | OpenAI Chat compatible |
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
/thinking off|low|medium|high
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
`compaction.strategy = "session"`。压缩默认按 `compaction.keep_recent_tokens = 20000`
保留最近完整工具闭环；长任务中途切分时单独总结任务前缀，再次压缩会更新旧摘要。
`compaction.reserve_tokens = 16384` 用于下一次请求的空间预留；历史摘要与任务前缀摘要
分别使用其 80% 和 50% 作为输出上限，并受 Provider 及当前任务预算进一步约束。
手动 `/compact` 和自动压缩共用这些配置。
修改提示模板、Skills 或已配置的 Python 扩展后，可在会话空闲时输入 `/reload`：TUI/Web 会释放旧扩展，
重新加载源码并刷新命令候选，不调用模型，也不重载 Provider。运行中的任务需先停止；尚未处理的排队消息会回到输入框，保留已有
草稿和图片，等待你修改后重新发送。普通 Act 默认使用 `read`、`bash`、`edit`、`write` 与配置的 MCP
工具，由主模型决定何时调用；最终回答直接显示为正文。
`structured` 和 `adaptive_evidence` 仍可显式选择以复现旧实验。
默认 `rules_only` 不额外调用分类模型：普通 Act
请求的画像标记为 `model_led`，规则仅作辅助提示。主模型结合完整会话决定回答、查询或提问，
不会因“什么模型”等关键词关闭文件或 Shell 工具，也不会因规则低置信度强制进入计划。
显式 Plan 模式、执行策略覆盖及权限/沙箱约束继续生效；默认不自动委派 Worker。
显式 `plan_first` 策略仍需计划审阅，批准后才通过新的 Act Turn 执行修改。
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
| `Ctrl+L` | 打开当前 Provider 的模型选择器 |
| `Ctrl+T` | 循环思考强度 `off → low → medium → high` |
| `Ctrl+O` | 展开或收起推理、工具步骤与完整输出 |
| `Ctrl+G` | 在 `$VISUAL`/`$EDITOR` 中编辑当前输入；Windows 未配置时使用记事本 |
| `Alt+V` | 从系统剪贴板直接附加截图；普通 `Ctrl+V` 仍用于粘贴文本 |
| `Ctrl+Q` | 退出 TUI；会话和 Core 状态不会被删除 |

常用命令：

| 类别 | 命令 |
|---|---|
| 帮助与输入 | `/help`（兼容 `/hotkeys`）、`/copy`、`/history status\|on\|off\|clear`、`/attachments [remove N\|clear]`、`/quit` |
| 会话 | `/resume`（兼容 `/sessions`）、`/new`、`/name`（兼容 `/rename`）、`/fork`、`/export`、`/import`、`/delete --yes`、`/delivery` |
| 模型 | `/config`、`/provider`、`/model`、`/thinking off\|low\|medium\|high`、`/doctor` |
| 执行 | `/plan`、`/goal`、`/mode`、`/permissions`、`/trust`、`/sandbox` |
| 审查 | `/changes`（`/diff`）、`/review`、`/rewind`、`/turn`、`/context`、`/compact`、`/cost` |
| 扩展 | `/skills`、`/mcp`、`/memory`、`/artifacts`、`/workers`、`/jobs` |
| Labs/高级 | `/preset tool-program`、`/workflow`、`/hooks` |

`/trust grant` 与 `/trust revoke` 按当前工作区持久保存到用户状态目录，并立即作用于该项目的后续
Turn 和新会话；切换到其他项目不会继承这一决定，活动 Turn 的冻结权限也不会被中途改变。

`/theme auto|dark|light|high-contrast` 可即时切换主题；高对比度会强化固定顶栏与状态栏，主题切换不
改变当前会话、权限或运行状态。

输入历史按工作区保存，可关闭或清空。密钥样式的输入不会写入历史；这是模式脱敏，不是完备的 DLP。
普通输入中键入 `@` 会打开工作区文件模糊搜索，使用方向键和 `Tab`/`Enter` 插入相对路径；含空格路径会
自动写成 `@"路径"`。每条消息最多建立 8 个有界文件引用；文本文件附加有限正文，超出限制时由 Agent
继续按需读取，不会把整份大文件塞入上下文。以 `!` 开头的输入表示用户明确要求执行其后的原始 Shell 命令，但命令仍经过同一
权限、Sandbox、审计和 Artifact 管线。`!命令` 由 Python 直接执行，不调用模型、不要求配置 Provider；
输出进入后续模型上下文。`!!命令` 只保存执行记录，不进入模型上下文。运行中的直接命令会排队单独执行。
Agent 运行时提交普通文本默认作为 steer；使用 `queue:` 或
`排队:` 前缀可把消息放到 Core 持久队列，并在当前 Turn 结束、会话锁释放后按提交顺序自动发送。Web
运行中可在 composer 切换“纠偏/排队”；队列由 TUI 与 Web 共享，不依赖任一前端进程内存。正在派发的
消息只能通过停止活动 Turn 处理，不能从队列界面假删除；daemon 在派发结果不确定时会把该消息标为
`blocked`，由用户确认后重试。
`/export [md|json|html]` 使用 session/title 生成默认目标，目标已存在时拒绝覆盖并显示精确路径；只有
`/export [md|json|html] --force --yes` 才允许覆盖。HTML 是可独立打开的单文件会话页面，包含
对话、思考折叠、工具调用与结果、图片和分支元数据，不加载外部脚本或样式。Markdown 与 HTML 使用
用户实际提交的展示正文，不暴露 `@文件`、管道输入等内部模型增强提示；Markdown 将思考和图片呈现为
可读区块。JSON 保留模型可重放正文，适合归档和再次导入。该命令不接受自定义输出路径。
`/import <文件>` 可把 CodeRook JSON 导出或 Pi JSONL 的当前活动分支导入为本工作区中的新会话；导入后
使用当前 CodeRook Provider、权限和工具配置继续，不复制来源运行时状态。
按 `Alt+V` 可直接读取系统剪贴板截图；粘贴本地图片路径仍作为通用入口。TUI 验证格式和尺寸，
写入 ArtifactStore，并随下一条消息交付；composer
上方附件条持续显示序号、尺寸和短 hash。发送前可用 `/attachments remove N` 或
`/attachments clear` 管理附件；发送失败会恢复附件。图片附件会以结构化块保存在本地 transcript，
用于后续请求和会话恢复，包括图片 base64；因此会增加本地历史大小及压缩前的模型输入成本。
操作系统缺少可用剪贴板图片后端时，`Alt+V` 会明确提示没有可附加图片，仍可粘贴图片文件路径。

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

一次性任务可以直接引用工作区文件，也可以接收 PowerShell 管道输入：

```powershell
uv run coderook -p "@README.md" "概括这个文件"
uv run coderook -p "@screenshot.png" "解释截图中的报错"
uv run coderook -c -p "继续上一轮并总结结果"
uv run coderook -p --fork SESSION_ID "从这里尝试另一种实现"
uv run coderook -p -n "发布审查" "检查当前改动"
uv run coderook -p --no-session "临时解释这段报错"
uv run coderook --no-tools "在 TUI 中只回答概念问题"
uv run coderook -p --no-tools "只回答概念问题"
uv run coderook -p --tools read,bash "只读取项目并运行诊断"
Get-Content .\build.log | uv run coderook -p "定位失败原因"
uv run coderook --provider aliyun --model qwen3.8-flash "检查当前项目"
uv run coderook --model aliyun/qwen3.8-flash "检查当前项目"
```

`@文本文件` 会附加有界正文，超出限制时模型再通过 `read` 读取必要范围；`@PNG/JPEG/GIF/WebP`
在打印模式和 `run` 中作为真实多模态附件交付，而不是二进制文字占位。管道正文和命令行中的任务说明会合并为同一次请求；省略任务说明时，
管道正文本身就是任务。文本模式始终在结束时输出最终回答，包括不发送流式 token 的 Provider。
打印模式默认保存可继续的会话；显式使用 `--no-session` 时，结果输出后删除本次临时会话。
`-n/--name` 可在 TUI、打印模式和 `run` 启动时直接设置会话名称；恢复已有会话时会更新其名称。
`--fork SESSION_ID` 可在 TUI、打印模式和 `run` 启动时复制该会话的当前分支，并只在新会话中
继续执行；原会话的 Ledger、标题和工作状态保持不变。
`--provider` 是 `--route` 的公开别名；`--model route/model` 可以同时选择已配置 Route 和模型。
这些参数也可用于 TUI 初始任务、`-p` 和 `run`；选择只绑定本次会话，不修改其他会话或
全局活动 route。只指定 `--model` 时沿用当前活动 route；没有活动 route 时必须同时指定二者。
`--tools read,bash` 可用于 TUI、打印模式和 `run`，只向模型公开指定的原生工具，支持 `read`、
`bash`、`edit`、`write`；`--no-tools` 完全关闭该进程提交任务时的工具调用。它与 `--allow-tool`
不同：前者裁剪模型工具目录，后者只决定无人值守运行遇到审批时是否允许，二者都不会绕过任务
画像和权限边界。

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

- `config.toml`、`routes.json`、credentials fallback、`policy.toml`、`workspace-trust.json`、
  `ipc-token`、`api-token`；
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
