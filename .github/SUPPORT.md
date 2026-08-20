# CodeRook 支持说明

CodeRook 目前是公开 Beta 前的社区项目，不提供商业支持或响应 SLA。

## 获取帮助

- 使用问题：先阅读 [README.md](../README.md)、[用户指南](../docs/guides/USER_GUIDE.md) 和
  [运行手册](../docs/operations/RUNBOOK.md)。
- 环境诊断：运行 `coderook doctor all --json`；分享前删除路径、账号、Prompt、密钥和业务数据。
- 可复现缺陷：使用 [Bug Report](https://github.com/kyletser/coderook/issues/new?template=bug_report.yml)。
- 功能建议：使用 [Feature Request](https://github.com/kyletser/coderook/issues/new?template=feature_request.yml)。
- 安全问题：不要开普通 Issue，遵循 [SECURITY.md](SECURITY.md)。
- 想贡献代码：从 [Contributor Tasks](../docs/operations/CONTRIBUTOR_TASKS.md) 选择边界明确的 `READY` 项；普通
  使用问题不要用 Contributor task 模板。

## 支持边界

- 支持 Python 3.12；其他 Python 版本不在当前兼容矩阵内。
- 主要支持 Windows、Ubuntu 和 macOS 最新 GitHub-hosted runner 对应环境。
- 第三方模型端点、代理、MCP server、Skill 和 Hook 的可用性由其提供方负责。
- 本地进程模式、full-access 权限和挂载宿主目录会扩大风险，用户需自行评估。
- 未复现的问题、包含秘密的日志和无法说明版本/平台的报告可能无法处理。

项目不承诺响应 SLA，维护责任与权限边界见[维护者说明](../docs/operations/MAINTAINERS.md)。
