# CodeRook 升级、备份与回滚

CodeRook 当前尚未发布到 PyPI 或 GitHub Releases，本指南首先适用于源码检出、本地构建 wheel 和
候选 portable archive。
出现公开版本后，安装来源与升级兼容性仍以
[发布评分卡](../status/RELEASE_SCORECARD.md)为准；没有完成跨版本 fixture 的版本不得宣称无损升级已经验证。

## 升级前先备份

先停止正在运行的 `coderook-core`，避免复制 SQLite 和 ledger 时得到不同时间点的数据。CodeRook 的用户状态默认位于
`~/.coderook/`，工作区状态位于项目根目录的 `.coderook/`。

需要保留的用户数据包括：

- `config.toml`、`routes.json`、`credentials.json` 与 `policy.toml`；
- `sessions/`、`goals/`、`runtime.db`、`fleet.db` 与 `workflow.db`；
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

当前 daemon 在进入 v1 Provider Catalog 迁移前还会调用 `UpgradeBackupManager`，把用户级 config、
routes、credentials、sessions、goals、Runtime/Fleet/Workflow 数据复制到
`~/.coderook/backups/`，并在 `~/.coderook/migrations/` 保存幂等标记。首次运行
`coderook configure` 或 `coderook provider add/edit/remove/use` 也会在同一个短时跨进程互斥区内先完成
这份备份，再修改 Provider 状态；它不会为此停止正在运行的 daemon。Marker 和 manifest 记录每个受控
备份项的目录树/文件 SHA-256。损坏、越界、摘要不一致或与 manifest 不一致的迁移标记会保留原证据并
让写入失败关闭，绝不会用已经变化的状态重建“迁移前”快照。修复时应恢复可信
备份或由用户明确移走无效 marker 后重新评估，而不能把自动重建当成恢复。这个自动备份只覆盖当前显式
登记的迁移目标，不能替代停止 daemon 后的整目录人工备份。

前置备份 marker `provider-catalog-v1.json` 与迁移完成收据
`provider-catalog-v1.receipt.json` 是两份独立证据。收据记录 `catalog_present`、
`legacy_not_configured` 或 `migrated`，以及不含密钥正文的旧配置摘要、结果 Catalog 摘要和自校验摘要。
收据损坏、未来版本、冲突覆盖，或完成收据与空 Catalog 的非法组合都会拒绝自动重迁移；首次迁移若收据
写入失败会精确恢复原 Route Catalog。Core 随后进入 `audit_degraded`，保留诊断/只读入口并暂停修改动作。

普通状态存储加载或显式 `coderook doctor runtime --repair` 可以在文件身份仍与检查时一致时，把单条坏
Session、Goal 或 Task 元数据移到相邻 `_quarantine/`，并在 `quarantine.jsonl` 写入不含原始内容的
原因；只读 `coderook doctor runtime --json` 只报告，不移动文件。已隔离记录继续计入报告，原内容不会
被猜测修复，恢复仍应从升级前备份进行。

当前 SQLite `PRAGMA user_version` 为 4；v4 为 `runtime_session_facades` 增加逐行
`schema_version`。数据库版本与公开记录版本不是同一概念：Thread、Turn、Item、Event 和 Facade 的当前
逐行 schema 仍为 1。Doctor 对未来数据库版本或未来逐行版本失败关闭，不把它们降级成旧记录，也不允许
repair 覆盖。`credentials.json` 当前文档版本为 2；v1 文件仍可读取并在下一次受管写入时升级，未来
版本、未知字段、损坏 JSON 或不安全路径均保留原文件并失败关闭。

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
daemon 的 wire protocol 必须来自同一安装版本。公开包发布并在评分卡登记后才可使用
`python -m pip install --upgrade coderook`。Homebrew formula 与 Scoop manifest 当前只计划作为
Release asset；外部 tap/bucket 尚未发布，不能作为升级来源。

## 升级后检查

至少确认以下事项：

1. `coderook config-status` 能读取原有 route，并且不会打印密钥；
2. `coderook ping` 能启动或连接本地 daemon；
3. TUI 能打开既有 thread，并能创建一个只读测试任务；
4. `~/.coderook/` 中的 session ledger 和数据库仍可读取；
5. `coderook doctor runtime --json` 没有意外的隔离记录或无效迁移标记；
6. 若使用 MCP、Hook 或 workspace Skill，逐项运行它们的 doctor 或最小示例。Hooks 属于默认关闭的
   Labs，只有在明确设置 `CODEROOK_LABS=1` 并重启 Core 后才会加载。

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
