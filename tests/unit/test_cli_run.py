from code_rook.cli.commands.run import _delete_transient_session
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
