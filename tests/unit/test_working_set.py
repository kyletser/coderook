from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from code_rook.core.bus.events import (
    ContextPrefixFingerprintEvent,
    LspDiagnosticsEvent,
)
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.loop import AgentLoop
from code_rook.core.lsp.diagnostics import Diagnostic, DiagnosticsReport
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.builtin.edit_file import EditFileTool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.working_set import WorkingSet
from code_rook.core.workspace import WorkspaceBoundary


class _NoopTool(BaseTool):
    name = "noop"
    description = "Continue one step without touching files"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 返回固定只读结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult("ok")


class _DiagnosticsClient:
    # 初始化固定诊断报告和调用路径记录
    def __init__(self, report: DiagnosticsReport) -> None:
        self._report = report
        self.paths: list[list[str]] = []

    # 返回固定报告并记录本次修改路径
    async def diagnose(self, paths: list[str]) -> DiagnosticsReport:
        self.paths.append(paths)
        return self._report


class _CapturingProvider:
    # 初始化三步响应并保存每次收到的 system prompt
    def __init__(self) -> None:
        self.systems: list[str] = []
        self._responses = iter(
            [
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[
                        ToolCallBlock(
                            id="edit-1",
                            name="edit_file",
                            input={
                                "path": "sample.py",
                                "old_text": "value = 1",
                                "new_text": "value = 'bad'",
                            },
                        )
                    ],
                ),
                LlmResponse(
                    stop_reason="tool_use",
                    tool_calls=[ToolCallBlock(id="noop-1", name="noop", input={})],
                ),
                LlmResponse(stop_reason="end_turn", text="done"),
            ]
        )

    # 记录完整系统提示并返回下一步固定响应
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.systems.append(system or "")
        return next(self._responses)


# 功能：验证 working set 合并来源、保留隐藏目录并按 LRU 淘汰
# 设计：容量设为二，重复触达一个路径后加入第三个路径，检查来源集合与淘汰顺序
def test_working_set_is_bounded_and_preserves_hidden_paths() -> None:
    working_set = WorkingSet(max_entries=2)
    working_set.touch(".github/workflows/ci.yml", "read", step=1)
    working_set.touch("src/app.py", "read", step=2, content_hash="abc")
    working_set.touch("src/app.py", "edit", step=3)
    working_set.touch("./tests/test_app.py", "diagnostic", step=4)

    entries = working_set.snapshot()

    assert [entry.path for entry in entries] == ["tests/test_app.py", "src/app.py"]
    assert entries[1].sources == frozenset({"read", "edit"})
    assert entries[1].content_hash == "abc"
    assert ".github/workflows/ci.yml" not in working_set.render_context()


# 功能：验证编辑后诊断只进入下一次模型请求，working set 继续保留路径摘要
# 设计：三步循环依次编辑、无路径工具、结束，比较三次 system prompt 与稳定指纹事件
async def test_edit_diagnostics_are_transient_and_prefix_stays_stable(
    tmp_path: Path,
) -> None:
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    boundary = WorkspaceBoundary(tmp_path)
    report = DiagnosticsReport(
        status="ok",
        tool="pyright",
        diagnostics=(
            Diagnostic(
                path="sample.py",
                line=1,
                column=9,
                severity="error",
                message="Type mismatch",
                rule="reportAssignmentType",
            ),
        ),
    )
    diagnostics_client = _DiagnosticsClient(report)
    provider = _CapturingProvider()
    registry = ToolRegistry()
    registry.register(EditFileTool(boundary))
    registry.register(_NoopTool())
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集诊断和指纹事件用于验证可观测性
    async def _collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(_collect)
    loop = AgentLoop(
        provider,  # type: ignore[arg-type]
        registry,
        bus,
        diagnostics_client=diagnostics_client,  # type: ignore[arg-type]
    )
    context = ExecutionContext(
        run_id="working-set-run",
        goal="edit sample",
        max_steps=5,
    )

    await loop.run(context)

    assert context.status == "success"
    assert "Transient Python Diagnostics" not in provider.systems[0]
    assert "Type mismatch" in provider.systems[1]
    assert "Type mismatch" not in provider.systems[2]
    assert "sample.py (diagnostic,edit" in provider.systems[1]
    assert diagnostics_client.paths == [["sample.py"]]
    lsp_events = [event for event in events if isinstance(event, LspDiagnosticsEvent)]
    assert len(lsp_events) == 1
    assert lsp_events[0].diagnostic_count == 1
    prefixes = [
        event for event in events if isinstance(event, ContextPrefixFingerprintEvent)
    ]
    assert len(prefixes) == 3
    assert prefixes[1].changed_sources == []
    assert prefixes[2].changed_sources == []
