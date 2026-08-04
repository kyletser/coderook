from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from code_rook.core.app import CoreApp
from code_rook.core.runtime.models import (
    ThreadRecord,
    ThreadStatus,
    TurnItemKind,
    TurnItemRecord,
    TurnRecord,
    TurnStatus,
)
from code_rook.core.session.model import Session


# 返回协议测试使用的固定时间
def _now() -> datetime:
    return datetime(2026, 8, 4, tzinfo=UTC)


class _Sessions:
    # 初始化可观察的 session facade 调用记录
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    # 创建固定 session
    async def create(self, mode: str, title: str = "") -> Session:
        self.calls.append(("create", title))
        return Session("thread-1", mode, "waiting_for_input", title, "t", "t")  # type: ignore[arg-type]

    # 更新 session 标题
    async def rename(self, session_id: str, title: str) -> Session:
        self.calls.append(("rename", title))
        return Session(session_id, "chat", "waiting_for_input", title, "t", "t")

    # 归档 session
    async def close(self, session_id: str) -> None:
        self.calls.append(("close", session_id))

    # 运行一次 turn 并记录调用
    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        run_id: str | None = None,
        runtime_mode: object = None,
    ) -> str:
        self.calls.append(("send", content))
        return run_id or ""

    # 中断 turn
    async def cancel_run(self, run_id: str) -> str:
        self.calls.append(("interrupt", run_id))
        return "thread-1"

    # 向 turn 注入纠偏消息
    async def steer_run(self, run_id: str, content: str) -> None:
        self.calls.append(("steer", content))


class _Runtime:
    # 初始化固定的 durable runtime 投影
    def __init__(self, workspace: Path) -> None:
        self.thread = ThreadRecord(
            id="thread-1",
            title="Thread",
            workspace=str(workspace),
            created_at=_now(),
            updated_at=_now(),
        )
        self.turn = TurnRecord(
            id="turn-1",
            thread_id="thread-1",
            status=TurnStatus.RUNNING,
            created_at=_now(),
            updated_at=_now(),
        )
        self.item = TurnItemRecord(
            id="item-1",
            turn_id="turn-1",
            kind=TurnItemKind.MESSAGE,
            payload={"content": "hello"},
            created_at=_now(),
        )

    # 返回 thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        assert thread_id == self.thread.id
        return self.thread

    # 列出 thread
    async def list_threads(self) -> list[ThreadRecord]:
        return [self.thread, self.thread.model_copy(update={"id": "archived", "status": ThreadStatus.ARCHIVED})]

    # 返回 turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        assert turn_id in {self.turn.id} or turn_id.startswith("run-")
        return self.turn

    # 列出 turn
    async def list_turns(self, thread_id: str) -> list[TurnRecord]:
        assert thread_id == self.thread.id
        return [self.turn]

    # 列出 item
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        assert turn_id == self.turn.id
        return [self.item]


# 功能：正式 thread/turn handlers 全部通过 session facade 写入并从 runtime 读取
# 设计：用独立可观察 stub 串联 CRUD、启动、纠偏、中断和 items，避免 handler 偷读私有内存
async def test_runtime_contract_handlers_project_one_durable_state(tmp_path: Path) -> None:
    app = CoreApp()
    sessions = _Sessions()
    runtime = _Runtime(tmp_path)
    app._sessions = sessions  # type: ignore[assignment]
    app._runtime = runtime  # type: ignore[assignment]

    created = await app._thread_create_handler({"title": "Thread"})  # type: ignore[attr-defined]
    listed = await app._thread_list_handler({})  # type: ignore[attr-defined]
    fetched = await app._thread_get_handler({"thread_id": "thread-1"})  # type: ignore[attr-defined]
    updated = await app._thread_update_handler(  # type: ignore[attr-defined]
        {"thread_id": "thread-1", "title": "Renamed"}
    )
    archived = await app._thread_archive_handler(  # type: ignore[attr-defined]
        {"thread_id": "thread-1"}
    )
    started = await app._turn_start_handler(  # type: ignore[attr-defined]
        {"thread_id": "thread-1", "content": "Work"}
    )
    await asyncio.gather(*app._running_runs)  # type: ignore[attr-defined]
    turn = await app._turn_get_handler({"turn_id": "turn-1"})  # type: ignore[attr-defined]
    turns = await app._turn_list_handler({"thread_id": "thread-1"})  # type: ignore[attr-defined]
    steered = await app._turn_steer_handler(  # type: ignore[attr-defined]
        {"turn_id": "turn-1", "content": "Adjust"}
    )
    interrupted = await app._turn_interrupt_handler(  # type: ignore[attr-defined]
        {"turn_id": "turn-1"}
    )
    items = await app._turn_items_handler({"turn_id": "turn-1"})  # type: ignore[attr-defined]
    capabilities = await app._runtime_capabilities_handler({})  # type: ignore[attr-defined]

    assert created.thread.id == fetched.thread.id == "thread-1"
    assert [thread.id for thread in listed.threads] == ["thread-1"]
    assert updated.thread.id == archived.thread.id == "thread-1"
    assert started.turn_id
    assert turn.turn == turns.turns[0] == steered.turn == interrupted.turn
    assert items.items == [runtime.item]
    assert "durable_threads" in capabilities.features
    assert ("rename", "Renamed") in sessions.calls
    assert ("steer", "Adjust") in sessions.calls
    assert ("interrupt", "turn-1") in sessions.calls
