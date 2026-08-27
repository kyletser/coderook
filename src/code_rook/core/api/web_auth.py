from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie

_COOKIE_NAME = "coderook_web_session"
_LAUNCH_TTL_SECONDS = 60
_SESSION_TTL_SECONDS = 12 * 60 * 60


@dataclass(frozen=True)
class WebSession:
    token: str
    csrf_token: str
    expires_at: float


class WebAuthManager:
    # 初始化仅驻留内存的一次性启动票据与浏览器会话存储
    def __init__(
        self,
        *,
        launch_ttl_seconds: int = _LAUNCH_TTL_SECONDS,
        session_ttl_seconds: int = _SESSION_TTL_SECONDS,
    ) -> None:
        self._launch_ttl_seconds = launch_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._launch_tickets: dict[str, float] = {}
        self._sessions: dict[str, WebSession] = {}

    @property
    # 返回公开给 CLI 的一次性启动票据有效期
    def launch_ttl_seconds(self) -> int:
        return self._launch_ttl_seconds

    @property
    # 返回浏览器认证 Cookie 的稳定名称
    def cookie_name(self) -> str:
        return _COOKIE_NAME

    # 清除过期启动票据和浏览器会话，避免守护进程长期积累
    def _purge(self, now: float) -> None:
        self._launch_tickets = {
            token: expires_at
            for token, expires_at in self._launch_tickets.items()
            if expires_at > now
        }
        self._sessions = {
            token: session
            for token, session in self._sessions.items()
            if session.expires_at > now
        }

    # 签发只允许交换一次且短期有效的浏览器启动票据
    def issue_launch_ticket(self) -> str:
        now = time.monotonic()
        self._purge(now)
        token = secrets.token_urlsafe(32)
        self._launch_tickets[token] = now + self._launch_ttl_seconds
        return token

    # 原子消费启动票据并创建独立的 Cookie 与 CSRF 会话凭据
    def exchange(self, launch_token: str) -> WebSession | None:
        now = time.monotonic()
        self._purge(now)
        expires_at = self._launch_tickets.pop(launch_token, None)
        if expires_at is None or expires_at <= now:
            return None
        session = WebSession(
            token=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(24),
            expires_at=now + self._session_ttl_seconds,
        )
        self._sessions[session.token] = session
        return session

    # 从 Cookie header 提取会话并用常量时间比较确认其仍然有效
    def authenticate(self, cookie_header: str | None) -> WebSession | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        morsel = cookie.get(_COOKIE_NAME)
        if morsel is None:
            return None
        presented = morsel.value
        now = time.monotonic()
        self._purge(now)
        for token, session in self._sessions.items():
            if secrets.compare_digest(token, presented):
                return session
        return None

    # 校验写请求携带当前浏览器会话对应的 CSRF token
    def csrf_authorized(self, session: WebSession, presented: str | None) -> bool:
        if presented is None or not presented:
            return False
        return secrets.compare_digest(
            session.csrf_token,
            presented,
        )

    # 生成不允许脚本读取且不跨站发送的本地浏览器会话 Cookie
    def cookie_header(self, session: WebSession) -> str:
        return (
            f"{_COOKIE_NAME}={session.token}; Path=/; HttpOnly; "
            f"SameSite=Strict; Max-Age={self._session_ttl_seconds}"
        )
