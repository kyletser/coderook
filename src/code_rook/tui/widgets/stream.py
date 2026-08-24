"""流式思考与工具调用时间线控件。"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Markdown, Static

from code_rook.tui.product import tr
from code_rook.tui.widgets import _params_str, _tool_action_text


class LLMStreamBlock(Widget):
    """流式思考时间线；最终回答自动退化为无标题的普通 Markdown。"""

    DEFAULT_CSS = """
    LLMStreamBlock { height: auto; padding: 0 2; color: $text; }
    LLMStreamBlock > .message-kind { color: #8e98a5; }
    LLMStreamBlock > .thought-body {
        padding: 0 1 0 2;
        margin-left: 1;
        border-left: solid #343b45;
        color: #aab2be;
    }
    LLMStreamBlock.answer > .message-kind { display: none; }
    LLMStreamBlock.answer > .thought-body {
        padding: 0;
        margin-left: 0;
        border-left: none;
        color: $text;
    }
    LLMStreamBlock.collapsed > .thought-body { display: none; }
    """

    # 初始化为空文本块
    def __init__(self, *, locale: str = "zh-CN") -> None:
        super().__init__()
        self._text = ""
        self._finalized = False
        self._kind = "working"
        self._locale = locale

    # 根据流式与折叠状态生成思考区标题
    def _header(self) -> str:
        state = tr(
            "stream.thought" if self._finalized else "stream.thinking",
            self._locale,
        )
        chevron = "›" if "collapsed" in self.classes else "⌄"
        return f"[#7f8996]◉[/#7f8996] [#8e98a5]{state}[/#8e98a5]  [dim]{chevron}[/dim]"

    # 根据流式状态挂载纯文本或支持屏幕选择的 Markdown 子组件
    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="message-kind")
        if self._finalized:
            if self._text.strip():
                yield Markdown(
                    self._text,
                    classes="assistant-response thought-body",
                )
            return
        yield Static(self._text, classes="stream-text thought-body", markup=False)

    # 追加一个 token 并刷新显示
    def append_token(self, token: str) -> None:
        if self._finalized:
            return
        self._text += token
        if self.is_attached:
            try:
                self.query_one(".stream-text", Static).update(self._text)
            except NoMatches:
                self.refresh(recompose=True)

    # 将当前可见模型消息标记为意图说明或最终回答
    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.set_class(kind in {"answer", "respond"}, "answer")
        if self.is_attached:
            self.refresh(recompose=True)

    # 返回当前流式块已累积的原始 Markdown 文本
    @property
    def text(self) -> str:
        return self._text

    # 将累积文本切换为 Textual Markdown，使最终内容可选择和复制
    def finalize_markdown(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self.is_attached:
            self.refresh(recompose=True)

    # 切换思考块语言并刷新标题
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        if self.is_attached:
            self.refresh(recompose=True)

    # 点击思考标题时折叠或展开正文，最终回答不参与折叠
    def on_click(self, event: events.Click) -> None:
        if "answer" in self.classes:
            return
        try:
            header = self.query_one(".message-kind", Static)
        except NoMatches:
            return
        if event.widget is not header:
            return
        self.toggle_class("collapsed")
        header.update(self._header())


class ToolCallBlock(Widget):
    """Codex 风格工具行；默认紧凑，完成后可展开完整输入与结果。"""

    DEFAULT_CSS = """
    ToolCallBlock { height: auto; padding: 0 1; color: #9aa4b2; }
    ToolCallBlock > .summary { color: #aab2be; }
    ToolCallBlock > .detail {
        display: none;
        height: auto;
        max-height: 12;
        margin: 0 0 1 2;
        padding: 1 2;
        border: round #343b45;
        background: #15191e;
        color: #aab2be;
        overflow-x: auto;
        overflow-y: auto;
    }
    ToolCallBlock.expanded > .detail { display: block; }
    ToolCallBlock .detail-content { width: auto; height: auto; }
    """

    # 初始化工具调用信息
    def __init__(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        locale: str | None = None,
    ) -> None:
        super().__init__()
        self._tool_name = tool_name
        self._params = params
        self._params_full = _params_str(params)
        self._output = ""
        self._elapsed_ms = 0
        self._is_error = False
        self._finished = False
        self._locale = locale or "zh-CN"
        self._legacy_detail = locale is None

    def compose(self) -> ComposeResult:
        yield Static(self._summary(), classes="summary")
        with ScrollableContainer(classes="detail"):
            yield Static("", classes="detail-content", markup=False)

    # 生成自然动作、轻量状态图标和展开提示组成的单行摘要
    def _summary(self) -> str:
        if self._is_error:
            icon = "[bold red]×[/bold red]"
            attempted = _tool_action_text(
                self._tool_name,
                self._params,
                finished=False,
                locale=self._locale,
            )
            action = tr("stream.failed_action", self._locale, action=attempted)
        elif self._finished:
            icon = "[green]✓[/green]"
            action = _tool_action_text(
                self._tool_name,
                self._params,
                finished=True,
                locale=self._locale,
            )
        else:
            icon = "[bold cyan]◌[/bold cyan]"
            action = _tool_action_text(
                self._tool_name,
                self._params,
                finished=False,
                locale=self._locale,
            )
        chevron = ""
        if self._finished:
            chevron = (
                "  [#66717e]⌄[/#66717e]"
                if "expanded" in self.classes
                else "  [#66717e]›[/#66717e]"
            )
        return f"{icon} [#aab2be]{escape(action)}[/#aab2be]{chevron}"

    # 生成完整工具输入、结果与终态，供滚动详情面板按需展示
    def _detail_text(self) -> str:
        if self._tool_name in {"bash", "Bash"} and self._params.get(
            "action", "run"
        ) == "run":
            input_text = str(self._params.get("command", ""))
        else:
            input_text = self._params_full
        result_label = (
            ("Error" if self._is_error else "Response")
            if self._legacy_detail
            else tr(
                "stream.error" if self._is_error else "stream.response",
                self._locale,
            )
        )
        status = tr(
            "stream.failed" if self._is_error else "stream.success",
            self._locale,
        )
        parts = [
            f"{self._tool_name}\n{input_text}",
            f"{result_label}\n{self._output.strip() or tr('stream.no_output', self._locale)}",
            f"{'×' if self._is_error else '✓'} {status} · {self._elapsed_ms} ms",
        ]
        return "\n\n".join(parts)

    # 切换工具调用块语言并刷新摘要和已展开详情
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        self._legacy_detail = False
        if not self.children:
            return
        self.query_one(".summary", Static).update(self._summary())
        if "expanded" in self.classes:
            self.query_one(".detail-content", Static).update(self._detail_text())

    # 工具调用完成时先更新持久状态，再在已挂载时刷新可见控件
    def set_result(self, output: str, elapsed_ms: int, *, is_error: bool = False) -> None:
        self._output = output
        self._elapsed_ms = elapsed_ms
        self._is_error = is_error
        self._finished = True
        self.add_class("finished")
        self.set_class(is_error, "failed")
        if self.children:
            self.query_one(".summary", Static).update(self._summary())

    # 仅允许已完成工具展开详情并同步摘要与完整内容
    def set_expanded(self, expanded: bool) -> None:
        if not self._finished:
            self.remove_class("expanded")
            return
        if expanded:
            self.query_one(".detail-content", Static).update(self._detail_text())
            self.add_class("expanded")
        else:
            self.remove_class("expanded")
        self.query_one(".summary", Static).update(self._summary())

    # 点击已完成工具行时切换完整详情
    def on_click(self) -> None:
        self.set_expanded("expanded" not in self.classes)


class ToolStepGroup(Widget):
    """把同一 Agent step 的工具调用合并为可折叠的步骤时间线。"""

    DEFAULT_CSS = """
    ToolStepGroup { height: auto; padding: 0 2; }
    ToolStepGroup > .step-header { color: #8e98a5; }
    ToolStepGroup > .step-body {
        height: auto;
        margin-left: 1;
        padding-left: 1;
        border-left: solid #343b45;
    }
    ToolStepGroup.collapsed > .step-body { display: none; }
    """

    # 初始化空步骤组并记录 step 编号
    def __init__(self, step: int, *, locale: str = "zh-CN") -> None:
        super().__init__()
        self.step = step
        self._blocks: list[ToolCallBlock] = []
        self._locale = locale

    # 按工具种类聚合同一步的自然动作摘要
    def _action_summary(self) -> str:
        names = [block._tool_name for block in self._blocks]
        labels: list[str] = []
        categories = (
            ({"bash", "background_run"}, "run_command"),
            ({"read_file", "list_dir"}, "read"),
            ({"grep", "glob", "memory_search"}, "search"),
            ({"web_search", "web_fetch"}, "web"),
            ({"edit_file", "write_file", "apply_patch"}, "edit"),
        )
        categorized: set[str] = set()
        for tool_names, category in categories:
            count = sum(name in tool_names for name in names)
            if count:
                key = f"stream.{category}.{'one' if count == 1 else 'many'}"
                labels.append(tr(key, self._locale, count=count))
                categorized.update(name for name in names if name in tool_names)
        for name in names:
            if name not in categorized:
                label = tr("stream.used_tool", self._locale, name=name)
                if label not in labels:
                    labels.append(label)
        return " · ".join(labels) or tr("stream.tools", self._locale)

    # 根据聚合动作和折叠状态生成步骤标题
    def _header(self) -> str:
        chevron = "›" if "collapsed" in self.classes else "⌄"
        return (
            "[#7f8996]▣[/#7f8996] "
            f"[#8e98a5]{escape(self._action_summary())}[/#8e98a5]  "
            f"[dim]{chevron}[/dim]"
        )

    # 挂载标题和当前已收集的工具调用
    def compose(self) -> ComposeResult:
        yield Static(self._header(), classes="step-header")
        yield Vertical(*self._blocks, classes="step-body")

    # 向步骤组追加一个工具块并刷新计数
    def add_tool(self, block: ToolCallBlock) -> None:
        self._blocks.append(block)
        if not self.children:
            return
        self.query_one(".step-body", Vertical).mount(block)
        self.query_one(".step-header", Static).update(self._header())

    # 切换步骤组及其工具块语言并立即刷新
    def set_locale(self, locale: str) -> None:
        self._locale = locale
        for block in self._blocks:
            block.set_locale(locale)
        if self.children:
            self.query_one(".step-header", Static).update(self._header())

    # 点击步骤标题时折叠或展开整个工具列表
    def on_click(self, event: events.Click) -> None:
        try:
            header = self.query_one(".step-header", Static)
        except NoMatches:
            return
        if event.widget is not header:
            return
        self.toggle_class("collapsed")
        header.update(self._header())
