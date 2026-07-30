from __future__ import annotations

import json

import httpx
import pytest

from code_rook.core.llm.provider_presets import (
    PROVIDER_PRESETS,
    discover_models,
    get_provider_preset,
)


# 功能：验证系统内置且仅内置四种指定 API 接入方式
# 设计：直接检查稳定标识和固定 endpoint，防止 UI 文案调整意外改变运行配置
def test_builtin_provider_presets_have_expected_endpoints() -> None:
    presets = {preset.id: preset for preset in PROVIDER_PRESETS}

    assert tuple(presets) == ("deepseek", "openai", "anthropic", "siliconflow")
    assert presets["deepseek"].chat_url == "https://api.deepseek.com/chat/completions"
    assert presets["openai"].chat_url == "https://api.openai.com/v1/chat/completions"
    assert presets["anthropic"].models_url == "https://api.anthropic.com/v1/models"
    assert presets["siliconflow"].chat_url == (
        "https://api.siliconflow.cn/v1/chat/completions"
    )


# 功能：验证 Bearer Provider 使用刚输入的 Key 探测模型并过滤 OpenAI 非聊天模型
# 设计：MockTransport 同时断言请求鉴权头和模型排序，避免测试访问真实外部 API
async def test_discover_openai_models_uses_key_and_filters_non_chat() -> None:
    captured: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "text-embedding-3-large"},
                    {"id": "gpt-4.1"},
                    {"id": "gpt-5.6-terra"},
                    {"id": "gpt-realtime-2"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await discover_models(
            get_provider_preset("openai"),
            "sk-test",
            client=client,
        )

    assert models == ["gpt-5.6-terra", "gpt-4.1"]
    assert captured[0].headers["Authorization"] == "Bearer sk-test"
    assert str(captured[0].url) == "https://api.openai.com/v1/models"


# 功能：验证 Anthropic 模型探测使用 x-api-key 和版本头
# 设计：返回一个真实格式的 Claude 模型对象并检查请求头，覆盖与 Bearer API 的协议差异
async def test_discover_anthropic_models_uses_anthropic_headers() -> None:
    captured: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "claude-sonnet-5"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await discover_models(
            get_provider_preset("anthropic"),
            "ant-test",
            client=client,
        )

    assert models == ["claude-sonnet-5"]
    assert captured[0].headers["x-api-key"] == "ant-test"
    assert captured[0].headers["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in captured[0].headers


# 功能：验证硅基流动模型探测只请求聊天文本模型
# 设计：检查官方 Models API 查询参数，确保图片、语音等模型不会进入 TUI 列表
async def test_discover_siliconflow_models_filters_server_side() -> None:
    captured: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen3-Test"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await discover_models(
            get_provider_preset("siliconflow"),
            "sf-test",
            client=client,
        )

    assert models == ["Qwen/Qwen3-Test"]
    assert dict(captured[0].url.params) == {"type": "text", "sub_type": "chat"}


# 功能：验证无效 API Key 会阻止进入模型选择而不是回退到硬编码列表
# 设计：模拟 401 响应并断言明确错误，保证“可用模型”只来源于真实探测结果
async def test_discover_models_rejects_invalid_api_key() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="HTTP 401"):
            await discover_models(
                get_provider_preset("deepseek"),
                "bad-key",
                client=client,
            )


# 功能：验证模型响应结构异常时不会展示伪造的可用模型
# 设计：返回缺少 data 的成功响应，断言解析失败而不是使用推荐模型兜底
async def test_discover_models_rejects_invalid_payload() -> None:
    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"models": []}), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="invalid model list"):
            await discover_models(
                get_provider_preset("deepseek"),
                "test-key",
                client=client,
            )
