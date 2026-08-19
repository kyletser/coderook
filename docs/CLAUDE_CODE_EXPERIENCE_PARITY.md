# CodeRook 与主流 Coding Agent 公开体验对齐矩阵

更新时间：2026-08-18

## 使用边界

本矩阵只对照公开产品行为与 CodeRook 当前代码、测试和可复现报告，不推断闭源实现。它是体验
定位资料，不是发布证明；公开 Beta 是否可发布以 `RELEASE_SCORECARD.md` 为准。旧版以功能数量
给出的 81/100 已撤销，因为它没有使用固定真实任务语料，也不能代表端到端成功率。

## 当前结果

| 体验域 | 已实现 | 仍需发布证据或后续增强 |
|---|---|---|
| 核心编码循环 | 多步 Plan-Act-Observe、steer/interrupt、并行工具批、max-step 续段、路由与成本证据 | 固定真实模型任务集的 pass@1 与失败分类 |
| 安全编辑与恢复 | 工作区边界、PatchPlan/base hash/hunk id、TUI/HTTP/VS Code 逐 hunk、checkpoint/rewind | Linux/macOS 真实沙箱负向报告；Windows 仍是明确降级而非 OS 隔离 |
| 会话与自动化 | durable session/thread/turn、resume/fork/export、text/json/stream-json headless、问题策略 | daemon kill/restart 矩阵与真实 provider 恢复报告 |
| Context 与成本 | 自动/手动 compact、artifact 溢出、provider usage、缓存与 USD 成本、成本路由 | 长上下文真实模型质量与长尾模型价格维护 |
| TUI 与产物 | 流式 Markdown、审批、任务/diff/context、MCP/Hooks/Memory/Jobs/Artifacts 管理、本地图片路径落 ArtifactStore | 终端原始位图剪贴板不是跨终端保证；诊断 P95 尚无跨平台报告 |
| 子 Agent 与进程 | subagent/fleet/worktree、daemon 级 ProcessSupervisor、常驻 shell、后台交互与取消 | 100 次真实进程级故障注入和跨平台孤儿进程证据 |
| 扩展与外部接口 | MCP stdio/TCP/Streamable HTTP、resources/prompts、Web 多后端、HTTP/SSE、Python SDK、VS Code 原型 | 官方 MCP server 兼容报告、VSIX 打包与真实 UI 冒烟 |
| 配置与分发 | doctor-before-commit、统一 doctor/脱敏诊断包、Docker 定义、Windows 安装与 portable 构建脚本 | 镜像、portable zip、wheel 在干净机器上的安装/升级/回滚证明 |

## 体验结论

仓库内 R0–R5 功能面已经覆盖“可用 Coding Agent”的主要交互链路，但这不等于生产发布合格。
在真实模型 benchmark、三平台安全与分发、MCP 兼容、进程级恢复证据齐备前，结论保持
**功能实现完成、公开 Beta NO-GO**。

相关证据：

- `PRODUCTION_GAP_MATRIX.md`：代码实现与外部证据的逐项拆分
- `PRODUCTION_READINESS_PLAN.md`：R0–R5 改造合同和门禁
- `RELEASE_SCORECARD.md`：当前发布判定
- `benchmarks/` 与 `.github/workflows/benchmark-*.yml`：固定任务与持续取证入口
