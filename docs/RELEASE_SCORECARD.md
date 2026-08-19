# CodeRook 发布评分卡

更新日期：2026-08-18

候选状态：**NO-GO（尚无真实模型与远端平台门禁结果）**

## 已有确定性证据

- 50 个任务清单可加载；类别数量至少为解释 6、单文件 8、多文件 10、测试 6、重构 6、安全 4，quick 固定 10 个，nightly/release 全覆盖。
- 未修改 fixture 的 50 个 baseline 均按预期失败，证明 verifier 不是天然通过。
- stream-json、resume、SDK、HTTP/SSE、MCP、PatchPlan、Artifact、ledger checksum、doctor 和配置事务均有针对性测试。
- 本机 Windows 沙箱检查结果是 `DEGRADED (windows_none)`；AUTO_REVIEW 不会把该状态当作强制隔离。Windows Job Object 后代终止测试通过，但只计为进程治理。
- VS Code 扩展已通过 TypeScript strict typecheck。
- 本轮 1047 项单元测试通过（1 项平台跳过），Ruff、品牌检查、Mypy 本机/Linux和协议生成检查通过；完整集成 pytest/build gate 仍按用户要求未执行。
- Windows 安装/portable 脚本通过 PowerShell 语法解析；本机 Docker CLI 存在，但 Linux engine
  未运行，因此没有把镜像构建记为通过。

这些证据证明运行时契约和安全降级行为，不证明真实模型编码效果。

## 发布门禁

| 门禁 | 要求 | 当前结果 |
|---|---:|---|
| 总体 pass@1 | ≥80% | 未运行真实模型候选集 |
| 多文件修改 | ≥75% | 未运行 |
| 只读分析 | ≥90% | 未运行 |
| 安全负例 | 三平台 100% | Windows degraded 合约通过；Linux/macOS 远端结果待产出 |
| 强杀恢复 | 100 次中 ≥95% | 真实 daemon 强杀门禁已实现，本机 2/2 smoke 通过；三平台 100 次报告未运行 |
| 两 wire format × 两次 | 4 份候选报告 | workflow 已配置，报告未产生 |
| 安装/升级 | 三平台、wheel、容器、portable | 构建入口已实现，干净机报告未产生 |
| 完整 CI | 全绿 | 1047 项单元测试与静态门禁通过；按用户要求未执行完整集成 pytest/build gate |

## 运行方式

```bash
# 离线契约
uv run python scripts/run_benchmark.py --validate
uv run python scripts/run_benchmark.py --suite quick --validate-baseline

# 真实模型（会产生费用）
uv run python scripts/run_benchmark.py --suite nightly
uv run python scripts/run_benchmark.py --suite release

# 候选强杀门禁（较慢，普通开发不运行）
uv run python scripts/run_crash_recovery_matrix.py --iterations 100 --min-rate 0.95
```

远端 workflow：`benchmark-nightly.yml` 使用单一固定 route；`benchmark-release.yml` 对 Anthropic Messages 与 OpenAI Responses 各重复两次。报告作为 Actions artifact 保存。

## 已知限制

- Windows 不具备文件系统/网络强制 sandbox；当前产品策略是诚实降级并回到 ASK。
- TUI 图片入口识别粘贴的本地图片路径，不承诺所有终端都能直接传递剪贴板位图。
- Python/TypeScript 诊断已可取消和去重，但尚无跨真实项目的 P95 对比数据。
- shell sandbox 的禁网/允许联网档位可强制；域名白名单请求在当前后端一律 fail closed，不会静默扩大权限，但尚无按域正向放行的 OS 强制后端。
- ProcessSupervisor 已把 wall-time、CPU、峰值内存、进程数与采样完整性投影到事件、runtime、统一 TurnReceipt 和 TUI；macOS 当前只保证 wall-time 与完整性标记。
- VS Code 是验证 runtime API 的原型，尚未发布 VSIX。
- 未达到本页全部量化门禁前，不发布 `0.2.0-beta`，也不宣称生产就绪。
