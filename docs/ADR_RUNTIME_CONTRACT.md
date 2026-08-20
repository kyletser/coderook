# ADR：统一 Runtime Contract 与持久化边界

- 状态：Accepted
- 日期：2026-08-04
- 适用范围：`SPECDRIVEN_SPEC.md` P0、R1–R9

## 背景

CodeRook 保留 Core/TUI 双进程与历史 `session.*` 行为，但所有新入口必须解释同一套
Thread/Turn/Item/Event 事实。本文冻结已经落地的关键决策，避免后续在实现 PR 中重新引入第二
套状态源。

## 决策

### 1. 数据库位置与版本

- 用户级 runtime 数据库固定为 `~/.coderook/runtime.db`，不写入项目仓库。
- schema 由 `runtime/migrations.py` 按 `PRAGMA user_version` 顺序迁移。
- migration 必须可重复打开，旧版本使用显式默认值升级，禁止根据当前进程内状态猜测字段。

### 2. Thread 事件序号

- 每个 Thread 在 `runtime_event_counters` 中独立保存 `next_seq`。
- 状态、Item、Event 与 `next_seq` 更新在同一 `BEGIN IMMEDIATE` transaction 内完成。
- transaction 中断必须整体回滚；恢复后的下一条事件继续使用未被消费的序号。

### 3. SQLite、JSONL 与 Artifact 的事实边界

- SQLite 保存 Thread、Turn、Item 元数据、结构化 payload、Event、seq、route、authority 与 usage。
- 历史 session transcript 和 run JSONL 作为兼容输入与诊断记录，不再是运行状态事实源。
- 工具输出达到 hard limit 时写入内容寻址的 `.coderook/artifacts/<sha256>`；SQLite 仅保存
  handle、hash、字节数和有界 preview。
- Receipt 只从 durable Turn/Item/Event 构建，不读取当前 Core 私有字典。

### 4. 历史 Session 导入

- `SessionManager` 首次访问时通过 `RuntimeService.bootstrap_sessions()` 惰性导入历史 session。
- Thread ID 沿用 Session ID，历史 run ID 沿用 Turn ID。
- Thread/Turn 的唯一键与 upsert/duplicate handling 使中断后的重复导入幂等。
- `session.*` 是兼容 facade；create/resume/rename/fork/archive/delete 同步更新 runtime 投影。

### 5. Boot recovery

- 每次 Core 启动生成新的 `boot_id`。
- 启动时，仍为 active 且 `boot_id` 不同的 Turn 原子转为 `interrupted` 并追加可回放事件。
- 未配对 tool call 不伪造成功 result；恢复事件明确记录中断原因，Turn Receipt 可解释缺口。
- Worker 使用相同原则：不同 boot 或 heartbeat lease 到期的 active Worker 转为
  `interrupted`，之后只能经有界 retry/resume 路径进入下一 attempt。

### 6. 同步/异步数据库访问

- RuntimeStore 保持同步、短事务、每次操作独立连接，便于迁移和离线 Receipt。
- RuntimeService 的所有 asyncio 公共路径使用 `asyncio.to_thread()` 调用 RuntimeStore，并用
  单一写锁保持事件顺序。
- 同步 Task event sink 只负责排队；SQLite 投影在线程池执行，并在 Turn 进入终态及 Core
  shutdown 前 `drain_pending_writes()`，不得阻塞 event loop 或丢失已接受的 timeline。

## 迁移与恢复证据

P0 fixture 覆盖：

- 空用户目录与幂等建库：`test_runtime_store.py`。
- 现有 session/transcript 惰性导入：`test_runtime_service.py`、`test_state_migration.py`。
- 损坏 metadata/transcript 尾部：`test_session_store.py`。
- 未完成 tool call：`test_session_store.py`、`test_runtime_recovery.py`。
- 异常退出时 running Turn：`test_runtime_recovery.py`。
- transaction 中断与异步 DB 边界：`test_runtime_store.py`、`test_runtime_service.py`。

## 后果

- CLI、TUI、headless、HTTP/SSE 和 Worker 事件可以按相同 ID/seq 解释一次执行。
- SQLite I/O 不进入 event loop 热路径，但写入失败会在 drain/turn completion 时传播，阻止静默
  完成。
- R10 的 IDE/Web/remote 能力只能消费当前 runtime contract，不能建立新的运行状态源。
