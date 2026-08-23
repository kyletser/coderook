# CodeRook 发布评分卡

**更新时间**：2026-08-21

**代码基线**：`main@1c7810a21a6afd58368d92240039e85ebe07fef4`（加本文档审计工作树）
候选状态：**NO-GO（真实模型效果、公开 benchmark 和首次公开发行尚未完成）**

本页区分三类事实：当前代码、绑定旧 commit 的历史证据、仍未完成的外部门禁。workflow 文件存在不等于
GitHub 当前启用了 Actions 或分支保护。

## 当前代码与仓库状态

- 包版本为 `0.1.0`，PyPI 元数据标记为 Alpha。
- 远端 `main` 指向本页代码基线；GitHub Actions 当前为 `enabled=false`。
- 远端没有 ruleset/branch protection、Git tag 或 GitHub Release。
- 基线提交曾通过完整本地门禁。本次文档审计工作树重新通过 Ruff、品牌与公开仓库检查、协议同步，
  以及 `1178 passed, 2 skipped`；测试数减少来自删除两项只维护求职/复盘文档的合同测试。
- 50 任务 fixture、verifier、quick/nightly/release 契约和 baseline 失败校验已经实现；这只证明
  benchmark 合同有效，不证明模型效果。
- Aider Polyglot 和 SWE-bench 适配器已经实现，但没有官方 harness 的真实判分 artifact。

## 历史远端证据

下列结果只适用于各自绑定的 commit，不自动证明当前 `main`：

- commit `fe3bd3b` 的三次 CI（`32368432365`、`32368834608`、`32369245347`）在
  Ubuntu、Windows、macOS 全绿。
- commit `a4e4fea` 的 Crash Recovery `32376310972` 在三平台各完成 100/100，合计
  300/300，孤儿工具调用为 0。
- commit `a4e4fea` 的 Distribution `32376314295` 完成三平台 wheel、Docker、
  Windows portable 和 VS Code Extension Host smoke。
- checked-in MCP 报告绑定 commit `c47ae23` 与官方 Python SDK 2.0.0，验证 stdio、
  legacy SSE、Streamable HTTP 的 tools/resources/prompts/cancellation/reconnect。

## 发布门禁

| 门禁 | 要求 | 当前结果 |
|---|---:|---|
| 总体 pass@1 | ≥80% | 未运行真实模型候选集 |
| 多文件任务 pass@1 | ≥75% | 未运行 |
| 只读任务 pass@1 | ≥90% | 未运行 |
| 两种 wire format × 两次 | 四份原始报告及聚合报告 | 未产生 |
| Aider/SWE-bench | 官方 harness 判分 artifact | 未产生 |
| 当前提交自动化验证 | Actions 对当前提交运行并通过 | Actions 已关闭 |
| 主分支治理 | active ruleset/branch protection | 未启用 |
| 公开发行 | tag、GitHub Release、PyPI、GHCR | 均未发布 |
| 供应链证据 | Release 上的 SBOM、checksum、provenance、签名 | 流程已实现，真实产物未产生 |
| 跨发布升级 | 两个真实发布 tag 之间升级、备份恢复和回滚 | 未运行 |

## 已知限制

- Windows 没有文件系统或网络强制 sandbox；当前明确降级为审批链和工作区边界。
- 域名出站白名单没有可接受的 OS 强制后端，无法强制时 fail closed。
- TUI 图片入口接收本地图片路径，不保证所有终端都能读取剪贴板原始位图。
- Python/TypeScript 诊断没有跨真实项目的 P95 基线。
- macOS 进程资源报告只保证 wall-time 与采样完整性标记。
- VS Code 扩展没有发布到 Marketplace。

未满足全部门禁前，不发布 Beta，也不宣称 production-ready。
