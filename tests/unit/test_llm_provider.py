from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.llm import openai_compatible as openai_module
from code_rook.core.llm import provider as provider_module
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.provider import AnthropicProvider
from code_rook.core.llm.types import LlmResponse

# --- helpers -----------------------------------------------------------------


def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_create: int = 0,
) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_create
    return u


def _make_final(
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> MagicMock:
    msg = MagicMock()
    msg.stop_reason = stop_reason
    msg.content = content or []
    msg.usage = _make_usage(input_tokens, output_tokens, cache_read)
    return msg


class FakeStream:
    """Minimal async context manager that fakes the anthropic streaming interface."""

    def __init__(self, texts: list[str], final: MagicMock) -> None:
        self._texts = texts
        self._final = final

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    @property
    def text_stream(self):  # type: ignore[return]
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    async def get_final_message(self) -> MagicMock:
        return self._final


def _make_provider(
    texts: list[str] | None = None,
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> tuple[AnthropicProvider, MagicMock]:
    final = _make_final(stop_reason, content, input_tokens, output_tokens, cache_read)
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(texts or [], final)
    return AnthropicProvider(model="test-model", client=client), client


async def _chat(
    provider: AnthropicProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    collected: list[BaseModel] = []
    bus = EventBus()

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r1",
    )
    return result, collected


# --- tests -------------------------------------------------------------------


# 功能：验证每次 chat 调用都发布携带模型名和路由策略的 llm.model_selected 事件
# 设计：检查事件数量为 1 且字段精确匹配，因为 S6 路由报告依赖此事件统计模型使用分布
async def test_model_selected_event_published() -> None:
    provider, _ = _make_provider()
    _, events = await _chat(provider)
    sel = [e for e in events if e.type == "llm.model_selected"]  # type: ignore[attr-defined]
    assert len(sel) == 1
    assert sel[0].model == "test-model"  # type: ignore[attr-defined]
    assert sel[0].strategy == "static"  # type: ignore[attr-defined]
    assert sel[0].run_id == "r1"  # type: ignore[attr-defined]


# 功能：验证流式响应的每个 token 触发独立的 llm.token 事件，内容与顺序均正确
# 设计：使用 FakeStream 控制精确的 token 序列，断言数量和各 token 的值，排除批量合并或跳过情况
async def test_token_events_published_per_chunk() -> None:
    provider, _ = _make_provider(texts=["Hello", " world"])
    _, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert len(tokens) == 2
    assert tokens[0].token == "Hello"  # type: ignore[attr-defined]
    assert tokens[1].token == " world"  # type: ignore[attr-defined]


# 功能：验证 llm.usage 事件中的 token 统计字段（input、output、cache_read）正确
# 设计：向 FakeStream 注入固定 usage 值，断言三个字段精确匹配，因为这些字段是 S6 成本计算的数据源
async def test_usage_event_published_after_stream() -> None:
    provider, _ = _make_provider(input_tokens=200, output_tokens=75, cache_read=150)
    _, events = await _chat(provider)
    usage_events = [e for e in events if e.type == "llm.usage"]  # type: ignore[attr-defined]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.input_tokens == 200  # type: ignore[attr-defined]
    assert ue.output_tokens == 75  # type: ignore[attr-defined]
    assert ue.cache_read_input_tokens == 150  # type: ignore[attr-defined]


# 功能：验证事件发布顺序为 model_selected → token（×N） → usage
# 设计：检查类型列表的首尾元素，聚焦 provider 的时序契约，而非中间 token 事件的顺序（那由流本身决定）
async def test_event_order_model_selected_first_usage_last() -> None:
    provider, _ = _make_provider(texts=["hi"])
    _, events = await _chat(provider)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "llm.model_selected"
    assert "llm.token" in types
    assert types[-1] == "llm.usage"


# 功能：验证 stop_reason=tool_use 时 final_message content block 被正确解析为 ToolCallBlock
# 设计：注入带 tool_use block 的 final_message，逐字段检查 ToolCallBlock 的 id/name/input，确认解析路径完整
async def test_tool_use_parsed_from_final_message() -> None:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "toolu_01"
    tool_block.name = "read_file"
    tool_block.input = {"path": "README.md"}
    provider, _ = _make_provider(stop_reason="tool_use", content=[tool_block])
    result, _ = await _chat(provider)
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "read_file"
    assert tc.input == {"path": "README.md"}


# 功能：验证 stop_reason=end_turn 时不产生任何工具调用
# 设计：与 test_tool_use_parsed 互补，确认 stop_reason 决策树的另一侧，防止 end_turn 响应被误解析为 tool_use
async def test_end_turn_produces_no_tool_calls() -> None:
    provider, _ = _make_provider(stop_reason="end_turn")
    result, _ = await _chat(provider)
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []


# 功能：验证多个流式 token 被正确拼接为 LlmResponse.text 字段
# 设计：三段 token 拼接，检查 result.text 完整字符串，因为 StdoutPrinter 消费 text 字段展示最终输出
async def test_text_accumulated_from_tokens() -> None:
    provider, _ = _make_provider(texts=["foo", "bar", "baz"])
    result, _ = await _chat(provider)
    assert result.text == "foobarbaz"


# 功能：验证缺少 ANTHROPIC_API_KEY 时 provider 初始化立即 SystemExit 而非等到调用时才报错
# 设计：用 monkeypatch 清除环境变量后实例化，确认 fail-fast 行为，防止"幽灵 run"（有 started 但无 finished 事件）
async def test_missing_api_key_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        AnthropicProvider(model="any")


# 功能：验证空流式响应不发布任何 llm.token 事件且 result.text 为空字符串
# 设计：texts=[] 覆盖零 token 边界条件，确认 text="" 而非 None，避免调用方对空内容做额外 None 判断
async def test_no_tokens_when_response_is_empty() -> None:
    provider, _ = _make_provider(texts=[])
    result, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert tokens == []
    assert result.text == ""


# 功能：per-agent 模型覆盖后使用覆盖模型的 context window 计算占用比例
# 设计：为默认模型和覆盖模型注入不同窗口，固定 input tokens 后断言结果采用覆盖模型分母
async def test_model_override_uses_resolved_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, _ = _make_provider(input_tokens=100)
    monkeypatch.setitem(provider_module._MODEL_CONTEXT_WINDOWS, "test-model", 2_000)
    monkeypatch.setitem(provider_module._MODEL_CONTEXT_WINDOWS, "override-model", 1_000)

    result = await provider.chat(
        messages=[],
        tool_schemas=[],
        bus=EventBus(),
        run_id="r-model",
        model="override-model",
    )

    assert result.usage is not None
    assert result.usage.context_pct == pytest.approx(0.1)


# 功能：OpenAI-compatible provider 同样按覆盖模型发送请求并计算 context 水位
# 设计：MockTransport 捕获请求 model，并为两个模型设置不同窗口以验证请求与 usage 使用同一模型
async def test_openai_model_override_uses_resolved_context_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_models: list[str] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_models.append(str(payload["model"]))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 64, "completion_tokens": 4},
            },
        )

    monkeypatch.setenv("TEST_OPENAI_KEY", "test-key")
    monkeypatch.setitem(openai_module._MODEL_CONTEXT_WINDOWS, "default-model", 256)
    monkeypatch.setitem(openai_module._MODEL_CONTEXT_WINDOWS, "override-model", 128)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            "default-model",
            base_url="https://example.test/v1/chat/completions",
            api_key_env="TEST_OPENAI_KEY",
            client=client,
        )
        result = await provider.chat(
            messages=[],
            tool_schemas=[],
            bus=EventBus(),
            run_id="r-openai-model",
            model="override-model",
        )

    assert seen_models == ["override-model"]
    assert result.usage is not None
    assert result.usage.context_pct == pytest.approx(0.5)


# 功能：验证 OpenAI 官方推理模型使用 max_completion_tokens 请求字段
# 设计：注入 MockTransport 捕获请求体，确保官方 preset 不发送不兼容的 max_tokens
async def test_openai_official_uses_max_completion_tokens() -> None:
    captured: list[dict[str, object]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            "gpt-5.6-terra",
            base_url="https://api.openai.com/v1/chat/completions",
            api_key_env="OPENAI_API_KEY",
            api_key="test-key",
            use_max_completion_tokens=True,
            client=client,
        )
        await provider.chat(
            messages=[],
            tool_schemas=[],
            bus=EventBus(),
            run_id="r-openai-official",
        )

    assert captured[0]["max_completion_tokens"] == 8192
    assert "max_tokens" not in captured[0]


# 功能：验证 DeepSeek thinking 模式单独发布英文 reasoning 且工具前正文不冒充思考
# 设计：同时注入 reasoning、中文 content 和工具调用，检查请求开关、事件类型及后续上下文回填
async def test_deepseek_reasoning_is_separate_and_preserved() -> None:
    captured: list[dict[str, object]] = []

    # 返回同时包含 reasoning、普通正文和工具调用的 DeepSeek 响应
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{
                    "message": {
                        "reasoning_content": "I should inspect the workspace first.",
                        "content": "我先检查工作区。",
                        "tool_calls": [{
                            "id": "call-2",
                            "type": "function",
                            "function": {
                                "name": "list_dir",
                                "arguments": "{}",
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 8},
            },
        )

    messages: list[dict[str, object]] = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Earlier reasoning."},
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "list_dir",
                    "input": {},
                },
            ],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "call-1",
                "content": "README.md",
            }],
        },
    ]
    events: list[BaseModel] = []
    bus = EventBus()

    # 收集 Provider 发布的事件以核对 reasoning 与正文边界
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            "deepseek-v4-flash",
            base_url="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
            api_key="test-key",
            client=client,
        )
        result = await provider.chat(
            messages=messages,
            tool_schemas=[{
                "name": "list_dir",
                "description": "List files",
                "input_schema": {"type": "object"},
            }],
            bus=bus,
            run_id="r-deepseek",
        )

    payload = captured[0]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    sent_messages = payload["messages"]
    assert isinstance(sent_messages, list)
    assert sent_messages[1]["reasoning_content"] == "Earlier reasoning."
    reasoning_events = [event for event in events if event.type == "llm.reasoning"]  # type: ignore[attr-defined]
    assert len(reasoning_events) == 1
    assert reasoning_events[0].content == "I should inspect the workspace first."  # type: ignore[attr-defined]
    assert not [event for event in events if event.type == "llm.token"]  # type: ignore[attr-defined]
    assert result.thinking_blocks == [{
        "type": "thinking",
        "thinking": "I should inspect the workspace first.",
    }]


# 功能：验证 OpenAI-compatible 的日志和异常都不会包含响应正文或 API key
# 设计：服务端在 401 正文回显密钥，同时检查 caplog、异常字符串及异常链均已脱敏
async def test_openai_compatible_http_error_is_fully_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 返回带密钥正文的认证失败响应
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, text="invalid provider-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = OpenAICompatibleProvider(
            "test-model",
            base_url="https://api.example.test/chat/completions",
            api_key_env="TEST_API_KEY",
            api_key="provider-secret",
            client=client,
        )
        with pytest.raises(RuntimeError) as captured:
            await provider.chat([], [], EventBus(), "run-redacted")

    assert "HTTP 401" in str(captured.value)
    assert "provider-secret" not in str(captured.value)
    assert "provider-secret" not in caplog.text
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
