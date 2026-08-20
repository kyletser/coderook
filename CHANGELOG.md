# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 的结构，并计划从公开 Beta 起遵循
[Semantic Versioning](https://semver.org/)。Beta 前的接口仍可能变化。

## [Unreleased]

### Added

- 开源级补全计划、贡献指南、安全策略、行为准则、支持与治理说明。
- GitHub Issue/PR 模板与公开仓库一致性门禁。
- Tag 驱动的版本合同、跨平台发行门禁、SPDX SBOM、checksums、OIDC provenance 与 Cosign keyless 签名流程。
- 稳定 CI/Security 汇总门禁、CODEOWNERS、分支保护合同、Roadmap、维护者边界与可认领 Contributor Tasks。

### Changed

- TUI 在没有 LLM 配置时直接进入界面并给出配置指引，不再强制弹出向导。
- 生产就绪候选流水线扩展到三平台、VSIX、容器和 Windows portable 产物。

## [0.1.0] - 2026-08-19

### Added

- daemon/client 双进程 Coding Agent runtime、TUI/CLI、HTTP/SSE 与 Python SDK。
- 类型化协议、工具权限、安全编辑、checkpoint/rewind、durable session/turn 与 context compaction。
- subagent/fleet/worktree/workflow、多 provider route、MCP/Skills/Hooks 和可审计 receipt。
- 50 任务 benchmark harness 与完整本地质量门禁。

[Unreleased]: https://github.com/kyletser/coderook/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/kyletser/coderook/releases/tag/v0.1.0
