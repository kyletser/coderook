# Main 分支保护合同

仓库内 workflow 定义“哪些检查必须成功”，GitHub ruleset 定义“谁不能绕过”。二者缺一不可。
当前代码已经提供稳定汇总 job；GitHub 网页上的 ruleset 属于仓库外状态，只有 API 查询结果才能
证明它真正启用。

## 必需检查

Ruleset 只绑定以下两个稳定名称，不直接绑定会随矩阵变化的平台 job：

| 必需检查 | 汇总范围 |
|---|---|
| `Required CI gate` | Windows、Ubuntu、macOS 的 lint、类型、测试、benchmark/sandbox 合同、协议、构建与安装 smoke |
| `Required security gate` | Gitleaks 历史扫描、PR dependency review、Python 与 JavaScript/TypeScript CodeQL |

两个汇总 job 均使用 `if: always()`；上游失败、取消或缺失时不会被“跳过即成功”掩盖。Dependabot
每周检查 uv、npm 和 GitHub Actions 依赖，但自动更新 PR 仍必须经过同一门禁。

## GitHub ruleset 配置

对默认分支 `main` 创建 active branch ruleset，并设置：

1. 所有修改必须通过 Pull Request；合并前对话必须解决；
2. 要求上表两个 status checks，且分支在合并前必须为最新；
3. 禁止 force push、删除分支和直接 push；仓库管理员也不绕过必需检查；
4. 当前只有一位维护者时批准数设为 `0`，避免形成无人能够批准的死锁；有第二位活跃维护者后改为
   `1`，同时启用 stale review dismissal；
5. `CODEOWNERS` 已声明高风险路径，但“Require review from Code Owners”同样在第二位维护者加入后启用，
   不能把自审伪装成双人复核。

配置完成后用 GitHub CLI 保存审计证据：

```bash
gh api repos/kyletser/coderook/rulesets \
  --jq '.[] | {name,enforcement,target,conditions,rules}' \
  > branch-ruleset-evidence.json
```

证据文件可能包含仓库配置细节，不直接提交；在 `docs/RELEASE_SCORECARD.md` 记录 run URL 或审核日期。
如果 ruleset 尚未启用，OS6-05 只能标记 `PARTIAL`，不能因为本文件存在就声明 main 已受保护。

## 变更规则

修改 workflow job 名、降低权限、允许失败、删除安全检查或改变 bypass 规则，必须同步本文件、
`CODEOWNERS`、公开仓库合同与 Changelog。发布 tag 另受 `docs/RELEASING.md` 的评分卡 GO 门禁约束。
