from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.families import FileTool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.workspace import WorkspaceBoundary

# --- stub provider -----------------------------------------------------------


class _StubProvider:
    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
        thinking: str | None = None,
    ) -> LlmResponse:
        return next(self._responses)


# --- stub tools ---------------------------------------------------------------


class _AsyncRead(BaseTool):
    name = "async_read"
    description = "pure-read tool that sleeps and returns its label"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "delay": {"type": "number"},
        },
        "required": ["label", "delay"],
    }

    def __init__(self) -> None:
        self.starts: list[float] = []
        self.ends: list[float] = []

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        delay = float(params["delay"])
        t0 = time.monotonic()
        self.starts.append(t0)
        await asyncio.sleep(delay)
        self.ends.append(time.monotonic())
        return ToolResult(content=str(params["label"]))


class _SyncEdit(BaseTool):
    name = "sync_edit"
    description = "side-effect tool that must run serially"
    side_effect = ToolSideEffect.LOCAL_WRITE
    can_parallel = False
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"label": {"type": "string"}, "delay": {"type": "number"}},
        "required": ["label", "delay"],
    }

    def __init__(self) -> None:
        self.order: list[str] = []

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        delay = float(params["delay"])
        await asyncio.sleep(delay)
        self.order.append(str(params["label"]))
        return ToolResult(content=str(params["label"]))


class _FailRead(BaseTool):
    name = "fail_read"
    description = "raise an exception when invoked"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raise RuntimeError("boom")


class _TrackedWrite(BaseTool):
    name = "write_file"
    description = "tracked file write"
    side_effect = ToolSideEffect.LOCAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
            "delay": {"type": "number"},
        },
        "required": ["path", "content"],
    }

    # 初始化并发计数器
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    # 模拟写入并记录峰值并发数
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(float(params.get("delay", 0.05)))
        self.active -= 1
        return ToolResult(content=str(params["path"]))


# --- helpers -----------------------------------------------------------------


def _tc(name: str, *, uid: str, label: str = "x", delay: float = 0.05) -> ToolCallBlock:
    return ToolCallBlock(id=uid, name=name, input={"label": label, "delay": delay})


def _make_loop(provider: Any, registry: ToolRegistry) -> tuple[AgentLoop, EventBus]:
    bus = EventBus()
    return AgentLoop(provider, registry, bus), bus  # type: ignore[arg-type]


def _ctx(max_steps: int = 3) -> ExecutionContext:
    return ExecutionContext(run_id="rp", goal="test", max_steps=max_steps)


# --- tests -------------------------------------------------------------------


# 功能：三个独立的只读工具（can_parallel=True）应该在同一步内并发执行
# 设计：每个工具 sleep 0.05 秒；并发总耗时远低于串行 0.15 秒，且 starts 都在最早 ends 之前
async def test_independent_read_tools_run_concurrently() -> None:
    tool = _AsyncRead()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _StubProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    _tc("async_read", uid="t1", label="a"),
                    _tc("async_read", uid="t2", label="b"),
                    _tc("async_read", uid="t3", label="c"),
                ],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    loop, bus = _make_loop(provider, registry)
    ctx = _ctx()
    t0 = time.monotonic()
    await loop.run(ctx)
    elapsed = time.monotonic() - t0

    assert ctx.status == "success"
    assert len(tool.starts) == 3
    assert len(tool.ends) == 3
    # 串行估计 0.15s+；若 gather 真并发，应在 0.1s 内（含 loop 开销）
    assert elapsed < 0.10, f"expect concurrent; elapsed={elapsed:.3f}"
    # start of last call 应 < end of first call，即三者并行运行
    assert max(tool.starts) < min(tool.ends), (
        f"tools were not concurrent: starts={tool.starts}, ends={tool.ends}"
    )


# 功能：验证并行工具基础设施异常会取消并等待仍在运行的兄弟调用
# 设计：首个只读工具长时间休眠，第二个 started 事件由 critical handler 抛错，断言无命名工具 task 遗留
async def test_parallel_infrastructure_failure_cancels_sibling_tasks() -> None:
    tool = _AsyncRead()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _StubProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, bus = _make_loop(provider, registry)

    # 仅让第二个调用的事件持久化边界失败，使第一个调用已进入长 sleep
    async def fail_second_started(event: Any) -> None:
        if getattr(event, "type", "") == "tool.call_started" and getattr(
            event, "tool_use_id", ""
        ) == "t2":
            raise RuntimeError("event persistence failed")

    bus.subscribe(fail_second_started, critical=True)
    calls = [
        _tc("async_read", uid="t1", label="slow", delay=10.0),
        _tc("async_read", uid="t2", label="fail", delay=10.0),
    ]

    with pytest.raises(RuntimeError, match="event persistence failed"):
        await loop._run_act_phase(calls, _ctx())

    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("tool-call:")
        and not task.done()
    ]


# 功能：副作用工具（can_parallel=False）必须串行执行；前后两个只读工具不会跨过它合并成一批
# 设计：tool_calls = [read_a, edit_x, read_b]，每个 delay 0.05；
#       若 batcher 串联得当，应当三批顺序执行，每个工具的 start/finish 严格不重叠
async def test_side_effect_breaks_batch_and_runs_serially() -> None:
    read = _AsyncRead()
    edit = _SyncEdit()

    registry = ToolRegistry()
    registry.register(read)
    registry.register(edit)

    # 直接驱动 _run_act_phase：这是为测批量切分语义
    provider = _StubProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()

    calls = [
        _tc("async_read", uid="t1", label="read_a"),
        _tc("sync_edit", uid="t2", label="edit_x"),
        _tc("async_read", uid="t3", label="read_b"),
    ]
    await loop._run_act_phase(calls, ctx)

    # 三个工具都执行了一次
    assert len(read.starts) == 2  # 两个 async_read
    assert len(edit.order) == 1

    # 通过 elapsed 时间证明：3 个 0.05s 串行共 0.15s 才结束
    last_end = max(read.ends[0], read.ends[1])
    first_start = min(read.starts[0], read.starts[1])
    # 串行耗时下界：0.15s - epsilon（asyncio 调度误差）
    assert last_end - first_start >= 0.14, (
        f"non-serial overlap detected; spread={last_end - first_start:.3f}"
    )


# 功能：并行批中一个工具抛异常不影响同批其它工具的执行和结果回填
# 设计：3 个工具：[ok_a, fail, ok_b] 都可并行；断言 a、b 都被调用，
#       context 最终 messages 中 a 与 b 的 tool_result 是 success，fail 的是 runtime_error
async def test_one_tool_in_parallel_batch_fails_does_not_break_others() -> None:
    ok_tool = _AsyncRead()
    fail_tool = _FailRead()
    registry = ToolRegistry()
    registry.register(ok_tool)
    registry.register(fail_tool)

    provider = _StubProvider(
        [
            LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    _tc("async_read", uid="t1", label="a", delay=0.05),
                    ToolCallBlock(
                        id="t2", name="fail_read", input={}
                    ),
                    _tc("async_read", uid="t3", label="b", delay=0.05),
                ],
            ),
            LlmResponse(stop_reason="end_turn", text="done"),
        ]
    )
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)

    assert ctx.status == "success"
    # ok_tool 被调用 2 次（a 和 b）
    assert len(ok_tool.starts) == 2
    assert len(ok_tool.ends) == 2

    # context.messages 中应包含三个 tool_result，按 t1->a, t2->runtime_error, t3->b
    tool_results = [
        m for m in ctx.messages
        if m.get("role") == "user"
        and isinstance(m.get("content"), list)
        and any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in m["content"]  # type: ignore[index]
        )
    ]
    # 把 tool_result 内容展平
    pairs: list[tuple[str, str, bool]] = []
    for m in tool_results:
        for b in m["content"]:  # type: ignore[index]
            if isinstance(b, dict) and b.get("type") == "tool_result":
                pairs.append((
                    str(b.get("tool_use_id", "")),
                    str(b.get("content", "")),
                    bool(b.get("is_error", False)),
                ))
    ids = [p[0] for p in pairs]
    assert ids == ["t1", "t2", "t3"], f"unexpected tool_use ids: {ids}"
    assert pairs[0] == ("t1", "a", False)
    assert pairs[1][0] == "t2"
    assert pairs[1][2] is True  # fail 的 tool_result 是 error
    assert pairs[2] == ("t3", "b", False)


# 功能：未知工具仍按原顺序串行执行；不混入并行批
# 设计：tool_calls=[unknown] — _is_parallelable 返回 False（registry.get 返回 None）；
#       调用 _run_act_phase 后 context 标记 runtime_error tool_result
async def test_unknown_tool_runs_serially_and_returns_runtime_error() -> None:
    provider = _StubProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider, ToolRegistry())
    ctx = _ctx()
    await loop._run_act_phase(
        [ToolCallBlock(id="u1", name="ghost", input={})],
        ctx,
    )
    # 检查 tool_result 内容
    pairs: list[tuple[str, bool, str | None]] = []
    for m in ctx.messages:
        if m.get("role") == "user":
            for b in m.get("content", []) or []:  # type: ignore[union-attr]
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pairs.append((
                        str(b.get("tool_use_id", "")),
                        bool(b.get("is_error", False)),
                        b.get("error_type") if isinstance(b, dict) else None,
                    ))
    assert len(pairs) == 1
    assert pairs[0][0] == "u1"
    assert pairs[0][1] is True


# 功能：单批只有一个工具时也走"单独 await"路径而非 gather；行为等价于直接 await
# 设计：_run_act_phase 传入 [tc_read]，断言工具只被调用一次，context 收到正确结果
async def test_single_parallel_tool_in_batch_still_works() -> None:
    tool = _AsyncRead()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _StubProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop._run_act_phase([_tc("async_read", uid="t1", label="solo", delay=0.0)], ctx)
    assert len(tool.starts) == 1
    # context 含一个成功 tool_result
    found = False
    for m in ctx.messages:
        if m.get("role") == "user":
            for b in m.get("content", []) or []:  # type: ignore[union-attr]
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b.get("tool_use_id") == "t1"
                    assert b.get("content") == "solo"
                    found = True
    assert found


# 功能：验证两个 File.write 声明同一路径时由 resource claims 自动串行
# 设计：tracked backend 记录峰值并发，两个相同独占 claim 必须使 max_active 保持 1
async def test_same_file_write_claims_run_serially(tmp_path: Path) -> None:
    backend = _TrackedWrite()
    family = FileTool(WorkspaceBoundary(tmp_path), {"write_file": backend})
    registry = ToolRegistry()
    registry.register(family)
    loop, _ = _make_loop(
        _StubProvider([LlmResponse(stop_reason="end_turn", text="done")]),
        registry,
    )
    ctx = _ctx()

    await loop._run_act_phase(
        [
            ToolCallBlock(
                id="w1",
                name="File",
                input={"action": "write", "path": "same.txt", "content": "a"},
            ),
            ToolCallBlock(
                id="w2",
                name="File",
                input={"action": "write", "path": "same.txt", "content": "b"},
            ),
        ],
        ctx,
    )

    assert backend.max_active == 1


# 功能：验证两个 File.write 声明不同路径时可以进入同一并行批次
# 设计：使用不同独占 claim，tracked backend 峰值必须达到 2，证明调度不是把所有写操作一律串行
async def test_disjoint_file_write_claims_run_concurrently(tmp_path: Path) -> None:
    backend = _TrackedWrite()
    family = FileTool(WorkspaceBoundary(tmp_path), {"write_file": backend})
    registry = ToolRegistry()
    registry.register(family)
    loop, _ = _make_loop(
        _StubProvider([LlmResponse(stop_reason="end_turn", text="done")]),
        registry,
    )
    ctx = _ctx()

    await loop._run_act_phase(
        [
            ToolCallBlock(
                id="w1",
                name="File",
                input={"action": "write", "path": "a.txt", "content": "a"},
            ),
            ToolCallBlock(
                id="w2",
                name="File",
                input={"action": "write", "path": "b.txt", "content": "b"},
            ),
        ],
        ctx,
    )

    assert backend.max_active == 2
