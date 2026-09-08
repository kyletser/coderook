from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import AuthoritySnapshot
from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookManager
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ToolCaller

if TYPE_CHECKING:
    from code_rook.core.permissions.manager import PermissionManager


@dataclass(frozen=True)
class UserShellCommand:
    command: str
    include_in_context: bool = True


# 解析 Pi 的 ! 与 !! 前缀，命令正文不经过模型改写。
def parse_user_shell(text: str) -> UserShellCommand | None:
    if not text.startswith("!"):
        return None
    excluded = text.startswith("!!")
    command = text[2 if excluded else 1:].strip()
    return UserShellCommand(command, not excluded) if command else None


# 直接执行用户命令，复用工具审批、Hook、输出和取消管线，不创建模型请求。
async def execute_user_shell(
    request: UserShellCommand, *, registry: ToolRegistry, bus: EventBus,
    run_id: str, operation_id: str, session_id: str = "",
    permission_manager: PermissionManager | None = None,
    hooks: HookManager | None = None, artifact_store: ArtifactStore | None = None,
    authority_snapshot: AuthoritySnapshot | None = None,
) -> ToolResult:
    from code_rook.core.tools.invocation import invoke_tool

    return await invoke_tool(
        registry,
        ToolCallBlock(id=operation_id, name="bash", input={"command": request.command}),
        bus, run_id,
        permission_manager=permission_manager, session_id=session_id, hooks=hooks,
        caller=ToolCaller.INTERNAL, artifact_store=artifact_store,
        authority_snapshot=authority_snapshot, step=1,
    )
