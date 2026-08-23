# CodeRook Roadmap

Roadmap 只记录尚未完成的结果，不保存已结束阶段的实施流水账。项目当前仍处于 Alpha；精确发布结论见
[发布评分卡](RELEASE_SCORECARD.md)。

## Now：形成真实效果证据

- 使用有效凭据运行固定 50 任务候选集，公开总体、多文件、只读 pass@1、成本、耗时和失败分类。
- 对两种 wire format 各重复两次，保留四份原始报告和聚合结果，不挑选最好一次。
- 用官方 harness 产出 Aider Polyglot 固定切片和 SWE-bench 小规模判分 artifact。

## Next：完成首次公开发行

- 重新启用 GitHub Actions 后验证当前提交，而不是继续引用旧 workflow 运行结果。
- 为 `main` 启用与仓库合同一致的 ruleset/branch protection。
- 发布首个 Git tag、GitHub Release、PyPI 包和 GHCR 镜像，并验证 SBOM、checksum、provenance 与签名。
- 在两个真实发布版本之间运行升级、备份恢复和回滚测试。
- 决定是否单独发布 VS Code Marketplace 扩展。

## Later：需要平台研究的能力

- Windows 文件系统与网络的强制沙箱后端。
- 不扩大权限的按域名出站白名单。
- 跨真实 Python/TypeScript 项目的诊断 P95 基线，以及更多语言诊断。

## 非目标

- 托管式多租户 Agent SaaS 或云端密钥托管。
- 未经人工审查自动合并、自动发布模型生成的代码。
- 宣称所有模型代理、MCP server、Skill 或 Hook 都安全兼容。
