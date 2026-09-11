import asyncio
from pathlib import Path

import pytest

from code_rook.cli.commands import run as run_module
from code_rook.cli.commands.run import (
    _delete_transient_session,
    _run_async,
    _stage_image_paths,
)
from code_rook.core.config import CodeRookConfig
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


# 功能：验证取消 no-session 任务后仍删除已创建的临时会话
# 设计：在真实等待点取消 _run_async，断言先取消运行再删除会话，覆盖此前提前返回的遗漏分支
async def test_cancelled_no_session_run_deletes_transient_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    calls: list[tuple[str, dict[str, object]]] = []

    class _Client:
        # 模拟成功建立 IPC 连接
        async def connect(self) -> None:
            return None

        # 保存事件回调供客户端生命周期完整装配
        def on_event(self, callback: object) -> None:
            self.callback = callback

        # 保持事件循环存活，让测试在运行等待阶段主动取消
        async def run_event_loop(self) -> None:
            await asyncio.Event().wait()

        # 返回固定运行与会话标识并记录取消、删除顺序
        async def send_command(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            calls.append((method, params))
            if method == "event.subscribe":
                return {}
            if method == "agent.run":
                started.set()
                return {"run_id": "run-cancel", "session_id": "sess-temp"}
            if method == "run.cancel":
                return {"accepted": True}
            if method == "session.delete":
                return {"session_id": "sess-temp"}
            raise AssertionError(method)

        # 模拟正常关闭 IPC 连接
        async def close(self) -> None:
            return None

    client = _Client()

    class _Factory:
        # 返回当前测试唯一的假客户端
        @staticmethod
        def from_config(_config: CodeRookConfig) -> _Client:
            return client

    monkeypatch.setattr(run_module, "SocketClient", _Factory)
    task = asyncio.create_task(
        _run_async(
            "wait",
            CodeRookConfig(),
            output_format="json",
            delete_session_after=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    result = await task

    assert result == 130
    assert ("run.cancel", {"run_id": "run-cancel"}) in calls
    assert ("session.delete", {"session_id": "sess-temp"}) in calls
    assert calls.index(("run.cancel", {"run_id": "run-cancel"})) < calls.index(
        ("session.delete", {"session_id": "sess-temp"})
    )


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
