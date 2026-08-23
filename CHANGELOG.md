# Changelog

本项目采用 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 结构。当前包版本为
`0.1.0` Alpha，但仓库尚未创建 Git tag 或公开 Release；Beta 前公共接口仍可能变化。

## [Unreleased]

### Added

- 本地双进程 Coding Agent runtime：Core daemon、TUI、CLI、HTTP/SSE 和 Python SDK。
- 类型化 IPC、durable thread/turn/event、会话恢复、上下文压缩和可审计 Turn Receipt。
- File/Git/Run/Bash 工具族、权限审批、checkpoint/rewind、仓库索引、诊断和持久 Shell。
- Task、Goal、Subagent、Fleet、Worktree 和声明式 Workflow。
- TUI `/goal` 持久目标模式，支持状态、历史、暂停、恢复、编辑、清除、预算和完成证据。
- Provider route、凭据存储、成本路由、MCP、Skills、Hooks、Memory 和 Artifact 管理。
- 50 任务 benchmark、Aider/SWE-bench 适配器、三平台发行与恢复验证脚本。
- VS Code Runtime API 客户端原型及 Extension Host smoke。
- 贡献、安全、治理、发布、兼容性和威胁模型文档。

### Changed

- 无可用模型配置时直接进入 TUI，由空状态提示用户运行 `/config`，不强制首屏配置。
- Session 持久化绑定 workspace；`coderook --continue` 恢复当前仓库最近会话，跨仓库启动时仅自动切换空闲受管 Core。
- TUI 顶栏显示当前仓库名；手动 `--no-auto-core` 模式也拒绝连接到其他 workspace。
- 受管 Core 意外退出时由 TUI 自动重新启动并恢复同一 session；界面明确显示新建、历史恢复与断线续接状态。
- 文档按用户指南、技术参考、运维发布和当前状态四类精简；删除历史计划、求职材料和重复报告。
- 远端 GitHub Actions 按维护者要求关闭；workflow 文件仍保留为可恢复的自动化定义。

### Fixed

- TUI 在任务、运行中纠偏、Goal 或问题回答发送失败时恢复草稿与附件；run 启动窗口不再清空提前输入的纠偏。
- Provider 诊断失败返回非零退出码。
- Benchmark `--help` 的百分号格式化错误。
- Windows 事件回放、wheel 冷启动和 Git racy-clean 相关竞态。

[Unreleased]: https://github.com/kyletser/coderook/commits/main
