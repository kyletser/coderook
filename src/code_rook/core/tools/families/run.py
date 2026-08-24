from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from code_rook.core.repository import (
    TestCommandCandidate,
    command_candidate_id,
    discover_test_commands,
    render_test_command,
)
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
from code_rook.core.workspace import WorkspaceBoundary

_GATE_OUTPUT_LIMIT = 16_000
_MAX_GATES = 8


class _TestsParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str = Field(min_length=1)
    candidate_id: str = Field(default="", max_length=64)
    timeout: int = Field(default=120, ge=1, le=120)


class _VerifierCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1)
    candidate_id: str = Field(default="", max_length=64)
    timeout: int = Field(default=120, ge=1, le=120)


class _VerifiersParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    commands: list[_VerifierCommand] = Field(min_length=1, max_length=_MAX_GATES)


# 把发现候选建立为稳定 ID 索引，供执行结果判断是否具备验证资格
def _candidate_index(
    candidates: tuple[TestCommandCandidate, ...],
) -> dict[str, TestCommandCandidate]:
    return {command_candidate_id(candidate): candidate for candidate in candidates}


# 在执行前后重新发现 manifest 候选，避免编辑后的旧目录快照继续获得验证资格
async def _live_candidate_index(
    boundary: WorkspaceBoundary | None,
    fallback: dict[str, TestCommandCandidate],
    *,
    refresh: bool,
) -> dict[str, TestCommandCandidate]:
    if boundary is None or not refresh:
        return fallback
    discovery = await asyncio.to_thread(discover_test_commands, boundary)
    return _candidate_index(discovery.candidates)


# 只在候选 ID 和 daemon 渲染命令都完全匹配时返回可信验证元数据
def _verification_metadata(
    candidates: dict[str, TestCommandCandidate],
    *,
    candidate_id: str,
    command: str,
) -> dict[str, Any]:
    candidate = candidates.get(candidate_id) if candidate_id else None
    if candidate is None:
        return {
            "verification_eligible": False,
            "verification_reason": (
                "missing_candidate_id" if not candidate_id else "unknown_candidate_id"
            ),
            "candidate_id": candidate_id,
            "source": "",
            "cwd": "",
        }
    expected = render_test_command(candidate)
    if command != expected:
        return {
            "verification_eligible": False,
            "verification_reason": "candidate_command_mismatch",
            "candidate_id": candidate_id,
            "source": candidate.source,
            "cwd": candidate.cwd,
        }
    return {
        "verification_eligible": True,
        "verification_reason": "manifest_declared_candidate",
        "candidate_id": candidate_id,
        "source": candidate.source,
        "cwd": candidate.cwd,
    }


# 将 shell 输出限制为验证事件可安全承载的长度并返回截断标记
def _bounded_gate_output(output: str) -> tuple[str, bool]:
    truncated = len(output) > _GATE_OUTPUT_LIMIT
    if truncated:
        return output[:_GATE_OUTPUT_LIMIT] + "\n[gate output truncated]", True
    return output, False


class RunTestsTool(BaseTool):
    name = "run_tests"
    description = "Run one focused project test command and return bounded output."
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "candidate_id": {
                "type": "string",
                "description": "ID returned by Repository.test_commands for verified evidence.",
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
        },
        "required": ["command"],
    }

    # 绑定固定 workspace shell backend 与 daemon 发现的项目测试候选
    def __init__(
        self,
        shell: BashTool,
        verification_candidates: tuple[TestCommandCandidate, ...] = (),
        verification_boundary: WorkspaceBoundary | None = None,
    ) -> None:
        self._shell = shell
        self._candidates = _candidate_index(verification_candidates)
        self._verification_boundary = verification_boundary

    # 执行单个测试命令并把项目候选资格写入不可由模型直接控制的结构化结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = _TestsParams.model_validate(params)
        candidates = await _live_candidate_index(
            self._verification_boundary,
            self._candidates,
            refresh=bool(request.candidate_id),
        )
        metadata = _verification_metadata(
            candidates,
            candidate_id=request.candidate_id,
            command=request.command,
        )
        result = await self._shell.invoke(
            {"command": request.command, "timeout": request.timeout}
        )
        if metadata["verification_eligible"] is True:
            candidates_after = await _live_candidate_index(
                self._verification_boundary,
                self._candidates,
                refresh=True,
            )
            after = _verification_metadata(
                candidates_after,
                candidate_id=request.candidate_id,
                command=request.command,
            )
            if after["verification_eligible"] is not True:
                metadata = {
                    **after,
                    "verification_reason": "candidate_changed_during_execution",
                }
        output, truncated = _bounded_gate_output(result.content)
        failed = int(result.is_error)
        payload = {
            "success": not result.is_error,
            "verdict": "fail" if result.is_error else "pass",
            "gate_count": 1,
            "passed": 1 - failed,
            "failed": failed,
            "verification_eligible": bool(metadata["verification_eligible"]),
            "verification_source": "manifest_declared",
            "verification_reason": metadata["verification_reason"],
            "gates": [
                {
                    "name": "project-tests",
                    "command": request.command,
                    "status": "failed" if result.is_error else "passed",
                    "duration_ms": 0,
                    "output": output,
                    "output_truncated": truncated,
                    **metadata,
                }
            ],
        }
        return ToolResult(
            json.dumps(payload, ensure_ascii=False, indent=2),
            is_error=result.is_error,
            error_type=result.error_type,
            process_usage=result.process_usage,
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
                        "candidate_id": {
                            "type": "string",
                            "description": (
                                "ID returned by Repository.test_commands for verified evidence."
                            ),
                        },
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

    # 绑定固定 workspace shell backend 与 daemon 发现的项目测试候选
    def __init__(
        self,
        shell: BashTool,
        verification_candidates: tuple[TestCommandCandidate, ...] = (),
        verification_boundary: WorkspaceBoundary | None = None,
    ) -> None:
        self._shell = shell
        self._candidates = _candidate_index(verification_candidates)
        self._verification_boundary = verification_boundary

    # 执行一个 verifier 并转换为有界结构化 gate 结果
    async def _run_gate(
        self,
        command: _VerifierCommand,
        candidates: dict[str, TestCommandCandidate],
    ) -> dict[str, object]:
        started = asyncio.get_running_loop().time()
        metadata = _verification_metadata(
            candidates,
            candidate_id=command.candidate_id,
            command=command.command,
        )
        result = await self._shell.invoke(
            {"command": command.command, "timeout": command.timeout}
        )
        elapsed_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        output, truncated = _bounded_gate_output(result.content)
        return {
            "name": command.name,
            "command": command.command,
            "status": "failed" if result.is_error else "passed",
            "duration_ms": elapsed_ms,
            "output": output,
            "output_truncated": truncated,
            **metadata,
        }

    # 并行执行独立 verifier 并汇总明确的 pass/fail verdict
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        request = _VerifiersParams.model_validate(params)
        refresh = any(command.candidate_id for command in request.commands)
        candidates = await _live_candidate_index(
            self._verification_boundary,
            self._candidates,
            refresh=refresh,
        )
        gates = await asyncio.gather(
            *(self._run_gate(command, candidates) for command in request.commands)
        )
        if any(gate["verification_eligible"] is True for gate in gates):
            candidates_after = await _live_candidate_index(
                self._verification_boundary,
                self._candidates,
                refresh=True,
            )
            for gate, command in zip(gates, request.commands, strict=True):
                if gate["verification_eligible"] is not True:
                    continue
                after = _verification_metadata(
                    candidates_after,
                    candidate_id=command.candidate_id,
                    command=command.command,
                )
                if after["verification_eligible"] is not True:
                    gate.update(after)
                    gate["verification_reason"] = "candidate_changed_during_execution"
        failed = sum(gate["status"] == "failed" for gate in gates)
        verification_eligible = all(
            gate["verification_eligible"] is True for gate in gates
        )
        payload = {
            "success": failed == 0,
            "verdict": "pass" if failed == 0 else "fail",
            "gate_count": len(gates),
            "passed": len(gates) - failed,
            "failed": failed,
            "verification_eligible": verification_eligible,
            "verification_source": "manifest_declared",
            "verification_reason": (
                "manifest_declared_candidates"
                if verification_eligible
                else "one_or_more_untrusted_commands"
            ),
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
                permission_policy_aliases=("run_tests",),
                approval_requirement=ApprovalRequirement.ALWAYS,
                parallel_policy=ParallelPolicy.SERIAL,
            ),
            ToolActionSpec(
                name="verifiers",
                description=self._backends["verifiers"].description,
                input_schema=self._backends["verifiers"].input_schema,
                capabilities=capability,
                permission_policy_aliases=("run_verifiers",),
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
    verification_candidates: tuple[TestCommandCandidate, ...] = (),
    verification_boundary: WorkspaceBoundary | None = None,
) -> RunTool | None:
    tests_backend = RunTestsTool(
        shell,
        verification_candidates,
        verification_boundary,
    )
    verifiers_backend = RunVerifiersTool(
        shell,
        verification_candidates,
        verification_boundary,
    )
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
