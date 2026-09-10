from code_rook.cli.commands import session as session_commands
from code_rook.core.config import CodeRookConfig


# 功能：CLI 将短会话的无需压缩状态展示为正常结果。
# 设计：替换 IPC 调用并检查输出与退出码，避免正常跳过再次退化为错误提示。
async def test_compact_reports_not_needed_without_error(monkeypatch, capsys) -> None:
    # 模拟 Core 返回无需压缩的正常结果。
    async def fake_call(*_args, **_kwargs):
        return 0, {
            "status": "not_needed",
            "original_tokens": 24,
            "compacted_tokens": 24,
            "saved_tokens": 0,
        }

    monkeypatch.setattr(session_commands, "_call", fake_call)

    result = await session_commands._compact("sess-short", "", CodeRookConfig())

    assert result == 0
    assert capsys.readouterr().out == (
        "no compaction needed for sess-short  context=24 tokens\n"
    )
