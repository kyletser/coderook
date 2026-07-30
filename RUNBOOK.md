# 运维手册（RUNBOOK）

## 日常操作

### 启动本地 Agent

```bash
uv run kyle-tui
```

TUI 会自动启动或复用监听 `127.0.0.1:7437` 的 Core daemon。退出 TUI 不会停止 daemon，因此下次启动可以直接恢复会话和后台任务。

### 手动管理 Core

```bash
uv run kyle core start
uv run kyle core status
uv run kyle core stop
```

仅在排障时前台运行：

```bash
uv run kyle-core
uv run kyle-tui --no-auto-core
```

### 验证连通

```bash
uv run kyle ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f kyle-core)
```

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kyle/config.toml` → `.env` → 系统环境变量**。

### 交互式 LLM 配置

本地开发推荐直接运行：

```powershell
uv run kyle configure
uv run kyle config-status
```

支持 Anthropic-compatible 与 OpenAI-compatible 格式。API key 使用隐藏输入，分别保存到
`~/.kyle/credentials.json`；连接地址、模型和 active provider 保存到配置文件。TUI 首次
启动会在缺少配置时自动进入该向导，也可在界面中输入 `/config` 重新配置。

配置变更后，由 Kyle 管理的后台 Core 会自动重启。手动启动的 Core 可执行：

```powershell
uv run kyle core restart
```

### `~/.kyle/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kyle/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

`.env` 适合 CI 或服务器部署。交互式向导会将其中的明文 LLM key 迁移到用户凭据文件：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KYLE_CONFIG` | `~/.kyle/config.toml` | 覆盖配置文件路径 |
| `KYLE_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KYLE_PORT` | `7437` | TCP 监听端口 |
| `KYLE_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KYLE_LOG_FILE` | `~/.kyle/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KYLE_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |
| `KYLE_LLM_PROVIDER` | `anthropic` | `anthropic` 或 `openai_compatible` |
| `KYLE_LLM_DEFAULT_MODEL` | `claude-sonnet-4-6` | 默认模型名 |
| `KYLE_LLM_BASE_URL` | 空 | Anthropic 根地址或 OpenAI Chat Completions 完整地址 |
| `KYLE_LLM_API_KEY_ENV` | `ANTHROPIC_API_KEY` | 指定读取 API key 的环境变量名 |
| `KYLE_CREDENTIALS_FILE` | `~/.kyle/credentials.json` | 覆盖本地凭据文件路径 |

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
tail -f ~/.kyle/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f kyle-core)` |
| `core not running` | 手动模式下未启动守护进程 | 直接运行 `uv run kyle-tui`，或先执行 `uv run kyle core start` |
| `Address already in use` | 端口被其他进程占用 | `KYLE_PORT=8000 uv run kyle-core` |
| `Config error: KYLE_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KYLE_PORT` 的值 |
