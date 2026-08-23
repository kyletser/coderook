# CodeRook 升级、备份与回滚

CodeRook 当前尚未发布到 PyPI 或 GitHub Releases，本指南首先适用于源码检出和本地构建 wheel。
出现公开版本后，安装来源与升级兼容性仍以
[发布评分卡](../status/RELEASE_SCORECARD.md)为准；没有完成跨版本 fixture 的版本不得宣称无损升级已经验证。

## 升级前先备份

先停止正在运行的 `coderook-core`，避免复制 SQLite 和 ledger 时得到不同时间点的数据。CodeRook 的用户状态默认位于
`~/.coderook/`，工作区状态位于项目根目录的 `.coderook/`。

需要保留的用户数据包括：

- `config.toml`、`routes.json`、`credentials.json` 与 `policy.toml`；
- `sessions/`、`runtime.db`、`fleet.db` 与 `workflow.db`；
- `memory/`、`traces/` 和其他希望保留的运行证据。

PowerShell：

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$source = Join-Path $env:USERPROFILE '.coderook'
$backup = Join-Path $env:USERPROFILE "coderook-backup-$stamp"
Copy-Item -LiteralPath $source -Destination $backup -Recurse
```

macOS/Linux：

```bash
stamp="$(date +%Y%m%d-%H%M%S)"
cp -a "$HOME/.coderook" "$HOME/coderook-backup-$stamp"
```

工作区中的 `.coderook/` 应单独复制。不要把凭据备份提交到 Git。

## 执行升级

先记录当前版本，然后使用原来的安装方式升级。源码检出使用：

```bash
uv run coderook --version
git pull --ff-only
uv sync
uv run coderook config-status
uv run coderook ping
```

本地 wheel 安装应重新构建并显式安装该 wheel。升级过程中不要同时运行两个版本的 daemon；客户端与
daemon 的 wire protocol 必须来自同一安装版本。公开包发布后才可使用
`python -m pip install --upgrade coderook`。

## 升级后检查

至少确认以下事项：

1. `coderook config-status` 能读取原有 route，并且不会打印密钥；
2. `coderook ping` 能启动或连接本地 daemon；
3. TUI 能打开既有 thread，并能创建一个只读测试任务；
4. `~/.coderook/` 中的 session ledger 和数据库仍可读取；
5. 若使用 MCP、Hook 或 workspace Skill，逐项运行它们的 doctor 或最小示例。

## 按需安装态 preflight

维护者可在不调用模型的情况下，从一个不可变历史 commit 或 tag 构建 baseline wheel，再构建当前
candidate wheel，依次验证：baseline 创建持久 thread、candidate 安装后保留旧 thread 并写入新 thread、
恢复完整备份并重新安装 baseline 后只剩旧 thread。

```bash
uv run python scripts/run_upgrade_preflight.py \
  --baseline-ref <full-commit-or-tag> \
  --evidence artifacts/upgrade-preflight.json
```

普通候选 preflight 接受完整 commit；正式跨已发布版本验收必须额外传入
`--require-baseline-tag`。该参数会要求 baseline commit 有精确 Git tag，不能用 Changelog 链接、版本号
字符串或未发布 commit 冒充公开版本。该检查不放入日常 CI；发布维护者按需运行并归档 JSON 证据，
避免每次提交都重复构建历史 wheel、创建隔离环境和执行回滚。历史候选曾在 Ubuntu、macOS、Windows
完成一次同 commit 验证；因为 baseline 没有精确 tag，只作为候选兼容性证据，不算跨已发布版本验收。

## 回滚

若升级失败，停止 daemon，重新安装之前记录的版本，再恢复完整备份。恢复 SQLite 时必须同时恢复同一次备份中的
`sessions/` 和数据库文件，不要把新旧版本文件混合覆盖。

```bash
python -m pip install "coderook==<previous-version>"
coderook --version
coderook config-status
coderook ping
```

如果失败只来自配置字段，优先保留备份并修正 `config.toml`；未知配置键会触发硬错误，这是为了避免拼写错误被静默忽略。
若数据已经由新版迁移且旧版无法读取，应恢复升级前的整个目录，而不是只替换单个数据库。

## 兼容性原则

- 配置 schema 的破坏性变更必须写入 `CHANGELOG.md`，并提供迁移或明确的人工步骤；
- durable ledger 和数据库迁移必须可检测版本，禁止静默丢弃未知字段或记录；
- wire protocol 变更必须同步生成 `WIRE_PROTOCOL.md`；
- 每个公开版本都应保存“上一公开版本 → 当前版本 → 回滚”的自动化 fixture 报告。
