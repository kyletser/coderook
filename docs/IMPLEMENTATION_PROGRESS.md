# CodeRook 功能完善进度

> 历史快照：本文记录 2026-07-16 的轻量 Agent 阶段，不再代表当前实现状态。R1–R9 的
> 当前事实与完整门禁以 `SPECDRIVEN_COMPLETION_AUDIT.md` 和
> `SPECDRIVEN_REFACTOR_PLAN.md` 为准。

更新时间：2026-07-16

本文是 `CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md` 的动态执行账本。差距报告负责说明目标，
本文负责记录当前代码已经证明了什么、还缺什么，以及下一轮应从哪里继续。

## 当前目标

将 CodeRook 从“可运行的 Agent Runtime 骨架”推进为标准轻量级 Coding Agent：

1. 有可靠的工作区与权限边界。
2. 能安全检索、精确修改、验证和回滚代码。
3. 会话可持久恢复，运行可取消和纠正。
4. CLI、TUI、未来 Desktop 共用稳定协议。
5. Windows/Linux 质量门禁持续全绿。

## 已完成并验证

| 能力 | 当前实现 | 证据 |
|---|---|---|
| 工作区硬边界 | File tools 统一使用 `WorkspaceBoundary`，拒绝绝对路径逃逸和 `..` | `test_workspace_boundary.py`、File tool 测试 |
| 结构化代码检索 | `glob` / `grep` 支持工作区边界、ignore 规则、稳定排序、结果上限和 Python fallback；`grep` 跳过二进制与超大文件 | `test_search_tools.py`、Agent registry/profile 测试 |
| 精确安全编辑 | `read_file` 返回 SHA-256；`edit_file` 支持唯一/批量精确替换、读后与写入中冲突检测、原子落盘和有界 unified diff；`write_file` 复用原子写 | `test_edit_file.py`、`test_read_file.py` |
| 多文件事务补丁 | `apply_patch` 使用 `unidiff` 解析标准 unified diff；全量预检后事务提交，支持新增/修改/删除、逐 hunk 冲突诊断、失败回滚与 dry-run | `test_apply_patch.py` |
| 结构化 Git 变更检查 | `git_diff` 以只读 Git 子进程返回 staged/unstaged/untracked、rename、numstat 和有界 diff；禁用 optional locks、external diff 与 textconv | `test_git_diff.py` |
| 自动 Checkpoint/Rewind | Edit/Write/ApplyPatch 写前持久化 preimage 与预期 post-hash；支持当前 run 列表、崩溃中间态恢复、冲突预检和多文件事务 rewind | `test_checkpoints.py` |
| 可取消运行与进程树终止 | `run.cancel` 贯通 Core、CLI、chat Ctrl+C 和 TUI；取消时清理权限 Future、前后台 Subagent 和 Windows/POSIX Bash 进程树，Session 落为 `interrupted` | `test_run_cancellation.py`、`test_session_manager.py`、CLI/TUI/IPC 测试 |
| Transcript v2 增量恢复 | assistant/tool_result 按 block 以稳定 ID、index/count 即时 flush+fsync；重复写抑制；取消/冷启动时归档半消息和孤立 tool call 后原子回退；自动 compact 摘要同步持久化 | Store/Loop/Runner/Session/Compactor 单元与取消集成测试 |
| Headless 权限模式 | `agent.run`/`coderook run` 支持 fail-fast、deny、allow-list；默认 ASK 不挂起，allow-list 不继承交互缓存且不绕过 deny/越界规则；权限所需退出码为 3 | PermissionManager、Runner、Core handler、CLI/协议与集成测试 |
| Trace 隐私与保留 | IPC/Event/LLM 默认元数据模式；统一递归 secret redaction；UTF-8 顺序写入、大小轮转、备份数量限制、重启超限处理和写盘失败传播 | TraceWriter/TracingProvider/Config 单元测试 |
| 本地 IPC 认证 | Core/SocketServer 双层拒绝非 loopback；环境变量或私有 token 文件提供随机凭据；首帧同步握手、常量时间比较、失败断连，认证帧不进入 Trace；CLI/TUI 全部迁移 | Auth/Socket/Config/Protocol 单元测试、真实 daemon 未认证拒绝与双客户端 E2E、`coderook ping` smoke |
| 工具重试语义 | Tool 显式声明 `NEVER` / `RATE_LIMIT` / `IDEMPOTENT`，副作用工具默认不重试 | `test_tool_retry.py` |
| Session 增量状态 | user message、run ID 和 `active` 状态在执行前落盘 | `test_send_message_persists_active_state_during_run` |
| Session 冷启动恢复 | Core 扫描 `meta.json`；未完成的 `active` 会话恢复为 `interrupted` | `test_rehydrate_marks_active_session_interrupted` |
| Session list/resume | 新增 `session.list`、`session.resume`，支持关闭后的 chat 恢复 | 单元测试与 `test_s4_session_ipc.py` |
| CLI 历史会话 | `coderook sessions`、`coderook chat --resume SESSION_ID` | CLI help smoke test |
| TUI 历史会话 | `coderook-tui --resume SESSION_ID`，恢复后加载历史消息 | TUI 单元测试、参数 smoke test |
| Session 完整生命周期 | rename/fork/export/delete 使用类型化 IPC；fork 原子复制当前 transcript+notes 并记录 lineage，不复制 runs；delete 原子 tombstone；CLI 管理命令与 TUI `/sessions` picker、`/new` | Store/Manager/Exporter/Protocol/TUI 测试、真实 CLI 生命周期 smoke |
| Windows Core 生命周期 | PID 探活在 Windows 使用 `OpenProcess`，`coderook core stop` 可可靠识别并停止后台 Core | `test_cli_core.py`、真实 stop/start smoke |
| 权限审批 TUI | 紧凑聚焦面板展示动作、完整 shell 命令或目标路径；四种决策、方向键、数字键与 Esc 保持可用，永久规则明确标注跨 Session | TUI 组件测试、真实 Textual 截图、完整权限流回归 |
| 跨平台交付门禁 | GitHub Actions 在 Windows/Ubuntu 执行 frozen sync、Ruff、Mypy、pytest、协议检查、构建和 wheel smoke；锁文件不再被忽略 | `.github/workflows/ci.yml`、`Makefile verify`、本机 wheel smoke |
| Wheel 运行烟测 | 校验内置 agent/skill/typing 资源，从 wheel 隔离导入，验证 CLI/TUI 入口并启动鉴权 Core 执行 ping | `scripts/smoke_wheel.py`、本机真实构建结果 |
| 跨平台测试基线 | Bash timeout 使用 Python 子进程；async 测试适配 Python 3.12 | 全量 pytest |
| 静态质量基线 | 清理 Ruff 遗留项，Mypy strict 通过 | Ruff、Mypy |

当前质量门禁：

- Pytest：388 passed，3 skipped。
- Ruff：All checks passed。
- Mypy：109 个 source files，0 errors。
- Build：wheel/sdist 成功；从 wheel 启动鉴权 Core 并执行 `coderook ping` 成功。

## Phase 0 状态

Phase 0 的代码项已经完成。Windows 本机门禁全绿，Windows/Ubuntu CI 矩阵已经提交到工作区；
当前目录没有有效 Git 仓库，且本机无可用 Linux runner，因此 Ubuntu job 仍需在首次推送后取得远端绿灯。

## Phase 1 核心 Backlog

按真实代码任务成功率排序：

1. steering、AskUserQuestion 和最小 Plan mode。
2. per-run model/effort/cost 与 ContextAssembler 层级规则。
3. TUI changed-files/diff/tasks/background/status bar。

Phase 1 验收场景保持为：

> 恢复历史 Session -> Grep/Glob 定位问题 -> 精确修改多个文件 -> 运行测试 -> 展示 Diff ->
> 用户中途取消或纠正 -> 一键 rewind。

## 下一轮建议

先推送到真实 Git 仓库并确认 Windows/Ubuntu 双绿；随后以 `RunController` 为边界实现
steering、AskUserQuestion 和 Plan mode，避免继续把运行控制分散在 SessionManager、Runner 与 TUI。

逐项证据和诚实边界见 `LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md`。
