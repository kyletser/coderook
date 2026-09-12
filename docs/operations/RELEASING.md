# CodeRook 发行说明

CodeRook 已于 2026-09-12 手工发布
[`v0.2.0-beta.1`](https://github.com/kyletser/coderook/releases/tag/v0.2.0-beta.1) GitHub
预发布。它提供 wheel、sdist、checksum、发行清单和发行契约；尚未发布 PyPI、GHCR、自包含 archive、
Homebrew tap 或 Scoop bucket。当前发布评分卡仍是 `NO-GO`，仅表示不能发布稳定 `v1.0.0`。

后续完整公开发行计划由 tag 驱动，但 tag 不能绕过质量证据。`release.yml` 在构建公开 Release 前使用
`--require-channel-readiness` 校验发布通道；预发行要求评分卡明确写出 `GO` 或 `NO-GO`，稳定 tag 必须是
`GO`。仓库级 GitHub Actions 当前关闭，因此首个 Beta 没有冒充自动化供应链发行。

## 版本顺序

当前公开序列为：

```text
v0.2.0-beta.1 → 后续 Beta 修复 → v1.0.0-rc.1 → 全部门禁 → v1.0.0
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

仓库级 Actions 重新启用后，`.github/workflows/release.yml` 由 release tag 触发，并先调用手动/可复用的
`distribution.yml`。成功路径准备执行：

- 三平台 wheel smoke；
- Docker 零凭据 Core/TUI smoke；
- 自包含 Windows x64、Linux x64/arm64、macOS x64/arm64 archive 构建与 smoke；
- PyPI Trusted Publishing；
- GHCR 镜像、SPDX SBOM、provenance 与 keyless 签名；
- GitHub Release 的 wheel、sdist、portable archive、checksum、manifest、SBOM 与签名资产。

VS Code job 只在维护者手动选择 `target=vscode` 时运行，不参与 workflow call 或稳定发布，不生成
Marketplace 承诺。

上述完整自动化路径仍只是代码中的发行流程。首个 Beta 是已发布、可下载安装的 GitHub Release，但不是
该 workflow 已通过的证据；第一次真实 workflow 通过前，PyPI、GHCR、portable、SBOM、provenance 与
签名仍必须写成“prepared”而不是“published”。

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

用户安装脚本 `scripts/install.sh` 和 `scripts/install-release.ps1` 面向包含 portable archive 的完整
GitHub Release：它们下载版本化 archive 并使用 `SHA256SUMS` 校验。首个 Beta 没有 portable archive，
因此应直接安装其 wheel，不能使用这两个脚本。

## PyPI、Homebrew 与 Scoop

- PyPI job 使用 OIDC Trusted Publishing，不使用长期 PyPI token；真实 PyPI project/environment 仍需
  仓库外配置并通过首次发布验证。
- `scripts/generate_package_manifests.py` 从 Release archive 的 SHA-256 生成 Homebrew formula 与 Scoop
  manifest，二者作为 GitHub Release asset 上传。
- 当前仓库没有发布外部 Homebrew tap 或 Scoop bucket，也没有自动把 manifest 推送到这些独立仓库。

因此在外部仓库真正建立并验收前，公开文档不得给出 `brew install coderook` 或
`scoop install coderook` 作为可用命令。

## Release 资产与供应链证据

`v0.2.0-beta.1` 当前实际包含：

- `coderook-0.2.0b1-py3-none-any.whl`；
- `coderook-0.2.0b1.tar.gz`；
- `SHA256SUMS`、`release-manifest.json` 和 `release-contract.json`。

这些资产在本地完整门禁后生成并上传，公开 wheel 也完成了下载与 `coderook --version` 验证；它们没有
workflow provenance、SPDX SBOM 或 Sigstore 签名，不能声称具备下面的完整自动化供应链证明。

release job 准备为可下载包生成 SPDX JSON SBOM，生成 `release-contract.json`、
`release-manifest.json` 和 `SHA256SUMS`，使用 GitHub OIDC/`actions/attest` 生成 provenance，并以
Cosign keyless bundle 签名 checksum。容器以不可变 digest 记录和签名。

这些证明 artifact 与 workflow/commit 的来源关系，不证明没有漏洞，也不替代 sandbox、恢复、真实模型
和用户体验门禁。

下载当前 Beta 的全部文件后，基础校验方式为：

```bash
sha256sum --check SHA256SUMS
```

未来完整 workflow 生成 `SHA256SUMS.sigstore.json` 后才能继续执行：

```bash
cosign verify-blob SHA256SUMS \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp \
    'https://github.com/kyletser/coderook/.github/workflows/release.yml@refs/tags/.*' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

只有远端 workflow 为评分卡绑定 commit 产生且验证通过的资产，才能把完整供应链门禁改为通过。
