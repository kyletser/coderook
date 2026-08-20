# CodeRook 发布评分卡

更新日期：2026-08-20

候选状态：**NO-GO（真实模型门禁未运行，最新远端三平台 CI 未通过）**

## 已有确定性证据

- 50 个任务清单可加载；类别数量至少为解释 6、单文件 8、多文件 10、测试 6、重构 6、安全 4，quick 固定 10 个，nightly/release 全覆盖。
- 真实候选报告落盘前强制完整 Git commit、显式 route/model/wire，以及 config/task/fixture/budget/candidate SHA-256；逐任务预算和允许工具可审计，比较器默认拒绝 fixture 或预算漂移。
- 未修改 fixture 的 50 个 baseline 均按预期失败，证明 verifier 不是天然通过。
- stream-json、resume、SDK、HTTP/SSE、MCP、PatchPlan、Artifact、ledger checksum、doctor 和配置事务均有针对性测试。
- 本机 Windows 沙箱检查结果是 `DEGRADED (windows_none)`；AUTO_REVIEW 不会把该状态当作强制隔离。Windows Job Object 后代终止测试通过，但只计为进程治理。
- VS Code 扩展已通过 TypeScript strict typecheck，并在本机实际生成 VSIX；CI 与独立 distribution workflow 均会上传该产物。
- 本机完整 pytest 为 1107 项通过（2 项平台跳过）。Ruff、品牌检查、公开仓库契约、Mypy 本机/Linux、协议生成、wheel/sdist 构建、8.73 秒 installed-wheel first-run smoke、VS Code typecheck 与含许可证 VSIX 打包全部通过。
- 2026-08-19 的远端 CI #31 在测试阶段失败：Ubuntu/macOS 各 3 项，Windows 1 项。当前候选已修复 POSIX shell 转义、宿主沙箱探测耦合和 Windows OEM 编码假设，并在本机完整 unit 中通过，仍需新一次远端 run 确认。
- Windows 安装/portable 与 Docker 构建入口已经纳入 `distribution.yml`；本机 Docker Linux engine 未运行，不能把容器或干净机 portable 记为通过。
- Aider Polyglot 固定 commit/container runner 与 SWE-bench 标准 prediction exporter 已通过离线契约测试；尚未产出真实模型切片和官方 SWE-bench harness artifact。
- 官方 MCP Python SDK 2.0 在 commit `c47ae23` 的 Windows 实测中，stdio、legacy SSE、Streamable HTTP 的 tools/resources/prompts/cancellation/reconnect 全通过；报告不覆盖 OAuth、sampling、elicitation 或任意第三方 server。

这些证据证明运行时契约和安全降级行为，不证明真实模型编码效果。

## 发布门禁

| 门禁 | 要求 | 当前结果 |
|---|---:|---|
| 总体 pass@1 | ≥80% | 未运行真实模型候选集 |
| 多文件修改 | ≥75% | 未运行 |
| 只读分析 | ≥90% | 未运行 |
| 安全负例 | 三平台 100% | Windows degraded 合约通过；Linux/macOS 远端结果待产出 |
| 强杀恢复 | 100 次中 ≥95% | 重启就绪竞态修复后本机 5/5 smoke 通过；三平台各 100 次报告未运行 |
| 两 wire format × 两次 | 4 份候选报告 | workflow 已配置，报告未产生 |
| 安装/升级 | 三平台、wheel、容器、portable | 本机 wheel build/smoke 与 VSIX 打包通过；distribution workflow 已配置，干净机报告未产生 |
| 完整 CI | 全绿 | 本机完整 gate 通过；远端 CI #31 三平台失败，修复待远端复验 |

## 运行方式

```bash
# 离线契约
uv run python scripts/run_benchmark.py --validate
uv run python scripts/run_benchmark.py --suite quick --validate-baseline

# 真实模型（会产生费用）
uv run python scripts/run_benchmark.py --suite nightly
uv run python scripts/run_benchmark.py --suite release

# 基线/候选回归比较（不调用模型）
uv run python scripts/compare_benchmark_reports.py \
  .benchmark-results/baseline/report.json \
  .benchmark-results/candidate/report.json \
  --output .benchmark-results/comparison

# 候选强杀门禁（较慢，普通开发不运行）
uv run python scripts/run_crash_recovery_matrix.py --iterations 100 --min-rate 0.95
```

远端 workflow：`benchmark-nightly.yml` 使用单一固定 route；`benchmark-release.yml` 对 Anthropic Messages 与 OpenAI Responses 各重复两次，先保存四份 candidate contract 原始报告，再由唯一 aggregate job 检查报告数、合同一致性、评分卡分类阈值与 ≤10% 重复波动。原始和聚合报告均作为 Actions artifact 保存；真实报告尚未产生。

每份真实模型报告现已包含任务 P50/P95 耗时、进程 wall/CPU、峰值 RSS、进程数和采样完整性；
`compare_benchmark_reports.py` 输出稳定 JSON/Markdown 差异，并默认把任务回退、安全负例失败、总体效果下降及
P95 成本/耗时显著上涨视为失败。资源字段和比较门禁已有离线单测，真实跨平台 P95 数字仍需候选运行产出。

## 已知限制

- Windows 不具备文件系统/网络强制 sandbox；当前产品策略是诚实降级并回到 ASK。
- TUI 图片入口识别粘贴的本地图片路径，不承诺所有终端都能直接传递剪贴板位图。
- Python/TypeScript 诊断已可取消和去重，但尚无跨真实项目的 P95 对比数据。
- shell sandbox 的禁网/允许联网档位可强制；域名白名单请求在当前后端一律 fail closed，不会静默扩大权限，但尚无按域正向放行的 OS 强制后端。
- ProcessSupervisor 已把 wall-time、CPU、峰值内存、进程数与采样完整性投影到事件、runtime、统一 TurnReceipt 和 TUI；macOS 当前只保证 wall-time 与完整性标记。
- README 的 TUI SVG 由真实 Textual 控件与正式事件结构确定性生成；它证明当前界面渲染契约，不代表在线模型效果或真实 benchmark 成绩。
- Tag release workflow 已配置版本/Changelog/协议/评分卡一致性、三平台 distribution、SPDX SBOM、SHA256SUMS、GitHub OIDC provenance 与 Cosign keyless 签名；评分卡为 NO-GO 时会在推送镜像和创建 Release 前失败，远端产物尚未生成。
- VS Code 已能生成 VSIX，但尚未完成真实 daemon Extension Host UI 冒烟，也未发布到 Marketplace。
- 未达到本页全部量化门禁前，不发布 `0.2.0-beta`，也不宣称生产就绪。
