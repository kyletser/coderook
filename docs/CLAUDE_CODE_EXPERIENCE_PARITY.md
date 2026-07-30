# CodeRook 与 Claude Code 公开体验对齐矩阵

更新时间：2026-07-30

## 边界与计分方法

本矩阵只使用 Claude Code 官方公开文档、公开 CLI 行为和 CodeRook 当前代码/测试作为证据，
不使用、复制或推断任何泄露源码。

公开基准：

- [权限与 Plan Mode](https://code.claude.com/docs/en/permission-modes)
- [权限规则](https://code.claude.com/docs/en/permissions)
- [交互模式](https://code.claude.com/docs/en/interactive-mode)
- [斜杠命令](https://code.claude.com/docs/en/commands)
- [工具参考](https://code.claude.com/docs/en/tools-reference)
- [CLI 参考](https://code.claude.com/docs/en/cli-usage)

分数按用户完成真实编码任务时的影响加权，不按命令数量计数。只有同时具备实现、用户入口和针对性
测试的能力才能获得完整分；只有底层类或文档不能得分。当前工作树的保守得分为 **72/100**，
尚未达到 80 分目标。

## 当前加权结果

| 体验域 | 权重 | 当前得分 | 已证明能力 | 主要缺口 |
|---|---:|---:|---|---|
| 核心编码循环与工具执行 | 20 | 17 | 多步 loop；Read/List/Glob/Grep；Bash；Edit/Write/ApplyPatch；真实工具结果回填；意图与进度事件 | 无交互式用户问题工具；缺少 turn steering |
| 安全编辑、Diff 与恢复 | 15 | 12 | 工作区硬边界；权限审批；结构化 git diff；写前 checkpoint；冲突安全 rewind | TUI 无 `/diff`、`/rewind` 专用交互；无 OS 级 Windows sandbox |
| Session 与续接 | 10 | 8 | 持久 transcript；恢复、中断、列表、重命名、fork、export、delete；TUI session picker | TUI 缺 `/rename`、`/resume`、会话搜索；没有跨设备续接 |
| 权限模式与 Plan Mode | 15 | 9 | typed `RuntimeMode`；Plan 只暴露纯读工具；authority 二次拒绝；`/plan`；计划审阅、批准、反馈、取消 | 无 Shift+Tab 模式循环；无 `/permissions` 规则管理；无 accept-edits/auto UI；Plan 暂不开放只读 Bash |
| Context、Memory 与压缩 | 10 | 7 | 全局/项目/session context；durable memory；notes；自动与手动 compact；工具输出预算 | 无 `/context` 可视化；无 memory 管理 UI；缺少清晰 token/cost 分解 |
| TUI 输入、输出与可观测性 | 10 | 7 | 流式 Markdown；真实剪贴板与 `/copy`；工具时间线和输出；内联审批；模型/API 切换 | 无 changed-files、tasks、background、diff 专用面板；无 Vim 输入模式 |
| Subagent、后台任务与 worktree | 8 | 5 | 子 Agent；后台命令注册表；结果轮询；受管 Git worktree；父子取消 | 无用户可见任务中心；无 steering/handoff；并行结果缺少统一汇总 UI |
| Skills、Hooks 与 MCP 扩展 | 7 | 4 | 内置/用户/项目 skills；生命周期 hooks；stdio/TCP MCP 工具接入 | 无 `/mcp`、`/hooks` 管理 UI；无插件市场与 MCP 资源/提示词入口 |
| Headless、自动化与诊断 | 5 | 3 | one-shot run；headless 权限模式；trace、脱敏、轮转；session export；wheel smoke | 缺少结构化流式输出格式、预算/成本参数、doctor/usage 命令 |
| **总计** | **100** | **72** |  | **距离目标至少 8 分** |

## Plan Mode 本轮验收合同

Plan Mode 不是提示词约定，而是以下三层共同成立：

1. `session.send_message.runtime_mode=plan` 被持久化为该 turn 的 mode。
2. `AgentRunner` 只注册 `side_effect=NONE` 的工具；Bash、写文件、memory/task 写入、
   background、subagent、worktree mutation 和未声明副作用的 MCP 工具不进入 schema。
3. `PermissionManager` 在执行层用 Plan authority 拒绝任何非 Read action，历史 always-allow
   不能绕过。

对应证据：

- `tests/unit/test_runner.py::test_plan_mode_enforces_read_only_registry_and_restores_authority`
- `tests/unit/test_authority_matrix.py::test_plan_denial_cannot_be_bypassed_by_permission_cache`
- `tests/unit/test_runtime_service.py::test_start_turn_applies_per_turn_plan_mode`
- `tests/unit/test_session_manager.py::test_plan_turn_publishes_reviewable_plan`
- `tests/unit/test_tui_app.py::test_plan_command_requires_review_before_act`

## 达到并证明 80 分的下一批工作

按加权收益与依赖顺序：

1. 实现 `/permissions` 和 Shift+Tab 的 `ask -> accept edits -> plan` 模式循环，补齐 TUI
   状态指示和每轮 authority 冻结，预计可验证增加 3 分。
2. 实现 typed `AskUserQuestion` 与运行中 steering queue，用户可在长任务中纠正方向而不是只能取消，
   预计可验证增加 3 分。
3. 实现 `/tasks`、`/diff`、`/rewind`、`/context` 四个高频 TUI 视图，复用现有 Core 能力，
   预计可验证增加 3 分。

达到 80 分只能由更新后的矩阵、完整 CI 和真实终端验收共同证明，不能用预计分数代替。
