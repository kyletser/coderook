# 运维手册（RUNBOOK）

## 日常操作

### 启动本地 Agent

```bash
uv run coderook
```

TUI 会自动启动或复用监听 `127.0.0.1:7437` 的 Core daemon。daemon 与 session 绑定启动时的
workspace；从另一个仓库启动时，空闲的受管 daemon 会自动切换，存在活动 run 时则拒绝切换。
退出 TUI 不会停止 daemon；使用 `uv run coderook --continue` 可恢复当前 workspace 最近会话。
受管 daemon 在 TUI 在线期间意外退出时，连接层会在线程中再次调用安全启动器并恢复同一 session；
`--no-auto-core` 不执行该自动恢复。
若活动 Turn 使 `session.resume` 返回 busy，TUI 会只读附着权威 thread，重新读取完整 transcript，按原
`after_seq` 订阅并恢复未决审批、问题和计划，而不是等待 Turn 超时。

### 手动管理 Core

```bash
uv run coderook core start
uv run coderook core status
uv run coderook core stop
```

仅在排障时前台运行：

```bash
uv run coderook-core
uv run coderook-tui --no-auto-core
```

### 验证连通

```bash
uv run coderook ping
```

### 停止守护进程

```bash
uv run coderook core stop
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.coderook/config.toml` →
`<workspace>/.coderook/config.toml` → 调用方显式指定的 env 文件 → 用户进程环境变量**。
仓库 `.env` 不自动读取。`CODEROOK_CONFIG` 只接受进程环境中的值并指定单一 TOML；项目 TOML
不能设置 route 安全字段，即使显式指向该文件也不能绕过。

### 交互式 LLM 配置

本地开发推荐直接运行：

```powershell
uv run coderook configure
uv run coderook config-status
```

共享 Catalog 支持 DeepSeek、OpenAI、Anthropic、Gemini、Kimi/Moonshot、OpenRouter、SiliconFlow、
Ollama 和 LM Studio，并允许自定义 OpenAI Chat、OpenAI Responses 与 Anthropic Messages route。
API key 使用隐藏输入，优先保存到系统 keyring；没有可用 keyring 时降级到
`~/.coderook/credentials.json`。TUI 缺少配置时仍直接进入主界面，并提示输入 `/config`；首次提交
任务前 readiness 不通过会保留草稿且不创建 run。

CLI/TUI 候选 route 默认先执行 ProviderDoctor；只有凭据、TLS、endpoint、模型、真实流式终止，
以及 route 声明的工具、并行工具和图片能力全部通过后，才一次性提交 route/active/credential。
单次 Doctor 最多使用 3 个小输出请求，并行工具探针同时作为普通工具证据；收据只保存 route/model
摘要和脱敏状态。公开 CLI 不提供绕过 Doctor 的保存入口；新增、编辑和切换 route 的任一必需探针
失败都会回滚配置事务。配置变更后，由 CodeRook 管理的后台 Core 会自动重启。手动
启动的 Core 可执行：

```powershell
uv run coderook core restart
```

每条 Route 只解析自身明确的 `env:`、`keyring:`、`file:` 或 `none:` 凭据引用；交互式保存优先写
keyring，失败后写用户凭据文件。前置备份 marker `provider-catalog-v1.json` 与迁移收据
`provider-catalog-v1.receipt.json` 是独立证据。收据损坏、未来版本、冲突覆盖，或完成收据配空
Catalog 都会拒绝自动重迁移并使 Core 进入 `audit_degraded`。Runtime Doctor 的
`credential_store_status` 只检查 fallback JSON 文件；实际 Route 是否可运行仍由 readiness/Provider
Doctor 判断。

### `~/.coderook/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.coderook/logs/core.log"
format = "text"    # "text" | "json"
```

### 显式 env 文件

CodeRook 不自动读取仓库 `.env`。需要 CI/部署文件时，必须由调用方显式传入；文件中的
`CODEROOK_CONFIG` 会被拒绝。解析关闭 `${NAME}` 插值，读取只形成不修改 `os.environ` 的进程内
overlay；用户进程同名变量优先。Core、TUI/CLI readiness、Doctor、Provider 命令和 WebSearch 共用该
overlay，凭据不经 IPC 传输。

```bash
uv run coderook --env-file /absolute/path/coderook.env
uv run coderook-core --env-file /absolute/path/coderook.env
```

TUI 使用显式文件时会停止无活动任务的同工作区受管 Core，并以相同路径重启。busy/non-managed Core、
停止后仍占用端口或 `--no-auto-core --env-file` 组合都会拒绝启动；这是因为当前不持久化 daemon 配置
指纹，不能安全猜测已运行 Core 的 credential overlay。日常开发优先使用用户配置与 credential store，
不要在仓库保存密钥。

### Labs 开关

Fleet、声明式 Workflow、Hooks v2、MCP Resources/Prompts 和 VS Code 原型属于 Labs，默认关闭。

```bash
CODEROOK_LABS=1 uv run coderook
```

PowerShell 先设置 `$env:CODEROOK_LABS = "1"`。修改开关后必须重启 Core。关闭时 Core 不读取用户或
项目 Hook 配置，不暴露/恢复 Workflow/Fleet 控制面；开启不绕过 workspace trust、权限或审计门禁。

### 系统环境变量

下列 `CODEROOK_LLM_*` 是旧 `LlmConfig` 的用户进程兼容入口；daemon 启动时可迁移为 Route Catalog。
项目 TOML 和自动发现的仓库文件不能借这些字段选择端点或凭据。新配置优先使用 `provider` 命令和
`routes.json`。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CODEROOK_CONFIG` | `~/.coderook/config.toml` | 覆盖配置文件路径 |
| `CODEROOK_HOST` | `127.0.0.1` | TCP 监听地址 |
| `CODEROOK_PORT` | `7437` | TCP 监听端口 |
| `CODEROOK_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `CODEROOK_LOG_FILE` | `~/.coderook/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `CODEROOK_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |
| `CODEROOK_LABS` | 空（关闭） | `1/true/yes/on` 显式启用 Labs；修改后重启 Core |
| `CODEROOK_LLM_PROVIDER` | `anthropic` | `anthropic` 或 `openai_compatible` |
| `CODEROOK_LLM_DEFAULT_MODEL` | `claude-sonnet-4-6` | 默认模型名 |
| `CODEROOK_LLM_BASE_URL` | 空 | Anthropic 根地址或 OpenAI Chat Completions 完整地址 |
| `CODEROOK_LLM_API_KEY_ENV` | `ANTHROPIC_API_KEY` | 指定读取 API key 的环境变量名 |
| `CODEROOK_CREDENTIALS_FILE` | `~/.coderook/credentials.json` | 覆盖本地凭据文件路径 |

---

## 诊断与恢复

```bash
uv run coderook doctor all --json
uv run coderook doctor runtime --json
uv run coderook doctor runtime --repair
uv run coderook doctor bundle --output coderook-diagnostics.zip --yes
```

`doctor all` 即使 route 或 runtime 文件损坏也会在 `errors` 中报告对应 section。
`doctor runtime --json` 是严格只读检查，不创建、移动或重写状态；报告分别列出 `backup_status`、
`provider_catalog_status`、`credential_store_status` 和 `route_catalog_status`，并包含事件 gap、外键、
投影漂移及隔离记录。当前 SQLite `PRAGMA user_version` 为 4，而 Thread/Turn/Item/Event/Facade 的逐行
schema 仍为 1；未来数据库或逐行 schema 都失败关闭，不会伪装成旧记录。

显式 `runtime --repair` 可隔离已通过文件身份复核的坏 Session/Goal/Task 元数据、升级受支持的旧 Runtime
schema、补建缺失投影并修复 event counter，同时写入 no-follow repair journal。它不会修补 event gap、
外键损坏、未来 schema、transcript checksum、无效 Route/Credential 或迁移收据。若运行中的 Core 已进入
`audit_degraded`，完成修复后必须重启 Core；独立 Doctor 不会热同步清除 daemon 的降级状态。
诊断包默认仅包含系统报告与脱敏日志，不包含 session 正文、credentials 或原始 trace，且必须显式确认。

## 分发入口

当前没有已发布 PyPI 或 GitHub Release，源码安装仍是公开入口。维护者可构建候选自包含包：

```bash
uv run python scripts/build_portable.py --target windows-x86_64
uv run python scripts/build_portable.py --target linux-x86_64
uv run python scripts/build_portable.py --target linux-arm64
uv run python scripts/build_portable.py --target macos-x86_64
uv run python scripts/build_portable.py --target macos-arm64
```

每条命令只能在与 target 匹配的 OS/CPU host 上执行；构建器会在删除或写入输出前检查宿主并失败
关闭，不支持把当前解释器伪装成交叉编译 runtime。五个 target 由对应 GitHub runner 分别构建。

未来 Release 中的 `scripts/install-release.ps1` 与 `scripts/install.sh` 会下载版本化 archive 并校验
`SHA256SUMS`。Homebrew formula 和 Scoop manifest 只作为 Release asset 生成；当前没有外部 tap/bucket，
不能把它们写成包管理器已上线。容器使用 `Dockerfile` 或
`docker compose -f deploy/docker-compose.example.yml up --build`。Runtime API 始终校验 Bearer token：
非空 `CODEROOK_API_TOKEN` 优先；空或纯空白值按未配置处理，不能关闭鉴权。未配置时 Core 以
no-follow/排他创建语义加载或创建 `~/.coderook/api-token`：POSIX 要求当前用户所有且严格为 0600，
Windows 验证父目录、普通文件、重解析点与句柄/路径身份而不宣称 POSIX chmod。包括 loopback 在内的
每个 HTTP/JSON 和 SSE 请求都必须发送
`Authorization: Bearer <token>`。

## 沙箱边界

```bash
uv run python scripts/check_sandbox_boundary.py
```

Linux/macOS 有后端时，该命令真实验证工作区内写成功、工作区外写失败、read-only 写失败。
Windows 会运行 Restricted Token + ACL 真实探针；成功时输出 `PASS (windows_acl)` 并报告 `partial`，
失败时输出 `DEGRADED (windows_none)`。Windows 即使探针成功也保持 Shell/Run Ask-only，因为读取和网络
不在该后端边界内；Job Object 只负责进程树回收。

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify                           # 完整验证（跨平台类型 + 测试 + 协议 + 构建）
```

---

## 日志

```bash
tail -f ~/.coderook/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `uv run coderook core stop` |
| `Core is busy in another workspace` | 另一仓库仍有活动 run | 回到该仓库完成或取消 run，再从当前仓库启动 |
| `core not running` | 手动模式下未启动守护进程 | 直接运行 `uv run coderook`，或先执行 `uv run coderook core start` |
| `automatic restart failed` | TUI 检测到 Core 退出，但安全启动器未能恢复 | 运行 `uv run coderook core status`，再查看 `~/.coderook/logs/core.log` |
| `Address already in use` | 端口被其他进程占用 | `CODEROOK_PORT=8000 uv run coderook-core` |
| `Config error: CODEROOK_PORT must be an integer` | 显式 env 文件或进程环境中的端口值非整数 | 检查 `CODEROOK_PORT` 的值 |
