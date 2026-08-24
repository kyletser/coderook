# Main 分支保护合同

仓库内 workflow 定义“哪些检查必须成功”，GitHub ruleset 定义“谁不能绕过”。二者缺一不可。
当前代码提供一个稳定的快速 required job；GitHub 网页上的 ruleset 属于仓库外状态，只有 API 查询结果才能
证明它真正启用。

## 必需检查

Ruleset 只绑定以下稳定名称：

| 必需检查 | 范围 |
|---|---|
| `Required Ubuntu gate` | Ruff、品牌/公开仓库合同、Mypy、快速单测、关键 daemon 集成 smoke、协议检查、wheel 构建与安装 smoke |

Windows/macOS、Security、沙箱、100 次恢复、真实模型、MCP 与分发矩阵不绑定日常分支保护，
只由 `workflow_dispatch` 或 release tag 触发并作为手动发布证据。这样日常 PR 只有一个快速、可复现的
必需门禁，重型环境问题不会持续制造 push 邮件。

Dependabot 每月分别将 uv、npm 和 GitHub Actions 更新合并为每个生态一个 PR，且每个生态最多保留一个
版本更新 PR。CI 的 push 触发仅限 `main`，避免同一 Dependabot 更新同时以分支 push 和 PR 运行两遍。

## GitHub ruleset 配置

对默认分支 `main` 创建 active branch ruleset，并设置：

1. 所有修改必须通过 Pull Request；合并前对话必须解决；
2. 要求上表 status check，且分支在合并前必须为最新；
3. 禁止 force push、删除分支和直接 push；仓库管理员也不绕过必需检查；
4. 当前只有一位维护者时批准数设为 `0`，避免形成无人能够批准的死锁；有第二位活跃维护者后改为
   `1`，同时启用 stale review dismissal；
5. `CODEOWNERS` 已声明高风险路径，但“Require review from Code Owners”同样在第二位维护者加入后启用，
   不能把自审伪装成双人复核。

配置完成后运行统一远端证据审计器；它同时检查 active ruleset、稳定必需检查、连续三次 CI，
以及 Security、Distribution、Crash Recovery、MCP 和 release benchmark 的最新成功记录：

```bash
uv run python scripts/audit_github_release_evidence.py \
  --repo kyletser/coderook \
  --output reports/github-release-evidence.json
```

`remote-evidence.yml` 定义了使用只读 `GITHUB_TOKEN` 手动执行同一审计并上传 JSON；任一 API
不可见、workflow 缺失或结论失败都 fail closed。Actions 是否启用属于仓库外状态，必须以本次 API
结果证明。证据文件不直接提交；在 `docs/status/RELEASE_SCORECARD.md` 记录 artifact/run URL。
不能因为本文件或 workflow 存在就声明 `main` 已受保护。

## 变更规则

修改 workflow job 名、降低权限、允许失败、删除安全检查或改变 bypass 规则，必须同步本文件、
`CODEOWNERS`、公开仓库合同与 Changelog。发布 tag 另受 `docs/operations/RELEASING.md` 的评分卡 GO 门禁约束。
