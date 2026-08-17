from __future__ import annotations

import asyncio
import json
from pathlib import Path

from code_rook.core.app import CoreApp
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
)
from code_rook.core.bus.events import (
    LlmUsageEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.types import LlmResponse, UsageStats
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.subagent.agent import AgentTool
from code_rook.core.subagent.models import WorkerStatus
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import SpawnAgentTool
from code_rook.core.workspace import WorkspaceBoundary


class _ResultProvider:
    # 保存测试返回正文
    def __init__(self, text: str = "SUMMARY\ndone") -> None:
        self._text = text

    # 返回一次无需工具调用的确定性子 Agent 结果
    async def chat(self, **_kwargs: object) -> LlmResponse:
        return LlmResponse(
            stop_reason="end_turn",
            text=self._text,
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


class _BlockingProvider:
    # 绑定由测试控制的释放事件
    def __init__(self) -> None:
        self.release = asyncio.Event()

    # 阻塞模型响应，保持 Worker claim 处于活跃状态
    async def chat(self, **_kwargs: object) -> LlmResponse:
        await self.release.wait()
        return LlmResponse(
            stop_reason="end_turn",
            text="SUMMARY\ndone",
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


class _EventProvider:
    # 发布包含敏感参数和大输出的工具事件后返回结构化正文
    async def chat(self, **kwargs: object) -> LlmResponse:
        bus = kwargs["bus"]
        assert isinstance(bus, EventBus)
        run_id = str(kwargs["run_id"])
        await bus.publish(
            ToolCallStartedEvent(
                run_id=run_id,
                tool_use_id="tool-1",
                tool_name="bash",
                params={"api_key": "must-not-leak", "command": "echo safe"},
                ts="2026-08-04T00:00:00+00:00",
            )
        )
        await bus.publish(
            ToolCallFinishedEvent(
                run_id=run_id,
                tool_use_id="tool-1",
                tool_name="bash",
                elapsed_ms=1,
                output="must-not-leak-output",
                ts="2026-08-04T00:00:01+00:00",
            )
        )
        return LlmResponse(
            stop_reason="end_turn",
            text=(
                "SUMMARY\n" + "x" * 6_000 + "\n"
                "CHANGES\n- changed a.py\n"
                "EVIDENCE\n- tests passed\n"
                "RISKS\n- none\n"
                "BLOCKERS\n- none"
            ),
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


class _BudgetProvider:
    # 发布刚好耗尽根预算的 usage，并让出调度点响应 registry 取消
    async def chat(self, **kwargs: object) -> LlmResponse:
        bus = kwargs["bus"]
        assert isinstance(bus, EventBus)
        await bus.publish(
            LlmUsageEvent(
                run_id=str(kwargs["run_id"]),
                input_tokens=3,
                output_tokens=2,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                ts="2026-08-04T00:00:00+00:00",
            )
        )
        await asyncio.sleep(0)
        return LlmResponse(
            stop_reason="end_turn",
            text="SUMMARY\nshould be cancelled",
            usage=UsageStats(input_tokens=3, output_tokens=2),
        )


class _FollowupProvider:
    # 初始化首轮阻塞点和调用计数
    def __init__(self) -> None:
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.calls = 0

    # 首轮结束前等待 followup，第二轮确认纠偏已进入 messages
    async def chat(self, **kwargs: object) -> LlmResponse:
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
            text = "SUMMARY\nold result"
        else:
            messages = kwargs["messages"]
            assert isinstance(messages, list)
            assert "new requirement" in json.dumps(messages)
            text = "SUMMARY\nfollowup applied"
        return LlmResponse(
            stop_reason="end_turn",
            text=text,
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


# 构造共享 registry 的统一 Agent action-family 工具
def _agent(
    tmp_path: Path,
    provider: object,
    registry: BackgroundTaskRegistry | None = None,
    *,
    permission_manager: PermissionManager | None = None,
    bus: EventBus | None = None,
    interaction_manager: InteractionManager | None = None,
) -> tuple[AgentTool, BackgroundTaskRegistry, EventBus]:
    task_registry = registry or BackgroundTaskRegistry(store_path=tmp_path / "workers")
    parent_bus = bus or EventBus()
    spawn = SpawnAgentTool(
        provider=provider,  # type: ignore[arg-type]
        parent_bus=parent_bus,
        parent_run_id="parent-turn",
        permission_manager=permission_manager,
        max_steps=5,
        task_registry=task_registry,
        runs_dir=tmp_path / "runs",
        session_id="session-1",
        workspace_boundary=WorkspaceBoundary(tmp_path),
        interaction_manager=interaction_manager,
    )
    return AgentTool(task_registry, spawn), task_registry, parent_bus


# 从 Agent start 返回消息提取稳定 Worker ID
def _worker_id(content: str) -> str:
    return content.split("worker_id=", 1)[1].split(".", 1)[0]


# 功能：统一 agent 工具只暴露规范要求的六个 action
# 设计：直接检查 ToolSpec action 顺序和名称，避免旧 spawn_agent 成为另一个模型入口
def test_agent_tool_exposes_six_actions(tmp_path: Path) -> None:
    tool, _, _ = _agent(tmp_path, _ResultProvider())
    assert [action.name for action in tool.build_spec().actions] == [
        "start",
        "status",
        "peek",
        "wait",
        "cancel",
        "followup",
    ]


# 功能：第二个 Worker 在 start 前因相同 exact_files claim 被拒绝
# 设计：用阻塞 provider 保持首个 Worker running，再通过统一 agent.start 提交相同写声明
async def test_agent_start_rejects_conflicting_write_claim(tmp_path: Path) -> None:
    provider = _BlockingProvider()
    tool, registry, _ = _agent(tmp_path, provider)
    start = {
        "action": "start",
        "description": "write app",
        "prompt": "update the app",
        "read_only": False,
        "exact_files": ["src/app.py"],
    }
    first = await tool.invoke(start)
    second = await tool.invoke(start)

    assert not first.is_error
    assert second.is_error
    assert second.error_type == "conflict"
    await registry.cancel(_worker_id(first.content))


# 功能：child profile 和执行请求不能提升 parent authority
# 设计：父会话限定为 Plan+READ，启动 executor 写任务后检查持久 authority ceiling 仍为从严交集
async def test_child_authority_is_narrower_than_parent(tmp_path: Path) -> None:
    permissions = PermissionManager()
    permissions.set_authority_snapshot(
        "session-1",
        AuthoritySnapshot(
            mode=RuntimeMode.PLAN,
            profile=AuthorityProfile.ASK,
            allowed_actions=frozenset({ToolAction.READ}),
        ),
    )
    provider = _BlockingProvider()
    tool, registry, _ = _agent(
        tmp_path,
        provider,
        permission_manager=permissions,
    )
    started = await tool.invoke(
        {
            "action": "start",
            "description": "attempt write",
            "prompt": "modify source",
            "subagent_type": "executor",
            "read_only": False,
            "exact_files": ["src/app.py"],
        }
    )
    worker_id = _worker_id(started.content)
    worker = registry.record(worker_id)

    assert worker is not None
    assert worker.authority_ceiling.mode == RuntimeMode.PLAN
    assert worker.authority_ceiling.profile == AuthorityProfile.ASK
    assert worker.authority_ceiling.allowed_actions == frozenset({ToolAction.READ})
    await registry.cancel(worker_id)


# 功能：parent 只获得五段结构化摘要和有界事件，不接收工具参数、输出或完整 transcript
# 设计：provider 主动发布含 secret 的工具事件与超长正文，分别检查 wait、peek 和父 bus 脱敏结果
async def test_agent_returns_structured_result_and_bounded_events(tmp_path: Path) -> None:
    parent_bus = EventBus()
    parent_events: list[object] = []

    # 收集桥接到父 TUI 的事件以验证敏感字段已清空
    async def collect(event: object) -> None:
        parent_events.append(event)

    parent_bus.subscribe(collect)
    tool, _, _ = _agent(tmp_path, _EventProvider(), bus=parent_bus)
    started = await tool.invoke(
        {"action": "start", "description": "inspect", "prompt": "inspect files"}
    )
    worker_id = _worker_id(started.content)
    waited = await tool.invoke(
        {"action": "wait", "worker_id": worker_id, "timeout_s": 5}
    )
    result = json.loads(waited.content)
    peeked = await tool.invoke(
        {"action": "peek", "worker_id": worker_id, "after_cursor": 0}
    )

    assert result["status"] == "completed"
    assert len(result["summary"]) == 4_000
    assert result["changes"] == ["changed a.py"]
    assert result["evidence"] == ["tests passed"]
    assert "messages" not in result
    assert "must-not-leak" not in peeked.content
    started_event = next(
        event for event in parent_events if getattr(event, "type", "") == "tool.call_started"
    )
    finished_event = next(
        event
        for event in parent_events
        if getattr(event, "type", "") == "tool.call_finished"
    )
    assert started_event.params == {}  # type: ignore[attr-defined]
    assert finished_event.output == ""  # type: ignore[attr-defined]


# 功能：usage 达到根 token budget 时 Worker 进入 budget_limited 终态
# 设计：provider 在一次调用中发布 3+2 tokens，预算设为 5，验证取消和持久终态同步发生
async def test_agent_budget_exhaustion_stops_worker(tmp_path: Path) -> None:
    tool, registry, _ = _agent(tmp_path, _BudgetProvider())
    started = await tool.invoke(
        {
            "action": "start",
            "description": "budgeted",
            "prompt": "use bounded tokens",
            "token_budget": 5,
        }
    )
    worker_id = _worker_id(started.content)
    waited = await tool.invoke(
        {"action": "wait", "worker_id": worker_id, "timeout_s": 5}
    )

    worker = registry.record(worker_id)
    assert waited.is_error
    assert worker is not None
    assert worker.status == WorkerStatus.BUDGET_LIMITED
    assert worker.token_usage == 5
    # 预算耗尽时结果常为空，父上下文必须拿到标注 budget_exhausted 的合成收尾回执
    assert "budget_exhausted" in worker.summary


# 功能：followup 在模型调用进行中到达时会触发下一轮决策而不会被旧 end_turn 吞掉
# 设计：首轮 provider 阻塞期间发送 followup，再释放旧响应并断言第二轮收到新指令
async def test_agent_followup_reaches_running_worker(tmp_path: Path) -> None:
    provider = _FollowupProvider()
    parent_bus = EventBus()
    interaction = InteractionManager(parent_bus)
    tool, _, _ = _agent(
        tmp_path,
        provider,
        bus=parent_bus,
        interaction_manager=interaction,
    )
    started = await tool.invoke(
        {"action": "start", "description": "followup", "prompt": "old requirement"}
    )
    worker_id = _worker_id(started.content)
    await asyncio.wait_for(provider.first_started.wait(), timeout=2)

    followup = await tool.invoke(
        {"action": "followup", "worker_id": worker_id, "message": "new requirement"}
    )
    provider.release_first.set()
    waited = await tool.invoke(
        {"action": "wait", "worker_id": worker_id, "timeout_s": 5}
    )

    assert not followup.is_error
    assert provider.calls == 2
    assert json.loads(waited.content)["summary"] == "followup applied"


# 功能：daemon 重启后的 interrupted Worker 可由 agent.start 恢复为下一 attempt
# 设计：复用持久目录切换 boot_id，并把 backoff 设为零后用现有 worker_id 重新启动
async def test_interrupted_worker_can_retry_after_restart(tmp_path: Path) -> None:
    workers = tmp_path / "workers"
    first = BackgroundTaskRegistry(store_path=workers, boot_id="boot-a")
    record = first.new_record(
        worker_id="worker-retry",
        parent_turn_id="old-turn",
        root_goal_id="goal-root",
        description="retry task",
        prompt="finish after restart",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=5,
        retry_backoff_s=0,
    )
    first.create(record)
    second = BackgroundTaskRegistry(store_path=workers, boot_id="boot-b")
    tool, _, _ = _agent(tmp_path, _ResultProvider(), second)

    restarted = await tool.invoke({"action": "start", "worker_id": "worker-retry"})
    waited = await tool.invoke(
        {"action": "wait", "worker_id": "worker-retry", "timeout_s": 5}
    )
    restored = second.record("worker-retry")

    assert not restarted.is_error
    assert not waited.is_error
    assert restored is not None
    assert restored.attempt == 2
    assert restored.status == WorkerStatus.COMPLETED


# 功能：worker.list IPC 视图可按 session 查询且不会泄露持久 prompt
# 设计：直接调用 Core handler 检查 TUI 数据契约，避免把完整 WorkerRecord 暴露给客户端
async def test_worker_list_handler_returns_redacted_status(tmp_path: Path) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    record = registry.new_record(
        worker_id="worker-visible",
        parent_turn_id="turn-1",
        root_goal_id="goal-1",
        session_id="session-visible",
        description="visible worker",
        prompt="private full worker prompt",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=5,
    )
    registry.create(record)
    app = CoreApp()
    app._subagent_registry = registry

    result = await app._worker_list_handler(
        {"session_id": "session-visible", "limit": 10}
    )
    payload = result.model_dump(mode="json")

    assert payload["workers"][0]["worker_id"] == "worker-visible"
    assert "prompt" not in payload["workers"][0]
    assert "private full worker prompt" not in json.dumps(payload)
