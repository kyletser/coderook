# CodeRook 文档索引

本文档说明哪些文件代表当前事实，避免历史路线图中的旧结论被误当成当前能力。

## 目录结构

| 目录 | 内容 | 是否代表当前事实 |
|---|---|---|
| `guides/` | 面向使用者的操作、配置和升级指南 | 是 |
| `reference/` | 架构、API、兼容、安全与 benchmark 合同 | 是 |
| `operations/` | 发行、维护、分支保护和贡献任务 | 是 |
| `status/` | 当前补全计划、差距矩阵、评分卡和续作报告 | 是，以评分卡为发布结论 |
| `career/` | 项目案例、简历证据和面试讲解 | 只引用已验证事实 |
| `plans/` | 专题改造计划与复盘 | 仅作执行参考 |
| `archive/` | 已过期的对标、规格和完成度审计 | 否 |
| `postmortems/` | 带日期的工程故障与重构复盘 | 代表对应日期的事实 |
| `evidence/` | 可提交的脱敏机器证据 | 以报告绑定的 commit 为准 |
| `images/` | README 和文档使用的可复现图片 | 仅作界面展示 |

## 权威文档

| 文档 | 用途 | 更新要求 |
|---|---|---|
| [README](../README.md) | 项目定位、快速开始、公开能力与限制 | 用户可见行为变化时更新 |
| [用户指南](guides/USER_GUIDE.md) | TUI 主产品面的完整使用说明 | 命令、快捷键、配置变化时更新 |
| [功能架构](reference/FUNCTIONAL_ARCHITECTURE.md) | 当前代码架构、数据流和诚实技术债 | 子系统或契约变化时更新 |
| [Runtime ADR](reference/ADR_RUNTIME_CONTRACT.md) | durable thread/turn 与 API 设计决策 | 契约决策变化时更新 |
| [Wire Protocol](../WIRE_PROTOCOL.md) | 由 bus 模型生成的 IPC 契约 | 修改 bus 后自动生成 |
| [Runtime API](reference/RUNTIME_API.md) | HTTP/SSE 外部接口 | endpoint/schema 变化时更新 |
| [兼容与弃用策略](reference/COMPATIBILITY.md) | HTTP/SSE、SDK、stream-json 的稳定边界和迁移窗口 | 公共契约或支持窗口变化时更新 |
| [MCP 互操作](reference/MCP_COMPATIBILITY.md) | stdio、legacy SSE、Streamable HTTP、官方 SDK 矩阵与安全边界 | MCP SDK pin 或 transport 变化时更新 |
| [运行手册](../RUNBOOK.md) | 安装、运行、排障与恢复 | 运维行为变化时更新 |
| [升级与回滚](guides/UPGRADING.md) | 数据备份、版本升级、校验与回滚 | 配置或持久化兼容性变化时更新 |
| [发行与供应链](operations/RELEASING.md) | tag、版本一致性、SBOM、OIDC provenance、Cosign 与下载验证 | 发行流程或产物变化时更新 |
| [分支保护](operations/BRANCH_PROTECTION.md) | 稳定必需检查、ruleset 配置与外部审计边界 | CI job 或 GitHub 规则变化时更新 |
| [维护者边界](operations/MAINTAINERS.md) | 组件责任、权限、评审与总线因子风险 | 维护者或权限变化时更新 |
| [Contributor Tasks](operations/CONTRIBUTOR_TASKS.md) | 可认领小任务、范围和完成定义 | 任务被认领、完成或替换时更新 |
| [项目案例](career/PROJECT_CASE_STUDY.md) | 一页问题、架构、职责、取舍、结果与当前边界 | 架构或关键证据变化时更新 |
| [简历证据](career/RESUME_EVIDENCE.md) | 可用数字、历史 checkpoint、禁止宣称与第三方边界 | 新完整 gate/benchmark/发行后更新 |
| [面试讲解](career/INTERVIEW_GUIDE.md) | 3/10 分钟讲解、演示路径与追问证据 | 主要能力或失败案例变化时更新 |
| [工程复盘](postmortems/README.md) | 失败与优化的根因、证据和未解决项 | 发生重要失败或完成结构优化后更新 |
| [威胁模型](reference/THREAT_MODEL.md) | 资产、信任边界、攻击面、降级与非目标 | 安全边界或外部能力变化时更新 |
| [公开 Benchmark](reference/PUBLIC_BENCHMARKS.md) | 内建集、Aider Polyglot、SWE-bench 与回归比较复现协议 | 数据集、格式或门禁变化时更新 |
| [开源补全计划](status/OPEN_SOURCE_COMPLETION_PLAN.md) | 当前长期 Goal、阶段状态和完成定义 | 每个工作项验收后更新 |
| [生产差距矩阵](status/PRODUCTION_GAP_MATRIX.md) | R0-R5 仓库证据与外部证据 | 证据变化时更新 |
| [发布评分卡](status/RELEASE_SCORECARD.md) | 唯一 GO/NO-GO 发布结论 | 每次候选发布更新 |
| [续作报告](status/CONTINUATION_REPORT.md) | 当前未完成项、外部阻塞、明日顺序与目录规范 | 每次阶段性收尾时更新 |

发生冲突时，运行代码与生成协议优先于手写描述；发布资格以
[发布评分卡](status/RELEASE_SCORECARD.md)为准。

## 当前专题文档

- [公开体验对齐矩阵](plans/CLAUDE_CODE_EXPERIENCE_PARITY.md)
- [生产就绪改造计划](plans/PRODUCTION_READINESS_PLAN.md)
- [优化路线图与实施复盘](plans/OPTIMIZATION_ROADMAP.md)
- [Spec-driven 重构计划](plans/SPECDRIVEN_REFACTOR_PLAN.md)
- [TUI 重构计划](plans/TUI_REFACTOR_PLAN.md)
- [PC/Desktop 迁移计划](plans/PC_DESKTOP_MIGRATION_PLAN.md)

专题计划只能说明其自身范围；其中状态与权威文档冲突时视为过期。

## 历史快照

以下文档保留设计过程和旧基线，不用于判断当前完成度：

- [`IMPLEMENTATION_PROGRESS.md`](archive/IMPLEMENTATION_PROGRESS.md)
- [`LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md`](archive/LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md)
- [`SPECDRIVEN_COMPLETION_AUDIT.md`](archive/SPECDRIVEN_COMPLETION_AUDIT.md)
- [`SPECDRIVEN_SPEC.md`](archive/SPECDRIVEN_SPEC.md)
- [`CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md`](archive/CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md)
- [`CODEROOK_VS_MAINSTREAM_AGENT_ARCH.md`](archive/CODEROOK_VS_MAINSTREAM_AGENT_ARCH.md)
- [`LEARN_CLAUDE_CODE_PORT.md`](archive/LEARN_CLAUDE_CODE_PORT.md)

历史快照不应继续更新测试计数、发布状态或当前缺口；需要当前结论时链接到功能架构、差距矩阵或
发布评分卡。
