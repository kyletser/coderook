from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ToolActionSpec,
    ToolCapability,
    ToolSpec,
)


class _RunProvider:
    # 初始化先调用一次 verifier 再正常结束的响应序列
    def __init__(self) -> None:
        self._responses = iter(
            [
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id="verify-1",
                            name="Run",
                            input={"action": "verifiers", "commands": []},
                        )
                    ],
                ),
                LlmResponse(stop_reason="end_turn", text="done"),
            ]
        )

    # 返回下一条固定模型响应
    async def chat(self, *args: object, **kwargs: object) -> LlmResponse:
        return next(self._responses)


class _VerificationTool(BaseTool):
    name = "Run"
    description = "Return one controlled verifier verdict"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 初始化固定验证结果
    def __init__(self, result: ToolResult) -> None:
        self._result = result

    # 声明可由模型调用的 verifiers action
    def build_spec(self) -> ToolSpec:
        action = ToolActionSpec(
            name="verifiers",
            description=self.description,
            capabilities=frozenset({ToolCapability.PROCESS}),
            approval_requirement=ApprovalRequirement.ALWAYS,
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=(action,),
            capabilities=frozenset({ToolCapability.PROCESS}),
        )

    # 返回注入的结构化 verdict
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return self._result


@pytest.mark.parametrize(
    ("is_error", "event_type", "verdict"),
    [
        (False, "verification.completed", "pass"),
        (True, "verification.failed", "fail"),
    ],
)
# 功能：验证 Run verdict 自动生成类型化成功或失败事件并关联编辑路径
# 设计：通过完整 AgentLoop 工具路径覆盖事件发布，参数化成功/失败避免只测纯解析 helper
async def test_run_verdict_emits_durable_verification_event(
    is_error: bool,
    event_type: str,
    verdict: str,
) -> None:
    payload = {
        "verdict": verdict,
        "gate_count": 1,
        "passed": 0 if is_error else 1,
        "failed": 1 if is_error else 0,
        "verification_eligible": True,
        "verification_source": "manifest_declared",
        "gates": [
            {
                "name": "unit",
                "status": "failed" if is_error else "passed",
                "duration_ms": 12,
                "output": "must not enter verification receipt",
                "output_truncated": False,
            }
        ],
    }
    result = ToolResult(
        json.dumps(payload),
        is_error=is_error,
        error_type="runtime_error" if is_error else None,
    )
    registry = ToolRegistry()
    registry.register(_VerificationTool(result))
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集完整 loop 事件以验证验证事件的持久化形状
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    context = ExecutionContext(run_id="verify-run", goal="verify", max_steps=3)
    context.working_set.touch("src/app.py", "edit", step=0)

    await AgentLoop(_RunProvider(), registry, bus).run(context)  # type: ignore[arg-type]

    verification = next(
        event for event in events if event.type == event_type  # type: ignore[attr-defined]
    )
    assert context.status == "success"
    assert verification.verdict == verdict  # type: ignore[attr-defined]
    assert verification.paths == ["src/app.py"]  # type: ignore[attr-defined]
    assert verification.gates == [  # type: ignore[attr-defined]
        {
            "name": "unit",
            "command": "",
            "status": "failed" if is_error else "passed",
            "duration_ms": 12,
            "output_truncated": False,
            "candidate_id": "",
            "source": "",
            "verification_eligible": False,
        }
    ]


# 功能：验证缺少 daemon 候选资格的成功命令只能产生失败验证事件
# 设计：让伪 Run 返回 exit-success 形状但不带资格标记，断言 loop fail closed 而非据此认证 Worker
async def test_arbitrary_success_command_is_not_daemon_verification() -> None:
    payload = {
        "verdict": "pass",
        "gate_count": 1,
        "passed": 1,
        "failed": 0,
        "gates": [{"name": "ship-it", "command": "echo ok", "status": "passed"}],
    }
    registry = ToolRegistry()
    registry.register(_VerificationTool(ToolResult(json.dumps(payload))))
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集 loop 发布的验证事件以断言成功 shell 结果没有升级为可信证据
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    await AgentLoop(_RunProvider(), registry, bus).run(
        ExecutionContext(run_id="untrusted-run", goal="verify", max_steps=3)
    )  # type: ignore[arg-type]

    failed = next(event for event in events if event.type == "verification.failed")  # type: ignore[attr-defined]
    assert failed.failure_class == "untrusted_verification_command"  # type: ignore[attr-defined]
    assert not any(event.type == "verification.completed" for event in events)  # type: ignore[attr-defined]
