# 为 CodeRook 贡献

感谢你愿意改进 CodeRook。项目目前处于公开 Beta 前阶段，优先接受可复现的缺陷修复、
安全与可靠性增强、评测改进、文档修正和小范围产品体验优化。

## 开始之前

- 缺陷和小改进可以直接提交 Issue/PR。
- 大型功能、协议变更或架构重写请先开 Feature Request，说明使用场景、替代方案和验收方法。
- 安全问题不要公开披露，按 [SECURITY.md](SECURITY.md) 报告。
- 行为规范见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

## 开发环境

要求 Python 3.12、Git 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
uv run coderook --version
```

TUI 是主要产品面。涉及任务管理、可观测和交互的修改，应优先在 TUI 中验证；CLI 主要用于
脚本与调试。

## 代码规范

- 生产函数的 `def` 上方需要一行中文注释，说明函数职责。
- 测试函数的 `def` 上方需要 `# 功能：` 与 `# 设计：` 两行中文注释。
- 跨进程命令/事件先修改 Pydantic 协议模型；修改 `src/code_rook/core/bus/` 后必须重新生成
  `WIRE_PROTOCOL.md`。
- 不把真实 API key、token、个人配置、trace、runtime database 或 benchmark 原始密钥提交到仓库。
- 新功能需要适度测试，并同步面向用户的文档、已知限制和降级行为。

## 验证

开发时可以先运行窄测试，提交前必须从头运行完整门禁：

```bash
uv sync --frozen
uv run ruff check .
uv run python scripts/check_brand.py
uv run python scripts/check_public_repo.py
uv run mypy src
uv run mypy --platform linux src
uv run pytest -q
uv run python scripts/gen_protocol_doc.py --check
uv build
uv run python scripts/smoke_wheel.py dist
```

Windows 上也必须运行 Linux 平台 Mypy 契约。任何命令失败都需要修复，并从完整门禁开头重新运行。

## Pull Request

PR 请保持单一目的，并提供：

1. 解决的用户问题或不变式。
2. 关键设计选择和未采用的替代方案。
3. 实际运行的验证命令与结果。
4. 对安全、兼容、持久化、协议和文档的影响。
5. UI 变更的截图或录屏；benchmark 变更的配置指纹和前后报告。

维护者会按正确性、安全边界、兼容性、测试证据和维护成本评审。提交 PR 即表示你有权按本项目
[MIT License](LICENSE) 提供相应贡献。
