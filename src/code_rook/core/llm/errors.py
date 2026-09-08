from __future__ import annotations

import anthropic
import httpx


# 将协议内结构化错误码归一化，绝不从可能包含秘密的错误正文猜测类别
def _stream_failure_code(code: object) -> str:
    return {
        "rate_limit_error": "rate_limit", "rate_limit_exceeded": "rate_limit",
        "overloaded_error": "server_error", "server_error": "server_error",
        "internal_error": "server_error", "api_error": "server_error",
        "timeout": "timeout", "request_timeout": "timeout",
    }.get(str(code), "request_error")


class ProviderRequestError(RuntimeError):
    # 只保留可公开的错误类型和状态码，供循环判断是否允许重试
    def __init__(self, provider: str, error: httpx.HTTPError | anthropic.APIError) -> None:
        self.error_kind = type(error).__name__
        self.status_code = (
            error.response.status_code
            if isinstance(error, (httpx.HTTPStatusError, anthropic.APIStatusError)) else None
        )
        self.retryable = (
            self.status_code in {408, 429} or self.status_code >= 500
            if self.status_code is not None
            else isinstance(error, (
                httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError,
                anthropic.APIConnectionError,
            ))
        )
        self.failure_code = (
            "rate_limit" if self.status_code == 429 else
            "server_error" if self.status_code is not None and self.status_code >= 500 else
            "timeout" if self.status_code == 408 or isinstance(
                error, (httpx.TimeoutException, anthropic.APITimeoutError)
            ) else "transport_error" if self.status_code is None else "request_error"
        )
        self.retry_after_s: float | None = None
        if isinstance(error, anthropic.APIError) and self.status_code is None:
            body = error.body
            if isinstance(body, dict):
                detail_body = body.get("error", body)
                if isinstance(detail_body, dict):
                    category = _stream_failure_code(
                        detail_body.get("code") or detail_body.get("type"),
                    )
                    if category != "request_error":
                        self.failure_code = category
                        self.retryable = True
        if isinstance(error, (httpx.HTTPStatusError, anthropic.APIStatusError)):
            from code_rook.core.llm.retry import parse_retry_after

            self.retry_after_s = parse_retry_after(error.response.headers.get("retry-after"))
        detail = f"HTTP {self.status_code}" if self.status_code is not None else self.error_kind
        super().__init__(f"{provider} request failed ({detail})")


class ProviderStreamError(ProviderRequestError):
    # 将 HTTP 200 流内错误转换为共享请求失败，不向调用方暴露服务端错误正文
    def __init__(self, provider: str, code: object) -> None:
        self.error_kind = "ProviderStreamError"
        self.status_code = None
        self.failure_code = _stream_failure_code(code)
        self.retryable = self.failure_code != "request_error"
        self.retry_after_s = None
        RuntimeError.__init__(self, f"{provider} stream failed ({self.failure_code})")


# 识别兼容接口的 error 事件及响应内错误对象，普通模型增量保持不变
def raise_for_stream_error(provider: str, payload: dict[str, object]) -> None:
    error = payload.get("error")
    if isinstance(error, dict):
        raise ProviderStreamError(provider, error.get("code") or error.get("type"))
    if payload.get("type") == "error":
        raise ProviderStreamError(provider, payload.get("code"))
