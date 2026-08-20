# Contributor Tasks

这里列出无需理解整个 Agent runtime 也能完成的小任务。状态为 `READY` 才可认领；先使用
[Contributor task 模板](https://github.com/kyletser/coderook/issues/new?template=contributor_task.yml)
创建 Issue 并写明任务 ID，等待维护者确认没有重复工作。每个 PR 只完成一个 ID。

## READY 任务

### DOC-001：复现一条跨平台安装路径

- **用户结果**：陌生用户能在 Windows、Ubuntu 或 macOS 按公开文档完成一种安装并看到版本输出；
- **范围**：选择 `README.md`、`docs/USER_GUIDE.md` 或 `docs/UPGRADING.md` 的一条路径，在干净临时环境
  逐条运行；修复发现的一个具体错误或补充缺失前置条件；
- **验收**：PR 记录 OS、Python/uv 版本、实际命令和已脱敏结果；`check_public_repo.py` 通过；
- **不在范围内**：发布包、修改运行时、提交本机路径/API key、笼统重写全部文档。

### TEST-001：增加发行清单的嵌套文件回归测试

- **用户结果**：SBOM 放入子目录时，manifest 使用 POSIX 相对路径且 checksum 仍可复算；
- **范围**：只修改 `tests/unit/test_release_contract.py`，在 `tmp_path` 创建一个嵌套 SPDX JSON 文件，
  断言名称、类型和 SHA-256；若测试暴露真实缺陷，可最小修改 `scripts/generate_release_manifest.py`；
- **验收**：目标测试、Ruff 和 Mypy 通过，测试函数保留“功能/设计”两行中文注释；
- **不在范围内**：修改 release workflow、引入新依赖或创建真实 tag。

### EXAMPLE-001：增加只读 repository-tour Skill

- **用户结果**：扩展作者能复制一个不会执行 shell、联网或写文件的仓库导览 Skill；
- **范围**：新增 `examples/skills/repository-tour/SKILL.md`，声明只读目标、输入、输出与禁止动作；更新
  `examples/README.md`，并参照 focused-fix 测试覆盖发现、安装 digest 与 prompt 渲染；
- **验收**：Skill 测试、公开仓库检查与文档链接检查通过；示例不包含模型密钥和个人路径；
- **不在范围内**：新增工具能力、自动批准、Bash、WebFetch 或 MCP server。

### TUI-001：补一个管理面板空状态测试

- **用户结果**：MCP、Hooks、Memory 或 Jobs 面板在没有数据时给出下一步，而不是空白区域；
- **范围**：任选一个尚无空状态断言的面板，复用 Textual test driver，最小调整文案和测试；
- **验收**：相关 TUI 单测通过，截图不是必需；用户文案不能暗示动作已经执行；
- **不在范围内**：重排 TUI 架构、增加 CLI 产品功能或调用在线模型。

## 完成定义

认领 Issue 获得维护者确认后再编码；PR 必须链接 Issue、遵守 `CONTRIBUTING.md`、列出实际验证，
并保持任务边界。合并后维护者将条目标为 `DONE` 或替换为新的等价小任务。任务不是悬赏，也不承诺
合并；安全、正确性和维护成本仍按 `GOVERNANCE.md` 评审。
