from __future__ import annotations

import json

import httpx
import pytest

from code_rook.core.llm.provider_presets import (
    PROVIDER_PRESETS,
    discover_models,
    get_provider_preset,
    probe_local_provider,
)


# 功能：验证统一 catalog 覆盖云端与本地九种正式 Provider
# 设计：直接检查稳定标识、关键 endpoint 和免密标记，防止 CLI 与配置层各自漂移
def test_builtin_provider_presets_have_expected_endpoints() -> None:
    presets = {preset.id: preset for preset in PROVIDER_PRESETS}

    assert tuple(presets) == (
        "deepseek",
        "openai",
        "anthropic",
        "siliconflow",
        "gemini",
        "moonshot",
        "openrouter",
        "ollama",
        "lm-studio",
    )
    assert presets["deepseek"].chat_url == "https://api.deepseek.com/chat/completions"
    assert presets["openai"].chat_url == "https://api.openai.com/v1/chat/completions"
    assert presets["anthropic"].models_url == "https://api.anthropic.com/v1/models"
    assert presets["siliconflow"].chat_url == ("https://api.siliconflow.cn/v1/chat/completions")
    assert presets["gemini"].api_key_env == "GEMINI_API_KEY"
    assert presets["moonshot"].chat_url == ("https://api.moonshot.ai/v1/chat/completions")
    assert presets["openrouter"].models_url == "https://openrouter.ai/api/v1/models"
    assert presets["ollama"].credential_required is False
    assert presets["lm-studio"].credential_required is False
    assert get_provider_preset("kimi").id == "moonshot"
    assert get_provider_preset("lm_studio").id == "lm-studio"


# 功能：验证本地 Provider 模型发现不要求 API key 且不发送 Authorization
# 设计：用 MockTransport 返回 OpenAI 模型列表并检查请求头，测试全程不访问真实本地端口
async def test_discover_local_models_without_api_key() -> None:
    captured: list[httpx.Request] = []

    # 记录本地模型发现请求并返回最小兼容响应
    async def respond(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": [{"id": "qwen3-coder"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        models = await discover_models(get_provider_preset("ollama"), client=client)

    assert models == ["qwen3-coder"]
    assert "Authorization" not in captured[0].headers


# 功能：验证轻量本地探测把 catalog endpoint 的主机端口传给可注入连接器
# 设计：注入纯内存异步连接器并断言 11434 参数，避免测试启动服务或访问真实网络
async def test_probe_local_provider_uses_injected_connector() -> None:
    calls: list[tuple[str, int, float]] = []

    # 模拟端口可达并记录探测参数
    async def connector(host: str, port: int, timeout_s: float) -> bool:
        calls.append((host, port, timeout_s))
        return True

    result = await probe_local_provider("ollama", connector=connector, timeout_s=0.2)

    assert result.reachable is True
    assert calls == [("127.0.0.1", 11434, 0.2)]


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
