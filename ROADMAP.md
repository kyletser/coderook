# CodeRook Roadmap

Roadmap 按可验证用户结果排序，不承诺日期。单个工作项的精确状态与证据以
[开源补全计划](docs/status/OPEN_SOURCE_COMPLETION_PLAN.md)和
[发布评分卡](docs/status/RELEASE_SCORECARD.md)为准；Issue 或外部贡献不会自动改变发布结论。

## Now：公开 Beta 证据

- 固定模型/route 运行 50 任务与公开 benchmark 适配器，公布 pass@1、成本、耗时和失败聚类；
- 在已有三平台安全、强杀恢复和候选升级/回滚证据上，补真实模型与跨已发布 tag 的升级报告；
- 启用 main ruleset，完成首次可验证的 GitHub Release、GHCR 镜像、SBOM 与 attestation；
- 只有评分卡全部达到阈值后才发布 `0.2.0-beta`。

## Next：Beta 可用性

- 基于真实失败报告收敛 repo map、working set、诊断闭环与长会话压缩质量；
- 为扩展作者稳定 MCP/Skill/Hook/SDK 示例和兼容迁移路径；
- 改进 TUI 可访问性、恢复引导和大任务证据浏览，不把 CLI 扩张成第二套产品面；
- 为公开 benchmark 建立候选与基线的持续回归报告。

## Later：明确依赖研究的能力

- Windows 文件系统/网络的真实 OS sandbox 后端；
- 不扩大权限的按 DNS 域出站白名单强制后端；
- Go/Rust 诊断、更多 IDE 客户端和更完整的多仓库任务；
- 在社区规模足够时，把单维护者治理迁移为分组件维护者团队。

## 当前非目标

- 托管式多租户 Agent SaaS、云端密钥托管和远程执行平台；
- 未经人工审查自动合并或自动发布模型生成代码；
- 宣称所有 MCP server、模型代理或第三方 Skill/Hook 都安全兼容；
- 为追逐框架名词重写已工作的双进程核心。

想贡献一个边界明确的小改动，请从
[Contributor Tasks](docs/operations/CONTRIBUTOR_TASKS.md)选择 `READY` 项并按模板认领；大型方向先开 Feature Request。
