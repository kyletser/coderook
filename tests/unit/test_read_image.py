from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

from pydantic import AnyHttpUrl

from code_rook.core.authority import RuntimeMode
from code_rook.core.config import CodeRookConfig
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.openai_compatible import _to_openai_messages
from code_rook.core.llm.openai_responses import _to_responses_input
from code_rook.core.llm.route_registry import ResolvedRoute
from code_rook.core.llm.routes import ProviderRoute
from code_rook.core.loop import AgentLoop
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.builtin.read_image import ReadImageTool
from code_rook.core.workspace import WorkspaceBoundary

# 最小合法 PNG：8 字节签名 + 单像素 IHDR/IDAT/IEND
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


# 构造一个最小合法 PNG 文件的字节串
def _tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# 功能：验证 read_image 返回 base64 image block 且内容含来源说明
# 设计：在临时工作区写入真实 PNG 字节，断言 media_type、data 与提示文本完整
async def test_read_image_returns_base64_block(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(_tiny_png())
    tool = ReadImageTool(WorkspaceBoundary(tmp_path))

    result = await tool.invoke({"path": "shot.png"})

    assert not result.is_error
    assert result.images is not None and len(result.images) == 1
    block = result.images[0]
    assert block["type"] == "image"
    source = block["source"]
    assert isinstance(source, dict)
    assert source["media_type"] == "image/png"
    assert base64.b64decode(str(source["data"])) == _tiny_png()
    assert "[image attached: shot.png" in result.content


# 功能：验证非图片扩展名与超大文件被拒绝且不产生附件
# 设计：写入 .txt 与超 2MB 的 .png，断言错误类型且 images 为空
async def test_read_image_rejects_bad_type_and_size(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    (tmp_path / "big.png").write_bytes(b"\x89PNG" + b"0" * (2 * 1024 * 1024 + 1))
    tool = ReadImageTool(WorkspaceBoundary(tmp_path))

    bad_type = await tool.invoke({"path": "notes.txt"})
    too_big = await tool.invoke({"path": "big.png"})

    assert bad_type.is_error and bad_type.error_type == "schema_error"
    assert "unsupported image type" in bad_type.content
    assert too_big.is_error and "too large" in too_big.content
    assert bad_type.images is None and too_big.images is None


# 功能：验证 openai_chat 转换把 user 消息中的 image block 转为 image_url data URI
# 设计：构造带 image block 的 user 消息调用转换函数，断言 content 变为分段列表
def test_openai_chat_converts_image_block() -> None:
    image_block = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "QUJD",
        },
    }
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "看这张图"}, image_block]}
    ]

    converted = _to_openai_messages(messages, "system-prompt")

    user_row = converted[-1]
    assert user_row["role"] == "user"
    content = user_row["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,QUJD"},
    }


# 功能：验证 Responses API 转换把 image block 转为 input_image 段
# 设计：同一内部消息格式驱动两个 provider 转换，断言各自的原生分段结构
def test_responses_converts_image_block() -> None:
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "WFla"},
    }
    messages = [{"role": "user", "content": [image_block]}]

    items = _to_responses_input(messages)

    assert items[-1]["role"] == "user"
    content = items[-1]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "input_text", "text": ""}
    assert content[1] == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64,WFla",
    }


# 功能：验证 loop 把待发送图片注入下一条 user 消息并在请求后替换为占位符
# 设计：直接驱动 _flush_pending_images 与 _placeholder_flushed_images，断言消息形态变化
def test_loop_flushes_and_placeholders_images() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    context = ExecutionContext(run_id="run-img", goal="g", max_steps=5)
    image_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }
    context.add_pending_image(dict(image_block))

    flushed = loop._flush_pending_images(context)

    assert flushed == 1
    assert context.pending_images == []
    last = context.messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list) and last["content"][0] is not None

    loop._placeholder_flushed_images(context)

    last = context.messages[-1]
    assert isinstance(last["content"], str)
    assert "pixels omitted" in last["content"]


# 功能：验证 read_image 在 ACT 与 PLAN 模式均注册且无待发送图片时不追加消息
# 设计：真实 Runner 构建两种模式目录断言可见；空 pending 调 flush 返回 0
def test_read_image_registered_in_act_and_plan(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=EventBus())
    for mode in (RuntimeMode.ACT, RuntimeMode.PLAN):
        registry = runner._build_registry(
            TaskManager(tmp_path / f".tasks-{mode.value}"),
            run_id=f"run-img-{mode.value}",
            bus=EventBus(),
            runtime_mode=mode,
        )
        names = {str(schema["name"]) for schema in registry.tool_schemas()}
        assert "read_image" in names

    loop = AgentLoop.__new__(AgentLoop)
    context = ExecutionContext(run_id="r", goal="g", max_steps=5)
    before = len(context.messages)

    assert loop._flush_pending_images(context) == 0
    assert len(context.messages) == before


# 功能：验证显式不支持图片的 route 不会向模型暴露 read_image
# 设计：用 ResolvedRoute 驱动真实工具装配，断言能力门禁发生在供应商请求之前
def test_read_image_hidden_for_route_without_image_capability(tmp_path: Path) -> None:
    route = ProviderRoute(
        id="text-only",
        provider="openai-compatible",
        wire_format="openai_chat",
        base_url=AnyHttpUrl("http://127.0.0.1:11434/v1/chat/completions"),
        model="text-model",
        credential_ref="env:TEST_KEY",
        supports_images=False,
    )
    resolved = ResolvedRoute(
        route=route,
        receipt=route.receipt("env"),
        credential="secret",
    )
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=EventBus())

    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks-text-only"),
        run_id="run-text-only",
        bus=EventBus(),
        resolved_route=resolved,
    )

    assert "read_image" not in {
        str(schema["name"]) for schema in registry.tool_schemas()
    }


# 功能：验证 _record_result 把 ToolResult.images 登记进 context 待发送列表
# 设计：构造真实注册表与守卫，手工调用结果记录方法，断言 pending_images 收到块而 tool_result 仍是文本
async def test_record_result_collects_images(tmp_path: Path) -> None:
    from code_rook.core.llm.types import ToolCallBlock
    from code_rook.core.loop import AgentLoop, ReadRepeatGuard, StuckGuard
    from code_rook.core.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(ReadImageTool(WorkspaceBoundary(tmp_path)))
    loop = AgentLoop.__new__(AgentLoop)
    loop._transcript = None
    loop._bus = EventBus()
    loop._registry = registry
    loop._stuck_guard = StuckGuard()
    loop._read_guard = ReadRepeatGuard()
    loop._todo_state = None
    context = ExecutionContext(run_id="run-rec", goal="g", max_steps=5)
    tc = ToolCallBlock(id="tu-1", name="read_image", input={"path": "a.png"})
    result = ToolResult(
        content="[image attached: a.png]",
        images=[
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "QQ"},
            }
        ],
    )

    await loop._record_result(0, 1, tc, result, context)

    assert len(context.pending_images) == 1
    assert context.messages[-1]["content"][0]["type"] == "tool_result"
