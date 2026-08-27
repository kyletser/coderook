from __future__ import annotations

import base64
import time

from code_rook.core.api.service import RuntimeApiService
from code_rook.core.api.web_auth import WebAuthManager
from code_rook.core.artifacts.store import ArtifactStore


# 功能：验证启动票据只能交换一次且浏览器 Cookie 能恢复同一认证会话
# 设计：直接使用内存认证管理器，不依赖 HTTP server，从而精确覆盖单次消费不变量
def test_web_launch_ticket_is_single_use() -> None:
    manager = WebAuthManager()
    ticket = manager.issue_launch_ticket()

    session = manager.exchange(ticket)

    assert session is not None
    assert manager.exchange(ticket) is None
    assert manager.authenticate(manager.cookie_header(session)) == session
    assert manager.csrf_authorized(session, session.csrf_token)
    assert not manager.csrf_authorized(session, "wrong")


# 功能：验证过期的一次性票据和浏览器会话都无法继续认证
# 设计：使用极短 TTL 并等待边界越过，覆盖清理逻辑而不访问任何持久凭据
def test_web_auth_rejects_expired_credentials() -> None:
    launch_manager = WebAuthManager(launch_ttl_seconds=0)
    assert launch_manager.exchange(launch_manager.issue_launch_ticket()) is None

    session_manager = WebAuthManager(session_ttl_seconds=0)
    session = session_manager.exchange(session_manager.issue_launch_ticket())
    assert session is not None
    time.sleep(0.001)
    assert session_manager.authenticate(session_manager.cookie_header(session)) is None


# 功能：验证 Web 图片上传校验图片头并生成可供 Turn 附件复用的内容寻址元数据
# 设计：构造最小 PNG 头写入临时 ArtifactStore，避免浏览器或真实图片解码依赖
async def test_web_image_upload_returns_turn_attachment(tmp_path) -> None:
    service = object.__new__(RuntimeApiService)
    service._artifact_store = ArtifactStore(tmp_path / "artifacts")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (2).to_bytes(4, "big") + (3).to_bytes(4, "big")

    attachment = await service.upload_image(base64.b64encode(png).decode("ascii"))

    assert attachment["media_type"] == "image/png"
    assert attachment["width"] == 2
    assert attachment["height"] == 3
    assert (tmp_path / "artifacts" / str(attachment["sha256"])).read_bytes() == png
