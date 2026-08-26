# CodeRook 文档

本文档是公开文档的唯一索引。代码和生成协议优先于手写说明；发布资格只由
[发布评分卡](status/RELEASE_SCORECARD.md)决定。

项目主 README 使用英文；中文入口包括[快速开始](zh-CN/README.md)和
[完整使用说明](guides/USER_GUIDE.md)。Capabilities 中的 `stable`、`labs`、`internal` 是功能稳定级别
的权威来源。Labs 默认关闭且不出现在命令面板；只有在启动进程前显式设置
`CODEROOK_LABS=1` 才启用，修改该值后必须重启 Core。

## 用户指南

- [中文快速开始](zh-CN/README.md)：源码启动、配置模型和第一个任务闭环。
- [使用说明](guides/USER_GUIDE.md)：安装、模型配置、TUI、CLI、权限和故障排查。
- [可靠长任务实验](guides/RELIABILITY_EXPERIMENTS.md)：固定路由、预算门禁、对照实验与原始报告约定。
- [升级、备份与回滚](guides/UPGRADING.md)：升级前检查、数据备份、迁移和回滚。
- [可运行示例](../examples/README.md)：只读审查、受控修改、MCP、Skill 和 Hook。

## 技术参考

- [功能架构](reference/FUNCTIONAL_ARCHITECTURE.md)：当前进程、模块、数据流和能力边界。
- [Runtime ADR](reference/ADR_RUNTIME_CONTRACT.md)：durable thread/turn 的关键设计决策。
- [Runtime API](reference/RUNTIME_API.md)：HTTP/JSON、SSE 和 Python SDK 契约。
- [Wire Protocol](reference/WIRE_PROTOCOL.md)：由 Pydantic bus 模型自动生成的 IPC 协议。
- [兼容性策略](reference/COMPATIBILITY.md)：公共接口版本和弃用规则。
- [MCP 互操作](reference/MCP_COMPATIBILITY.md)：支持的 transport、官方 SDK 证据和限制。
- [公开 Benchmark](reference/PUBLIC_BENCHMARKS.md)：内建任务、Aider 和 SWE-bench 适配器。
- [威胁模型](reference/THREAT_MODEL.md)：资产、信任边界、沙箱降级和非目标。

## 运维与发布

- [运行手册](operations/RUNBOOK.md)：Core 生命周期、配置、诊断和恢复。
- [发行说明](operations/RELEASING.md)：版本合同、构建产物、SBOM、签名和验证。
- [分支保护合同](operations/BRANCH_PROTECTION.md)：期望的 GitHub ruleset 与远端 API 验证方法。

## 当前状态

- [Roadmap](status/ROADMAP.md)：尚未完成的近期、后续和非目标事项。
- [发布评分卡](status/RELEASE_SCORECARD.md)：已验证证据、未完成门禁和已知限制。

## 维护规则

- 用户行为变化时更新 `README.md` 和 `guides/USER_GUIDE.md`。
- stable/Labs 级别、默认开关或实验功能恢复语义变化时，同步更新功能架构、兼容策略与评分卡。
- HTTP/SSE、兼容策略或安全边界变化时更新对应 reference 文档。
- 修改 `src/code_rook/core/bus/` 或协议生成器后运行
  `uv run python scripts/gen_protocol_doc.py`，不得手工编辑 `WIRE_PROTOCOL.md`。
- 已完成计划、临时续作记录、求职材料和过期审计不进入仓库；历史由 Git 保存。
- 文档不得把 workflow 文件的存在写成远端 Actions、ruleset 或 Release 已启用。
