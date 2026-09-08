from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.core.agent_runtime.messages import from_provider, to_provider
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.openai_compatible import _to_openai_messages
from code_rook.core.llm.openai_responses import _to_responses_input
from code_rook.core.llm.types import LlmResponse
from code_rook.core.loop import AgentLoop
from code_rook.core.session.store import SessionStore, SessionTranscriptSink
from code_rook.core.tools.base import ToolResult
from code_rook.core.tools.registry import ToolRegistry


# 功能：文字模型可继续带图片的历史，省略只影响实际请求和快照，不修改会话原图。
# 设计：通过真实循环和 Ledger 分别检验用户图片、工具图片及两种模型能力。
@pytest.mark.parametrize("supports_images", [False, True])
async def test_model_switch_preserves_images_in_history(tmp_path: Path, supports_images: bool) -> None:
    image = {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "YWJj",
    }}
    history = [
        {"role": "user", "content": [{"type": "text", "text": "Image"}, image, image]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "r",
            "name": "read", "input": {"path": "image.png"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r",
            "content": [image]}]},
        {"role": "user", "content": "Continue"},
    ]
    store = SessionStore(tmp_path / "sessions")
    store.append_messages("sess-images", history, "old")
    transcript = SessionTranscriptSink(store, "sess-images", "new")
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(stop_reason="end_turn", text="Continued"))
    context = ExecutionContext(run_id="new", goal="Continue", max_steps=2)
    context.messages = deepcopy(history)
    await AgentLoop(provider, ToolRegistry(), EventBus(), supports_images=supports_images,
                    transcript=transcript).run(context)
    assert context.status == "success", context.reason
    sent = provider.chat.call_args.kwargs["messages"]
    assert ("YWJj" in str(sent)) == supports_images
    if not supports_images:
        assert str(sent).count("(image omitted: model does not support images)") == 1
        assert "(tool image omitted: model does not support images)" in str(sent)
    assert "YWJj" in str(store.read_messages("sess-images"))
    assert "YWJj" in str(context.messages)
    assert list(transcript.latest_request_snapshot().messages) == sent


# 功能：验证工具图片在原生历史与两种 OpenAI 请求中保留图片而不是字典字符串。
# 设计：同一份工具结果通过三个格式路径，核对配对 ID、图片 URI 和入参未变。
def test_tool_image_history_and_wire_formats() -> None:
    image = {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "YWJj",
    }}
    result = ToolResult("Image read", images=[image])
    messages = [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "read-one", "content": result.model_content(),
    }]}]
    original = deepcopy(messages)
    assert to_provider(from_provider(messages)) == original
    chat = _to_openai_messages(messages, "")[1:]
    assert chat[0] == {"role": "tool", "tool_call_id": "read-one", "content": "Image read"}
    assert chat[1]["content"][-1]["image_url"]["url"] == "data:image/png;base64,YWJj"
    responses = _to_responses_input(messages)
    assert responses[0] == {
        "type": "function_call_output", "call_id": "read-one", "output": "Image read",
    }
    assert responses[1]["content"][-1]["image_url"] == "data:image/png;base64,YWJj"
    assert messages == original
