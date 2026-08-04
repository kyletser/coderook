from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import RuntimeMode
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.base import LLMProvider
from code_rook.core.mcp.server import McpServerManager
from code_rook.core.memory import MemoryStore
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.skills.loader import SkillLoader
from code_rook.core.subagent.registry import BackgroundTaskRegistry
from code_rook.core.subagent.tool import AgentResultTool, SpawnAgentTool
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.artifact import ArtifactReadTool
from code_rook.core.tools.base import BaseTool
from code_rook.core.tools.builtin import (
    ApplyPatchTool,
    AskUserQuestionTool,
    BashTool,
    CheckpointListTool,
    CheckpointRewindTool,
    EditFileTool,
    GitDiffTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    MemoryForgetTool,
    MemorySaveTool,
    MemorySearchTool,
    NoteSaveTool,
    ReadFileTool,
    SkillTool,
    TaskClaimTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WorktreeCreateTool,
    WorktreeListTool,
    WorktreeRemoveTool,
    WriteFileTool,
)
from code_rook.core.tools.discovery import ToolSearchTool
from code_rook.core.tools.families import (
    register_bash_family,
    register_file_family,
    register_git_family,
    register_run_family,
)
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.workspace import WorkspaceBoundary
from code_rook.core.worktree import WorktreeManager

if TYPE_CHECKING:
    from code_rook.core.hooks import HookManager
    from code_rook.core.llm.route_registry import RouteRegistry


class RuntimeToolAssembly:
    # 保存跨 run 复用的工具依赖，集中管理根 Agent 的工具表装配
    def __init__(
        self,
        *,
        workspace_boundary: WorkspaceBoundary,
        artifact_store: ArtifactStore,
        memory_store: MemoryStore,
        worktree_manager: WorktreeManager,
        skill_loader: SkillLoader,
        task_registry: BackgroundTaskRegistry,
        permission_manager: PermissionManager | None,
        max_steps: int,
        runs_dir: Path,
        background_registry: BackgroundJobRegistry | None,
        interaction_manager: InteractionManager | None,
        mcp_manager: McpServerManager | None,
        route_registry: RouteRegistry | None,
        hooks: HookManager | None = None,
    ) -> None:
        self._boundary = workspace_boundary
        self._artifact_store = artifact_store
        self._memory_store = memory_store
        self._worktree_manager = worktree_manager
        self._skill_loader = skill_loader
        self._task_registry = task_registry
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._runs_dir = runs_dir
        self._background_registry = background_registry
        self._interaction_manager = interaction_manager
        self._mcp_manager = mcp_manager
        self._route_registry = route_registry
        self._hooks = hooks

    # 根据本次 run 的动态依赖构建完整且受 Mode/白名单裁剪的工具目录
    def build(
        self,
        task_manager: TaskManager,
        *,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        checkpoint_store: CheckpointStore | None = None,
        runtime_mode: RuntimeMode = RuntimeMode.ACT,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        # 判断工具名称是否处于显式白名单内
        def _name_allowed(name: str) -> bool:
            return allowed is None or name in allowed

        # Plan Mode 只向模型暴露明确声明为纯读的工具
        def _ok(tool: BaseTool) -> bool:
            return _name_allowed(tool.name) and (
                runtime_mode != RuntimeMode.PLAN or tool.is_read_only
            )

        registry = ToolRegistry(runtime_mode=runtime_mode)
        file_tools = [
            ReadFileTool(self._boundary),
            GlobTool(self._boundary),
            GrepTool(self._boundary),
            EditFileTool(self._boundary, checkpoint_store=checkpoint_store),
            ApplyPatchTool(self._boundary, checkpoint_store=checkpoint_store),
            WriteFileTool(self._boundary, checkpoint_store=checkpoint_store),
            ListDirTool(self._boundary),
        ]
        register_file_family(
            registry,
            self._boundary,
            file_tools,
            allowed_names=allowed,
        )
        artifact_tool = ArtifactReadTool(self._artifact_store)
        if _ok(artifact_tool):
            registry.register(artifact_tool)
        register_git_family(
            registry,
            self._boundary,
            GitDiffTool(self._boundary),
            allowed_names=allowed,
        )
        shell = BashTool(self._boundary.root)
        register_run_family(registry, shell, allowed_names=allowed)
        register_bash_family(
            registry,
            shell,
            background_registry=self._background_registry,
            session_id=session_id,
            run_id=run_id or "",
            allowed_names=allowed,
        )
        skill_tool = SkillTool(self._skill_loader)
        if _ok(skill_tool):
            registry.register(skill_tool)
        if checkpoint_store is not None:
            for checkpoint_tool in (
                CheckpointListTool(checkpoint_store),
                CheckpointRewindTool(checkpoint_store),
            ):
                if _ok(checkpoint_tool):
                    registry.register(checkpoint_tool)
        for task_tool in (
            TaskCreateTool(task_manager),
            TaskClaimTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ):
            if _ok(task_tool):
                registry.register(task_tool)
        for memory_tool in (
            MemorySaveTool(self._memory_store, session_id, run_id or ""),
            MemorySearchTool(self._memory_store),
            MemoryForgetTool(self._memory_store),
        ):
            if _ok(memory_tool):
                registry.register(memory_tool)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(store, session.id, run_id)
            if _ok(note_tool):
                registry.register(note_tool)
        if self._interaction_manager is not None and run_id is not None:
            question_tool = AskUserQuestionTool(
                self._interaction_manager,
                session_id,
                run_id,
            )
            if _ok(question_tool):
                registry.register(question_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            if runtime_mode != RuntimeMode.PLAN and _name_allowed("spawn_agent"):
                registry.register(
                    SpawnAgentTool(
                        provider=provider,
                        parent_bus=bus,
                        parent_run_id=run_id,
                        permission_manager=self._permission_manager,
                        max_steps=self._max_steps,
                        task_registry=self._task_registry,
                        runs_dir=runs_dir,
                        session_id=session_id,
                        depth=0,
                        workspace_boundary=self._boundary,
                        task_manager=task_manager,
                        route_registry=self._route_registry,
                        hooks=self._hooks,
                    )
                )
            agent_result_tool = AgentResultTool(self._task_registry)
            if _ok(agent_result_tool):
                registry.register(agent_result_tool)
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool):
                    registry.register(mcp_tool)
        for worktree_tool in (
            WorktreeCreateTool(self._worktree_manager),
            WorktreeListTool(self._worktree_manager),
            WorktreeRemoveTool(self._worktree_manager),
        ):
            if _ok(worktree_tool):
                registry.register(worktree_tool)
        search_tool = ToolSearchTool(registry)
        if _ok(search_tool):
            registry.register(search_tool)
        return registry
