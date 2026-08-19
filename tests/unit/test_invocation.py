from __future__ import annotations

import asyncio

from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry

# --- stub tools --------------------------------------------------------------


class _EchoParams(BaseModel):
    msg: str


class _EchoTool(BaseTool):
    name = "echo"
    description = "Echoes the msg param"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }
    params_model = _EchoParams

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=str(params["msg"]))


class _SlowTool(BaseTool):
    name = "slow"
    description = "Sleeps forever"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        await asyncio.sleep(60)
        return ToolResult(content="done")


class _BrokenTool(BaseTool):
    name = "broken"
    description = "Always raises"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raise RuntimeError("boom")


class _UsageTool(BaseTool):
    name = "usage"
    description = "Returns process usage"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 返回固定资源证据以验证 invocation 事件透传
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(
            content="done",
            process_usage={"wall_time_ms": 5, "process_count": 1, "complete": True},
        )


# --- helpers -----------------------------------------------------------------


def _call(name: str, inp: dict[str, object] | None = None, uid: str = "t1") -> ToolCallBlock:
    return ToolCallBlock(id=uid, name=name, input=inp or {})


async def _run(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    timeout: float = 5.0,
) -> tuple[ToolResult, list[BaseModel]]:
    bus = EventBus()
    events: list[BaseModel] = []

    async def _collect(e: BaseModel) -> None:
        events.append(e)

    bus.subscribe(_collect)
    result = await invoke_tool(registry, tool_call, bus, run_id="r1", timeout=timeout)
    return result, events


# --- tests -------------------------------------------------------------------


# 功能：验证正常调用时返回工具内容且发布 started + finished 事件
# 设计：同时检查返回值和事件序列，因为 invoke_tool 的双重职责是"返回结果 + 发布事件"，缺一不可
async def test_success_returns_content_and_finished_event() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result, events = await _run(registry, _call("echo", {"msg": "hi"}))
    assert not result.is_error
    assert result.content == "hi"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "tool.call_started"
    assert "tool.call_finished" in types
    assert "tool.call_failed" not in types
    finished = next(
        event for event in events if event.type == "tool.call_finished"  # type: ignore[attr-defined]
    )
    assert finished.output == "hi"  # type: ignore[attr-defined]


# 功能：验证 ToolResult 的进程资源证据原样进入 tool.call_finished durable 事件
# 设计：使用固定 usage stub 隔离 OS 采样波动，精确检查 invocation 到事件的投影边界
async def test_success_event_preserves_process_usage() -> None:
    registry = ToolRegistry()
    registry.register(_UsageTool())

    _result, events = await _run(registry, _call("usage"))
    finished = next(
        event for event in events if event.type == "tool.call_finished"  # type: ignore[attr-defined]
    )

    assert finished.process_usage == {  # type: ignore[attr-defined]
        "wall_time_ms": 5,
        "process_count": 1,
        "complete": True,
    }


# 功能：验证调用不存在的工具时返回 runtime_error 并发布 failed 事件而非 finished
# 设计：传入空 registry，确认 error_type 和事件类型同时正确，排除"未知工具却发布了 finished"的情况
async def test_unknown_tool_returns_runtime_error() -> None:
    result, events = await _run(ToolRegistry(), _call("nonexistent"))
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "unknown tool" in result.content
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_started" in types
    assert "tool.call_failed" in types
    assert "tool.call_finished" not in types


# 功能：验证缺少必填参数时返回 schema_error 而非 runtime_error
# 设计：注册需要 msg 参数的 EchoTool 但传空 input，确认错误分类准确，schema 错误与运行时错误对 S4 重试策略有不同影响
async def test_missing_required_param_gives_schema_error() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    result, events = await _run(registry, _call("echo", {}))  # "msg" is required
    assert result.is_error
    assert result.error_type == "schema_error"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_failed" in types


# 功能：验证工具执行超时时返回 timeout 类型错误而非 runtime_error
# 设计：使用永久 sleep 的 SlowTool + 极短超时（50ms），测试 asyncio.wait_for 的超时路径，确认超时被正确分类
async def test_timeout_gives_timeout_error() -> None:
    registry = ToolRegistry()
    registry.register(_SlowTool())
    result, events = await _run(registry, _call("slow"), timeout=0.05)
    assert result.is_error
    assert result.error_type == "timeout"
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert "tool.call_failed" in types


# 功能：验证工具内部抛出异常时被捕获并转为 runtime_error，错误信息保留原始异常消息
# 设计：工具直接 raise RuntimeError，确认异常不向上传播（invoke_tool 的"不抛异常"契约），error_message 包含 "boom"
async def test_runtime_exception_gives_runtime_error() -> None:
    registry = ToolRegistry()
    registry.register(_BrokenTool())
    result, events = await _run(registry, _call("broken"))
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "boom" in result.content


# 功能：验证 tool.call_started 始终是第一个被发布的事件，即使工具调用最终失败
# 设计：用不存在的工具触发失败路径，确认即使失败也先发布 started，保证事件流的时序可观测性
async def test_started_event_always_first() -> None:
    result, events = await _run(ToolRegistry(), _call("nonexistent"))
    assert events[0].type == "tool.call_started"  # type: ignore[attr-defined]


# 功能：验证 tool_call_before、approval_requested、tool_call_after 按真实执行顺序触发
# 设计：让默认高权 EchoTool 进入 ASK，并由 EventBus 立即批准，完整覆盖工具与权限接线
async def test_tool_and_approval_hooks_follow_execution_order() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    bus = EventBus()
    permission_manager = PermissionManager(timeout_s=1)
    hooks = HookManager()
    seen: list[str] = []

    # 记录每个 Hooks V2 工具生命周期名称
    async def record(context: dict[str, object]) -> None:
        seen.append(str(context["phase"]))

    # 收到真实 permission.requested 后立刻批准，避免测试等待人工输入
    async def approve(event: BaseModel) -> None:
        if getattr(event, "type", "") == "permission.requested":
            permission_manager.respond("t1", "allow_once")

    # 为三个生命周期分别注入稳定 phase 标记
    async def before(_context: dict[str, object]) -> None:
        await record({"phase": "before"})

    # 记录实际需要审批而非静态猜测的 approval 事件
    async def approval(_context: dict[str, object]) -> None:
        await record({"phase": "approval"})

    # 记录工具成功完成后的 after 事件
    async def after(_context: dict[str, object]) -> None:
        await record({"phase": "after"})

    hooks.register("tool_call_before", before)
    hooks.register("approval_requested", approval)
    hooks.register("tool_call_after", after)
    bus.subscribe(approve)

    result = await invoke_tool(
        registry,
        _call("echo", {"msg": "hi"}),
        bus,
        run_id="r1",
        permission_manager=permission_manager,
        session_id="sess-1",
        hooks=hooks,
    )

    assert not result.is_error
    assert seen == ["before", "approval", "after"]


class _UnlimitedSleepTool(BaseTool):
    name = "unlimited_sleep"
    description = "Sleeps briefly without timeout"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}
    timeout_s = 0.0

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        await asyncio.sleep(0.05)
        return ToolResult(content="done")


class _ShortTimeoutTool(BaseTool):
    name = "short_timeout"
    description = "Sleeps longer than its own timeout"
    input_schema: dict[str, object] = {"type": "object", "properties": {}, "required": []}
    timeout_s = 0.01

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        await asyncio.sleep(1.0)
        return ToolResult(content="done")


# 功能：timeout_s=0 的工具豁免调用方超时限制
# 设计：调用方传 0.01s 超时，工具睡 0.05s 仍成功，证明无限时覆盖生效而非被 wait_for 杀掉
async def test_tool_timeout_zero_disables_caller_timeout() -> None:
    registry = ToolRegistry()
    registry.register(_UnlimitedSleepTool())
    result, _ = await _run(registry, _call("unlimited_sleep"), timeout=0.01)
    assert not result.is_error
    assert result.content == "done"


# 功能：工具级 timeout_s 覆盖调用方更宽松的超时
# 设计：调用方给 5s 但工具自带 0.01s，睡 1s 必触发 timeout 错误，证明覆盖双向生效
async def test_tool_timeout_overrides_caller_timeout() -> None:
    registry = ToolRegistry()
    registry.register(_ShortTimeoutTool())
    result, _ = await _run(registry, _call("short_timeout"), timeout=5.0)
    assert result.is_error
    assert result.error_type == "timeout"
