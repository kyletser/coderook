from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from code_rook.core.capabilities import (
    CapabilityContribution,
    CapabilityKernel,
    CapabilityKind,
    CapabilityScope,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.execution.invariants import (
    InvariantViolation,
    validate_request_snapshot,
    validate_session_events,
)
from code_rook.core.execution.ledger import SessionLedgerBridge
from code_rook.core.execution.models import RequestSnapshot, SessionEventEnvelope
from code_rook.core.presets import MINIMAL_PRESET, STANDARD_PRESET
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.subagent.backends import (
    WorkerBackendCapabilities,
    WorkerBackendRegistry,
    WorkerBackendResult,
    WorkerLaunchSpec,
)
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.program import ToolProgram, ToolProgramExecutor
from code_rook.core.tools.registry import ToolRegistry


# 构造可复用的事实事件并保持时间戳、会话和序号显式可控
def _event(seq: int, event_type: str, **values: Any) -> SessionEventEnvelope:
    return SessionEventEnvelope(
        session_id="sess-test",
        seq=seq,
        timestamp="2026-08-24T00:00:00Z",
        type=event_type,
        **values,
    )


class _EchoTool(BaseTool):
    name = "echo"
    description = "Return one value"
    input_schema = {
        "type": "object",
        "properties": {"value": {}},
        "required": ["value"],
    }
    side_effect = ToolSideEffect.NONE

    # 返回 JSON 兼容文本，便于后续 Tool Program 节点通过受限引用读取
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(str(params["value"]))


class _FakeHandle:
    def __init__(self) -> None:
        self.disposed = False

    # 返回空事件历史以隔离 Backend Registry 的能力校验行为
    def events(self, after_cursor: int = 0) -> tuple[dict[str, Any], ...]:
        return ()

    # 模拟支持继续消息的 Backend 句柄
    async def followup(self, message: str) -> None:
        return None

    # 模拟无副作用取消
    async def cancel(self) -> None:
        return None

    # 返回固定成功终态
    async def result(self) -> WorkerBackendResult:
        return WorkerBackendResult("completed", "done")

    # 记录 Registry 是否真正释放句柄
    async def dispose(self) -> None:
        self.disposed = True


class _FakeBackend:
    name = "fake"
    capabilities = WorkerBackendCapabilities(
        continuation=False,
        read_only_guarantee=False,
    )

    def __init__(self) -> None:
        self.handle = _FakeHandle()

    # 返回同一测试句柄以验证所有权转移
    async def start(self, spec: WorkerLaunchSpec) -> _FakeHandle:
        return self.handle

    # 明确模拟不支持持久恢复
    async def restore(self, worker_id: str) -> _FakeHandle | None:
        return None


# 功能：验证请求快照摘要能检测记录内容与实际 Provider 入参之间的任何漂移
# 设计：先构造合法快照再只替换消息而保留旧摘要，覆盖持久化被篡改的失败关闭路径
def test_request_snapshot_rejects_message_drift() -> None:
    snapshot = RequestSnapshot.create(
        messages=[{"role": "user", "content": "original"}],
        system="system",
        tool_schemas=[],
    )
    tampered = snapshot.model_copy(
        update={"messages": ({"role": "user", "content": "tampered"},)}
    )

    with pytest.raises(InvariantViolation, match="digest is invalid"):
        validate_request_snapshot(tampered, snapshot)


# 功能：验证 Turn、Step 和 Tool Call 的完整嵌套事实链能够通过不变量检查
# 设计：使用最小六事件闭环覆盖三层配对，避免额外 payload 掩盖结构错误
def test_session_event_invariants_accept_complete_tool_chain() -> None:
    events = [
        _event(1, "turn.started", turn_id="run-1"),
        _event(2, "step.started", turn_id="run-1", step_id="run-1:1"),
        _event(
            3,
            "tool.call_started",
            turn_id="run-1",
            step_id="run-1:1",
            payload={"tool_use_id": "tool-1"},
        ),
        _event(
            4,
            "tool.call_finished",
            turn_id="run-1",
            step_id="run-1:1",
            payload={"tool_use_id": "tool-1"},
        ),
        _event(5, "step.finished", turn_id="run-1", step_id="run-1:1"),
        _event(6, "turn.finished", turn_id="run-1"),
    ]

    validate_session_events(events)


# 功能：验证孤立 Tool Result 会被运行时不变量立即拒绝
# 设计：只提供 result 而没有 started，锁定“一次调用对应一次终态”的最小反例
def test_session_event_invariants_reject_orphan_tool_result() -> None:
    with pytest.raises(InvariantViolation, match="no matching call"):
        validate_session_events(
            [
                _event(
                    1,
                    "tool.call_finished",
                    turn_id="run-1",
                    payload={"tool_use_id": "tool-1"},
                )
            ]
        )


# 功能：验证 Session Ledger Bridge 在广播前持久化事件并回填关联 ledger_seq
# 设计：通过真实 EventBus 发布 turn 事件，再比较同一事件对象和磁盘事实序号以锁定 post-commit 语义
async def test_session_ledger_bridge_persists_before_broadcast(tmp_path: Path) -> None:
    from code_rook.core.bus.events import RunStartedEvent

    store = SessionStore(tmp_path)
    bus = EventBus()
    bridge = SessionLedgerBridge(store, "sess-test", run_id="run-1")
    bridge.subscribe(bus)
    event = RunStartedEvent(
        run_id="run-1",
        goal="inspect",
        ts="2026-08-24T00:00:00Z",
    )

    await bus.publish(event)
    await bridge.close()

    persisted = store.read_session_events("sess-test")
    assert event.ledger_seq == persisted[0].seq
    assert persisted[0].type == "turn.started"


# 功能：验证 Runtime Doctor 使用的执行账本检查能识别 checksum 合法但请求摘要失配的记录
# 设计：重新计算外层行 checksum 前写入旧 digest 与新消息组合，隔离内容寻址不变量而非文件链错误
def test_execution_ledger_verifier_detects_request_digest_mismatch(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    snapshot = RequestSnapshot.create(
        messages=[{"role": "user", "content": "safe"}],
        system="system",
        tool_schemas=[],
    )
    payload = snapshot.model_dump(mode="json")
    payload["messages"] = [{"role": "user", "content": "changed"}]
    store.append_session_event(
        "sess-test",
        event_type="llm.request_prepared",
        turn_id="run-1",
        payload=payload,
    )

    assert store.verify_ledger("sess-test") == []
    assert store.verify_execution_ledger("sess-test") == [
        "request snapshot digest mismatch at seq 1"
    ]


# 功能：验证最近作用域覆盖全局贡献且批量卸载会执行资源清理
# 设计：注册同 ID 的 global/session 两层贡献并记录 cleanup，覆盖解析优先级和无残留撤销
def test_capability_kernel_resolves_nearest_scope_and_disposes_cleanup() -> None:
    kernel = CapabilityKernel()
    cleaned: list[str] = []
    kernel.register(CapabilityContribution(id="tools", kind="registry", provider="global"))
    session_scope = CapabilityScope(workspace="repo", session="sess-1")
    kernel.register(
        CapabilityContribution(
            id="tools",
            kind="registry",
            provider="session",
            scope=session_scope,
        ),
        activate=lambda _context: lambda: cleaned.append("session"),
    )

    assert kernel.resolve("registry", "tools", session_scope) == "session"
    assert kernel.dispose_scope(session_scope) == 1
    assert cleaned == ["session"]
    assert kernel.resolve("registry", "tools", session_scope) == "global"


# 功能：验证非空 Session 的 Preset 摘要是冻结事实且发生漂移时不能加载
# 设计：用合法 standard 元数据替换为 minimal ID 但保留旧摘要，模拟历史工具集被静默切换
def test_session_rejects_preset_digest_drift() -> None:
    payload = {
        "schema_version": 3,
        "id": "sess-preset",
        "mode": "chat",
        "status": "active",
        "title": "preset",
        "created_at": "2026-08-24T00:00:00Z",
        "updated_at": "2026-08-24T00:00:00Z",
        "run_ids": ["run-1"],
        "preset_id": MINIMAL_PRESET.id,
        "preset_digest": STANDARD_PRESET.digest,
    }

    with pytest.raises(ValueError, match="preset digest"):
        Session.from_dict(payload)


# 功能：验证 Tool Program 允许 sequence 内后续调用引用前序结果
# 设计：执行真实 Tool Registry 与 invoke_tool 管线，并断言引用被解析而非由 DSL 解释器求值
async def test_tool_program_executes_prior_result_reference_through_pipeline() -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    program = ToolProgram.model_validate(
        {
            "nodes": [
                {
                    "kind": "sequence",
                    "id": "sequence",
                    "nodes": [
                        {"kind": "call", "id": "first", "tool": "echo", "arguments": {"value": 7}},
                        {
                            "kind": "call",
                            "id": "second",
                            "tool": "echo",
                            "arguments": {"value": {"$ref": "first.result"}},
                        },
                    ],
                }
            ]
        }
    )
    result = await ToolProgramExecutor(
        registry,
        EventBus(),
        "run-1",
        "parent-1",
    ).execute(program)

    nodes = {item["id"]: item for item in result["nodes"]}
    assert nodes["first"]["result"] == 7
    assert nodes["second"]["result"] == 7


# 功能：验证 Tool Program 拒绝并行兄弟间引用和未知动态结果来源
# 设计：在同一 parallel 内让第二节点引用第一节点，覆盖并发轨迹不确定性的静态阻断
def test_tool_program_rejects_parallel_sibling_reference() -> None:
    with pytest.raises(ValidationError, match="parallel siblings"):
        ToolProgram.model_validate(
            {
                "nodes": [
                    {
                        "kind": "parallel",
                        "id": "parallel",
                        "nodes": [
                            {"kind": "call", "id": "first", "tool": "echo", "arguments": {"value": 1}},
                            {
                                "kind": "call",
                                "id": "second",
                                "tool": "echo",
                                "arguments": {"value": {"$ref": "first.result"}},
                            },
                        ],
                    }
                ]
            }
        )


# 功能：验证 Worker Backend 对不支持的 continuation 和只读保证请求明确失败
# 设计：用能力全否的假 Backend 分别启动两次，证明 Registry 不会静默忽略能力差异
async def test_worker_backend_registry_fails_loud_on_unsupported_capability(
    tmp_path: Path,
) -> None:
    registry = WorkerBackendRegistry()
    backend = _FakeBackend()
    registry.register(backend)
    spec = WorkerLaunchSpec(
        worker_id="worker-1",
        prompt="task",
        cwd=tmp_path,
        read_only=True,
        env={},
    )

    with pytest.raises(ValueError, match="continuation"):
        await registry.start("fake", spec, require_continuation=True)
    with pytest.raises(ValueError, match="read-only"):
        await registry.start("fake", spec, require_read_only=True)


# 功能：验证 Worker Backend Registry 释放句柄时同步移除所有权并调用 disposer
# 设计：正常启动假 Backend 后走公开 dispose_handle，两次观察句柄表和清理标志避免只删映射
async def test_worker_backend_registry_disposes_owned_handle(tmp_path: Path) -> None:
    registry = WorkerBackendRegistry()
    backend = _FakeBackend()
    registry.register(backend)
    spec = WorkerLaunchSpec(
        worker_id="worker-1",
        prompt="task",
        cwd=tmp_path,
        read_only=False,
        env={},
    )
    await registry.start("fake", spec)

    await registry.dispose_handle("worker-1")

    assert registry.handle("worker-1") is None
    assert backend.handle.disposed is True


# 功能：验证 Worker Backend 注册、解析和关闭都真正经过共享 CapabilityKernel
# 设计：向 workspace 作用域 Registry 注册假 Backend，分别从 Kernel 和 Registry 解析再检查关闭清零
async def test_worker_backend_registry_uses_capability_kernel(tmp_path: Path) -> None:
    kernel = CapabilityKernel()
    scope = CapabilityScope(workspace=str(tmp_path))
    registry = WorkerBackendRegistry(kernel, scope=scope)
    backend = _FakeBackend()
    registry.register(backend)

    assert kernel.resolve(CapabilityKind.WORKER_BACKEND, "fake", scope) is backend
    assert registry.require("fake") is backend

    await registry.close()

    assert kernel.resolve(CapabilityKind.WORKER_BACKEND, "fake", scope) is None
