from __future__ import annotations

import html as html_module
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.tools.base import BaseTool, ToolResult, ToolRetryPolicy, ToolSideEffect

_USER_AGENT = "CodeRook-agent/0.1 (+local coding agent)"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_REQUEST_TIMEOUT_S = 20.0

# 阻断脚本、样式与导航类区块，避免正文提取混入代码或菜单
_STRIP_TAG_RE = re.compile(
    r"<(script|style|noscript|template|svg|nav|footer|header|aside)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(
    r"</?(p|div|br|li|tr|h[1-6]|section|article|blockquote|pre|ul|ol|table)\b[^>]*>",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\n{3,}")


# 把 HTML 转成可读纯文本：去脚本样式、块级标签断行、实体解码、空行折叠
def html_to_text(raw: str) -> str:
    stripped = _STRIP_TAG_RE.sub(" ", raw)
    stripped = _BLOCK_TAG_RE.sub("\n", stripped)
    stripped = _TAG_RE.sub(" ", stripped)
    text = html_module.unescape(stripped).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return _WHITESPACE_RE.sub("\n\n", "\n".join(lines)).strip()


# 判断主机名解析结果是否全部为公网地址，用于 SSRF 防护
def _host_is_public(host: str) -> bool:
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            parsed = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            return False
        if not parsed.is_global:
            return False
    return True


# 校验单个请求 URL：仅允许 http/https 且目标主机解析到公网
def _validate_url(url: str) -> httpx.URL:
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"invalid url: {url}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"only http/https urls are allowed: {url}")
    if not _host_is_public(parsed.host):
        raise ValueError(f"blocked non-public host: {parsed.host or '(empty)'}")
    return parsed


class WebFetchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: str
    max_chars: int = Field(default=20000, ge=200, le=100000)


class WebFetchTool(BaseTool):
    name = "web_fetch"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    can_parallel = True
    retry_policy = ToolRetryPolicy.IDEMPOTENT
    description = (
        "Fetch a public http(s) page and return readable text. "
        "Use for documentation pages, raw source files, and API references."
    )
    params_model = WebFetchParams
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "absolute http/https URL"},
            "max_chars": {
                "type": "integer",
                "minimum": 200,
                "maximum": 100000,
                "description": "truncate returned text to this length (default 20000)",
            },
        },
        "required": ["url"],
    }

    # 可注入自定义 transport 供测试复现响应与重定向
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    # 逐请求校验 URL，确保重定向不会绕过 SSRF 防护
    async def _request_hook(self, request: httpx.Request) -> None:
        _validate_url(str(request.url))

    # 抓取公开网页并返回可读正文
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = WebFetchParams.model_validate(params)
        try:
            url = _validate_url(parsed.url)
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="schema_error")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=_REQUEST_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
                transport=self._transport,
                event_hooks={"request": [self._request_hook]},
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                raw = response.text[:_MAX_RESPONSE_BYTES]
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                content=f"http {exc.response.status_code} for {parsed.url}",
                is_error=True,
                error_type="runtime_error",
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(
                content=f"fetch failed: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            body = html_to_text(raw)
        else:
            body = raw.strip()
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw, re.IGNORECASE | re.DOTALL)
        title = html_module.unescape(title_match.group(1)).strip() if title_match else ""
        if len(body) > parsed.max_chars:
            body = body[: parsed.max_chars] + "\n…(truncated)"
        header = f"url: {response.url}\ntitle: {title or '(none)'}\n\n"
        return ToolResult(content=header + body)


class WebSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str
    max_results: int = Field(default=5, ge=1, le=10)


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


# 从 DuckDuckGo HTML 结果页解析标题、真实链接与摘要
def parse_duckduckgo_html(raw: str) -> list[SearchHit]:
    hits: list[SearchHit] = []
    pattern = re.compile(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    for href, title, snippet in pattern.findall(raw):
        link = unquote(href)
        if "uddg=" in link:
            maybe = parse_qs(urlparse(link).query).get("uddg", [""])[0]
            if maybe:
                link = maybe
        if link.startswith("//duckduckgo.com"):
            continue
        clean_title = html_to_text(title)
        clean_snippet = html_to_text(snippet)
        if clean_title:
            hits.append(SearchHit(title=clean_title, url=link, snippet=clean_snippet))
    return hits


class WebSearchTool(BaseTool):
    name = "web_search"
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    can_parallel = True
    description = (
        "Search the public web and return ranked results with title, url, and snippet. "
        "Use for current library versions, error messages, and documentation lookups."
    )
    params_model = WebSearchParams
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "number of results to return (default 5)",
            },
        },
        "required": ["query"],
    }

    # 绑定搜索端点与可注入 transport；默认使用免 key 的 DuckDuckGo HTML 端点
    def __init__(
        self,
        base_url: str = "https://html.duckduckgo.com/html/",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._transport = transport

    # 调用搜索端点并解析为结构化结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = WebSearchParams.model_validate(params)
        query = parsed.query.strip()
        if not query:
            return ToolResult(
                content="query must not be empty",
                is_error=True,
                error_type="schema_error",
            )
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_S,
                headers={"User-Agent": _USER_AGENT},
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self._base_url,
                    data={"q": quote_plus(query).replace("%20", "+")},
                )
                response.raise_for_status()
                hits = parse_duckduckgo_html(response.text[:_MAX_RESPONSE_BYTES])
        except httpx.HTTPStatusError as exc:
            return ToolResult(
                content=f"search http {exc.response.status_code}",
                is_error=True,
                error_type="runtime_error",
            )
        except httpx.HTTPError as exc:
            return ToolResult(
                content=f"search failed: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        if not hits:
            return ToolResult(
                content=f"no results for {query!r} (endpoint layout may have changed)",
                is_error=True,
                error_type="runtime_error",
            )
        lines: list[str] = [f"web search results for: {query}"]
        for index, hit in enumerate(hits[: parsed.max_results], start=1):
            lines.append(f"{index}. {hit.title}\n   {hit.url}\n   {hit.snippet}")
        return ToolResult(content="\n".join(lines))


# 供注册表展示的工具导出清单
WEB_TOOLS: tuple[type[BaseTool], ...] = (WebFetchTool, WebSearchTool)
