from __future__ import annotations

from pydantic import ValidationError

from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.background import (
    BackgroundCancelTool,
    BackgroundInteractTool,
    BackgroundListTool,
    BackgroundResultTool,
    BackgroundStartTool,
)
from code_rook.core.tools.builtin.bash import BashTool as LegacyBashTool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ResourceClaim,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolSpec,
)

_ACTION_ALIASES = {
    "bash": "run",
    "background_start": "run",
    "background_result": "wait",
    "background_interact": "interact",
    "background_cancel": "cancel",
}


class BashTool(BaseTool):
    name = "Bash"
    description = (
        "Execute a shell command in the workspace or manage its background lifecycle. "
        "Use run, wait, interact, or cancel; prefer background for commands over five seconds."
    )
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 绑定前台 shell 和可选 daemon 后台生命周期 backend
    def __init__(
        self,
        shell: LegacyBashTool,
        *,
        start: BackgroundStartTool | None = None,
        result: BackgroundResultTool | None = None,
        interact: BackgroundInteractTool | None = None,
        cancel: BackgroundCancelTool | None = None,
    ) -> None:
        self._shell = shell
        self._start = start
        self._result = result
        self._interact = interact
        self._cancel = cancel

    # 返回当前装配真实支持的 Bash lifecycle action schema
    def build_spec(self) -> ToolSpec:
        process = frozenset({ToolCapability.PROCESS})
        actions = [
            ToolActionSpec(
                name="run",
                description="Run a foreground or background shell command.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 3600 if self._start is not None else 120,
                        },
                        "background": {"type": "boolean"},
                        "session": {
                            "type": "string",
                            "enum": ["isolated", "persistent"],
                            "description": (
                                "persistent reuses a resident shell so cwd/env/venv "
                                "persist across calls; isolated (default) uses a fresh "
                                "process per command."
                            ),
                        },
                    },
                    "required": ["command"],
                },
                capabilities=process,
                approval_requirement=ApprovalRequirement.ALWAYS,
                parallel_policy=ParallelPolicy.SERIAL,
            )
        ]
        if self._result is not None:
            actions.append(
                ToolActionSpec(
                    name="wait",
                    description="Poll or wait for a background shell job.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "timeout": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 120,
                            },
                        },
                        "required": ["job_id"],
                    },
                    capabilities=frozenset({ToolCapability.READ}),
                    approval_requirement=ApprovalRequirement.NEVER,
                    parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
                )
            )
        if self._interact is not None:
            actions.append(
                ToolActionSpec(
                    name="interact",
                    description="Send stdin to a running background shell job.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "job_id": {"type": "string"},
                            "stdin": {"type": "string"},
                            "close_stdin": {"type": "boolean"},
                        },
                        "required": ["job_id"],
                    },
                    capabilities=process,
                    approval_requirement=ApprovalRequirement.ALWAYS,
                    parallel_policy=ParallelPolicy.SERIAL,
                )
            )
        if self._cancel is not None:
            actions.append(
                ToolActionSpec(
                    name="cancel",
                    description="Cancel a running background shell job.",
                    input_schema={
                        "type": "object",
                        "properties": {"job_id": {"type": "string"}},
                        "required": ["job_id"],
                    },
                    capabilities=process,
                    approval_requirement=ApprovalRequirement.ALWAYS,
                    parallel_policy=ParallelPolicy.SERIAL,
                )
            )
        action_tuple = tuple(actions)
        capabilities = frozenset(
            capability
            for action in action_tuple
            for capability in action.capabilities
        )
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            actions=action_tuple,
            capabilities=capabilities,
            approval_requirement=ApprovalRequirement.POLICY,
            parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
        )

    # 分派前台执行和后台 wait/interact/cancel 生命周期
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        action = params.get("action")
        payload = dict(params)
        payload.pop("action", None)
        try:
            if action == "run":
                background = bool(payload.pop("background", False))
                if background:
                    if self._start is None:
                        return ToolResult(
                            "background shell is unavailable in this runtime",
                            is_error=True,
                            error_type="runtime_error",
                        )
                    return await self._start.invoke(payload)
                return await self._shell.invoke(payload)
            if action == "wait" and self._result is not None:
                payload["wait"] = True
                return await self._result.invoke(payload)
            if action == "interact" and self._interact is not None:
                return await self._interact.invoke(payload)
            if action == "cancel" and self._cancel is not None:
                return await self._cancel.invoke(payload)
        except ValidationError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        return ToolResult(
            f"unknown or unavailable Bash action: {action}",
            is_error=True,
            error_type="schema_error",
        )

    # 对后台任务状态声明共享读取或独占控制 claim
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        action = str(params.get("action", ""))
        job_id = str(params.get("job_id", ""))
        if not job_id or action == "run":
            return ()
        return (
            ResourceClaim(
                resource=f"background:{job_id}",
                capability=(
                    ToolCapability.READ
                    if action == "wait"
                    else ToolCapability.PROCESS
                ),
                exclusive=action in {"interact", "cancel"},
            ),
        )


# 注册 Bash family，并把旧 shell/background 名称降级为 internal/replay alias
def register_bash_family(
    registry: ToolRegistry,
    shell: LegacyBashTool,
    *,
    background_registry: BackgroundJobRegistry | None = None,
    session_id: str = "",
    run_id: str = "",
    allowed_names: set[str] | None = None,
) -> BashTool | None:
    tools: list[BaseTool] = [shell]
    start: BackgroundStartTool | None = None
    result: BackgroundResultTool | None = None
    interact: BackgroundInteractTool | None = None
    cancel: BackgroundCancelTool | None = None
    if background_registry is not None:
        start = BackgroundStartTool(background_registry, session_id, run_id)
        result = BackgroundResultTool(background_registry)
        interact = BackgroundInteractTool(background_registry)
        cancel = BackgroundCancelTool(background_registry)
        tools.extend(
            (
                start,
                result,
                BackgroundListTool(background_registry, session_id),
                interact,
                cancel,
            )
        )
    enabled_actions = {
        action
        for alias, action in _ACTION_ALIASES.items()
        if any(tool.name == alias for tool in tools)
        and (
            allowed_names is None
            or "Bash" in allowed_names
            or alias in allowed_names
        )
    }
    if not enabled_actions:
        return None
    for tool in tools:
        if allowed_names is not None and tool.name not in allowed_names:
            continue
        registry.register(
            tool,
            spec=tool.build_spec().model_copy(
                update={
                    "model_visible": False,
                    "allowed_callers": frozenset(
                        {ToolCaller.INTERNAL, ToolCaller.REPLAY}
                    ),
                }
            ),
        )
    family = BashTool(
        shell,
        start=start,
        result=result,
        interact=interact,
        cancel=cancel,
    )
    spec = family.build_spec()
    selected_actions = tuple(
        action for action in spec.actions if action.name in enabled_actions
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
