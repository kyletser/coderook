# CodeRook Benchmarks

这里存放可复现的真实编码任务。每次运行都会把 fixture 复制到临时目录，Agent 只在副本上工作；随后框架审计文件改动并运行 manifest 声明的 verifier。

## 快速使用

```bash
# 只校验清单和 fixture，不调用模型
uv run python scripts/run_benchmark.py --validate
uv run python scripts/run_benchmark.py --validate-baseline

# 使用当前配置的 route/model 运行单个任务
uv run python scripts/run_benchmark.py --task py-clamp-upper-bound

# 分阶段门禁：quick 是快速子集，nightly/release 覆盖完整任务集
uv run python scripts/run_benchmark.py --suite quick
uv run python scripts/run_benchmark.py --suite nightly
uv run python scripts/run_benchmark.py --suite release
```

报告默认写入 `.benchmark-results/latest/report.json` 和 `report.md`。该目录已被 Git 忽略。

## 清单约束

- verifier 使用 argv 数组直接启动，不经过 shell。
- `fixture`、verifier cwd 和文件规则都必须位于仓库或临时工作区内。
- `allowed_tools` 同时裁剪模型可见工具，并作为 headless 权限 allow-list。
- `.coderook/`、pytest cache 和 Python bytecode 属于运行噪声，不计入任务改动。
- 未知模型价格记为 `unknown`，不会错误地按 0 美元计费。
- `--validate-baseline` 要求所有未修改 fixture 的 verifier 失败，防止任务天然通过。

每个任务通过 `suites` 声明归属；`nightly` 与 `release` 必须覆盖完整任务集，`quick` 只保留
能在开发循环中快速暴露回归的代表性任务。
