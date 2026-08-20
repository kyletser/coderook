from __future__ import annotations

import io
import urllib.error

import pytest
from scripts import run_crash_recovery_matrix as crash_matrix


# 功能：验证写请求 HTTP 失败包含方法、路径、状态和服务端错误摘要
# 设计：注入真实 HTTPError 响应体且禁止重试，确保远端矩阵失败 artifact 能直接定位端点
def test_request_json_reports_http_context(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(
        url="http://127.0.0.1/v1/threads/t/turns",
        code=500,
        msg="Internal Server Error",
        hdrs=None,
        fp=io.BytesIO(b'{"error":"projection delayed"}'),
    )

    # 模拟服务端立即返回 HTTP 500
    def fail_request(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(crash_matrix.urllib.request, "urlopen", fail_request)

    with pytest.raises(RuntimeError) as caught:
        crash_matrix._request_json(
            7438,
            "test-token",
            "POST",
            "/v1/threads/t/turns",
            {"content": "work"},
        )

    message = str(caught.value)
    assert "POST /v1/threads/t/turns returned HTTP 500" in message
    assert "projection delayed" in message


# 功能：验证重启后的 GET 会重试短暂 500 并返回随后出现的 durable 记录
# 设计：前两次注入 HTTPError、第三次返回最小 JSON 响应，覆盖有界重试而不延长测试时间
def test_request_json_retries_transient_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class _Response:
        # 进入伪造的 urlopen 上下文
        def __enter__(self) -> _Response:
            return self

        # 退出伪造的 urlopen 上下文
        def __exit__(self, *_args: object) -> None:
            return None

        # 返回最小 durable turn JSON
        def read(self) -> bytes:
            return b'{"status":"interrupted"}'

    # 模拟重启期间两次暂态失败后返回 durable 记录
    def eventually_ready(*_args: object, **_kwargs: object) -> _Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise urllib.error.HTTPError(
                url="http://127.0.0.1/v1/turns/t",
                code=500,
                msg="Internal Server Error",
                hdrs=None,
                fp=io.BytesIO(b'{"error":"reconciling"}'),
            )
        return _Response()

    monkeypatch.setattr(crash_matrix.urllib.request, "urlopen", eventually_ready)
    monkeypatch.setattr(crash_matrix.time, "sleep", lambda _seconds: None)

    payload = crash_matrix._request_json(7438, "test-token", "GET", "/v1/turns/t")

    assert attempts == 3
    assert payload == {"status": "interrupted"}


# 功能：验证 Windows 慢 runner 的模型到达窗口保留足够抖动余量
# 设计：锁定 30 秒下限，同时保持等待函数仍要求真实 request_count 达标而非按时间自动成功
def test_model_request_timeout_allows_slow_runner_jitter() -> None:
    assert crash_matrix._MODEL_REQUEST_TIMEOUT_S >= 30.0
