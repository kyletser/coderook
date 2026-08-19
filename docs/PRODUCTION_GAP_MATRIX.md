# CodeRook 生产就绪差距矩阵

更新日期：2026-08-18

本表区分“实现已落地”和“发布门禁已有外部实测证据”。前者不能替代真实模型、真实操作系统与安装包验收。

| 阶段 | 能力 | 实现状态 | 本仓库证据 | 尚未取得的发布证据 |
|---|---|---|---|---|
| R0 | 50 个离线任务、fixture、verifier、审计与 JSON/Markdown 报告 | 完成 | 类别下限、quick=10、suite 全覆盖和显式预算由 loader 强制校验；50 个 baseline 均失败 | 固定真实模型的首次 pass@1 基线 |
| R0 | quick/nightly/release 三档执行 | 完成 | `ci.yml`、`benchmark-nightly.yml`、`benchmark-release.yml` | secrets/vars 配置后实际运行 nightly 与两 route×两次 release |
| R1 | Windows 无真实后端时降级 ASK | 完成 | `SandboxPlan.enforced`、权限负例、`check_sandbox_boundary.py` | Windows AppContainer/WSL 方案未采用；当前结论是永久 degraded |
| R1 | SandboxBackend 与 Linux/macOS 真实边界 | 完成实现 | `probe/plan/spawn/describe` 后端契约；shell/Run/background/fleet 共用 spawn 计划；receipt 持久完整计划；真实文件、symlink、子进程和网络负例脚本；域白名单不可强制时在 spawn 前 fail closed，绝不扩大为全网 | 本机仅验证 Windows degraded；Linux/macOS CI 运行结果待远端产生；按域正向放行仍需可接受的 OS 强制后端 |
| R1 | Family resolved invocation 管线 | 完成 | backend 参数校验、hook、权限、重试、输出策略统一；相关单测 | 无 |
| R1 | 稳定 PatchPlan 与逐 hunk 审批 | 完成 | base hash、hunk id、TUI/HTTP/VS Code 选择、落盘 hash | 跨平台端到端人工交互报告 |
| R2 | ledger checksum、reconcile/repair | 完成 | checksum chain、损坏拒写、幂等 repair、runtime doctor | daemon 强杀/系统重启的 100 次进程级矩阵尚未执行 |
| R2 | 故障注入 | 候选门禁已实现 | 100 个均匀截断点；`run_crash_recovery_matrix.py` 真实阻塞模型请求、强杀、重启并重建 receipt；本机 2/2 smoke | 三平台各 100 次 workflow 报告尚未产生 |
| R2 | Durable usage/cost/router | 完成 | pricing source/date、持久 usage、receipt、cost-budget 同源读取 | 真实模型成本分位数 |
| R2 | Headless 提问等待策略 | 完成 | fail-fast/timeout/preset、interrupt/shutdown 清理 | 无 |
| R2 | ProcessSupervisor 与持久 Shell | 完成 | shell/search/git/worktree/diagnostics/hooks/MCP/fleet/background 统一监管；Windows kill-on-close Job Object；Windows Job Accounting 与 Linux `/proc` 进程组采样统一记录 wall/CPU/峰值内存/进程数/完整性，事件、runtime、TurnReceipt 和 TUI 可离线审计 | Job Object 明确不是文件系统安全边界；macOS 资源采样当前只保证 wall-time 与完整性标记 |
| R2 | Artifact list/gc/TUI | 完成 | 类型化 daemon/CLI、引用二次扫描、dry-run、receipt、`/artifacts` | 无 |
| R3 | Web 多后端与 SSRF/下载边界 | 完成 | structured→SearXNG→DuckDuckGo、fixture 与 fallback 测试 | 生产端点长期可用性基线 |
| R3 | 图片 capability 与 TUI attachment | 完成 | 内容识别、尺寸上限、ArtifactStore、发送前门禁、永久账本无 base64 | 原始剪贴板位图依赖终端；当前入口是粘贴本地图片路径 |
| R3 | Python/TypeScript 诊断 | 完成 | 并发、取消、去重、文件过滤、统一进程回收；每次耗时进入事件并由 benchmark 汇总 P95 | 编辑后 P95 跨项目基线尚未采集，20% 回归门禁无基线可比较 |
| R3 | 路由与价格证据 | 完成 | 每步候选/规则/原因/成本阈值事件；删除未接入的 brief 启发式 | 长尾模型价格持续维护 |
| R4 | 稳定 headless 契约 | 完成 | text/json/stream-json、resume、filter、partial、退出码、golden | 无 |
| R4 | Python SDK 与 IDE API | 完成 | 同步/异步 SDK、thread/turn/SSE/receipt/approval/diff | 真实 daemon + 模型 SDK 示例尚未在 CI 消费真实 key |
| R4 | MCP Streamable HTTP | 完成实现 | POST/GET SSE、session、cursor、取消、resources、prompts | 官方兼容 server 的外部认证报告 |
| R4 | VS Code 原型 | 完成 | HTTP/SSE、创建/恢复、发送、审批/逐 hunk、diff、steer/interrupt；`npm run check` | VSIX 打包和真实 daemon UI 冒烟 |
| R5 | 共享 ConfigurationService | 完成 | CLI/TUI 共用 route/credential 事务；默认 doctor 后一次原子提交 | 无 |
| R5 | 长任务单屏状态 | 完成 | Turn Inspector 同屏展示目标、活跃 worker、成本、待审批、失败原因与 sandbox backend | 真实长任务人工可用性报告 |
| R5 | 升级/降级阻断 | 完成实现 | runtime/session/routes/policy 未来 schema fixture 均阻断且保持原文件 | 跨已发布版本的干净机升级报告 |
| R5 | doctor 与脱敏诊断包 | 完成 | 环境/端口/sandbox/工具/磁盘/runtime 汇总；确认导出 ZIP | 无 |
| R5 | wheel/Docker/Windows 安装与 portable | 完成实现 | Dockerfile/compose、安装脚本、内置 Python portable 构建脚本 | Docker daemon、签名 Windows 安装包和干净机安装报告 |
| R5 | 三平台 CI | 完成配置 | Ubuntu/Windows/macOS matrix、sandbox、benchmark contract、VS Code typecheck | 当前分支远端 CI 结果 |

## 当前发布结论

仓库内可实现的主要改造已经覆盖 R0–R5，但 **CodeRook 仍不满足公开 Beta 门禁**。进程资源证据链已经完成；shell 域名级出站策略已保证无法强制时 fail closed，但按域正向放行仍没有可接受的 OS 后端，不能冒充完成。外部门禁包括真实模型 pass@1、Linux/macOS 安全矩阵、三平台各 100 次 daemon 强杀、候选安装包/容器实装和官方 MCP 兼容报告。最新结论以 `RELEASE_SCORECARD.md` 为准。
