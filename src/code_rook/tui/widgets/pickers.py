"""键盘驱动的选择类弹窗控件：问题、checkpoint 与计划审阅。"""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from textual import events
from textual.message import Message
from textual.widgets import Static


class UserQuestionSelect(Static):
    can_focus = True

    DEFAULT_CSS = """
    UserQuestionSelect {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    UserQuestionSelect:focus { border: solid #72c7d4; }
    """

    class Answered(Message):
        # 初始化结构化问题回答消息，answer 为 None 时改用输入框自由回答
        def __init__(
            self,
            select: UserQuestionSelect,
            answer: str | None,
        ) -> None:
            self.select = select
            self.answer = answer
            super().__init__()

    # 初始化结构化问题选择器并添加自由回答入口
    def __init__(
        self,
        question_id: str,
        question: str,
        header: str,
        options: list[str],
        multi_select: bool,
    ) -> None:
        super().__init__("")
        self.question_id = question_id
        self._question = question
        self._header = header
        self._options = options
        self._choices = [*options, "输入自定义答案"]
        self._multi_select = multi_select
        self._selected: set[int] = set()
        self._cursor = 0

    # 挂载后渲染问题和可选答案并取得焦点
    def on_mount(self) -> None:
        self.border_title = f" {self._header} "
        self.border_subtitle = (
            " ↑↓ move   Space toggle   Enter confirm "
            if self._multi_select
            else " ↑↓ move   Enter select "
        )
        self.update(self._render_ui())
        self.focus()

    # 渲染结构化问题、当前光标和多选状态
    def _render_ui(self) -> str:
        lines = [f"[bold]{escape(self._question)}[/bold]"]
        for index, choice in enumerate(self._choices):
            cursor = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            checked = (
                "[cyan][x][/cyan]"
                if index in self._selected
                else ("[dim][ ][/dim]" if self._multi_select else "")
            )
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{cursor} {checked} [{style}]{escape(choice)}[/{style}]"
            )
        return "\n".join(lines)

    # 提交当前选择，多选时按原选项顺序合并答案
    def _submit(self) -> None:
        custom_index = len(self._choices) - 1
        if self._cursor == custom_index:
            self.post_message(self.Answered(self, None))
            return
        if self._multi_select:
            self._selected.add(self._cursor)
            answers = [
                self._choices[index]
                for index in sorted(self._selected)
                if index < custom_index
            ]
            if answers:
                self.post_message(self.Answered(self, ", ".join(answers)))
            return
        self.post_message(self.Answered(self, self._choices[self._cursor]))

    # 处理结构化问题的键盘导航、多选和确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._choices)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._choices)
            self.update(self._render_ui())
        elif event.key == "space" and self._multi_select:
            event.stop()
            if self._cursor < len(self._choices) - 1:
                if self._cursor in self._selected:
                    self._selected.remove(self._cursor)
                else:
                    self._selected.add(self._cursor)
                self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self._submit()
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Answered(self, None))


class CheckpointPicker(Static):
    can_focus = True

    DEFAULT_CSS = """
    CheckpointPicker {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #8c6d3f;
        border-title-color: #d8a65b;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    CheckpointPicker:focus { border: solid #d8a65b; }
    """

    class Selected(Message):
        # 初始化 checkpoint 选择消息
        def __init__(self, picker: CheckpointPicker, checkpoint_id: str) -> None:
            self.picker = picker
            self.checkpoint_id = checkpoint_id
            super().__init__()

    class Dismissed(Message):
        # 初始化 checkpoint 选择器关闭消息
        def __init__(self, picker: CheckpointPicker) -> None:
            self.picker = picker
            super().__init__()

    # 初始化 checkpoint 选择器，仅接收可恢复状态的条目
    def __init__(self, checkpoints: list[dict[str, Any]]) -> None:
        super().__init__("")
        self._checkpoints = checkpoints
        self._cursor = 0

    # 挂载后渲染安全恢复点并取得焦点
    def on_mount(self) -> None:
        self.border_title = " Rewind "
        self.border_subtitle = " ↑↓ move   Enter restore   Esc close "
        self.update(self._render_ui())
        self.focus()

    # 渲染 checkpoint 标签、文件和创建时间
    def _render_ui(self) -> str:
        lines = ["[bold yellow]选择要恢复的 checkpoint[/bold yellow]"]
        for index, checkpoint in enumerate(self._checkpoints):
            marker = "[bold #d8a65b]>[/bold #d8a65b]" if index == self._cursor else " "
            label = escape(str(checkpoint.get("label", "checkpoint")))
            paths = ", ".join(str(path) for path in checkpoint.get("paths", []))
            created = escape(str(checkpoint.get("created_at", ""))[:19])
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{label}[/{style}]"
                f"  [dim]{escape(paths)}  {created}[/dim]"
            )
        lines.append("[dim]恢复会拒绝覆盖 checkpoint 之后再次修改的文件[/dim]")
        return "\n".join(lines)

    # 处理 checkpoint 选择器的键盘导航和确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._checkpoints)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._checkpoints)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            checkpoint_id = str(
                self._checkpoints[self._cursor].get("checkpoint_id", "")
            )
            self.post_message(self.Selected(self, checkpoint_id))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Dismissed(self))


class PlanReview(Static):
    """Keyboard-driven review prompt shown after a read-only planning run."""

    can_focus = True
    _CHOICES = (
        ("approve", "批准并实施", "退出 Plan Mode，按当前权限逐项执行"),
        ("revise", "继续规划", "输入反馈后再次进行只读分析"),
        ("cancel", "取消", "保留计划但不执行任何改动"),
    )

    DEFAULT_CSS = """
    PlanReview {
        height: auto;
        margin: 1 2 0 2;
        padding: 0 2 1 2;
        border: solid #4d8994;
        border-title-color: #72c7d4;
        border-subtitle-color: #8b929d;
        background: #17191d;
        color: $text;
    }
    PlanReview:focus { border: solid #72c7d4; }
    """

    class Decided(Message):
        # 初始化计划审阅决定消息
        def __init__(self, review: PlanReview, decision: str) -> None:
            self.review = review
            self.decision = decision
            super().__init__()

    # 初始化计划审阅面板并默认选中批准
    def __init__(self, run_id: str) -> None:
        super().__init__("")
        self.run_id = run_id
        self._cursor = 0

    # 挂载后渲染选项并取得键盘焦点
    def on_mount(self) -> None:
        self.border_title = " Plan ready "
        self.border_subtitle = " ↑↓ move   Enter select   Esc cancel "
        self.update(self._render_ui())
        self.focus()

    # 渲染批准、继续规划和取消三个明确分支
    def _render_ui(self) -> str:
        lines = ["[bold]计划已完成，下一步？[/bold]"]
        for index, (_decision, label, detail) in enumerate(self._CHOICES):
            marker = "[bold #72c7d4]>[/bold #72c7d4]" if index == self._cursor else " "
            style = "bold white" if index == self._cursor else "#c6cad0"
            lines.append(
                f"{marker} [{style}]{escape(label)}[/{style}]  [dim]{escape(detail)}[/dim]"
            )
        return "\n".join(lines)

    # 处理计划审阅的键盘导航与确认
    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            event.stop()
            self._cursor = (self._cursor - 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif event.key in ("down", "j"):
            event.stop()
            self._cursor = (self._cursor + 1) % len(self._CHOICES)
            self.update(self._render_ui())
        elif event.key == "enter":
            event.stop()
            self.post_message(self.Decided(self, self._CHOICES[self._cursor][0]))
        elif event.key == "escape":
            event.stop()
            self.post_message(self.Decided(self, "cancel"))