# CodeRook Roadmap

Roadmap 只记录当前代码尚未完成的结果；已有能力与精确证据缺口见
[功能架构](../reference/FUNCTIONAL_ARCHITECTURE.md)和[发布评分卡](RELEASE_SCORECARD.md)。项目仍是
`0.2.0-beta.1` 候选工作树，不能把代码路径、workflow 或测试 fixture 写成公开发行成绩。

## Now：稳定当前 Beta 候选

- 完成最终 P0/P1 清零审计，重点复核显式 credential overlay、跨 session 事件/审批、Worker handoff、
  Goal 预算、审计降级和进程树取消。
- 在每次推送前从头连续运行完整本地门禁。仓库级 GitHub Actions 当前按维护者要求关闭；准备公开预发行
  时只恢复单个 `Required Ubuntu gate` 并绑定同一候选 commit，不恢复 cron/nightly 或三平台 push matrix。
- 运行 80×24、100×30、140×40 三种尺寸与中英文产品矩阵，覆盖 onboarding、Provider、权限、结果卡、
  Change Center、rewind、session 切换、Goal、Worker 与附件；修复后重新跑完整矩阵。
- 对 Web 的 1280×720、1920×1080 和 390×844 做中英文人工验收，覆盖一次性登录、SSE 重连、
  文件/Diff、Provider Doctor、审批、恢复和同一会话在 TUI/Web 间切换。
- 对升级备份、坏 Goal/Task/Session 隔离和 `doctor runtime --repair` 做一次真实旧状态目录恢复演练；
  repair 只处理可证明安全的投影，不猜测损坏记录。

## Next：补齐外部发布证据

- 使用有效凭据运行固定 50 任务候选集，公开总体、多文件、只读 pass@1、成本、耗时和失败分类。
- 对两个不同 wire format 各重复两次，保留四份原始报告与聚合结果，不挑最好一次。
- 用官方 harness 产出 Aider Polyglot 固定切片和 SWE-bench 小规模判分 artifact。
- 手动运行同一候选 commit 的三平台安全负例、100 次强杀恢复、MCP 和五平台分发矩阵。
- 验证 PyPI Trusted Publishing、GitHub Release、GHCR、SBOM、checksum、provenance 与签名；外部
  Homebrew tap/Scoop bucket 真正发布前，继续只把生成文件称为 Release asset。
- 在两个真实 tag 之间完成升级、备份恢复与回滚验收。
- 完成 10 名新用户首次成功测试，达到至少 8 名在 10 分钟内无需指导完成有效任务。

## 首发素材

- 录制脱敏真实任务的 20 秒 GitHub GIF：理解、计划、修改、验证、结果卡、Change Center 与 rewind。
- 录制 daemon 强杀后恢复 Goal，以及 Worker worktree 审查后显式 apply 的可复现演示。
- 准备真实 Bug 修复、长 Goal 恢复和多 Agent Worktree 三个案例；benchmark 未产出前不展示占位数字。
- GitHub README 只放真实安装入口、已知限制和可复现证据；小红书明确写出 Windows 无强制 sandbox。

## Later

- Windows 文件系统与网络强制 sandbox 后端。
- 不扩大权限的按域名 Shell 出站白名单。
- 桌面端；VS Code Marketplace 是否发布单独决策，不阻塞 v1。
- 更多语言 Diagnostics 与大型 monorepo 的跨真实项目 P95 基线。
- Labs Fleet/Workflow、MCP Resources/Prompts 与 Hooks 的稳定化，仅在安全与恢复合同成熟后考虑。

## 非目标

- 托管式多租户 Agent SaaS、模型代付或云端密钥托管。
- 默认遥测。
- 未经人工审查自动 merge、自动 push 或自动发布模型生成代码。
- 宣称任意模型、MCP server、Skill 或 Hook 天然安全兼容。
