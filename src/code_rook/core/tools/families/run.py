from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.bash import BashTool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolSpec,
)

_GATE_OUTPUT_LIMIT = 16_000
_MAX_GATES = 8


class _TestsParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=120)


class _VerifierCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1)
    timeout: int = Field(default=120, ge=1, le=120)


class _VerifiersParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    commands: list[_VerifierCommand] = Field(min_length=1, max_length=_MAX_GATES)


class RunTestsTool(BaseTool):
    name = "run_tests"
    description = "Run one focused project test command and return bounded output."
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
        },
        "required": ["command"],
    }

    # 绑定固定 workspace shell backend
    def __init__(self, shell: BashTool) -> None:
        self._shell = shell

    # 执行单个测试命令并保留现有 Bash 超时和输出边界
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = _TestsParams.model_validate(params)
        return await self._shell.invoke(
            {"command": request.command, "timeout": request.timeout}
        )


class RunVerifiersTool(BaseTool):
    name = "run_verifiers"
    description = (
        "Run up to eight independent project verification commands concurrently and return "
        "one structured verdict."
    )
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "commands": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_GATES,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "command": {"type": "string"},
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 120,
                        },
                    },
                    "required": ["name", "command"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["commands"],
    }

    # 绑定固定 workspace shell backend
    def __init__(self, shell: BashTool) -> None:
        self._shell = shell

    # 执行一个 verifier 并转换为有界结构化 gate 结果
    async def _run_gate(self, command: _VerifierCommand) -> dict[str, object]:
        started = asyncio.get_running_loop().time()
        result = await self._shell.invoke(
            {"command": command.command, "timeout": command.timeout}
        )
        elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        output = result.content
        truncated = len(output) > _GATE_OUTPUT_LIMIT
        if truncated:
            output = output[:_GATE_OUTPUT_LIMIT] + "\n[gate output truncated]"
        return {
            "name": command.name,
            "command": command.command,
            "status": "failed" if result.is_error else "passed",
            "duration_ms": elapsed_ms,
            "output": output,
            "output_truncated": truncated,
        }

    # 并行执行独立 verifier 并汇总明确的 pass/fail verdict
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = _VerifiersParams.model_validate(params)
        gates = await asyncio.gather(
            *(self._run_gate(command) for command in request.commands)
        )
        failed = sum(gate["status"] == "failed" for gate in gates)
        payload = {
            "success": failed == 0,
            "verdict": "pass" if failed == 0 else "fail",
            "gate_count": len(gates),
            "passed": len(gates) - failed,
            "failed": failed,
            "gates": gates,
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False, indent=2),
            is_error=failed > 0,
            error_type="runtime_error" if failed > 0 else None,
        )


class RunTool(BaseTool):
    name = "Run"
    description = (
        "Run focused tests or a bounded set of independent verifier commands. "
        "Choose tests for one test command and verifiers for a parallel quality gate."
    )
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 绑定 tests 与 verifiers 两个隐藏兼容 backend
    def __init__(self, tests: RunTestsTool, verifiers: RunVerifiersTool) -> None:
        self._backends: dict[str, BaseTool] = {
            "tests": tests,
            "verifiers": verifiers,
        }

    # 返回 Run family 的 action 级执行能力声明
    def build_spec(self) -> ToolSpec:
        capability = frozenset({ToolCapability.PROCESS})
        actions = (
            ToolActionSpec(
                name="tests",
                description=self._backends["tests"].description,
                input_schema=self._backends["tests"].input_schema,
                capabilities=capability,
                approval_requirement=ApprovalRequirement.ALWAYS,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
            ToolActionSpec(
                name="verifiers",
                description=self._backends["verifiers"].description,
                input_schema=self._backends["verifiers"].input_schema,
                capabilities=capability,
                approval_requirement=ApprovalRequirement.ALWAYS,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=actions,
            capabilities=capability,
            approval_requirement=ApprovalRequirement.POLICY,
            parallel_policy=ParallelPolicy.SERIAL,
        )

    # 校验 action 后把调用分派到对应隐藏 backend
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = params.get("action")
        if not isinstance(action, str) or action not in self._backends:
            return ToolResult(
                f"unknown Run action: {action}",
                is_error=True,
                error_type="schema_error",
            )
        payload = dict(params)
        payload.pop("action", None)
        try:
            return await self._backends[action].invoke(payload)
        except ValidationError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")

    # 将 Run action 解析为对应 verifier backend，统一执行其校验和超时策略
    def execution_target(
        self,
        params: dict[str, object],
    ) -> tuple[BaseTool, dict[str, object]]:
        action = params.get("action")
        if not isinstance(action, str) or action not in self._backends:
            return self, dict(params)
        payload = dict(params)
        payload.pop("action", None)
        return self._backends[action], payload


# 注册 Run family，并把旧测试工具限制为 internal/replay alias
def register_run_family(
    registry: ToolRegistry,
    shell: BashTool,
    *,
    allowed_names: set[str] | None = None,
) -> RunTool | None:
    tests_backend = RunTestsTool(shell)
    verifiers_backend = RunVerifiersTool(shell)
    aliases: dict[str, BaseTool] = {
        "tests": tests_backend,
        "verifiers": verifiers_backend,
    }
    enabled = {
        action: backend
        for action, backend in aliases.items()
        if allowed_names is None
        or "Run" in allowed_names
        or backend.name in allowed_names
    }
    if not enabled:
        return None
    for backend in aliases.values():
        if allowed_names is not None and backend.name not in allowed_names:
            continue
        registry.register(
            backend,
            spec=backend.build_spec().model_copy(
                update={
                    "model_visible": False,
                    "allowed_callers": frozenset(
                        {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                    ),
                }
            ),
        )
    family = RunTool(tests_backend, verifiers_backend)
    spec = family.build_spec()
    selected_actions = tuple(
        action for action in spec.actions if action.name in enabled
    )
    registry.register(
        family,
        spec=spec.model_copy(
            update={
                "actions": selected_actions,
                "capabilities": frozenset(
                    capability
                    for action in selected_actions
                    for capability in action.capabilities
                ),
            }
        ),
    )
    return family
