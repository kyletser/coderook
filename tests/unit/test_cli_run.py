from pathlib import Path

from code_rook.cli.commands.run import _delete_transient_session, _stage_image_paths
from code_rook.core.transport.socket_client import IpcError


class _BusyThenDeletedClient:
    # 初始化可重复触发会话忙竞态的假客户端。
    def __init__(self) -> None:
        self.calls = 0

    # 前两次模拟 run.finished 后尚未释放的会话锁，第三次删除成功。
    async def send_command(self, method: str, params: dict[str, str]) -> dict[str, object]:
        assert method == "session.delete"
        assert params == {"session_id": "sess-temp"}
        self.calls += 1
        if self.calls < 3:
            raise IpcError(-32012, "session busy")
        return {"session_id": "sess-temp"}


# 功能：临时会话清理会等待 run 完成后的短暂锁释放窗口。
# 设计：模拟两次 busy 后成功，验证只重试该确定性竞态且最终不留下警告。
async def test_delete_transient_session_retries_busy_state() -> None:
    client = _BusyThenDeletedClient()

    result = await _delete_transient_session(client, "sess-temp")  # type: ignore[arg-type]

    assert result is None
    assert client.calls == 3


# 功能：验证命令行图片被写入当前工作区 ArtifactStore 并生成完整附件元数据。
# 设计：构造带有效尺寸头的最小 PNG，避免依赖 Pillow 或真实 Provider。
async def test_stage_image_paths_creates_replayable_attachment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (2).to_bytes(4, "big") + (3).to_bytes(4, "big")
    )
    monkeypatch.chdir(tmp_path)

    attachments = await _stage_image_paths([image])

    assert len(attachments) == 1
    assert attachments[0].media_type == "image/png"
    assert (tmp_path / ".coderook" / "artifacts" / attachments[0].sha256).is_file()
