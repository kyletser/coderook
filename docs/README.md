# CodeRook 文档索引

本文档说明哪些文件代表当前事实，避免历史路线图中的旧结论被误当成当前能力。

## 权威文档

| 文档 | 用途 | 更新要求 |
|---|---|---|
| [README](../README.md) | 项目定位、快速开始、公开能力与限制 | 用户可见行为变化时更新 |
| [用户指南](USER_GUIDE.md) | TUI 主产品面的完整使用说明 | 命令、快捷键、配置变化时更新 |
| [功能架构](FUNCTIONAL_ARCHITECTURE.md) | 当前代码架构、数据流和诚实技术债 | 子系统或契约变化时更新 |
| [Runtime ADR](ADR_RUNTIME_CONTRACT.md) | durable thread/turn 与 API 设计决策 | 契约决策变化时更新 |
| [Wire Protocol](../WIRE_PROTOCOL.md) | 由 bus 模型生成的 IPC 契约 | 修改 bus 后自动生成 |
| [Runtime API](RUNTIME_API.md) | HTTP/SSE 外部接口 | endpoint/schema 变化时更新 |
| [兼容与弃用策略](COMPATIBILITY.md) | HTTP/SSE、SDK、stream-json 的稳定边界和迁移窗口 | 公共契约或支持窗口变化时更新 |
| [运行手册](../RUNBOOK.md) | 安装、运行、排障与恢复 | 运维行为变化时更新 |
| [升级与回滚](UPGRADING.md) | 数据备份、版本升级、校验与回滚 | 配置或持久化兼容性变化时更新 |
| [威胁模型](THREAT_MODEL.md) | 资产、信任边界、攻击面、降级与非目标 | 安全边界或外部能力变化时更新 |
| [公开 Benchmark](PUBLIC_BENCHMARKS.md) | 内建集、Aider Polyglot、SWE-bench 与回归比较复现协议 | 数据集、格式或门禁变化时更新 |
| [开源补全计划](OPEN_SOURCE_COMPLETION_PLAN.md) | 当前长期 Goal、阶段状态和完成定义 | 每个工作项验收后更新 |
| [生产差距矩阵](PRODUCTION_GAP_MATRIX.md) | R0-R5 仓库证据与外部证据 | 证据变化时更新 |
| [发布评分卡](RELEASE_SCORECARD.md) | 唯一 GO/NO-GO 发布结论 | 每次候选发布更新 |

发生冲突时，运行代码与生成协议优先于手写描述；发布资格以 `RELEASE_SCORECARD.md` 为准。

## 当前专题文档

- [公开体验对齐矩阵](CLAUDE_CODE_EXPERIENCE_PARITY.md)
- [生产就绪改造计划](PRODUCTION_READINESS_PLAN.md)
- [优化路线图与实施复盘](OPTIMIZATION_ROADMAP.md)
- [Spec-driven 重构计划](SPECDRIVEN_REFACTOR_PLAN.md)
- [TUI 重构计划](TUI_REFACTOR_PLAN.md)
- [PC/Desktop 迁移计划](PC_DESKTOP_MIGRATION_PLAN.md)

专题计划只能说明其自身范围；其中状态与权威文档冲突时视为过期。

## 历史快照

以下文档保留设计过程和旧基线，不用于判断当前完成度：

- `IMPLEMENTATION_PROGRESS.md`
- `LIGHTWEIGHT_AGENT_COMPLETION_AUDIT.md`
- `SPECDRIVEN_COMPLETION_AUDIT.md`
- `CODEROOK_VS_CLAUDE_CODE_GAP_ANALYSIS.md`
- `CODEROOK_VS_MAINSTREAM_AGENT_ARCH.md`
- `LEARN_CLAUDE_CODE_PORT.md`

历史快照不应继续更新测试计数、发布状态或当前缺口；需要当前结论时链接到功能架构、差距矩阵或
发布评分卡。
