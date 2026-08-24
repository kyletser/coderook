# CodeRook 发行说明

CodeRook 的公开发行由 tag 驱动，但 tag 不能绕过质量证据。当前发布评分卡是 `NO-GO`；
`release.yml` 在构建公开 Release 前使用 `--require-channel-readiness` 校验发布通道。预发行要求评分卡
明确写出 `GO` 或 `NO-GO`，稳定 tag 必须是 `GO`。当前尚无可供用户下载的 PyPI、GitHub Release、
GHCR、Homebrew tap 或 Scoop bucket。

## 版本顺序

计划中的首次公开序列固定为：

```text
v0.9.0-beta.1 → 真实用户修复 → v1.0.0-rc.1 → 全部门禁 → v1.0.0
```

稳定 tag 使用 `vMAJOR.MINOR.PATCH`；预发行使用 `-alpha.N`、`-beta.N` 或 `-rc.N`。Python 包使用
对应 PEP 440 版本，例如 `v1.0.0-rc.1` 对应 `1.0.0rc1`。`pyproject.toml`、
`code_rook.__version__`、实验性 VS Code `package.json` 和 Changelog 仍由发行合同统一校验；VS Code
版本同步不表示 VSIX 会进入 v1 Release。

## 发版前硬门禁

1. 当前候选 commit 的本地完整门禁和 required Ubuntu CI 通过；
2. 无已知 P0/P1，安全负例 100% 通过；
3. 内建 50 任务真实模型达到评分卡阈值，两种 wire format 各重复两次；
4. Aider Polyglot 固定切片和 SWE-bench 小规模官方 harness artifact 公开；
5. 当前 commit 的三平台强杀、sandbox、安全与自包含安装矩阵达标；
6. 10 名新用户中至少 8 名无需指导在 10 分钟内完成首次有效任务；
7. 评分卡逐项链接到 commit-bound 报告，并明确改为 `GO`；
8. 包版本、Changelog、协议文档和 tag 完全一致。

结构检查可以在创建 tag 前运行：

```bash
uv run python scripts/check_release_contract.py --tag v1.0.0-rc.1
```

真实 tag workflow 额外传入 `--require-channel-readiness`。不要为了让脚本通过而只修改评分卡文字；
预发行允许 `NO-GO` 是为了收集修复反馈，不会降低稳定 `v1.0.0` 的 GO 门禁。

## 当前 release workflow

`.github/workflows/release.yml` 由 release tag 触发，并先调用手动/可复用的
`distribution.yml`。成功路径准备执行：

- 三平台 wheel smoke；
- Docker 零凭据 Core/TUI smoke；
- 自包含 Windows x64、Linux x64/arm64、macOS x64/arm64 archive 构建与 smoke；
- PyPI Trusted Publishing；
- GHCR 镜像、SPDX SBOM、provenance 与 keyless 签名；
- GitHub Release 的 wheel、sdist、portable archive、checksum、manifest、SBOM 与签名资产。

VS Code job 只在维护者手动选择 `target=vscode` 时运行，不参与 workflow call 或稳定发布，不生成
Marketplace 承诺。

上述是代码中的发行流程，不是已经产生的外部证据。第一次真实 workflow 通过前，必须继续写
“prepared”而不是“published”或“supported install”。

## 自包含包

候选包由受控 CPython 3.12 runtime、CodeRook wheel 和平台启动器组成：

| Target | Release archive |
|---|---|
| Windows x64 | `coderook-windows-x86_64.zip` |
| Linux x64 | `coderook-linux-x86_64.tar.gz` |
| Linux arm64 | `coderook-linux-arm64.tar.gz` |
| macOS x64 | `coderook-macos-x86_64.tar.gz` |
| macOS arm64 | `coderook-macos-arm64.tar.gz` |

维护者本地构建示例：

```bash
uv run python scripts/build_portable.py --target linux-x86_64
```

portable 不是交叉编译器：target 必须匹配当前 host 的 OS 与 CPU，构建器会在接触输出目录前拒绝不匹配
或未知宿主。五个平台包应在对应 runner 上分别构建，并核对包内 CPython 架构。

用户安装脚本 `scripts/install.sh` 和 `scripts/install-release.ps1` 面向未来 GitHub Release：它们下载
版本化 archive 并使用 `SHA256SUMS` 校验。Release 未创建时这些命令不构成可用安装渠道。

## PyPI、Homebrew 与 Scoop

- PyPI job 使用 OIDC Trusted Publishing，不使用长期 PyPI token；真实 PyPI project/environment 仍需
  仓库外配置并通过首次发布验证。
- `scripts/generate_package_manifests.py` 从 Release archive 的 SHA-256 生成 Homebrew formula 与 Scoop
  manifest，二者作为 GitHub Release asset 上传。
- 当前仓库没有发布外部 Homebrew tap 或 Scoop bucket，也没有自动把 manifest 推送到这些独立仓库。

因此在外部仓库真正建立并验收前，公开文档不得给出 `brew install coderook` 或
`scoop install coderook` 作为可用命令。

## Release 资产与供应链证据

release job 准备为可下载包生成 SPDX JSON SBOM，生成 `release-contract.json`、
`release-manifest.json` 和 `SHA256SUMS`，使用 GitHub OIDC/`actions/attest` 生成 provenance，并以
Cosign keyless bundle 签名 checksum。容器以不可变 digest 记录和签名。

这些证明 artifact 与 workflow/commit 的来源关系，不证明没有漏洞，也不替代 sandbox、恢复、真实模型
和用户体验门禁。

真实 Release 产生后，下载目录中的基础校验方式为：

```bash
sha256sum --check SHA256SUMS

cosign verify-blob SHA256SUMS \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp \
    'https://github.com/kyletser/coderook/.github/workflows/release.yml@refs/tags/.*' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

只有远端 workflow 为评分卡绑定 commit 产生且验证通过的资产，才能把对应门禁改为通过。
