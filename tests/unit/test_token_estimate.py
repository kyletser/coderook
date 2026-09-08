from unittest.mock import MagicMock

from code_rook.core.agent_runtime.token_estimate import estimate_conversation_tokens
from code_rook.core.compact.protocol import estimate_messages_tokens
from code_rook.core.context import ExecutionContext
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.types import LlmResponse, UsageStats, estimate_request_input_tokens
from code_rook.core.loop import AgentLoop
from code_rook.core.tools.registry import ToolRegistry


# 功能：验证窗口预测采用真实 usage 加新增消息，而不是重新估算已计费历史。
# 设计：使用与字符数明显不同的真实用量及缓存用量，核对输出和新增工具结果只计一次。
def test_next_request_estimate_anchors_to_provider_usage() -> None:
    context = ExecutionContext(run_id="estimate", goal="x" * 100_000, max_steps=5)
    context.add_assistant_message([{"type": "text", "text": "done"}])
    usage_count = len(context.messages)
    context.messages.append({"role": "user", "content": "next"})
    response = LlmResponse(stop_reason="end_turn", usage=UsageStats(
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=2000,
        cache_creation_input_tokens=1000, context_pct=0.4,
    ))
    loop = AgentLoop(MagicMock(), ToolRegistry(), EventBus())
    projected = loop._projected_context_pct(
        context, response, output_reserve_tokens=1000, usage_message_count=usage_count,
    )
    assert projected == 5101 / 10000


# 功能：验证直接附件和工具图片的上下文估算不随 base64 大小线性膨胀。
# 设计：对比相同图片块的短编码和百万字符编码，覆盖压缩预算及 Provider 缺失 usage 的估算。
def test_image_estimates_ignore_encoded_size() -> None:
    for nested in (False, True):
        estimates = []
        for size in (4, 1_000_000):
            blocks = [{"type": "image", "source": {"type": "base64", "data": "A" * size}}]
            content = [{"type": "tool_result", "tool_use_id": "one", "content": blocks}]
            messages = [{"role": "user", "content": content if nested else blocks}]
            assert estimate_conversation_tokens(messages) == 1200
            assert estimate_messages_tokens(messages) == 1200
            estimates.append(estimate_request_input_tokens(messages, [], "system"))
        assert estimates[0] == estimates[1]


# 功能：验证思考正文、工具名称及参数计入估算，而签名不计入。
# 设计：改变签名长度但保持模型可见内容，检查结果稳定并覆盖非文本消息结构。
def test_estimate_counts_tool_arguments_not_signatures() -> None:
    messages = [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "four", "signature": "x" * 1000},
        {"type": "toolCall", "name": "read", "arguments": {}},
    ]}]
    assert estimate_conversation_tokens(messages) == 3
