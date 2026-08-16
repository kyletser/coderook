from __future__ import annotations

from code_rook.core.authority import RuntimeMode
from code_rook.core.context import ExecutionContext
from code_rook.core.llm.factory import create_provider_for_route
from code_rook.core.llm.openai_compatible import OpenAICompatibleProvider
from code_rook.core.llm.openai_responses import OpenAIResponsesProvider
from code_rook.core.llm.provider import AnthropicProvider, anthropic_thinking_params
from code_rook.core.llm.routes import get_route_preset
from code_rook.core.loop import AgentLoop


# 功能：验证 thinking 档位映射为 Anthropic 预算并同步抬高 max_tokens
# 设计：覆盖 off/low/high 三档，断言 off 不带参数、预算小于 max_tokens 的约束
def test_anthropic_thinking_params_mapping() -> None:
    off_max, off_param = anthropic_thinking_params("off")
    low_max, low_param = anthropic_thinking_params("low")
    high_max, high_param = anthropic_thinking_params("high")

    assert off_max == 8192 and off_param is None
    assert low_param is not None and int(low_param["budget_tokens"]) == 4096  # type: ignore[arg-type]
    assert low_max > 4096
    assert high_param is not None and int(high_param["budget_tokens"]) == 16384  # type: ignore[arg-type]
    assert high_max > 16384


# 功能：验证 route.thinking 透传到三种 wire format 的 provider
# 设计：构造 thinking=high 的各预设 route 经工厂创建，断言 provider 保存档位
def test_factory_passes_thinking_to_providers() -> None:
    anthropic_route = get_route_preset("anthropic").model_copy(
        update={"thinking": "high"}
    )
    openai_route = get_route_preset("openai").model_copy(update={"thinking": "medium"})
    responses_route = get_route_preset("openai").model_copy(
        update={"wire_format": "openai_responses", "thinking": "low"}
    )

    a = create_provider_for_route(anthropic_route, "k")
    b = create_provider_for_route(openai_route, "k")
    c = create_provider_for_route(responses_route, "k")

    assert isinstance(a, AnthropicProvider) and a._thinking == "high"
    assert isinstance(b, OpenAICompatibleProvider) and b._thinking == "medium"
    assert isinstance(c, OpenAIResponsesProvider) and c._thinking == "low"


# 功能：验证 route 模型 thinking 字段默认 off 且接受合法档位
# 设计：默认构造与显式构造对比，非法值经完整校验路径应被 pydantic 拒绝
def test_route_thinking_field_default_and_validation() -> None:
    import pytest
    from pydantic import ValidationError

    from code_rook.core.llm.routes import ProviderRoute

    route = get_route_preset("anthropic")
    assert route.thinking == "off"

    tuned = route.model_copy(update={"thinking": "medium"})
    assert tuned.thinking == "medium"

    payload = route.model_dump()
    payload["thinking"] = "extreme"
    with pytest.raises(ValidationError):
        ProviderRoute.model_validate(payload)


# 功能：验证 PLAN 模式在启用联动时返回 high 覆盖，Act 模式保持 None
# 设计：直接驱动决策方法覆盖 PLAN/ACT 两种 runtime_mode 与开关两种状态
def test_loop_plan_thinking_override_decision() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    plan_ctx = ExecutionContext(run_id="r", goal="g", max_steps=5,
                                runtime_mode=RuntimeMode.PLAN)
    act_ctx = ExecutionContext(run_id="r", goal="g", max_steps=5,
                               runtime_mode=RuntimeMode.ACT)

    loop._escalate_plan_thinking = True
    assert loop._thinking_override_for(plan_ctx) == "high"
    assert loop._thinking_override_for(act_ctx) is None

    loop._escalate_plan_thinking = False
    assert loop._thinking_override_for(plan_ctx) is None


# 功能：验证 DeepSeek 域名在未配置 thinking 时保持默认高推理的旧行为
# 设计：仅注入 client 与 base_url 构造 provider，检查推理开关解析无需真实请求
async def test_deepseek_keeps_default_high_thinking() -> None:
    import httpx

    class _CaptureClient(httpx.AsyncClient):
        # 捕获请求体以便断言 payload 中的推理参数
        def __init__(self) -> None:
            super().__init__()
            self.captured: dict[str, object] = {}

        async def send(self, request: httpx.Request, **_kw: object) -> httpx.Response:  # type: ignore[override]
            import json as json_module

            self.captured = json_module.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 1,
                    },
                },
                request=request,
            )

    from code_rook.core.events.bus import EventBus

    provider = OpenAICompatibleProvider(
        "deepseek-v4",
        base_url="https://api.deepseek.com/v1/chat/completions",
        api_key_env="DS_TEST_KEY",
        api_key="test-key",
        client=_CaptureClient(),
    )

    response = await provider.chat(
        [{"role": "user", "content": "hi"}], [], EventBus(), "run-ds"
    )

    assert response.text == "ok"
    captured = provider._client.captured  # type: ignore[attr-defined]
    assert captured["reasoning_effort"] == "high"
    assert captured["thinking"] == {"type": "enabled"}
