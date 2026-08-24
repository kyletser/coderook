from __future__ import annotations

from rich.markup import render

from code_rook.tui.panels.manage import render_mcp_servers
from code_rook.tui.panels.turn import render_turn_inspector
from code_rook.tui.widgets.permission import PermissionModePicker, PermissionSelect
from code_rook.tui.widgets.pickers import PlanReview
from code_rook.tui.widgets.selectors import ModelPicker, SessionPicker


# 功能：验证稳定选择器、审批卡和计划审阅在中英文下都渲染正确
# 设计：直接调用纯渲染方法对照两种 locale，避免终端宽度与异步挂载影响文案断言
def test_stable_widgets_render_in_zh_cn_and_en_us() -> None:
    zh_permission = PermissionSelect(
        "tool-1",
        "write_file",
        "src/app.py",
        locale="zh-CN",
    )._render_ui()
    en_permission = PermissionSelect(
        "tool-1",
        "write_file",
        "src/app.py",
        locale="en-US",
    )._render_ui()
    zh_sessions = SessionPicker([], None, locale="zh-CN")._render_ui()
    en_sessions = SessionPicker([], None, locale="en-US")._render_ui()
    zh_models = ModelPicker(
        ["deepseek-chat"],
        "deepseek-chat",
        ("tools", "thinking"),
        locale="zh-CN",
    )._render_ui()
    en_models = ModelPicker(
        ["deepseek-chat"],
        "deepseek-chat",
        ("tools", "thinking"),
        locale="en-US",
    )._render_ui()

    assert "需要批准" in zh_permission
    assert "Approval required" in en_permission
    assert "没有保存的 chat 会话" in zh_sessions
    assert "No saved chat sessions" in en_sessions
    assert "route 能力" in zh_models
    assert "route capabilities" in en_models
    assert "计划已完成" in PlanReview("run-1", locale="zh-CN")._render_ui()
    assert "The plan is ready" in PlanReview("run-1", locale="en-US")._render_ui()


# 功能：验证语言切换后已存在的权限选择器立即使用新文案
# 设计：在同一实例上调用 set_locale 并比较切换前后纯渲染结果，覆盖无需重建控件的不变式
def test_existing_permission_picker_switches_locale_immediately() -> None:
    picker = PermissionModePicker("ask", locale="zh-CN")

    assert "选择后续消息使用的权限模式" in picker._render_ui()
    picker.set_locale("en-US")
    assert "Choose the permission mode for subsequent messages" in picker._render_ui()
    assert "current" in picker._render_ui()


# 功能：验证稳定管理面板和 Turn 检查器的空状态可按 locale 双语渲染
# 设计：使用最小结构化输入渲染纯文本，同时覆盖面板标题与空集合提示
def test_stable_panels_render_in_zh_cn_and_en_us() -> None:
    zh_mcp = render(render_mcp_servers([], locale="zh-CN")).plain
    en_mcp = render(render_mcp_servers([], locale="en-US")).plain
    zh_turn = render(render_turn_inspector({}, locale="zh-CN")).plain
    en_turn = render(render_turn_inspector({}, locale="en-US")).plain

    assert "当前没有配置 MCP server" in zh_mcp
    assert "No MCP servers are configured" in en_mcp
    assert "Turn 检查器" in zh_turn
    assert "Turn Inspector" in en_turn
