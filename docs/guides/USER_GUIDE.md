# CodeRook Agent 使用说明书

**适用版本**：`0.1.0` Alpha

**主要入口**：`uv run coderook`

**运行方式**：本地 Core daemon + TUI/CLI 客户端

CodeRook 是一个本地 AI 编程 Agent。它可以读取和检索仓库、分析代码、规划修改、编辑文件、运行测试、
查看 Git diff、调用子 Agent，并把会话、任务和执行证据保存在本机。TUI 是主要产品界面；CLI 用于
脚本、诊断和无人值守运行。

当前版本以源码方式提供，要求 Python 3.12、Git 和
[`uv`](https://docs.astral.sh/uv/)。尚未发布到 PyPI，安装命令不要写成
`pip install coderook`。

## 五分钟开始使用

首次使用按以下顺序操作：

1. 克隆仓库并运行 `uv sync`。
2. 执行 `uv run coderook` 进入 TUI。
3. 输入 `/config`，选择 API 平台、填写密钥并选择模型。
4. 输入 `/doctor`，确认活动模型 route 可用。
5. 在可信项目中输入 `/trust grant`；不确定来源时保持 untrusted。
6. 建议先使用 `/mode act` 和 `/permissions ask`。
7. 直接用自然语言描述任务；完成后输入 `/diff` 审查改动。

最小任务示例：

```text
先理解当前仓库，只读分析启动流程、核心模块和测试入口，不要修改文件。
```

```text
修复当前失败的单元测试。先说明原因，再做最小修改，最后运行相关测试并汇报结果。
```

斜杠命令用于控制 Agent；普通任务不需要特殊语法。

## 安装与启动

```bash
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
uv run coderook
```

无参数 `coderook` 启动 TUI，并自动复用或启动本地 Core。没有可用模型配置时也会进入 TUI，
不会强制打开配置向导；输入 `/config` 后再配置即可。

使用 `uv run coderook --continue` 可恢复当前 workspace 最近会话；使用
`uv run coderook --resume SESSION_ID` 可恢复指定会话。Core 与 session 都绑定 workspace：切换仓库时，
空闲的受管 Core 会自动重启到当前目录；如果旧仓库仍有活动 run，CodeRook 会拒绝切换，避免串到错误仓库。

Core 默认监听：

- `127.0.0.1:7437`：CLI/TUI 使用的 JSON-RPC/NDJSON IPC；
- `127.0.0.1:7438`：外部集成使用的 HTTP/SSE Runtime API。

手动管理 Core：

```bash
uv run coderook core start
uv run coderook core status
uv run coderook core restart
uv run coderook core stop
```

## 配置模型

最简单的方式是：

```bash
uv run coderook configure
```

TUI 中的 `/config` 提供 DeepSeek、OpenAI、Anthropic 和硅基流动入口，并从对应模型接口读取当前
账号可见的文本聊天模型。CLI route 管理支持 `anthropic`、`openai`、
`openai-compatible`、`anthropic-compatible` 和 `opencode-zen` preset。

```bash
uv run coderook provider list
uv run coderook provider add local --preset openai-compatible \
  --base-url http://127.0.0.1:11434/v1/chat/completions \
  --model local-model --set-key --activate
uv run coderook provider test local
uv run coderook provider use local
uv run coderook model list
uv run coderook config-status
```

`provider add/edit` 默认先运行脱敏连接诊断，再提交 route 和凭据；只有明确使用
`--skip-doctor` 才跳过探测。密钥优先保存在系统 keyring，没有可用后端时降级到
`~/.coderook/credentials.json`，不会出现在 `provider list`、日志或诊断包正文中。

配置优先级从低到高为：内置默认值、`~/.coderook/config.toml`、项目
`.coderook/config.toml`、`.env`、`CODEROOK_*` 环境变量。项目 TOML 不允许设置
`provider`、`base_url`、`api_key_env` 或 `active_route_id` 等路由安全字段。

## TUI

常用键位：

| 输入 | 行为 |
|---|---|
| `Enter` | 发送消息 |
| `Shift+Enter` | 插入换行 |
| `Tab` | 循环 plan/act/operate |
| `Shift+Tab` | 循环权限姿态 |
| `Ctrl+C` | 任务运行中请求取消；空闲时复制选区或上一条回复 |
| `Ctrl+Q` | 退出 TUI |

粘贴本地图片路径后，CodeRook 会验证格式和尺寸，把内容写入 ArtifactStore，并随下一条消息发送。
永久 transcript 不保存图片 base64；当前不承诺直接读取所有终端的剪贴板位图。

### 推荐工作流程

处理代码修改时建议使用以下闭环：

1. **理解**：让 Agent 先读取相关代码、配置和测试，不要一开始就扩大修改范围。
2. **规划**：复杂任务先输入 `/plan <任务>`，审查计划后再切换到 `act`。
3. **执行**：在审批卡中核对工具、路径、命令和 diff；不确定时拒绝。
4. **验证**：要求运行与改动风险相匹配的测试、静态检查或构建命令。
5. **审查**：输入 `/diff` 查看最终改动，输入 `/turn` 查看 route、用量和执行收据。
6. **恢复**：发现改动方向错误时使用 `/rewind` 选择安全恢复点。

Agent 的最终回答应说明完成内容、修改文件、验证结果和未解决问题。回答中的“已完成”不能替代
实际 diff、测试输出或外部发布证据。

### 常见任务写法

| 目标 | 推荐输入 |
|---|---|
| 理解仓库 | `只读分析项目架构、启动流程、数据流和主要测试，不修改文件。` |
| 修复缺陷 | `复现这个错误，定位根因，做最小修复并运行相关测试。` |
| 实现功能 | `先检查现有模式和协议，给出简短计划，实施后补测试和用户文档。` |
| 代码审查 | `审查当前 git diff，只报告可复现问题，给出文件位置、风险和验证方法。` |
| 文档同步 | `以当前代码和命令帮助为准，删除过期描述并检查所有本地链接。` |
| 安全检查 | `只读检查权限、路径边界、密钥处理和 fail-closed 行为，不执行高风险命令。` |

不要只输入“优化一下”“全部修好”这类没有边界的要求。最好明确目标、允许修改的范围、必须保留的
兼容行为和期望验证。

### 会话与模型

| 命令 | 作用 |
|---|---|
| `/help` | 显示键位和全部命令 |
| `/new`、`/sessions` | 新建或切换会话 |
| `/rename <标题>` | 重命名当前会话 |
| `/fork [标题]` | 从当前上下文复制会话 |
| `/export [md\|json]` | 导出当前会话 |
| `/delete --yes` | 永久删除当前会话 |
| `/provider [route]` | 查看或切换 Provider route |
| `/model [模型 ID]` | 查看或切换模型 |
| `/doctor` | 诊断活动 route |
| `/config` | 配置平台、模型和密钥 |
| `/compact` | 手动压缩上下文 |
| `/copy` | 复制上一条回复 |

### 运行控制

| 命令 | 作用 |
|---|---|
| `/plan [任务]` | 进入只读规划，或直接提交规划任务 |
| `/goal <目标>` | 创建 session 级持久 Goal 并立即开始执行 |
| `/goal`、`/goal status` | 查看当前未终结 Goal、run 数、预算和状态 |
| `/goal list` | 查看当前 session 的 Goal 历史 |
| `/goal pause`、`/goal resume` | 立即暂停当前 run，或恢复 Goal 并开始继续轮次 |
| `/goal edit <新目标>` | 修改持久目标；运行中会把修改作为 steer 注入当前 run |
| `/goal complete [验收说明]` | 用户确认目标已验收；记录最近 run 与说明作为完成证据 |
| `/goal clear` | 清除当前 Goal 并取消关联 run；审计记录仍保留 |
| `/mode plan\|act\|operate` | 查看或切换工作模式 |
| `/permissions ask\|auto-review\|full-access` | 查看或切换权限姿态 |
| `/trust status\|grant\|revoke` | 查看或修改工作区信任 |
| `/sandbox` | 探测当前 OS 隔离能力 |
| `/tasks` | 查看本次 run 的任务板 |
| `/workers` | 查看 durable workers 和 fleet |
| `/workflow [start <文件>]` | 查看或启动 workflow |
| `/diff` | 查看工作区改动 |
| `/rewind` | 从安全恢复点回滚文件 |
| `/context`、`/cost`、`/turn [id]` | 查看上下文、成本和 turn 收据 |

Goal 与普通消息不同：它保存在 `~/.coderook/goals/`，绑定创建它的 session，并在每轮系统上下文中
重复注入。一次 run 正常结束只代表这一轮执行成功，Goal 仍保持 `active`，不会因为模型返回了一段答案就
自动宣称完成。Agent 只有在目标及全部完成标准都已验证后，才能用 `update_goal` 工具提交测试、文件、
commit 或报告等具体证据并进入 `completed`；用户也可以执行 `/goal complete [验收说明]` 显式确认。
运行失败时 Goal 进入 `blocked` 并保留原因，可用 `/goal resume` 继续。daemon 在 run 中退出时，重启会
把遗留 Goal 恢复为 `blocked`。Goal 不扩大当前 mode、权限、工作区信任或沙箱能力。

`/goal pause`、`/goal edit`、`/goal complete` 和 `/goal clear` 可在 run 执行期间使用。token 预算可
通过 typed IPC 的 `goal.create.token_budget` 设置；预算耗尽会暂停并取消关联 run。TUI 创建时以目标
文本作为默认完成定义；需要独立完成标准的集成方可传入 `completion_criteria`。

### 扩展与后台状态

| 命令 | 作用 |
|---|---|
| `/skills list\|show\|install\|remove\|audit` | 管理 Skill |
| `/<skill-name>` | 调用已安装 Skill |
| `/mcp [server]` | 查看 MCP server 和工具 |
| `/hooks [rerun <id> --yes]` | 查看或重跑 Hook |
| `/memory [delete <id> --yes]` | 查看或删除项目记忆 |
| `/jobs [show <id>\|cancel <id> --yes]` | 查看或取消后台任务 |
| `/artifacts [gc [days] [--yes]]` | 查看产物或执行感知式清理 |

## 权限与沙箱

工作模式和权限姿态是两个独立维度：

| 设置 | 适用场景 | 行为 |
|---|---|---|
| `plan` | 调研、方案设计、风险评估 | 只读规划 |
| `act` | 日常修复和功能开发 | 允许受控修改 |
| `operate` | 明确需要较广操作面的维护任务 | 允许更多操作，但仍经过权限管线 |
| `ask` | 默认推荐 | 需要权限的动作逐次询问 |
| `auto-review` | 已建立审查流程的可信项目 | 自动接受受支持的可审阅编辑 |
| `full-access` | 用户明确监督的高权限任务 | 扩大自动执行范围，但不取消危险命令规则 |

- 工作区来源不明确时不要执行 `/trust grant`。
- 使用 `full-access` 前先确认任务目标、工作目录和 Git 状态。
- Linux 在探测到 bubblewrap 时可使用强制包装；macOS 使用 Seatbelt。
- Windows 当前没有文件系统/网络强制沙箱，相关 Shell 动作会明确降级到审批链。
- 域名白名单在没有强制后端时拒绝执行，不会静默变成全网访问。

MCP server、Skill、Hook、网页内容和模型输出都应视为不受信任输入。只安装已审查来源，不要在
Prompt、项目 TOML、Issue 或 trace 中放入密钥。

## CLI 与无人值守运行

```bash
uv run coderook ping
uv run coderook chat
uv run coderook sessions --all
uv run coderook chat --resume SESSION_ID
uv run coderook review --goal "审查当前改动" --output-format json
uv run coderook run --goal "分析项目" --output-format stream-json
```

Headless 默认在需要人工审批时 fail-fast。只允许明确工具：

```bash
uv run coderook run --goal "修改并验证代码" \
  --permission-mode allow-list \
  --allow-tool edit_file \
  --allow-tool apply_patch \
  --allow-tool Run.run \
  --allow-tool Bash.run
```

实际可见工具取决于当前 mode、authority、trust、sandbox 和 deferred discovery。若任务可能提问，
还要设置 `--question-mode timeout` 或 `preset`；完整参数以
`uv run coderook run --help` 为准。

会话管理：

```bash
uv run coderook session rename SESSION_ID "新标题"
uv run coderook session fork SESSION_ID --title "实验分支"
uv run coderook session export SESSION_ID --format markdown -o session.md
uv run coderook session delete SESSION_ID --yes
```

诊断与产物：

```bash
uv run coderook doctor all --json
uv run coderook doctor runtime --json
uv run coderook doctor bundle --output coderook-diagnostics.zip --yes
uv run coderook artifacts list --json
uv run coderook artifacts gc --days 30
uv run coderook trace --follow
```

`artifacts gc` 默认预览；执行删除需要显式 `--yes`。诊断包会脱敏，但分享前仍应人工检查路径、
Prompt 和业务数据。

## 本地数据

用户级状态位于 `~/.coderook/`：

- `config.toml`、`routes.json`、`credentials.json`（仅在 keyring 不可用时）；
- `sessions/`、`runtime.db`、`fleet.db`、`workflow.db`；
- `policy.toml`、`ipc-token`、`traces/`。

项目级状态位于 `<workspace>/.coderook/`：

- `context.md`、`memory/`、`artifacts/`；
- `worktrees/`、`skills/`、`agents/`、`hooks.toml`。

升级或迁移前按[升级与回滚指南](UPGRADING.md)备份这些状态。

退出 TUI 不等于停止 Core。Core 可继续持有当前 workspace 的会话、worker 和后台任务；完全停止请运行
`uv run coderook core stop`。

## 常见问题

### 一直显示 connecting

```bash
uv run coderook core status
uv run coderook core restart
uv run coderook ping
```

### 7437 或 7438 被占用

先停止旧 Core。确需改端口时使用 `CODEROOK_PORT` 和 `CODEROOK_API_PORT`；CLI/TUI 与 Core
必须读取同一配置。

### 模型配置后仍不可用

```bash
uv run coderook config-status
uv run coderook provider list
uv run coderook provider test
```

HTTP 401/403 通常表示凭据或账号权限问题；模型列表为空可能是 provider 返回格式不兼容。不要把
真实 key 贴进 Issue。

### 修改配置后没有生效

```bash
uv run coderook core restart
```

环境变量优先级高于文件配置；同时检查终端中遗留的 `CODEROOK_*` 值。

### 需要接口或运维细节

- HTTP/SSE：[Runtime API](../reference/RUNTIME_API.md)
- Core、日志与恢复：[运行手册](../operations/RUNBOOK.md)
- 安全边界：[威胁模型](../reference/THREAT_MODEL.md)
