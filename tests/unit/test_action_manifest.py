from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from code_rook.core.authority import ToolAction
from code_rook.core.config import CodeRookConfig
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.policy import PermissionDecision, ToolPolicy
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.presentation import build_tool_presentation
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCapability,
    ToolCatalogError,
    ToolPresentationAction,
    ToolPresentationKind,
    ToolSpec,
)


class _ManifestTool(BaseTool):
    name = "File"
    description = "manifest test tool"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"action": {"type": "string"}},
        "required": ["action"],
    }

    # 返回固定成功结果以隔离 manifest 解析行为
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("ok")


# 构建同时声明 schema、能力、审批、并行和旧权限别名的单 action manifest
def _manifest_spec() -> ToolSpec:
    action = ToolActionSpec(
        name="patch",
        input_schema={
            "type": "object",
            "properties": {"patch": {"type": "string"}},
            "required": ["patch"],
        },
        capabilities=frozenset({ToolCapability.WRITE}),
        approval_requirement=ApprovalRequirement.ALWAYS,
        parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
        permission_policy_aliases=("apply_patch",),
    )
    return ToolSpec(
        name="File",
        description="file patch",
        input_schema=_ManifestTool.input_schema,
        actions=(action,),
        capabilities=action.capabilities,
    )


# 收集权限事件而不依赖真实 TUI 客户端
async def _event_collector() -> tuple[list[dict[str, Any]], Any]:
    events: list[dict[str, Any]] = []

    # 将权限事件追加到内存列表
    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    return events, emit


# 功能：一次 resolve 结果同时驱动 schema、能力、authority、审批、并行和权限范围
# 设计：对同一个 ToolActionSpec 逐项核对目录 schema 与 ResolvedToolCall 投影，防止消费者另建映射
def test_resolved_manifest_is_the_single_action_contract() -> None:
    registry = ToolRegistry()
    registry.register(_ManifestTool(), spec=_manifest_spec())

    resolved = registry.resolve_call("File", {"action": "patch", "patch": "x"})
    schema = registry.tool_schemas()[0]["input_schema"]

    assert isinstance(schema, dict)
    assert schema["oneOf"][0]["properties"]["action"]["enum"] == ["patch"]
    assert resolved.input_schema["required"] == ["patch"]
    assert resolved.capabilities == frozenset({ToolCapability.WRITE})
    assert resolved.authority_action == ToolAction.MUTATE
    assert resolved.effective_approval_requirement == ApprovalRequirement.ALWAYS
    assert resolved.effective_parallel_policy == ParallelPolicy.RESOURCE_CLAIMS
    assert resolved.permission_scope.key == "File.patch"
    assert resolved.permission_scope.lookup_keys == ("File.patch", "apply_patch")


# 功能：PermissionManager 只按 resolved manifest 回退旧策略别名且保留新 action 缓存键
# 设计：只配置 apply_patch=ALLOW，传入 File.patch manifest 后应静默放行而不扩大到其他 action
async def test_permission_policy_alias_is_consumed_from_manifest() -> None:
    registry = ToolRegistry()
    original = _manifest_spec()
    policy_action = original.actions[0].model_copy(
        update={"approval_requirement": ApprovalRequirement.POLICY}
    )
    policy_spec = original.model_copy(update={"actions": (policy_action,)})
    registry.register(_ManifestTool(), spec=policy_spec)
    resolved = registry.resolve_call("File", {"action": "patch", "patch": "x"})
    manager = PermissionManager(
        {"apply_patch": ToolPolicy(default=PermissionDecision.ALLOW)}
    )
    events, emit = await _event_collector()

    allowed, decision = await manager.check_and_wait(
        tool_use_id="patch",
        tool_name="File",
        params={"action": "patch", "patch": "x"},
        session_id="session",
        event_emitter=emit,
        resolved_call=resolved,
    )

    assert (allowed, decision) == (True, "auto_allow")
    assert events == []


# 功能：未知 action 和调用参数与 manifest 不一致时均在执行前失败关闭
# 设计：Catalog 拒绝未声明 action，权限层再拒绝把已解析 patch manifest 套到 delete 参数
async def test_unknown_or_mismatched_action_fails_closed() -> None:
    registry = ToolRegistry()
    registry.register(_ManifestTool(), spec=_manifest_spec())
    with pytest.raises(ToolCatalogError, match="unknown action"):
        registry.resolve_call("File", {"action": "delete"})
    resolved = registry.resolve_call("File", {"action": "patch", "patch": "x"})
    manager = PermissionManager()
    events, emit = await _event_collector()

    allowed, decision = await manager.check_and_wait(
        tool_use_id="mismatch",
        tool_name="File",
        params={"action": "delete"},
        session_id="session",
        event_emitter=emit,
        resolved_call=resolved,
    )

    assert (allowed, decision) == (False, "manifest_mismatch")
    assert events == []


# 功能：非法或重复权限策略键在 ToolSpec 建立阶段被严格拒绝
# 设计：分别注入路径字符 alias 与两个 action 共用显式 key，覆盖格式和权限扩大两类漂移
def test_permission_scope_declarations_are_strict() -> None:
    with pytest.raises(ValidationError, match="unsupported characters"):
        ToolActionSpec(
            name="read",
            capabilities=frozenset({ToolCapability.READ}),
            permission_policy_aliases=("../read_file",),
        )

    first = ToolActionSpec(
        name="read",
        capabilities=frozenset({ToolCapability.READ}),
        permission_policy_key="File.shared",
    )
    second = ToolActionSpec(
        name="write",
        capabilities=frozenset({ToolCapability.WRITE}),
        permission_policy_key="File.shared",
    )
    with pytest.raises(ValidationError, match="unique permission policy keys"):
        ToolSpec(
            name="File",
            description="invalid shared permission",
            input_schema={"type": "object"},
            actions=(first, second),
            capabilities=frozenset({ToolCapability.READ, ToolCapability.WRITE}),
        )


# 功能：真实默认工具族在各自 manifest 中声明全部旧平铺策略兼容名
# 设计：从 Runner 组装后的 Catalog 逐 action 解析，防止以后新增或改名时重新依赖权限层手写表
def test_default_action_families_declare_legacy_policy_aliases(tmp_path: Path) -> None:
    registry = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
    )._build_registry(TaskManager(tmp_path / ".tasks"))
    expected = {
        ("Bash", "run"): "bash",
        ("File", "read"): "read_file",
        ("File", "list"): "list_dir",
        ("File", "search_name"): "glob",
        ("File", "search_content"): "grep",
        ("File", "write"): "write_file",
        ("File", "edit"): "edit_file",
        ("File", "patch"): "apply_patch",
        ("Git", "diff"): "git_diff",
        ("Run", "tests"): "run_tests",
        ("Run", "verifiers"): "run_verifiers",
        ("memory", "save"): "memory_save",
        ("memory", "search"): "memory_search",
        ("tasks", "create"): "task_create",
        ("tasks", "list"): "task_list",
    }

    for (tool_name, action), alias in expected.items():
        resolved = registry.resolve_call(tool_name, {"action": action})
        assert alias in resolved.permission_scope.aliases


# 功能：验证 action-family 生成跨前端一致的语义动作、位置和实时输出能力
# 设计：使用真实 Runner Catalog 覆盖 File、Bash、Run 三类展示，避免 Web/TUI 再按工具名各自猜测
def test_presentation_exposes_shared_semantic_activity(tmp_path: Path) -> None:
    registry = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
    )._build_registry(TaskManager(tmp_path / ".tasks"))

    file_call = registry.resolve_call("File", {"action": "read", "path": "README.md"})
    file_view = build_tool_presentation(
        file_call,
        {"action": "read", "path": "README.md"},
        None,
    )
    bash_call = registry.resolve_call("Bash", {"action": "run", "command": "git status"})
    bash_view = build_tool_presentation(
        bash_call,
        {"action": "run", "command": "git status"},
        None,
    )
    run_call = registry.resolve_call("Run", {"action": "tests", "command": "pytest -q"})
    run_view = build_tool_presentation(
        run_call,
        {"action": "tests", "command": "pytest -q"},
        None,
    )

    assert file_view.action == ToolPresentationAction.READ_FILE
    assert file_view.kind == ToolPresentationKind.READ
    assert file_view.locations == ("README.md",)
    assert bash_view.action == ToolPresentationAction.RUN_COMMAND
    assert bash_view.command == "git status"
    assert bash_view.supports_live_output is True
    assert run_view.action == ToolPresentationAction.RUN_TESTS
    assert run_view.kind == ToolPresentationKind.TERMINAL
