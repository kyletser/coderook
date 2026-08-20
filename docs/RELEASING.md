# 发行、SBOM、签名与验证

CodeRook 的公开发行由 tag 驱动，但 tag 不是绕过质量门禁的快捷方式。仓库当前评分卡仍是
`NO-GO`；在 `docs/RELEASE_SCORECARD.md` 变为 `GO` 之前，`release.yml` 会在创建任何 Release
或推送 GHCR 镜像前失败。

## 版本规则

稳定版 tag 使用 `vMAJOR.MINOR.PATCH`。预发行使用严格 SemVer：
`vMAJOR.MINOR.PATCH-alpha.N`、`-beta.N` 或 `-rc.N`。不同生态的声明必须同时更新：

| 位置 | 稳定版示例 | Beta 示例 |
|---|---|---|
| Git tag / Changelog / VSIX | `0.2.0` | `0.2.0-beta.1` |
| `pyproject.toml` / `code_rook.__version__` | `0.2.0` | `0.2.0b1`（PEP 440） |

`scripts/check_release_contract.py` 会校验以上三处版本、dated Changelog heading 与链接、当前
HTTP/SSE/stream-json 协议清单、`WIRE_PROTOCOL.md` 是否存在，以及发布评分卡。完整 workflow
随后运行 `make verify`，其中生成协议同步检查会发现过期 wire 文档。

## 发版前清单

1. 完成候选 benchmark、三平台安全/恢复/安装报告，把评分卡更新为有证据的 `GO`；
2. 将 `Unreleased` 内容移动到带日期的新版本 heading，补 release link，并让 Unreleased compare
   从新 tag 开始；
3. 同时更新 Python、包元数据和 VSIX 版本；若 bus 改动，重新生成 `WIRE_PROTOCOL.md`；
4. 在干净工作区运行 `make verify` 和对应版本的 contract check；
5. 合并后创建指向 `main` 已审查 commit 的签名 tag，再推送 tag。

以当前历史版本做不要求 GO 的本地合同检查：

```bash
uv run python scripts/check_release_contract.py --tag v0.1.0
```

真实 tag workflow 会额外传 `--require-go`。不要为了通过脚本只改“GO”文字；评分卡中的每项数字
必须链接到真实报告。

## Tag workflow 产物

`.github/workflows/release.yml` 先调用三平台 `distribution.yml`，再运行完整仓库门禁。全部通过后：

- wheel、Docker 镜像与 Windows portable 都在零凭据、隔离 HOME 和随机 loopback 端口下验证版本、
  未配置状态、TUI help、真实 Core 启动与 `ping`；
- 从 Ubuntu 构建取得 wheel/sdist，从 Windows 取得 portable ZIP，从 Linux 取得 VSIX；
- 构建并推送 `ghcr.io/<owner>/<repo>:<tag>`，以 OCI digest 作为容器校验值；
- 用 Syft 为 wheel、sdist、portable、VSIX 和容器分别生成 SPDX JSON SBOM；
- 生成 `release-contract.json`、`release-manifest.json` 与标准 `SHA256SUMS`；
- 用 GitHub OIDC 获得短期 Sigstore 身份，`actions/attest` 为下载资产和容器生成 provenance；
- 用 Cosign keyless 签名容器 digest 和 `SHA256SUMS`，立即在 workflow 内回验 bundle；
- 最后才创建 GitHub Release。预发行 tag 自动标为 prerelease。

该流程没有 PAT、PyPI token 或长期签名私钥。GitHub 的 artifact attestation 将产物 digest 与
workflow、仓库、commit 和触发事件绑定；它证明来源，不证明软件没有漏洞。实现依据为
[GitHub actions/attest](https://github.com/actions/attest)、
[GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)、
[Anchore SBOM Action](https://github.com/anchore/sbom-action) 和
[Sigstore blob signing](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/)。

## 下载后验证

先在 Release 资产目录验证所有下载文件：

```bash
sha256sum --check SHA256SUMS

cosign verify-blob SHA256SUMS \
  --bundle SHA256SUMS.sigstore.json \
  --certificate-identity-regexp \
    'https://github.com/kyletser/coderook/.github/workflows/release.yml@refs/tags/.*' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'

gh attestation verify coderook-0.2.0-py3-none-any.whl \
  --repo kyletser/coderook
```

容器使用 Release 中 `container-image.json` 的完整 digest，而不是可移动 tag：

```bash
cosign verify ghcr.io/kyletser/coderook@sha256:<digest> \
  --certificate-identity-regexp \
    'https://github.com/kyletser/coderook/.github/workflows/release.yml@refs/tags/.*' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

SBOM 是构建时依赖清单，不是漏洞扫描通过证明。PyPI 可信发布和一次真实公开 Release 仍属于外部
运营门禁；只有远端 workflow 产生并可验证的资产，才能把评分卡对应项改为通过。
