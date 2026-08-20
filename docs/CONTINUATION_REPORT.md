# CodeRook 续作报告

更新日期：2026-08-20

## 今日收尾

- 日常远端检查已简化：删除三平台 upgrade/rollback 与 aggregate CI，只保留本地按需 preflight。
- commit `47a440f` 推送后的 CI `32387003843` 与 Security `32387003835` 均成功，没有手动触发
  Distribution、benchmark 或强杀矩阵。
- 修复 `scripts/run_benchmark.py --help` 因未转义百分号而崩溃的问题。
- provider doctor/`provider test` 在凭据、网络或模型诊断失败时改为非零退出，避免自动化误判成功。
- 本次 CLI 变更已通过 Ruff、相关 Mypy、14 项定向测试和真实 401 退出码 smoke；完整门禁留到下次推送前统一运行。
- GitHub 仓库设置中的 Actions 已按维护者要求关闭，API 复核为 `enabled=false`；当前无排队或运行任务，
  后续 push 不会自动运行 CI、Security、Distribution 或其他 workflow。重新公开发行前必须由维护者明确开启。

## 当前未完成项

| 优先级 | 项目 | 当前证据 | 下一步 |
|---|---|---|---|
| P0 | 真实模型 quick/nightly/release 成绩 | 本机 `legacy-anthropic` route 请求 DeepSeek 返回 HTTP 401 | 在本地凭据存储中替换有效 key，先跑 10 题 quick；通过后再跑两种 wire format × 两次 release |
| P0 | 公开 benchmark 官方判分 | Aider Polyglot、SWE-bench 适配与离线合同已完成 | 有效模型 route 就绪后运行固定小样本并保存原始报告，不把自定义切片冒充官方榜单 |
| P0 | main ruleset | 远端审计明确报告未启用；仓库 Actions 当前按要求关闭 | 发布前先明确重新开启 Actions，再要求稳定 CI/Security 汇总检查并运行一次远端审计 |
| P0 | 首次公开发行 | release workflow、SBOM、checksums、OIDC provenance 和 Cosign 合同已完成 | 真实模型评分卡达标后创建首个 tag，验证 PyPI、GHCR、Release 与公开安装 |
| P1 | 跨已发布版本升级 | 三平台历史 commit → 当前候选 → 备份回滚已成功，但 baseline 无 tag | 首个公开 tag 后用 `--require-baseline-tag` 按需运行，不放入日常 CI |

发布状态仍为 **NO-GO**：仓库工程能力基本齐全，但真实模型效果、active ruleset 和首次公开发行证据未完成。

## 目录规范

当前稳定目录职责如下：

| 目录 | 唯一职责 | 命名规则 |
|---|---|---|
| `src/code_rook/` | 可安装 Python 产品代码 | 包和模块使用 `snake_case` |
| `tests/unit/`、`tests/integration/` | 快速契约测试与真实 daemon 集成测试 | 文件使用 `test_<subject>.py` |
| `scripts/` | 维护、生成、smoke、benchmark 与发行入口 | 动词开头的 `snake_case.py` 或平台明确的 `.ps1` |
| `docs/` | 当前权威文档、专题计划、证据和复盘 | 面向主题的 `UPPER_SNAKE_CASE.md`；日期复盘放 `postmortems/` |
| `benchmarks/` | 任务清单、fixture 与公开 benchmark 容器 | 稳定 task id 使用小写 kebab-case |
| `editors/` | IDE 集成 | 按编辑器名称分子目录 |
| `examples/` | 从零可运行示例、Hook、Skill、MCP | 示例文件使用用途明确的 `snake_case` |
| `.github/` | Actions、Issue/PR 模板、CODEOWNERS 与依赖配置 | workflow 使用小写 kebab-case |

根目录只保留社区治理、构建入口和最高层用户文档。`dist*`、`reports/`、`.interop-results/`、
`.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/` 均为可再生成且被 Git 忽略的本地产物，阶段收尾时可清理；
`.venv/`、`.coderook/` 与 `.workbuddy/` 分别属于开发环境、项目运行状态和工具状态，不应随意删除。

历史规格已统一放入 `docs/SPECDRIVEN_SPEC.md`，权限诊断入口已放入
`scripts/trace_permission_flow.py`；根目录不再混放专题文档或维护脚本。

## 明日开始顺序

1. 读取本报告并确认工作树；不要重复今天已经完成的 CI 简化。
2. 更新有效 DeepSeek 凭据，仅运行 `provider test`，不得打印或提交 key。
3. 本地运行 quick 10 题并审计报告的 commit、route、model、wire、成本、耗时和失败分类。
4. 根据 quick 结果修产品缺陷；不要先扩 CI。
5. 一组变更完成后统一运行本地门禁、提交并推送；Actions 保持关闭，除非维护者明确要求恢复。
