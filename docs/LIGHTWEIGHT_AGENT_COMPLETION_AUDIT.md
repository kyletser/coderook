# CodeRook 轻量级 Agent 完成度审计

更新时间：2026-07-16

## 结论

CodeRook 已达到“可本地运行、可完成真实代码修改闭环、具备安全与恢复边界”的标准轻量级
Coding Agent 基线，但尚未完成差距分析文档中的全部 Phase 1 增强项。

- **轻量级 Agent 基线：达到。** 能完成检索、精确编辑、多文件补丁、测试、diff、checkpoint、
  rewind、交互审批、取消和历史 Session 恢复。
- **Phase 0 代码交付：完成。** Windows 本地全量门禁和 wheel 运行烟测通过，Windows/Linux
  GitHub Actions 已配置。
- **完整 Phase 1：未完成。** run steering、AskUserQuestion、Plan mode、模型路由/成本和 TUI
  独立 changed-files/tasks 状态区仍是明确 backlog。
- **PC 版：已完成可执行工作计划，尚未开始 Tauri/PyInstaller 产品实现。**

## 需求证据

| 要求 | 判定 | 当前证据 |
|---|---|---|
| 多步 Agent loop 与工具回填 | 已完成 | `AgentLoop`、`AgentRunner`，run/step/tool 事件与单元测试 |
| 工作区边界 | 已完成 | `WorkspaceBoundary` 统一约束 File/Search/Edit；绝对路径、`..`、symlink/junction 测试 |
| 代码检索 | 已完成 | Read/List/Glob/Grep，ignore、二进制、大小和结果上限测试 |
| 精确编辑 | 已完成 | read hash、Edit 冲突检测、原子写、ApplyPatch 事务与回滚 |
| 变更检查与恢复 | 已完成 | GitDiff、自动 checkpoint、冲突预检与事务 rewind |
| Shell 与权限 | 已完成基线 | 超时/输出限制/进程树取消；策略审批、headless fail-fast/allow-list |
| 审批 TUI | 已完成 | 展示完整 shell 命令或目标路径；四种决策、键盘操作和跨 Session 提示 |
| Session 生命周期 | 已完成 | 增量 transcript、崩溃恢复、list/resume/rename/fork/export/delete、TUI picker |
| 运行取消 | 已完成 | `run.cancel` 清理 Bash、权限 Future 和 Subagent，状态落为 interrupted |
| 本地 IPC 安全 | 已完成 | 强制 loopback、随机私有 token、首帧鉴权、常量时间比较、失败断连 |
| 隐私与诊断 | 已完成基线 | Trace 默认元数据、递归脱敏、轮转和 retention |
| 打包可运行性 | 已完成开发机 smoke | wheel/sdist 构建；从 wheel 启动鉴权 Core 并由 CLI ping |
| Windows/Linux CI | 已配置，远端待运行 | `.github/workflows/ci.yml` 双平台矩阵；本机无可用 Linux runner |
| run steering | 未完成 | 当前 busy Session 拒绝新消息 |
| AskUserQuestion / Plan mode | 未完成 | 无对应类型化命令、事件和 TUI 状态机 |
| TUI 独立 diff/tasks/status 面板 | 部分完成 | tool timeline 可看输出，尚无专用侧栏/状态区 |
| OS 级 sandbox | 未完成，非轻量基线 | 有工作区和审批边界，但 Bash 不是容器/AppContainer 隔离 |

## 本轮验证

```text
Ruff: all checks passed
Mypy strict: 109 source files, 0 errors
Pytest: 388 passed, 3 skipped
WIRE_PROTOCOL.md: generated document is current
Build: coderook-0.0.1-py3-none-any.whl + source distribution
Wheel smoke: packaged resources, CLI/TUI entry points, authenticated Core and ping passed
```

Linux 没有在当前 Windows 主机上被伪装验证：本机无 WSL，Docker daemon 不可访问，并且当前目录
没有有效 Git 历史可触发 GitHub Actions。首次推送仓库后，必须以 Actions 的 Ubuntu job 结果补齐证据。

## 达到完整 Phase 1 的最短路径

1. 增加 `RunController`：steering queue、等待用户、取消和 run 状态机统一归口。
2. 增加类型化 AskUserQuestion 请求/响应，并在 TUI 原位呈现。
3. 增加最小 Plan mode：只读探索、计划审批、批准后切换执行。
4. 接通 per-run model/effort/cost，并在 TUI 状态区展示。
5. 增加 changed-files、diff、tasks/background 专用视图。
6. 在 GitHub Actions 获得 Windows/Ubuntu 双绿，并增加干净 Windows VM 安装包测试。

这些是下一阶段增强，不应反向否定当前已成立的轻量级 Agent 基线，也不能在简历中写成已实现。
