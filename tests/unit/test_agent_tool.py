from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from code_rook.core.app import CoreApp
from code_rook.core.authority import (
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
)
from code_rook.core.bus.envelope import HandlerError
from code_rook.core.bus.events import (
    LlmUsageEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    VerificationCompletedEvent,
    VerificationFailedEvent,
)
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.subagent.agent import AgentTool
from code_rook.core.subagent.models import WorkerStatus
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import SpawnAgentTool, _WorkerVerificationTracker
from code_rook.core.tools.base import ToolResult
from code_rook.core.workspace import WorkspaceBoundary


# 初始化带基线提交的临时 Git 仓库以覆盖真实 worktree 隔离
def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=CodeRook Test",
            "-c",
            "user.email=coderook@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )


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
                input_tokens=128,
                output_tokens=128,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                ts="2026-08-04T00:00:00+00:00",
            )
        )
        await asyncio.sleep(0)
        return LlmResponse(
            stop_reason="end_turn",
            text="SUMMARY\nshould be cancelled",
            usage=UsageStats(input_tokens=128, output_tokens=128),
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


class _WritingProvider:
    # 初始化写入动作与最终 handoff 的两轮确定性响应
    def __init__(self, path: str = "worker.txt") -> None:
        self.calls = 0
        self.path = path

    # 首轮调用 File.write，次轮报告证据但由后端 Git 检查决定真实 handoff
    async def chat(self, **_kwargs: object) -> LlmResponse:
        self.calls += 1
        if self.calls == 1:
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCallBlock(
                        id="write-1",
                        name="File",
                        input={
                            "action": "write",
                            "path": self.path,
                            "content": "isolated change\n",
                        },
                    )
                ],
                usage=UsageStats(input_tokens=1, output_tokens=1),
            )
        return LlmResponse(
            stop_reason="end_turn",
            text="SUMMARY\ndone\nEVIDENCE\n- reported test command",
            usage=UsageStats(input_tokens=1, output_tokens=1),
        )


# 功能：Worker 只用类型化 daemon 验证事件升级 verified，且任一失败永久优先
# 设计：依次注入 completed 与 failed 事件，固定聚合器的 fail-closed 状态和脱敏 receipt 来源
def test_worker_verification_tracker_is_typed_and_fail_closed() -> None:
    tracker = _WorkerVerificationTracker("worker-verified")
    tracker.record(
        VerificationCompletedEvent(
            run_id="worker-verified",
            step=1,
            tool="Run",
            action="tests",
            gate_count=1,
            passed=1,
            failed=0,
            paths=["worker.txt"],
            gates=[{"name": "pytest", "status": "passed"}],
            ts="2026-08-24T00:00:00+00:00",
        )
    )
    assert tracker.status() == "verified"

    tracker.record(
        VerificationFailedEvent(
            run_id="worker-verified",
            step=2,
            tool="Run",
            action="tests",
            gate_count=1,
            passed=0,
            failed=1,
            failure_class="runtime_error",
            paths=["worker.txt"],
            gates=[{"name": "pytest", "status": "failed"}],
            ts="2026-08-24T00:00:01+00:00",
        )
    )
    assert tracker.status() == "failed"
    assert tracker.receipt()["verification"]["source"] == "daemon_tool_events"


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


# 先生成单任务委派计划票据，再用票据启动 Worker 以复用生产约束
async def _start_with_ticket(
    tool: AgentTool,
    params: dict[str, object],
    *,
    task_id: str = "task",
) -> ToolResult:
    read_only = bool(params.get("read_only", True))
    token_budget = max(256, int(params.get("token_budget", 256) or 256))
    validated = await tool.invoke(
        {
            "action": "validate_plan",
            "tasks": [
                {
                    "id": task_id,
                    "role": str(params.get("subagent_type") or params.get("description") or "worker"),
                    "prompt": str(params.get("prompt") or "complete task"),
                    "write_claim": {
                        "read_only": read_only,
                        "exact_files": list(params.get("exact_files", [])),
                        "write_roots": list(params.get("write_roots", [])),
                        "coordination_contract": str(
                            params.get("coordination_contract", "")
                        ),
                    },
                    "acceptance": list(params.get("acceptance", ["task completed"])),
                    "token_budget": token_budget,
                    "wall_time_s": int(params.get("wall_time_s", 900) or 900),
                }
            ],
            "total_token_budget": token_budget,
        }
    )
    assert not validated.is_error
    ticket = str(json.loads(validated.content)["plan_ticket"])
    return await tool.invoke(
        {"action": "start", "plan_ticket": ticket, "task_id": task_id}
    )


# 功能：统一 agent 工具暴露计划校验、start/retry 与完整控制 action
# 设计：直接检查 ToolSpec action 顺序和名称，确保委派必须先经过确定性计划校验入口
def test_agent_tool_exposes_control_actions(tmp_path: Path) -> None:
    tool, _, _ = _agent(tmp_path, _ResultProvider())
    assert [action.name for action in tool.build_spec().actions] == [
        "validate_plan",
        "start",
        "retry",
        "status",
        "peek",
        "wait",
        "cancel",
        "followup",
    ]


# 功能：委派计划 schema 向模型完整公开任务、验收条件和 Write Claim 字段
# 设计：直接检查 validate_plan 的嵌套 JSON Schema，防止模型只能靠失败重试猜参数结构
def test_agent_plan_schema_describes_nested_task_contract(tmp_path: Path) -> None:
    tool, _, _ = _agent(tmp_path, _ResultProvider())
    action = tool.build_spec().action("validate_plan")

    assert action is not None
    schema = action.input_schema
    assert schema is not None
    task_schema = schema["properties"]["tasks"]["items"]  # type: ignore[index]
    assert set(task_schema["required"]) == {
        "id",
        "role",
        "prompt",
        "write_claim",
        "acceptance",
        "token_budget",
    }
    claim_schema = task_schema["properties"]["write_claim"]
    assert set(claim_schema["properties"]) == {
        "read_only",
        "exact_files",
        "write_roots",
        "coordination_contract",
    }


# 功能：验证模型不能跳过 Delegation Plan 票据直接启动 Worker
# 设计：对 start 提交完整旧参数但不提供票据，断言在创建进程和 worktree 前失败关闭
async def test_agent_start_requires_delegation_ticket(tmp_path: Path) -> None:
    tool, registry, _ = _agent(tmp_path, _ResultProvider())

    result = await tool.invoke(
        {"action": "start", "description": "bypass", "prompt": "run directly"}
    )

    assert result.is_error
    assert result.error_type == "permission_required"
    assert registry.list_records() == []


# 功能：两个可写 Worker 自动进入不同受管 worktree，不能直接共享主工作区
# 设计：在真实 Git 仓库并发启动相同 claim，断言后端自动分配不同隔离目录
async def test_agent_start_isolates_writers_in_distinct_worktrees(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    provider = _BlockingProvider()
    tool, registry, _ = _agent(tmp_path, provider)
    start = {
        "action": "start",
        "description": "write app",
        "prompt": "update the app",
        "read_only": False,
        "exact_files": ["src/app.py"],
    }
    first = await _start_with_ticket(tool, start, task_id="first")
    second = await _start_with_ticket(tool, start, task_id="second")

    assert not first.is_error
    assert not second.is_error
    first_worker = registry.record(_worker_id(first.content))
    second_worker = registry.record(_worker_id(second.content))
    assert first_worker is not None
    assert second_worker is not None
    assert first_worker.worktree != second_worker.worktree
    assert Path(first_worker.workspace).is_relative_to(tmp_path / ".coderook" / "worktrees")
    assert Path(second_worker.workspace).is_relative_to(tmp_path / ".coderook" / "worktrees")
    await registry.cancel(first_worker.id)
    await registry.cancel(second_worker.id)


# 功能：可写 Worker 的真实文件只落入受管 worktree，完成后产生待审查 Git handoff
# 设计：让模型真实调用 File.write，再断言主工作区不变且状态来自 Git 而非模型自报 CHANGES
async def test_writing_worker_returns_authoritative_pending_handoff(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    tool, registry, _ = _agent(tmp_path, _WritingProvider())
    started = await _start_with_ticket(
        tool,
        {
            "action": "start",
            "description": "write isolated file",
            "prompt": "create worker.txt",
            "read_only": False,
            "exact_files": ["worker.txt"],
        }
    )
    worker_id = _worker_id(started.content)
    waited = await tool.invoke(
        {"action": "wait", "worker_id": worker_id, "timeout_s": 5}
    )
    worker = registry.record(worker_id)

    assert not waited.is_error
    assert worker is not None
    assert not (tmp_path / "worker.txt").exists()
    assert (Path(worker.workspace) / "worker.txt").read_text(encoding="utf-8") == (
        "isolated change\n"
    )
    assert worker.handoff_status == "pending_review"
    assert worker.changed_files == ["worker.txt"]
    assert "Untracked: worker.txt" in worker.diff_preview
    assert worker.verification_status == "not_reported"
    assert worker.approved is None


# 功能：可写 Worker 越出 exact_files claim 时 handoff 必须 fail closed
# 设计：模型真实写入未声明文件，保留隔离 worktree 证据但阻止进入待应用审查状态
async def test_writing_worker_handoff_blocks_write_claim_violation(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    tool, registry, _ = _agent(tmp_path, _WritingProvider("outside.txt"))
    started = await _start_with_ticket(
        tool,
        {
            "action": "start",
            "description": "attempt claim escape",
            "prompt": "write outside the declared claim",
            "read_only": False,
            "exact_files": ["allowed.txt"],
        }
    )
    worker_id = _worker_id(started.content)
    await tool.invoke({"action": "wait", "worker_id": worker_id, "timeout_s": 5})
    worker = registry.record(worker_id)

    assert worker is not None
    assert worker.changed_files == ["outside.txt"]
    assert worker.handoff_status == "blocked_claim_violation"
    assert any("outside.txt" in blocker for blocker in worker.blockers)
    assert worker.approved is None


# 功能：child profile 和执行请求不能提升 parent authority
# 设计：父会话限定为 Plan+READ，启动 executor 写任务后检查持久 authority ceiling 仍为从严交集
async def test_child_authority_is_narrower_than_parent(tmp_path: Path) -> None:
    permissions = PermissionManager()
    frozen = AuthoritySnapshot(
        mode=RuntimeMode.PLAN,
        profile=AuthorityProfile.ASK,
        allowed_actions=frozenset({ToolAction.READ}),
    )
    permissions.set_authority_snapshot("session-1", frozen)
    permissions.begin_turn("session-1", frozen)
    permissions.set_authority_snapshot(
        "session-1",
        AuthoritySnapshot(profile=AuthorityProfile.FULL_ACCESS),
    )
    provider = _BlockingProvider()
    tool, registry, _ = _agent(
        tmp_path,
        provider,
        permission_manager=permissions,
    )
    started = await _start_with_ticket(
        tool,
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
    permissions.end_turn("session-1")


# 功能：READ-only 子 Agent 即使模型伪造 File.write 也不能修改父工作区
# 设计：父 Turn 冻结 Plan 权限后让子模型调用写 action，目录裁剪应先于执行并保持文件不存在
async def test_child_registry_denies_hidden_mutation_under_read_ceiling(
    tmp_path: Path,
) -> None:
    permissions = PermissionManager()
    frozen = AuthoritySnapshot(
        mode=RuntimeMode.PLAN,
        allowed_actions=frozenset({ToolAction.READ}),
    )
    permissions.set_authority_snapshot("session-1", frozen)
    permissions.begin_turn("session-1", frozen)
    tool, registry, _ = _agent(
        tmp_path,
        _WritingProvider("must-not-exist.txt"),
        permission_manager=permissions,
    )

    started = await _start_with_ticket(
        tool,
        {
            "action": "start",
            "description": "attempt hidden write",
            "prompt": "try to mutate the workspace",
            "read_only": False,
            "exact_files": ["must-not-exist.txt"],
        }
    )
    worker_id = _worker_id(started.content)
    await tool.invoke({"action": "wait", "worker_id": worker_id, "timeout_s": 5})
    worker = registry.record(worker_id)

    assert worker is not None
    assert worker.authority_ceiling.allowed_actions == frozenset({ToolAction.READ})
    assert not (tmp_path / "must-not-exist.txt").exists()
    permissions.end_turn("session-1")


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
    started = await _start_with_ticket(
        tool,
        {"action": "start", "description": "inspect", "prompt": "inspect files"},
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
    started = await _start_with_ticket(
        tool,
        {
            "action": "start",
            "description": "budgeted",
            "prompt": "use bounded tokens",
            "token_budget": 256,
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
    assert worker.token_usage == 256
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
    started = await _start_with_ticket(
        tool,
        {"action": "start", "description": "followup", "prompt": "old requirement"},
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


# 功能：daemon 重启后的 interrupted Worker 可由显式 agent.retry 恢复为下一 attempt
# 设计：复用持久目录切换 boot_id，并把 backoff 设为零后用原 worker_id 重试
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

    restarted = await tool.invoke(
        {
            "action": "retry",
            "worker_id": "worker-retry",
            "read_only": False,
            "write_roots": ["."],
            "merge_owner": "attacker",
            "merge_reviewer": "attacker",
        }
    )
    waited = await tool.invoke(
        {"action": "wait", "worker_id": "worker-retry", "timeout_s": 5}
    )
    restored = second.record("worker-retry")

    assert not restarted.is_error
    assert not waited.is_error
    assert restored is not None
    assert restored.attempt == 2
    assert restored.status == WorkerStatus.COMPLETED
    assert restored.write_claim.read_only is True
    assert restored.worktree == ""


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


# 功能：Worker 控制面的读取、指令、审查与取消全部拒绝跨会话记录
# 设计：对同一持久 Worker 逐一调用五个真实 handler，断言在任何 registry 操作前统一失败
async def test_worker_control_handlers_reject_cross_session_access(
    tmp_path: Path,
) -> None:
    registry = BackgroundTaskRegistry(store_path=tmp_path / "workers")
    record = registry.new_record(
        worker_id="worker-private",
        parent_turn_id="turn-private",
        root_goal_id="goal-private",
        session_id="session-owner",
        description="private worker",
        prompt="private prompt",
        workspace=str(tmp_path),
        authority_ceiling=AuthoritySnapshot(),
        depth=1,
        max_steps=5,
    )
    registry.create(record)
    app = CoreApp()
    app._subagent_registry = registry

    listed = await app._worker_list_handler(
        {"session_id": "session-other", "limit": 10}
    )
    assert listed.workers == []

    calls = (
        (
            app._worker_list_handler,
            {
                "session_id": "session-other",
                "worker_id": record.id,
                "limit": 10,
            },
        ),
        (
            app._worker_events_handler,
            {"session_id": "session-other", "worker_id": record.id},
        ),
        (
            app._worker_followup_handler,
            {
                "session_id": "session-other",
                "worker_id": record.id,
                "message": "steal context",
            },
        ),
        (
            app._worker_review_handler,
            {
                "session_id": "session-other",
                "worker_id": record.id,
                "approved": False,
            },
        ),
        (
            app._worker_cancel_handler,
            {"session_id": "session-other", "worker_id": record.id},
        ),
    )

    for handler, params in calls:
        with pytest.raises(HandlerError, match="not found for session"):
            await handler(params)

    untouched = registry.record(record.id)
    assert untouched is not None
    assert untouched.status == WorkerStatus.QUEUED
    assert untouched.event_cursor == 0
