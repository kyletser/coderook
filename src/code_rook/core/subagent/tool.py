from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from code_rook.core.agents.loader import AgentProfile, AgentProfileLoader
from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import RuntimeMode
from code_rook.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.events.writer import EventWriter
from code_rook.core.loop import AgentLoop
from code_rook.core.lsp import PythonDiagnosticsClient
from code_rook.core.prompt_context import build_capability_context, build_runtime_context
from code_rook.core.runs import new_run_id
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.artifact import ArtifactReadTool
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.apply_patch import ApplyPatchTool
from code_rook.core.tools.builtin.bash import BashTool
from code_rook.core.tools.builtin.checkpoint import (
    CheckpointListTool,
    CheckpointRewindTool,
)
from code_rook.core.tools.builtin.edit_file import EditFileTool
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.tools.builtin.glob import GlobTool
from code_rook.core.tools.builtin.grep import GrepTool
from code_rook.core.tools.builtin.list_dir import ListDirTool
from code_rook.core.tools.builtin.read_file import ReadFileTool
from code_rook.core.tools.builtin.skill import SkillTool
from code_rook.core.tools.builtin.task_claim import TaskClaimTool
from code_rook.core.tools.builtin.task_create import TaskCreateTool
from code_rook.core.tools.builtin.task_get import TaskGetTool
from code_rook.core.tools.builtin.task_list import TaskListTool
from code_rook.core.tools.builtin.task_update import TaskUpdateTool
from code_rook.core.tools.builtin.write_file import WriteFileTool
from code_rook.core.tools.discovery import ToolSearchTool
from code_rook.core.tools.families import (
    register_bash_family,
    register_file_family,
    register_git_family,
    register_run_family,
)
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import WorktreeError, WorktreeManager

if TYPE_CHECKING:
    from code_rook.core.llm.base import LLMProvider
    from code_rook.core.permissions.manager import PermissionManager
    from code_rook.core.task.manager import TaskManager

def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""
    worktree: str = ""


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": "Agent role profile (planner/executor/reviewer). Leave empty for default.",  # noqa: E501
            },
            "worktree": {
                "type": "string",
                "description": (
                    "Optional managed worktree name from worktree_create. "
                    "All child file and bash tools are confined to that worktree."
                ),
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大允许嵌套深度为 2
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
        workspace_boundary: WorkspaceBoundary | None = None,
        task_manager: TaskManager | None = None,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_registry = task_registry
        self._runs_dir = runs_dir
        self._session_id = session_id
        self._depth = depth
        self._workspace_boundary = workspace_boundary or WorkspaceBoundary.current()
        self._task_manager = task_manager
        self._profile_loader = AgentProfileLoader(self._workspace_boundary.root)
        self._skill_loader = SkillLoader(self._workspace_boundary.root)
        profiles = self._profile_loader.list_all()
        profile_names = [profile.name for profile in profiles]
        profile_descriptions = "; ".join(
            f"{profile.name}: {' '.join(profile.description.split())}"
            for profile in profiles
        )
        if profile_descriptions:
            self.description += f" Available profiles: {profile_descriptions}"
        subagent_schema: dict[str, object] = {
            "type": "string",
            "description": (
                "Agent role profile selected by matching its description. "
                "Leave empty for the general-purpose default."
            ),
        }
        if profile_names:
            subagent_schema["enum"] = ["", *profile_names]
        instance_schema = copy.deepcopy(type(self).input_schema)
        properties = instance_schema["properties"]
        if isinstance(properties, dict):
            properties["subagent_type"] = subagent_schema
        self.input_schema = instance_schema

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        profile: AgentProfile | None = None
        if p.subagent_type:
            profile = self._profile_loader.load(p.subagent_type)
            if profile is None:
                return ToolResult(
                    content=f"Unknown subagent profile: {p.subagent_type}",
                    is_error=True,
                    error_type="runtime_error",
                )

        child_run_id = new_run_id()
        child_capabilities = build_capability_context(
            self._skill_loader.list_all_skills(),
            self._profile_loader.list_all(),
        )
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            runtime_context=build_runtime_context(self._workspace_boundary.root),
            capability_context=child_capabilities,
            system_prompt_override=profile.system_prompt if profile else None,
        )

        child_boundary = self._workspace_boundary
        if p.worktree:
            try:
                worktree_path = WorktreeManager(
                    self._workspace_boundary.root
                ).path_for(p.worktree)
            except WorktreeError as exc:
                return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
            if not worktree_path.is_dir():
                return ToolResult(
                    content=f"managed worktree not found: {p.worktree}",
                    is_error=True,
                    error_type="runtime_error",
                )
            child_boundary = WorkspaceBoundary(worktree_path)
            child_context.project_context = (
                f"You are isolated in Git worktree '{p.worktree}' at {worktree_path}. "
                "All file and shell operations must remain in this worktree."
            )
            child_context.runtime_context = build_runtime_context(child_boundary.root)
            child_context.capability_context = build_capability_context(
                SkillLoader(child_boundary.root).list_all_skills(),
                AgentProfileLoader(child_boundary.root).list_all(),
            )

        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        # 同 run 内所有 child 共享 TaskManager，或 fallback 新建独立实例
        task_manager = self._task_manager or TaskManager(
            self._runs_dir / child_run_id / ".tasks"
        )
        child_registry = self._build_child_registry(
            child_bus,
            child_run_id,
            profile,
            child_boundary,
            task_manager,
        )
        # 若 profile 指定 model，包装 provider 在每次 chat 时注入 model kwarg
        child_provider = self._provider
        if profile and profile.model:
            _parent_provider = self._provider
            _child_model = profile.model

            class _ModelOverrideProvider:
                async def chat(self, *a: Any, **kw: Any) -> Any:
                    return await _parent_provider.chat(*a, model=_child_model, **kw)

            child_provider = _ModelOverrideProvider()

        child_loop = AgentLoop(
            child_provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            todo_state=task_manager,
            artifact_store=ArtifactStore(
                child_boundary.root / ".coderook" / "artifacts"
            ),
            diagnostics_client=PythonDiagnosticsClient(child_boundary),
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)

        if p.run_in_background:
            task: asyncio.Task[None] = asyncio.create_task(
                self._run_background(
                    child_loop, child_context, child_bus, child_run_path, child_run_id
                )
            )
            self._task_registry.register(
                child_run_id,
                task,
                child_context,
                parent_run_id=self._parent_run_id,
            )
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        try:
            async with EventWriter(child_run_path / "events.jsonl") as writer:
                writer.subscribe(child_bus)
                await child_loop.run(child_context)
        except asyncio.CancelledError:
            if not child_context.is_done():
                child_context.mark_failed("cancelled")
            raise
        finally:
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=child_run_id,
                    parent_run_id=self._parent_run_id,
                    status=child_context.status,
                    ts=_now(),
                )
            )

        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # 后台任务协程：写事件文件，运行 loop，发布完成事件
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        run_id: str,
    ) -> None:
        try:
            async with EventWriter(run_path / "events.jsonl") as writer:
                writer.subscribe(bus)
                await loop.run(context)
        except asyncio.CancelledError:
            if not context.is_done():
                context.mark_failed("cancelled")
            raise
        finally:
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=run_id,
                    parent_run_id=self._parent_run_id,
                    status=context.status,
                    ts=_now(),
                )
            )

    # 构造子 registry；基于角色配置过滤工具：显式 allowed_tools 与 restrict capability 取最严子集
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        boundary: WorkspaceBoundary,
        task_manager: TaskManager,
    ) -> ToolRegistry:
        allowed: set[str] | None = (
            set(profile.allowed_tools) if profile and profile.allowed_tools else None
        )
        restrict_read_only = bool(profile and profile.restrict == "read_only")

        # 显式名单和 restrict read-only 同时存在时取从严子集，单独存在时分别生效
        def _allowed(tool: BaseTool | str) -> bool:
            name = tool.name if isinstance(tool, BaseTool) else tool
            if restrict_read_only:
                is_read_only_t = (
                    tool.is_read_only
                    if isinstance(tool, BaseTool)
                    else name == "agent_result"
                )
                if not is_read_only_t:
                    return False
            return allowed is None or name in allowed

        registry = ToolRegistry(
            runtime_mode=RuntimeMode.PLAN if restrict_read_only else RuntimeMode.ACT
        )
        checkpoint_store = CheckpointStore(
            self._runs_dir / child_run_id / ".checkpoints",
            boundary,
        )
        file_tools = [
            ReadFileTool(boundary),
            GlobTool(boundary),
            GrepTool(boundary),
            EditFileTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            ApplyPatchTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            WriteFileTool(
                boundary,
                checkpoint_store=checkpoint_store,
            ),
            ListDirTool(boundary),
        ]
        file_allowed = allowed
        if restrict_read_only:
            read_aliases = {tool.name for tool in file_tools if tool.is_read_only}
            file_allowed = (
                read_aliases
                if allowed is None or "File" in allowed
                else read_aliases & allowed
            )
        register_file_family(
            registry,
            boundary,
            file_tools,
            allowed_names=file_allowed,
        )
        artifact_tool = ArtifactReadTool(
            ArtifactStore(boundary.root / ".coderook" / "artifacts")
        )
        if _allowed(artifact_tool):
            registry.register(artifact_tool)
        register_git_family(
            registry,
            boundary,
            GitDiffTool(boundary),
            allowed_names=allowed,
        )
        shell = BashTool(boundary.root)
        if not restrict_read_only:
            register_run_family(registry, shell, allowed_names=allowed)
            register_bash_family(
                registry,
                shell,
                allowed_names=allowed,
            )
        skill_tool = SkillTool(SkillLoader(boundary.root))
        if _allowed(skill_tool):
            registry.register(skill_tool)
        for checkpoint_tool in [
            CheckpointListTool(checkpoint_store),
            CheckpointRewindTool(checkpoint_store),
        ]:
            if _allowed(checkpoint_tool):
                registry.register(checkpoint_tool)

        child_task_manager = task_manager
        for t in [
            TaskCreateTool(child_task_manager),
            TaskClaimTool(child_task_manager),
            TaskUpdateTool(child_task_manager),
            TaskListTool(child_task_manager),
            TaskGetTool(child_task_manager),
        ]:
            if _allowed(t):
                registry.register(t)

        if self._depth < 1:
            nested = SpawnAgentTool(
                provider=self._provider,
                parent_bus=child_bus,
                parent_run_id=child_run_id,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                task_registry=self._task_registry,
                runs_dir=self._runs_dir,
                session_id=self._session_id,
                depth=self._depth + 1,
                workspace_boundary=boundary,
                task_manager=task_manager,
            )
            if _allowed("spawn_agent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry))

        search_tool = ToolSearchTool(registry)
        if _allowed(search_tool):
            registry.register(search_tool)

        return registry


class AgentResultParams(BaseModel):
    run_id: str


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    name = "agent_result"
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
        },
        "required": ["run_id"],
    }
    params_model = AgentResultParams

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    # 查询指定 run_id 的后台任务状态，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._task_registry.get(p.run_id)
        if entry is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}. Only background subagents can be queried.",
                is_error=True,
                error_type="runtime_error",
            )
        task, context = entry
        if not task.done():
            return ToolResult(content="still running")
        if task.cancelled():
            return ToolResult(
                content="Subagent was cancelled.", is_error=True, error_type="runtime_error"
            )
        exc = task.exception()
        if exc is not None:
            return ToolResult(
                content=f"Subagent raised an exception: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=context.result or "Subagent completed with no text result.")
