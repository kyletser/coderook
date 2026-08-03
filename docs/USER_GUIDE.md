# CodeRook 使用说明

CodeRook 是一个运行在本机终端中的 AI 编程 Agent。日常使用只需要启动一次
TUI；后台 Core 会自动启动或复用，不需要分别打开两个终端。

## 1. 快速开始

### 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Git

### 安装

```powershell
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
```

macOS 和 Linux 使用相同命令。

### 启动

在项目目录执行：

```powershell
uv run coderook
```

这是推荐的日常启动方式。CodeRook 会：

1. 检查模型配置；
2. 自动启动或复用本机 Core；
3. 打开 TUI；
4. 创建或恢复对话。

第一次启动时，按照提示填写模型 API。API Key 使用隐藏输入并保存在本机
`~/.coderook/credentials.json`，不会写入项目仓库。

以后再次使用仍然只需：

```powershell
uv run coderook
```

## 2. 第一次配置模型

### 在 TUI 中配置

输入 `/config` 并按一次 `Enter`，依次完成：

1. 选择 API 平台；
2. 输入 API Key；
3. 等待 CodeRook 使用该 Key 查询真实可用的模型；
4. 从探测结果中选择模型。

当前内置四种接入方式：

| 接入方式 | 接口 |
|---|---|
| DeepSeek API | DeepSeek 官方 API |
| OpenAI | OpenAI 官方 API |
| Anthropic | Claude 官方 API |
| 硅基流动 | SiliconFlow OpenAI-compatible API |

模型列表不是写死的。CodeRook 会调用平台的模型查询接口，只显示当前 API Key
实际能够访问并适用于文本对话的模型。探测失败时不会用虚构列表代替。

配置完成后，CodeRook 会保存配置、重启自己管理的 Core，并恢复当前会话。

### 在命令行中配置

无法进入 TUI 时，可以运行：

```powershell
uv run coderook configure
uv run coderook config-status
```

`configure` 也支持 Anthropic-compatible 和 OpenAI-compatible 自定义接口。
`config-status` 会显示生效的模型配置，但不会显示 API Key 正文。

## 3. 日常对话

直接在底部输入框中描述目标，例如：

```text
检查当前项目的登录逻辑，先说明问题，再修复并运行相关测试。
```

CodeRook 会根据任务自行读取代码、搜索项目、修改文件和调用工具。与系统、工具及模型
交互的内部指令尽量使用简洁英文以节省 token；给用户的回复会跟随用户使用的语言，
中文提问通常使用中文回答。

### 输入快捷键

| 操作 | 快捷键 |
|---|---|
| 发送消息或执行完整斜杠命令 | `Enter` |
| 输入换行 | `Shift+Enter`、`Alt+Enter`、`Ctrl+J`；macOS 也可用 `Cmd+Enter` |
| 打开斜杠命令补全 | 输入 `/` |
| 在补全列表中移动 | `↑` / `↓` |
| 补全当前命令 | `Tab` |
| 关闭补全列表 | `Esc` |
| 退出 TUI | `Ctrl+Q` |

完整命令（例如 `/model`、`/config`）按一次 `Enter` 就会执行，不需要再按第二次。

### 在 Agent 运行时补充要求

任务运行期间输入框仍然可用。直接输入补充要求并按 `Enter`，内容会作为实时纠偏信息
送入当前任务，并在模型下一次决策前生效，例如：

```text
不要修改数据库结构，只修复服务层。
```

如果尚未选择文本，`Ctrl+C` 会取消当前任务。

### 查看工具执行

用户消息靠右显示；支持 reasoning 的模型会把真实推理内容以英文显示在带竖线的
“深度思考”时间线中，不支持 reasoning 的模型不会显示该区域。工具前的普通说明不会
冒充深度思考。每一轮连续工具调用会合并到动作摘要分组，成功项使用绿色勾标识，例如
`已读取 README.md`、`已搜索 TODO in src` 或 `已执行命令 git status`。

点击“深度思考”或工具动作摘要可以折叠对应内容。折叠后的工具组仅显示
“运行了 3 条命令”“读取了文件 · 搜索了内容”这类聚合摘要。

展开工具组后，每个工具仍保持单行；继续点击单个工具，可以查看完整输入、完整结果、
成功或失败状态与耗时。长内容限制在详情面板内部滚动，不会撑开整个会话。

### 回答 Agent 的问题

Agent 需要关键选择时会显示选项面板：

- `↑` / `↓` 或 `j` / `k` 移动；
- 单选题按 `Enter` 确认；
- 多选题按 `Space` 勾选，再按 `Enter` 确认；
- 选择“输入自定义答案”，或按 `Esc`，可在主输入框自由回答。

## 4. 复制输出

CodeRook 支持三种复制方式：

1. 用鼠标拖选输出，再按 `Ctrl+C`；
2. 按 `Ctrl+Shift+C`：有选区时复制选区，否则复制上一条完整回复；
3. 输入 `/copy`：复制上一条完整回复。

如果终端对鼠标选择或快捷键有自己的拦截规则，优先使用 `/copy`。

## 5. 模型增加与切换

### 从已保存模型中选择

```text
/model
```

用 `↑` / `↓` 或 `j` / `k` 选择，按 `Enter` 切换，按 `Esc` 关闭。

### 直接切换到指定模型

```text
/model 模型ID
```

### 添加自定义模型并切换

```text
/model add 模型ID
```

模型切换会保存新的默认模型、重启 CodeRook 管理的 Core，并恢复当前会话。正在执行任务时
不能切换模型，请先等待任务完成或用 `Ctrl+C` 取消任务。

如果需要更换平台或 API Key，请使用 `/config`，不要用 `/model`。

## 6. 权限模式

Mode 决定怎么工作，Authority 决定工具是否需要批准，两者彼此独立。

输入 `/permissions` 可选择后续消息使用的权限姿态。也可以用 `Shift+Tab` 在三种姿态间循环：

| 模式 | 行为 |
|---|---|
| 询问后修改 | 文件修改、命令和外部操作按安全策略请求确认 |
| 自动接受修改 | 工作区内文件修改自动执行；命令和外部操作仍按策略确认 |
| 全自动执行 | 本机命令、修改和外部操作自动批准；Plan Mode 与工具边界仍然生效 |

选择器中使用 `↑` / `↓` 或 `j` / `k` 移动，`Enter` 确认，`Esc` 关闭。

也可以直接输入 `/permissions ask|auto-review|full-access`。选择会写入
`~/.coderook/policy.toml`，后续新会话和重启后继续生效，不需要每次重新设置。

### 工作模式

使用 `Tab` 在 `act → operate → plan` 之间循环；斜杠补全打开时，`Tab` 仍优先完成命令。
也可以直接输入：

```text
/mode plan|act|operate
```

- `Plan`：只读分析；
- `Act`：当前会话直接工作；
- `Operate`：权限上限与 Act 相同，后续用于耐久 Worker 调度偏好。

Header 会分别显示 Mode、Authority 和 workspace trust，不再把它们合并成一个状态。

### Workspace trust 与 Sandbox

```text
/trust status|grant|revoke
/sandbox status
```

Trust 是项目是否受信任的独立状态。Sandbox 显示操作系统真实隔离能力；Windows 没有可用
隔离后端时会明确显示 `unavailable`，不会把审批或 Full Access 描述成 sandbox。

### 工具审批

需要确认时，面板会显示工具名称以及命令、目标文件或任务内容。可选择：

| 选择 | 快捷键 | 作用 |
|---|---|---|
| Allow once | `1` 或 `y` | 只允许这一次 |
| Always allow | `2` 或 `a` | 始终允许该工具动作并记住后续会话，包括工作区外路径 |
| Deny | `3` 或 `n` | 拒绝这一次 |
| Always deny | `4` 或 `d` | 拒绝并记住后续会话 |

也可以用 `↑` / `↓` 选择后按 `Enter`。`Esc` 等同于拒绝这一次。
“始终允许/拒绝”规则保存在 `~/.coderook/policy.toml`。

## 7. Plan Mode

Plan Mode 用于先分析、确认方案，再修改代码。

### 让下一条消息进入 Plan Mode

```text
/plan
```

然后输入任务描述。

### 直接规划一个任务

```text
/plan 分析认证模块的改造方案
```

规划阶段只开放只读能力，Core 也会拒绝写文件、执行命令等越权操作。计划生成后有三个选择：

- 批准并实施：退出 Plan Mode，保留当前权限姿态并开始实施；
- 继续规划：输入反馈，再进行一轮只读分析；
- 取消：保留已输出的计划，不执行改动。

使用 `↑` / `↓` 或 `j` / `k` 移动，`Enter` 确认，`Esc` 取消。

## 8. 斜杠命令

| 命令 | 作用 |
|---|---|
| `/new` | 创建并切换到新会话 |
| `/sessions` | 打开历史会话选择器 |
| `/model` | 查看或切换当前模型 |
| `/config` | 更换 API 平台、Key 或模型 |
| `/compact` | 手动压缩当前会话上下文 |
| `/copy` | 复制上一条完整回复 |
| `/plan` | 让下一条消息使用 Plan Mode |
| `/plan 任务描述` | 立即执行一次只读规划 |
| `/mode plan\|act\|operate` | 查看或切换工作模式 |
| `/permissions` | 查看或修改权限模式 |
| `/trust status\|grant\|revoke` | 查看或修改工作区信任状态 |
| `/sandbox status` | 查看真实 OS 隔离能力 |
| `/tasks` | 查看最近一次运行的任务状态 |
| `/diff` | 查看当前工作区改动和统一 diff |
| `/rewind` | 从安全 checkpoint 恢复文件 |
| `/context` | 查看消息数、token 估算、运行次数和上下文占用 |
| `/技能名` | 调用已注册的 Skill |

输入 `/` 会同时列出内置命令和当前已注册的 Skills。

### 恢复文件

`/rewind` 会列出可用 checkpoint。选择后，CodeRook 只恢复该 checkpoint 管理的文件。
如果文件在 checkpoint 之后又被其他操作修改，恢复会拒绝覆盖，避免丢失新改动。

### 管理上下文

TUI 顶部会显示当前上下文占用。CodeRook 默认在上下文接近 80% 时自动压缩旧历史；
也可以用 `/compact` 手动压缩，用 `/context` 查看当前估算。

## 9. 会话恢复

在 TUI 中输入：

```text
/sessions
```

选择历史会话后按 `Enter` 恢复。也可以从命令行指定：

```powershell
uv run coderook-tui --resume SESSION_ID
```

常用会话管理命令：

```powershell
uv run coderook sessions --all
uv run coderook session rename SESSION_ID "新标题"
uv run coderook session fork SESSION_ID --title "实验分支"
uv run coderook session export SESSION_ID --format markdown -o session.md
uv run coderook session delete SESSION_ID --yes
```

`delete --yes` 会永久删除指定会话，执行前请确认 ID。

## 10. 脚本和无人值守运行

TUI 是主要使用界面。CLI 适合脚本、调试和一次性任务：

```powershell
uv run coderook ping
uv run coderook run --goal "分析项目并运行相关测试"
```

Headless 任务默认使用 `fail-fast`：遇到需要人工审批的工具就退出。明确允许指定工具时：

```powershell
uv run coderook run --goal "修改并验证代码" `
  --permission-mode allow-list `
  --allow-tool edit_file `
  --allow-tool apply_patch `
  --allow-tool Bash.run
```

在 macOS/Linux shell 中，将 PowerShell 的续行符 `` ` `` 换成 `\`。

`allow-list` 仍然不能绕过危险命令规则和工作区边界。完全不允许审批类工具时可使用
`--permission-mode deny`。

## 11. Core 管理

正常使用不需要手动管理 Core。以下命令主要用于开发和排障：

```powershell
uv run coderook core status
uv run coderook core start
uv run coderook core restart
uv run coderook core stop
```

默认监听地址是 `127.0.0.1:7437`，只接受本机连接，并使用
`~/.coderook/ipc-token` 认证客户端。

需要自己在前台运行 Core 时：

```powershell
uv run coderook-core
uv run coderook-tui --no-auto-core
```

第一个终端运行 Core，第二个终端运行 TUI。前台方式便于直接查看启动错误，日常使用不推荐。

## 12. 配置和本地数据

| 路径 | 内容 |
|---|---|
| `~/.coderook/config.toml` | 全局配置 |
| `.coderook/config.toml` | 当前项目配置 |
| `~/.coderook/credentials.json` | 模型 API Key |
| `~/.coderook/policy.toml` | 永久权限规则 |
| `~/.coderook/sessions/` | 会话和 transcript |
| `~/.coderook/runtime.db` | 运行时状态 |
| `~/.coderook/ipc-token` | 本地 IPC 认证 token |
| `~/.coderook/logs/core.log` | Core 日志 |
| `~/.coderook/logs/tui.log` | TUI 日志 |
| `~/.coderook/traces/daemon.jsonl` | 脱敏 Trace |
| `.coderook/memory/` | 当前项目的长期记忆 |

不要提交 `credentials.json`、`ipc-token` 或包含密钥的 `.env`。

配置优先级由低到高为：

```text
内置默认值
→ ~/.coderook/config.toml
→ 当前项目 .coderook/config.toml
→ 当前项目 .env
→ 系统环境变量
```

常用环境变量：

| 变量 | 作用 |
|---|---|
| `CODEROOK_CONFIG` | 指定唯一 TOML 配置文件 |
| `CODEROOK_HOST` | Core 监听地址 |
| `CODEROOK_PORT` | Core 端口 |
| `CODEROOK_LOG_LEVEL` | 日志级别 |
| `CODEROOK_LOG_FILE` | Core 日志文件 |
| `CODEROOK_LLM_PROVIDER` | LLM 协议类型 |
| `CODEROOK_LLM_BASE_URL` | LLM 接口地址 |
| `CODEROOK_LLM_DEFAULT_MODEL` | 默认模型 |
| `CODEROOK_LLM_API_KEY_ENV` | 保存 API Key 的环境变量名 |

完整示例见项目根目录的 `.env.example`。

## 13. 常见问题

### 启动后一直显示 connecting

先检查 Core：

```powershell
uv run coderook core status
uv run coderook ping
```

仍然失败时，在前台启动以查看错误：

```powershell
uv run coderook core stop
uv run coderook-core
```

同时检查 `~/.coderook/logs/core.log` 和 `~/.coderook/logs/tui.log`。

### 端口 7437 已被占用

确认是否已有 CodeRook Core：

```powershell
uv run coderook core status
```

如果是其他程序占用，可在 `.env` 中设置新端口：

```dotenv
CODEROOK_PORT=8000
```

然后重新启动 CodeRook。不要让 Core 和 TUI 使用不同端口配置。

### `/config` 无法列出模型

依次检查：

1. API Key 是否属于所选平台；
2. 当前网络是否能够访问平台 API；
3. Key 是否具有模型查询权限；
4. 代理或防火墙是否拦截 HTTPS 请求。

模型探测请求最长等待约 20 秒。修正后重新执行 `/config`。

### 修改配置后没有生效

TUI 内 `/config` 和 `/model` 会自动重启由 CodeRook 管理的 Core。手工修改配置文件后运行：

```powershell
uv run coderook core restart
```

如果 Core 是手动以前台方式启动的，请先停止该进程再启动。

### `Ctrl+C` 没有复制

`Ctrl+C` 只有在 TUI 存在选区时才复制；没有选区时用于取消正在运行的任务。
可改用 `Ctrl+Shift+C` 或 `/copy`。

### 查看运行细节

```powershell
uv run coderook trace --follow
uv run coderook trace RUN_ID
uv run coderook trace RUN_ID --layer llm
```

Trace 默认不记录完整 LLM 正文，并会对敏感信息进行脱敏。
