from __future__ import annotations

from textual.app import App, ComposeResult

from code_rook.tui.commands import BUILTIN_SLASH_COMMANDS, complete_command_arg_text
from code_rook.tui.widgets.input import (
    CompletionItem,
    SlashCompleteWidget,
    _fuzzy_match,
)


# 功能：验证斜杠命令参数 Tab 补全把前缀参数补成首个候选
# 设计：走真实的命令注册表与匹配函数，直接断言返回的完整命令文本
def test_arg_tab_completion_prefix_fills_candidate() -> None:
    assert complete_command_arg_text("/mode a") == "/mode act"
    assert complete_command_arg_text("/mode ") == "/mode plan"
    assert complete_command_arg_text("/export j") == "/export json"


# 功能：验证参数 Tab 补全在已精确命中候选时循环进位，并按前缀排除不匹配候选
# 设计：assert 精确命中后进入下一候选；对不匹配前缀返回 None，隔绝无参数命令
def test_arg_tab_completion_cycles_and_filters() -> None:
    assert complete_command_arg_text("/mode plan") == "/mode act"
    assert complete_command_arg_text("/mode act") == "/mode operate"
    assert complete_command_arg_text("/mode operate") == "/mode plan"
    # 无参数候选的命令即使带空格也不触发补全
    assert complete_command_arg_text("/sessions abc") is None
    assert complete_command_arg_text("/model gpt") is None
    # 未知命令前缀不触发补全
    assert complete_command_arg_text("/nonexistent x") is None


# 功能：验证模糊匹配对多个字段按"包含优先于子序列、再按原顺序"返回命中下标
# 设计：直接调用底层 _fuzzy_match 断言排序规则，避免 UI 挂载干扰
def test_fuzzy_match_contains_before_subsequence() -> None:
    # "road"(下标0) 仅子序列命中，而 "mode"(下标1) 包含 "od"，包含项应排前面
    assert _fuzzy_match("od", "road", "mode") == [1, 0]
    # 两个字段都包含命中时保持原下标顺序
    assert _fuzzy_match("mo", "mode", "monday") == [0, 1]


# 功能：验证模糊匹配结果稳定且空查询全量命中
# 设计：子序列命中按原顺序稳定返回，不依赖字典顺序
def test_fuzzy_match_subsequence_stable_and_empty() -> None:
    assert _fuzzy_match("md", "mode", "monday") == [0, 1]
    assert _fuzzy_match("md", "sunday", "monday") == [1]
    assert _fuzzy_match("", "mode", "mcp") == [0, 1]


# 功能：验证补全筛选优先匹配命令 name，description 仅作次选兜底
# 设计：构造 name/description 互补的一对，保证 name 命中的项排在被 description 命中的项之前
def test_set_query_prefers_name_over_description() -> None:
    items = [
        CompletionItem("view", "word"),
        CompletionItem("word", "view"),
    ]
    widget = SlashCompleteWidget(items)
    widget.set_query("word")
    assert [item.name for item in widget._filtered] == ["word", "view"]


# 功能：验证 usage 行随选中项切换而更新，无 usage 时回退为说明文案
# 设计：挂载真实弹窗并调用 move_down 切换光标，读渲染后的纯文本断言 usage 行变化
async def test_usage_line_follows_selection() -> None:
    class _Harness(App[None]):
        # 挂载带 usage 与不带 usage 的两条补全项
        def compose(self) -> ComposeResult:
            yield SlashCompleteWidget(
                [
                    CompletionItem("permissions", "查看或切换权限模式", "ask|auto-review|full-access"),
                    CompletionItem("doctor", "诊断活动 Provider route"),
                ]
            )

    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        widget = app.query_one(SlashCompleteWidget)
        # 初始光标停留在第一条，显示 usage
        widget._redraw()
        text = widget.render().plain
        assert "usage: /permissions ask|auto-review|full-access" in text
        # 下移切换到无 usage 的命令，底部回退为说明文案
        widget.move_down()
        widget._redraw()
        text = widget.render().plain
        assert "usage: /permissions" not in text
        assert "诊断活动 Provider route" in text


# 功能：验证内建命令 usage 与参数候选字段类型合法，便于补全渲染稳定
# 设计：逐条检查可空字段类型，确保注册表单引用不因缺字段断裂
def test_builtin_commands_usage_and_arg_candidates_types() -> None:
    for cmd in BUILTIN_SLASH_COMMANDS:
        assert isinstance(cmd.usage, str)
        assert isinstance(cmd.arg_candidates, tuple)
    by_name = {cmd.name: cmd for cmd in BUILTIN_SLASH_COMMANDS}
    assert by_name["mode"].arg_candidates == ("plan", "act", "operate")
    assert by_name["permissions"].usage == "ask|auto-review|full-access"