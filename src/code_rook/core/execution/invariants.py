from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from code_rook.core.execution.models import RequestSnapshot, SessionEventEnvelope


class InvariantViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class _RegisteredInvariant:
    name: str
    check: Callable[[tuple[SessionEventEnvelope, ...]], None]


class InvariantRegistry:
    # 初始化按注册顺序执行且拒绝重复名称的不变量注册表
    def __init__(self) -> None:
        self._checks: dict[str, _RegisteredInvariant] = {}

    # 注册一个关系不变量并返回可撤销注册的闭包
    def register(
        self,
        name: str,
        check: Callable[[tuple[SessionEventEnvelope, ...]], None],
    ) -> Callable[[], None]:
        if not name or name in self._checks:
            raise ValueError(f"duplicate or empty invariant name: {name}")
        self._checks[name] = _RegisteredInvariant(name=name, check=check)

        # 撤销当前不变量注册且允许重复调用
        def dispose() -> None:
            self._checks.pop(name, None)

        return dispose

    # 对不可变事件快照运行全部已注册不变量
    def validate(self, events: Iterable[SessionEventEnvelope]) -> None:
        snapshot = tuple(events)
        for registered in tuple(self._checks.values()):
            try:
                registered.check(snapshot)
            except InvariantViolation:
                raise
            except Exception as exc:
                raise InvariantViolation(
                    f"invariant {registered.name} failed: {exc}"
                ) from exc


# 验证会话序号、Turn/Step 包围关系及 Tool Call 配对关系
def validate_session_events(
    events: Iterable[SessionEventEnvelope],
    *,
    allow_incomplete: bool = False,
) -> None:
    expected_seq: int | None = None
    active_turns: set[str] = set()
    active_steps: set[str] = set()
    pending_tools: dict[str, str] = {}
    for event in events:
        if expected_seq is not None and event.seq <= expected_seq:
            raise InvariantViolation("session event seq must be strictly increasing")
        expected_seq = event.seq
        if event.type == "turn.started":
            if not event.turn_id or event.turn_id in active_turns:
                raise InvariantViolation("turn.started must open one new turn")
            active_turns.add(event.turn_id)
        elif event.type == "turn.finished":
            if event.turn_id not in active_turns:
                raise InvariantViolation("turn.finished has no active turn")
            if any(step.startswith(f"{event.turn_id}:") for step in active_steps):
                raise InvariantViolation("turn cannot finish with an active step")
            active_turns.remove(event.turn_id)
        elif event.type == "step.started":
            if event.turn_id not in active_turns or not event.step_id:
                raise InvariantViolation("step.started must be enclosed by an active turn")
            if event.step_id in active_steps:
                raise InvariantViolation("step.started duplicated an active step")
            active_steps.add(event.step_id)
        elif event.type == "step.finished":
            if event.step_id not in active_steps:
                raise InvariantViolation("step.finished has no active step")
            active_steps.remove(event.step_id)
        elif event.type == "tool.call_started":
            tool_use_id = str(event.payload.get("tool_use_id", ""))
            if not tool_use_id or tool_use_id in pending_tools:
                raise InvariantViolation("tool call id must be new and non-empty")
            pending_tools[tool_use_id] = event.step_id
        elif event.type in {"tool.call_finished", "tool.call_failed"}:
            tool_use_id = str(event.payload.get("tool_use_id", ""))
            if tool_use_id not in pending_tools:
                raise InvariantViolation("tool result has no matching call")
            if pending_tools[tool_use_id] != event.step_id:
                raise InvariantViolation("tool result belongs to a different step")
            if event.payload.get("terminal", True):
                pending_tools.pop(tool_use_id, None)
    if not allow_incomplete and (active_turns or active_steps or pending_tools):
        raise InvariantViolation("session event chain ends with active execution state")


# 验证记录后的请求快照和实际即将发送的请求完全等价
def validate_request_snapshot(
    recorded: RequestSnapshot,
    actual: RequestSnapshot,
) -> None:
    if recorded.calculated_digest() != recorded.digest:
        raise InvariantViolation("recorded request snapshot digest is invalid")
    if actual.calculated_digest() != actual.digest:
        raise InvariantViolation("actual request snapshot digest is invalid")
    if recorded.model_dump() != actual.model_dump():
        raise InvariantViolation("provider request differs from the recorded snapshot")
