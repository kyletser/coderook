# Changelog

本项目采用 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 结构。当前包版本为
`0.2.0b1`（对应产品候选名 `0.2.0-beta.1`），但仓库尚未创建 Git tag 或公开 Release；
稳定版前公共接口仍可能变化。

## [Unreleased]

### Added

- 同一 Core 上的 CodeRook Web：`coderook web` 一键启动、本地静态 SPA、durable SSE 时间线、
  会话/模型/文件/Diff/审批/恢复/Goal/Worker/Skill/MCP/Memory 控制面。
- 单次 60 秒 Web 启动票据、HttpOnly SameSite Cookie、CSRF/Origin/Host 校验与严格 CSP；浏览器
  不接触 Core bearer token，Provider API Key 也不进入 URL 或 Web Storage。
- 本地双进程 Coding Agent runtime：Core daemon、TUI、CLI、HTTP/SSE 和 Python SDK。
- 类型化 IPC、durable thread/turn/event、会话恢复、上下文压缩和可审计 Turn Receipt。
- File/Git/Run/Bash 工具族、权限审批、checkpoint/rewind、仓库索引、Diagnostics、持久 Shell 和 Artifact。
- 统一 Provider Catalog/readiness/Doctor，覆盖 DeepSeek、OpenAI、Anthropic、Gemini、Kimi/Moonshot、
  OpenRouter、SiliconFlow、Ollama、LM Studio 与自定义 wire format。
- TUI 全屏 Change Center、权威结果卡、workspace 输入历史、附件条和稳定界面 `zh-CN`/`en-US` 文案。
- stable 有界 Goal：默认总计 3 个 Turn/1800 秒，支持硬 token budget、权限 ceiling、暂停/恢复和
  daemon 验证证据或用户显式验收。
- daemon-owned WorkerController：session-scoped start/status/peek/followup/retry/cancel/review/apply，
  可写 Worker 强制受管 Worktree，并以 verification、完整 Diff 和人工 digest 保护显式应用。
- Provider Catalog 升级备份、坏 Goal/Task/Session 单记录隔离，以及 Runtime Doctor 的迁移/隔离报告。
- MCP Tools、Skills、Memory 和基础子 Agent stable contract；Fleet、声明式 Workflow、Hooks v2、
  MCP Resources/Prompts 与 VS Code 原型作为默认关闭的 Labs。
- 50 任务 benchmark、Aider/SWE-bench 适配器、三平台发行与恢复验证脚本。
- VS Code Runtime API 客户端原型及 Extension Host smoke。
- 贡献、安全、治理、发布、兼容性和威胁模型文档。
- 混合 Task Strategy Router，以确定性风险规则和有界结构化分类冻结任务意图、作用域、风险、
  执行策略与工具可见性，并将结果写入 Ledger、Request Snapshot 和 Turn Receipt。
- Evidence-Preserving Adaptive Compaction，以来源事件固定目标、约束、失败和未决审批，去重重复读取，
  在事实覆盖校验失败时拒绝压缩。
- 受约束 DelegationPlan、Write Claim/DAG 校验和多 Worktree 批量审查应用，无法安全拆分时回退单 Agent。
- 50 任务路由、12 个冻结长会话、多 Agent 对照和五阶段强杀恢复实验入口，以及进程级真实模型费用硬门禁。

### Changed

- 无可用模型配置时直接进入 TUI，由空状态提示用户运行 `/config`，不强制首屏配置。
- 仓库 `.env` 不再自动加载；只有显式 `--env-file` 才形成禁用插值且不修改进程环境的 credential
  overlay。项目 TOML 不得选择 route、endpoint 或 credential 引用。
- Provider/authority/sandbox/tool capability 在每个 Turn 开始时冻结，运行中设置变化只影响下一 Turn；
  子 Agent 权限只能收窄。
- Anthropic Messages、OpenAI Chat 与 OpenAI Responses 统一 completion 语义；截断、incomplete、提前
  EOF、content filter、失败和取消不再误报成功。
- Windows Shell/Run 保持 Ask-only；Restricted Token + ACL 探针成功时提供 `partial` 写隔离，明确不限制读取与网络，探针失败降级为 `unavailable`。Linux/macOS 只有真实强制探针成功才启用收窄后的 sandbox profile。
- Session 持久化绑定 workspace；`coderook --continue` 恢复当前仓库最近会话，跨仓库启动时仅自动切换空闲受管 Core。
- TUI 顶栏显示当前仓库名；手动 `--no-auto-core` 模式也拒绝连接到其他 workspace。
- 受管 Core 意外退出时由 TUI 自动重新启动并恢复同一 session；界面明确显示新建、历史恢复与断线续接状态。
- Labs 默认关闭且从命令面板隐藏，只有 `CODEROOK_LABS=1` 激活；关闭时不读取 Hook 配置，也不暴露或
  恢复 Workflow/Fleet 控制面。
- GitHub 自动化收敛为一个快速 Ubuntu required CI；安全、恢复、MCP、分发与真实模型矩阵仅手动或
  release tag 运行，不配置 cron/nightly push matrix。
- 文档按用户指南、技术参考、运维发布和当前状态四类精简；删除历史计划、求职材料和重复报告。

### Fixed

- Turn 启动在返回 ID 前原子预留会话，消除并发请求同时通过 preflight 后其中一个后台静默失败的窗口。
- 权限响应必须携带匹配的 session ID；IPC 连接限流错误保留原请求 ID，不再让客户端 Future 永久等待。
- 成本路由读取同一会话的 durable 累计成本；压缩与工具蒸馏复用真实 run 事件总线并计入用量投影。
- Fleet 事件与 Worker 游标快照在同一 SQLite 事务提交；Workflow 使用持久序号计数器避免跨进程争抢 `MAX(seq)+1`。
- 文件事务增加用户级强杀恢复日志；Windows 受限进程使用显式 Unicode 环境块，避免系统缓存路径落入工作区。
- TUI 在任务、运行中纠偏、Goal 或问题回答发送失败时恢复草稿与附件；run 启动窗口不再清空提前输入的纠偏。
- TUI 事件订阅按 session 保存 durable cursor，切换与重连不再复用旧 session 的 busy、cost、审批或结果状态。
- `/export` 默认拒绝覆盖已有目标，只有 `--force --yes` 显式确认才覆盖。
- Shell/后台任务使用有界 ring buffer 与 Artifact，大量无换行输出、并发调用和取消不再依赖无界读取。
- Event/Runtime 写入失败进入可见 `audit_degraded` 并暂停修改工具；Trace 写入失败不再伪装正常。
- Worker apply 重新核对 session、base commit、完整文件集合、review digest 和干净工作区，冲突失败关闭。
- Provider 诊断失败返回非零退出码。
- Benchmark `--help` 的百分号格式化错误。
- Windows 事件回放、wheel 冷启动和 Git racy-clean 相关竞态。

[Unreleased]: https://github.com/kyletser/coderook/commits/main
