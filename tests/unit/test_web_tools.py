from __future__ import annotations

from pathlib import Path

import httpx
import pytest

import code_rook.core.tools.builtin.web as web_module
from code_rook.core.authority import RuntimeMode
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.builtin.web import (
    WebFetchTool,
    WebSearchTool,
    html_to_text,
    parse_duckduckgo_html,
)
from code_rook.core.tools.spec import ApprovalRequirement


# 构造一个按请求返回预设响应的 mock transport
def _transport(handler):  # type: ignore[no-untyped-def]
    return httpx.MockTransport(handler)


# 功能：验证 HTML 转纯文本会剔除脚本样式并保留正文结构
# 设计：覆盖脚本剔除、块级标签断行与实体解码三类转换，避免正文混入代码
def test_html_to_text_strips_scripts_and_keeps_body() -> None:
    raw = (
        "<html><head><title>T</title><style>.a{}</style></head>"
        "<body><nav>menu</nav><h1>Fast &amp; Safe</h1><p>line one</p>"
        "<script>alert(1)</script><p>line&nbsp;two</p></body></html>"
    )

    text = html_to_text(raw)

    assert "Fast & Safe" in text
    assert "line one" in text
    assert "line two" in text
    assert "alert" not in text
    assert "menu" not in text


# 功能：验证 DuckDuckGo 结果解析能还原 uddg 跳转参数中的真实链接
# 设计：用一段结构等价的 HTML 片段驱动解析器，断言标题、链接与摘要都被清洗
def test_parse_duckduckgo_html_unwraps_redirect_links() -> None:
    raw = """
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fguide&amp;rut=abc">
       Official <b>Guide</b></a>
    <a class="result__snippet">Use the <b>guide</b> for setup &amp; config.</a>
    <a rel="nofollow" class="result__a" href="https://plain.example.org/page">Plain Page</a>
    <a class="result__snippet">A page.</a>
    """

    hits = parse_duckduckgo_html(raw)

    assert len(hits) == 2
    assert hits[0].url == "https://docs.example.com/guide"
    assert hits[0].title == "Official Guide"
    assert "setup & config" in hits[0].snippet
    assert hits[1].url == "https://plain.example.org/page"


# 功能：验证 web_fetch 成功抓取公开 HTML 页并返回标题与正文
# 设计：注入 MockTransport 并放行公网校验，避免测试触网，覆盖文本截断标记
async def test_web_fetch_returns_readable_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_module, "_host_is_public", lambda _host: True)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.com/docs"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><title>Docs</title></head><body><p>hello web</p></body></html>",
        )

    tool = WebFetchTool(transport=_transport(handler))
    result = await tool.invoke({"url": "https://example.com/docs"})

    assert not result.is_error
    assert "title: Docs" in result.content
    assert "hello web" in result.content


# 功能：验证 web_fetch 截断超长正文并追加省略标记
# 设计：放行公网校验后返回长文本，用小 max_chars 触发截断分支
async def test_web_fetch_truncates_long_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_module, "_host_is_public", lambda _host: True)
    body = "<html><body><p>" + "word " * 2000 + "</p></body></html>"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body)

    tool = WebFetchTool(transport=_transport(handler))
    result = await tool.invoke({"url": "https://example.com/big", "max_chars": 500})

    assert not result.is_error
    assert result.content.endswith("…(truncated)")
    assert len(result.content) < 700


# 功能：验证 loopback、私网与非 http 协议地址被 SSRF 防护拒绝
# 设计：直接调用工具，校验函数无需 DNS，断言 schema_error 且不发起网络请求
async def test_web_fetch_blocks_private_targets() -> None:
    tool = WebFetchTool()

    loopback = await tool.invoke({"url": "http://127.0.0.1:8080/admin"})
    private = await tool.invoke({"url": "http://192.168.1.10/router"})
    bad_scheme = await tool.invoke({"url": "file:///etc/passwd"})

    assert loopback.is_error and "non-public" in loopback.content
    assert private.is_error and "non-public" in private.content
    assert bad_scheme.is_error and "http/https" in bad_scheme.content


# 功能：验证重定向到内网地址时逐请求校验仍会拦截
# 设计：MockTransport 返回 302 到 127.0.0.1，公网校验仅放行 example.com，断言第二次请求被钩子拒绝
async def test_web_fetch_blocks_redirect_to_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        web_module, "_host_is_public", lambda host: host == "example.com"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "127.0.0.1" in str(request.url):
            return httpx.Response(200, text="internal")
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    tool = WebFetchTool(transport=_transport(handler))
    result = await tool.invoke({"url": "https://example.com/redirect"})

    assert result.is_error
    assert "non-public" in result.content


# 功能：验证 HTTP 4xx/5xx 返回结构化错误而非异常
# 设计：MockTransport 返回 404，断言错误类型为 runtime_error 且带状态码
async def test_web_fetch_reports_http_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_module, "_host_is_public", lambda _host: True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")

    tool = WebFetchTool(transport=_transport(handler))
    result = await tool.invoke({"url": "https://example.com/missing"})

    assert result.is_error
    assert "http 404" in result.content


# 功能：验证 web_search 解析端点结果并按 max_results 截取
# 设计：注入含两条结果的 HTML，断言输出包含编号、标题、链接与摘要
async def test_web_search_returns_structured_hits() -> None:
    canned = """
    <a class="result__a" href="https://a.example.com/1">Result One</a>
    <a class="result__snippet">First snippet.</a>
    <a class="result__a" href="https://b.example.com/2">Result Two</a>
    <a class="result__snippet">Second snippet.</a>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        assert "q=" in body
        return httpx.Response(200, text=canned)

    tool = WebSearchTool(transport=_transport(handler))
    result = await tool.invoke({"query": "pydantic v2", "max_results": 1})

    assert not result.is_error
    assert "1. Result One" in result.content
    assert "https://a.example.com/1" in result.content
    assert "Result Two" not in result.content


# 功能：验证 web_search 端点结构变化时返回明确错误
# 设计：MockTransport 返回无法解析的页面，断言错误信息提示无结果而非崩溃
async def test_web_search_handles_unparseable_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>consent page</html>")

    tool = WebSearchTool(transport=_transport(handler))
    result = await tool.invoke({"query": "anything"})

    assert result.is_error
    assert "no results" in result.content


# 功能：验证 web 工具在 ACT 模式注册、PLAN 模式被裁剪且默认需要审批
# 设计：用真实 Runner 构建目录，断言模型可见面与 spec 的 approval 语义
def test_web_tools_registered_and_gated(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, bus=EventBus())
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        run_id="run-web",
        bus=EventBus(),
    )
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert {"web_fetch", "web_search"} <= names
    fetch = registry.get("web_fetch")
    search = registry.get("web_search")
    assert fetch is not None and search is not None
    assert not fetch.is_read_only
    assert fetch.build_spec().approval_requirement == ApprovalRequirement.POLICY
    assert search.build_spec().approval_requirement == ApprovalRequirement.POLICY

    plan_registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks-plan"),
        run_id="run-web-plan",
        bus=EventBus(),
        runtime_mode=RuntimeMode.PLAN,
    )
    plan_names = {str(schema["name"]) for schema in plan_registry.tool_schemas()}

    assert "web_fetch" not in plan_names
    assert "web_search" not in plan_names
