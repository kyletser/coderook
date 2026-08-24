from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority import ToolAction
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.mcp.client import McpClient, McpToolDef
from code_rook.core.mcp.tool import McpTool
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.builtin.bash import BashTool
from code_rook.core.tools.discovery import ToolSearchTool
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import OutputPolicy, ToolCaller, ToolCatalogError, ToolSpec


class _DeferredTool(BaseTool):
    side_effect = ToolSideEffect.NONE
    can_parallel = True
    deferred = True
    allowed_callers = frozenset(
        {ToolCaller.MODEL, ToolCaller.INTERNAL, ToolCaller.REPLAY}
    )
    name = "remote__issues"
    description = "Search remote issue tracker tickets and labels"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
    }

    # 返回固定远程结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("issue-result")


class _MediumTool(BaseTool):
    side_effect = ToolSideEffect.NONE
    name = "medium"
    description = "return medium output"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 返回超过默认 soft limit 的确定性文本
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("large-output-" * 3_000)

    # 使用小型测试边界证明任何摘要都附带可继续读取的 artifact
    def build_spec(self) -> ToolSpec:
        return super().build_spec().model_copy(
            update={
                "output_policy": OutputPolicy(
                    soft_limit=1_000,
                    hard_limit=50_000,
                    spill_to_artifact=True,
                )
            }
        )


# 功能：子运行的 authority ceiling 同时裁剪工具 schema 与直接调用目录
# 设计：只允许 READ 后注册读工具和 Shell，断言模型不可见 Shell 且内部解析也失败
def test_registry_authority_ceiling_filters_catalog_and_execution() -> None:
    registry = ToolRegistry(
        allowed_authority_actions=frozenset({ToolAction.READ})
    )
    registry.register(_MediumTool())
    registry.register(BashTool())

    assert [schema["name"] for schema in registry.tool_schemas()] == ["medium"]
    assert registry.get("bash") is None
    with pytest.raises(ToolCatalogError, match="unknown tool"):
        registry.resolve_call(
            "bash",
            {"command": "echo must-not-run"},
            caller=ToolCaller.INTERNAL,
        )


# 功能：验证 deferred 工具搜索结果稳定激活到尾部且 active head hash 不变
# 设计：搜索前后比较 schema 顺序和 head hash，并重复搜索确认不重复追加
async def test_tool_search_activates_deferred_tail_deterministically() -> None:
    registry = ToolRegistry()
    deferred = _DeferredTool()
    registry.register(deferred)
    registry.register(ToolSearchTool(registry))
    before = registry.active_head_hash()

    assert [schema["name"] for schema in registry.tool_schemas()] == ["tool_search"]
    result = await registry.get("tool_search").invoke(  # type: ignore[union-attr]
        {"query": "issue tracker"}
    )
    again = await registry.get("tool_search").invoke(  # type: ignore[union-attr]
        {"query": "remote"}
    )

    assert json.loads(result.content)["activated"] == ["remote__issues"]
    tool_summary = json.loads(result.content)["tools"][0]
    assert tool_summary["reason"] == "all_query_terms"
    assert tool_summary["schema_handle"].startswith(
        "tool-schema:remote__issues:1:"
    )
    assert json.loads(again.content)["activated"] == ["remote__issues"]
    assert [schema["name"] for schema in registry.tool_schemas()] == [
        "tool_search",
        "remote__issues",
    ]
    assert registry.active_head_hash() == before


# 功能：验证未激活 deferred 工具对模型 fail closed，但 replay caller 仍可兼容
# 设计：同一调用分别使用 MODEL 与 REPLAY caller，随后激活并证明 MODEL 可解析
def test_deferred_tool_requires_activation_for_model() -> None:
    registry = ToolRegistry()
    registry.register(_DeferredTool())

    with pytest.raises(ToolCatalogError, match="not activated"):
        registry.resolve_call("remote__issues", {"query": "x"})
    assert (
        registry.resolve_call(
            "remote__issues",
            {"query": "x"},
            caller=ToolCaller.REPLAY,
        ).spec.name
        == "remote__issues"
    )
    registry.search_deferred("issues")
    assert registry.resolve_call("remote__issues", {"query": "x"}).spec.name


# 功能：验证模型可见工具硬上限同时约束默认头部和 deferred 激活尾部
# 设计：把上限设为二并注册一个 active 与多个 deferred，确认搜索最多再激活一个
async def test_tool_search_respects_model_visible_limit() -> None:
    registry = ToolRegistry(model_tool_limit=2)
    registry.register(ToolSearchTool(registry))
    for name in ("remote__alpha", "remote__beta", "remote__gamma"):
        tool = _DeferredTool()
        tool.name = name
        registry.register(tool)

    result = await registry.get("tool_search").invoke(  # type: ignore[union-attr]
        {"query": "remote", "limit": 8}
    )

    assert len(json.loads(result.content)["activated"]) == 1
    assert len(registry.tool_schemas()) == registry.model_tool_limit == 2


# 功能：验证 soft 到 hard 之间的 typed summary 也必须附带完整 Artifact
# 设计：使用自定义窄边界工具，恢复 artifact 原文以证明摘要没有造成不可逆数据丢失
async def test_medium_tool_output_summary_has_recoverable_artifact(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(_MediumTool())
    store = ArtifactStore(tmp_path / ".coderook" / "artifacts")

    result = await invoke_tool(
        registry,
        ToolCallBlock(id="medium-1", name="medium", input={}),
        EventBus(),
        "run-medium",
        artifact_store=store,
    )
    payload = json.loads(result.content)
    reference = payload["artifact"]
    restored = await store.read(reference["sha256"], limit=50_000)

    assert payload["kind"] == "tool_output_summary"
    assert payload["truncated"] is True
    assert reference["handle"] == f"artifact:{reference['sha256']}"
    assert restored.content == "large-output-" * 3_000


# 功能：验证前台 Bash 的 10MB 无换行输出分块读取并完整转存可分页 Artifact
# 设计：让当前 Python 写固定字节流，经统一 invocation 恢复全量，覆盖 communicate OOM 回归
async def test_large_bash_output_is_bounded_and_recoverable(tmp_path: Path) -> None:
    size = 10 * 1024 * 1024
    command = (
        f'"{sys.executable}" -c "import sys;'
        f"sys.stdout.buffer.write(b'x'*{size})\""
    )
    registry = ToolRegistry()
    registry.register(BashTool(tmp_path))
    store = ArtifactStore(tmp_path / ".coderook" / "artifacts")

    result = await invoke_tool(
        registry,
        ToolCallBlock(id="bash-large-1", name="bash", input={"command": command}),
        EventBus(),
        "run-bash-large",
        artifact_store=store,
    )
    payload = json.loads(result.content)
    reference = payload["artifact"]
    restored = await store.read_bytes(reference["sha256"], max_bytes=size + 1)

    assert payload["kind"] == "tool_output_summary"
    assert reference["size"] == size
    assert restored == b"x" * size


# 功能：验证超大 MCP 输出不直接进入 prompt/event，而是保存为可校验 artifact
# 设计：激活真实 McpTool 后走统一调用入口，并比较返回值、完成事件与原文恢复内容
async def test_large_mcp_output_spills_to_artifact_and_bounds_event(tmp_path: Path) -> None:
    original = "mcp-large-output-" * 2_500
    client = AsyncMock(spec=McpClient)
    client.call_tool = AsyncMock(return_value=original)
    tool = McpTool(
        client,
        "remote",
        McpToolDef(
            name="report",
            description="Fetch a very large remote report",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    registry = ToolRegistry()
    registry.register(tool)
    registry.search_deferred("large remote report")
    store = ArtifactStore(tmp_path / ".coderook" / "artifacts")
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集完成事件，证明 event 与模型拿到的是同一个有界引用
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    result = await invoke_tool(
        registry,
        ToolCallBlock(id="mcp-large-1", name=tool.name, input={}),
        bus,
        "run-mcp-large",
        artifact_store=store,
    )
    payload = json.loads(result.content)
    reference = payload["artifact"]
    restored = await store.read(reference["sha256"], limit=50_000)
    finished = next(event for event in events if event.type == "tool.call_finished")

    assert tool.build_spec().deferred is True
    assert payload["kind"] == "tool_output_summary"
    assert reference["handle"] == f"artifact:{reference['sha256']}"
    assert len(result.content.encode("utf-8")) <= tool.build_spec().output_policy.hard_limit
    assert finished.output == result.content  # type: ignore[attr-defined]
    assert len(finished.output.encode("utf-8")) < len(original.encode("utf-8"))  # type: ignore[attr-defined]
    assert restored.content == original
