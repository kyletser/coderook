import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.tools import EditTool, ReadTool, Replacement, apply_edits
from code_rook.core.checkpoints import CheckpointStore
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.presentation import build_tool_presentation
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ToolPresentationAction
from code_rook.core.workspace import WorkspaceBoundary, WorkspaceBoundaryError


# 功能：验证模型可以经正式工具管线读取工作区外已启用 Skill 及其参考文件。
# 设计：使用真实文件和循环请求两次 read，确保结果进入下一次模型上下文而非只检查路径解析。
async def test_read_enabled_skill_resources(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    skill = tmp_path / "installed" / "guide"
    workspace.mkdir()
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("Read references/details.md", encoding="utf-8")
    reference = skill / "references" / "details.md"
    reference.write_text("Use the documented public API", encoding="utf-8")
    provider = MagicMock()
    count = 0

    # 先读取说明再读参考文件，最后检查工具结果并给出回答。
    async def chat(messages, *_args, **_kwargs):
        nonlocal count
        count += 1
        if count == 2:
            assert "Read references/details.md" in str(messages[-1])
        if count <= 2:
            path = skill / "SKILL.md" if count == 1 else reference
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock(id=f"skill-{count}", name="read", input={"path": str(path)}),
            ])
        assert "Use the documented public API" in str(messages[-1])
        return LlmResponse(stop_reason="end_turn", text="Loaded skill and references")

    provider.chat = AsyncMock(side_effect=chat)
    registry = ToolRegistry()
    registry.register(ReadTool(WorkspaceBoundary(workspace), resource_roots=(skill,)))
    context = ExecutionContext(run_id="skill-read", goal="Use the guide", max_steps=4)
    await AgentLoop(provider, registry, EventBus()).run(context)
    assert context.status == "success", context.reason
    assert count == 3


# 功能：验证 Skill 读取范围不会使其他目录可读或使 Skill 文件可写。
# 设计：显式列入一个 Skill 根目录，分别读取其兄弟路径及调用 edit，检查原文件保持不变。
async def test_skill_read_roots_do_not_expand_edits(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    skill = tmp_path / "guide"
    workspace.mkdir()
    skill.mkdir()
    target = skill / "SKILL.md"
    target.write_text("original", encoding="utf-8")
    boundary = WorkspaceBoundary(workspace)
    read = ReadTool(boundary, resource_roots=(skill,))
    with pytest.raises(WorkspaceBoundaryError):
        await read.invoke({"path": str(tmp_path / "other.md")})
    with pytest.raises(WorkspaceBoundaryError):
        await EditTool(boundary, None).invoke({
            "path": str(target), "oldText": "original", "newText": "changed",
        })
    assert target.read_text(encoding="utf-8") == "original"


# 功能：原生 read 重复读取同一路径时看到外部最新修改，不返回旧运行时缓存。
# 设计：两次模型请求之间模拟编辑器改盘，不执行修改工具，验证真正的循环读取结果。
async def test_native_read_observes_external_file_changes(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("before", encoding="utf-8")
    provider = MagicMock()
    requests = []

    # 首次读取后由外部改盘，再要求读取完全相同的路径。
    async def chat(messages, *_args, **_kwargs):
        requests.append(messages)
        if len(requests) == 2:
            assert messages[-1]["content"][0]["content"] == "before"
            path.write_text("after", encoding="utf-8")
        if len(requests) <= 2:
            return LlmResponse(stop_reason="tool_use", tool_calls=[
                ToolCallBlock(id=f"read-{len(requests)}", name="read", input={"path": "sample.txt"})
            ])
        assert messages[-1]["content"][0]["content"] == "after"
        return LlmResponse(stop_reason="end_turn", text="Observed latest content")

    provider.chat = AsyncMock(side_effect=chat)
    registry = ToolRegistry()
    registry.register(ReadTool(WorkspaceBoundary(tmp_path)))
    context = ExecutionContext(run_id="fresh-read", goal="Read twice", max_steps=4)
    await AgentLoop(provider, registry, EventBus()).run(context)
    assert context.status == "success", context.reason
    assert len(requests) == 3
    assert context.result == "Observed latest content"


# 功能：Pi 支持的单项、JSON 字符串和旧参数均能完成同一批编辑。
# 设计：调用真实文件工具并检查输入未变，防止参数适配改写审计中的原始调用。
@pytest.mark.parametrize("arguments", [
    {"edits": {"oldText": "before", "newText": "after"}},
    {"edits": '[{"oldText":"before","newText":"after"}]'},
    {"edits": '{"oldText":"before","newText":"after"}'},
    {"oldText": "before", "newText": "after"},
])
async def test_edit_normalizes_pi_argument_shapes(tmp_path: Path, arguments: dict) -> None:
    path = tmp_path / "sample.txt"
    path.write_text("before", encoding="utf-8")
    params = {"path": "sample.txt", **arguments}
    original = json.dumps(params)
    await EditTool(WorkspaceBoundary(tmp_path), None).invoke(params)
    assert path.read_text(encoding="utf-8") == "after"
    assert json.dumps(params) == original


# 功能：多处修改针对同一原文匹配，而非依次重查已替换的内容。
# 设计：首处替换引入第二处的匹配文本，结果仍只能替换原来的第二处。
def test_edit_matches_original_file() -> None:
    assert (
        apply_edits(
            "alpha\nbeta\n",
            [
                Replacement(oldText="alpha", newText="beta"),
                Replacement(oldText="beta", newText="gamma"),
            ],
        )
        == "beta\ngamma\n"
    )


# 功能：重叠编辑被拒绝，合法批量修改保留 Windows BOM/CRLF 并支持 Checkpoint。
# 设计：调用新工具读取真实文件，校验失败无写入及成功后可恢复原始字节。
async def test_edit_checkpoint_and_atomic_validation(tmp_path: Path) -> None:
    boundary = WorkspaceBoundary(tmp_path)
    checkpoints = CheckpointStore(tmp_path / ".coderook" / "checkpoints", boundary)
    path = tmp_path / "file.py"
    original = b"\xef\xbb\xbfalpha = 1\r\nbeta = 2\r\n"
    path.write_bytes(original)
    tool = EditTool(boundary, checkpoints)
    with pytest.raises(ValueError, match="overlap"):
        await tool.invoke(
            {
                "path": "file.py",
                "edits": [
                    {"oldText": "alpha = 1", "newText": "alpha = 3"},
                    {"oldText": "alpha", "newText": "renamed"},
                ],
            }
        )
    assert path.read_bytes() == original
    result = await tool.invoke(
        {
            "path": "file.py",
            "edits": [
                {"oldText": "alpha = 1", "newText": "alpha = 3"},
                {"oldText": "beta = 2", "newText": "beta = 4"},
            ],
        }
    )
    payload = json.loads(result.content)
    assert payload["replacements"] == 2
    assert path.read_bytes() == b"\xef\xbb\xbfalpha = 3\r\nbeta = 4\r\n"
    checkpoints.rewind(payload["checkpoint_id"])
    assert path.read_bytes() == original


# 功能：读取工具按 offset/limit 返回可继续的原文页。
# 设计：使用短文件和两行窗口，检查原文、后续行号和越界提示。
async def test_read_has_explicit_pagination(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    tool = ReadTool(WorkspaceBoundary(tmp_path))
    first = await tool.invoke({"path": "file.txt", "limit": 2})
    assert first.content.startswith("one\ntwo\n")
    assert "offset=3" in first.content
    last = await tool.invoke({"path": "file.txt", "offset": 3})
    assert last.content == "three\n"
    assert (await tool.invoke({"path": "file.txt", "offset": 5})).is_error


# 功能：原生 read 可以直接列出目录内容，简单探索不必调用需要审批的 Shell。
# 设计：创建包含隐藏目录和文件的真实工作区，验证目录树由同一个 read 工具返回。
async def test_read_lists_workspace_directories(tmp_path: Path) -> None:
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "README.md").write_text("project", encoding="utf-8")
    result = await ReadTool(WorkspaceBoundary(tmp_path)).invoke({"path": "."})
    assert not result.is_error
    assert ".hidden/" in result.content
    assert "README.md" in result.content
    assert result.details == {"content_kind": "directory"}


# 功能：目录读取完成后向 Web 与 TUI 暴露“浏览文件”而不是“读取文件”的展示语义。
# 设计：让原生 read 执行真实目录分支，再经统一 Presentation 转换，覆盖用户时间线中的错误动作标签。
async def test_directory_read_presents_as_file_browsing(tmp_path: Path) -> None:
    registry = ToolRegistry()
    tool = ReadTool(WorkspaceBoundary(tmp_path))
    registry.register(tool)
    params: dict[str, object] = {"path": "."}

    result = await tool.invoke(params)
    presentation = build_tool_presentation(registry.resolve_call("read", params), params, result)

    assert presentation.action == ToolPresentationAction.BROWSE_FILES


# 功能：Unicode/尾空格模糊匹配不会改写未触及的行。
# 设计：使用智能引号与尾空格，并在邻行保留同类字符，排除整文件归一化污染。
def test_fuzzy_edit_preserves_untouched_lines() -> None:
    original = "keep = ‘unchanged’  \nvalue = ‘old’  \nend = ‘untouched’  \n"
    updated = apply_edits(original, [Replacement(oldText="value = 'old'", newText="value = 'new'")])
    assert updated == "keep = ‘unchanged’  \nvalue = 'new'\nend = ‘untouched’  \n"


# 功能：真实 Runner 默认四工具可以完成写入、读取、批量修改和最终回答。
# 设计：用确定性 Provider 驱动生产工具管线，在临时项目验证结果而不消耗模型费用。
async def test_native_tools_run_through_python_runner(tmp_path: Path) -> None:
    from code_rook.core.config import CodeRookConfig
    from code_rook.core.llm.types import LlmResponse, ToolCallBlock
    from code_rook.core.runner import AgentRunner

    class Provider:
        step = 0

        # 按三个真实工具步骤返回调用，最后把文件结果交给用户。
        async def chat(self, **kwargs: object) -> LlmResponse:
            assert {tool["name"] for tool in kwargs["tool_schemas"]} == {
                "read",
                "bash",
                "edit",
                "write",
            }
            calls = [
                ("write", {"path": "example.py", "content": "a = 1\nb = 2\n"}),
                ("read", {"path": "example.py"}),
                (
                    "edit",
                    {"path": "example.py", "edits": [{"oldText": "a = 1", "newText": "a = 3"}]},
                ),
            ]
            if self.step == len(calls):
                return LlmResponse(stop_reason="end_turn", text="Updated example.py.")
            name, arguments = calls[self.step]
            self.step += 1
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(id=f"tool-{self.step}", name=name, input=arguments)],
            )

    runner = AgentRunner(
        CodeRookConfig(), provider=Provider(), workspace_root=tmp_path, runs_dir=tmp_path / "runs"
    )
    outcome = await runner.run_and_capture("Create and update example.py")
    assert outcome.status == "success"
    assert (tmp_path / "example.py").read_text(encoding="utf-8") == "a = 3\nb = 2\n"
