from __future__ import annotations

import asyncio
from typing import Any

from code_rook.core.authority import RuntimeMode
from code_rook.tui.connection import TuiConnection


# 功能：验证 daemon 尚未启动时首次拒绝连接会立即禁用输入并显示恢复建议
# 设计：让 fake client 在 connect 阶段抛出 ConnectionRefusedError，等首次状态回调后取消重试循环
async def test_initial_connection_refusal_marks_app_disconnected() -> None:
    attempted = asyncio.Event()

    class _RefusingSocket:
        # 模拟目标端口没有监听并通知测试首次尝试已经发生
        async def connect(self) -> None:
            attempted.set()
            raise ConnectionRefusedError

    class _FakeApp:
        # 初始化断线状态与产品提示记录
        def __init__(self) -> None:
            self._client = None
            self.states: list[str] = []
            self.disconnected = 0
            self.problems: list[tuple[str, str]] = []

        # 记录顶栏状态而不依赖 Textual 消息泵
        def _update_header(self, state: str) -> None:
            self.states.append(state)

        # 记录输入框被切换为断线状态
        def _mark_disconnected(self) -> None:
            self.disconnected += 1

        # 记录产品层恢复建议的类别和详情
        def _show_connection_problem(self, kind: str, detail: str) -> None:
            self.problems.append((kind, detail))

        # 提供连接层启动时查询的最小 header 替身
        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                # 接收 header markup 更新
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    connection = TuiConnection(
        app,
        None,
        host="127.0.0.1",
        port=9999,
        client_factory=lambda _host, _port, *, auth_token=None: _RefusingSocket(),
    )
    task = asyncio.create_task(connection.run())
    try:
        await asyncio.wait_for(attempted.wait(), timeout=1)
        while not app.problems:
            await asyncio.sleep(0)
        assert app.states == ["disconnected"]
        assert app.disconnected == 1
        assert app.problems == [("unreachable", "cannot connect to 127.0.0.1:9999")]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证断线后 TuiConnection 重连并恢复同一会话
# 设计：注入按序生产的 fake client 工厂模拟“连接→断线→重连”，断言两次 session.resume 命中同一会话 id
async def test_reconnect_resumes_same_session() -> None:
    resumed: list[str] = []
    created: list[str] = []
    block = asyncio.Event()

    class _FakeSocket:
        def __init__(self, index: int) -> None:
            self.index = index
            self.handler: Any = None
            self.closed = False

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def run_event_loop(self) -> None:
            # 首条连接立即返回以模拟断线；后续连接阻塞以维持连接存活
            if self.index >= 1:
                await block.wait()

        async def send_command(self, method: str, params: dict) -> dict:
            if method == "session.create":
                created.append(str(params.get("mode", "")))
                return {"session_id": "sess-fixed"}
            if method == "session.resume":
                resumed.append(str(params["session_id"]))
                return {
                    "session": {"session_id": "sess-fixed", "title": "Existing"},
                    "messages": [],
                }
            if method == "session.get_history":
                return {"messages": []}
            if method == "event.subscribe":
                return {}
            return {}

        def on_event(self, handler: Any) -> None:
            self.handler = handler

    class _FakeApp:
        def __init__(self) -> None:
            self._client = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = "sess-fixed"
            self._replay_run_id: str | None = None
            self._history_loaded = True
            self._session_title = ""
            self._titled = False
            self._first_user_text = ""
            self._plan_review_pending = False
            self._input_runtime_mode = RuntimeMode.ACT
            self.states: list[str] = []
            self.mark_connected = 0
            self.mark_disconnected = 0
            self.break_llm = 0

        def _update_header(self, state: str) -> None:
            self.states.append(state)

        async def _refresh_authority(self) -> None:
            return None

        def _break_llm(self) -> None:
            self.break_llm += 1

        def _append_history(self, _messages: list[dict[str, Any]]) -> None:
            return None

        def _prompt(self) -> None:
            return None

        def _mark_connected(self) -> None:
            self.mark_connected += 1

        def _mark_disconnected(self) -> None:
            self.mark_disconnected += 1

        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    count = 0

    def factory(_host: str, _port: int, *, auth_token: str | None = None) -> Any:
        nonlocal count
        client = _FakeSocket(count)
        count += 1
        return client

    conn = TuiConnection(
        app,
        None,
        host="127.0.0.1",
        port=9999,
        auth_token="tok",
        client_factory=factory,
    )
    task = asyncio.create_task(conn.run())
    try:
        # 等待第二次连接完成会话恢复（重连成功）
        while app.mark_connected < 2:
            await asyncio.sleep(0.05)
        assert resumed == ["sess-fixed", "sess-fixed"]
        assert created == []
        assert app._session_id == "sess-fixed"
        assert conn.client is app._client
        assert app.mark_connected == 2
        assert app.mark_disconnected == 1
        assert app.break_llm == 1
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证首次连接创建会话，断线重连后恢复同一新建会话
# 设计：初始无 resume 会话时走 session.create，随后模拟断线重连并断言第二次 resume 命中自动创建的 id
async def test_first_connect_creates_then_reconnect_resumes() -> None:
    resumed: list[str] = []
    created: int = 0
    block = asyncio.Event()

    class _FakeSocket:
        def __init__(self, index: int) -> None:
            self.index = index
            self.handler: Any = None

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def run_event_loop(self) -> None:
            if self.index >= 1:
                await block.wait()

        async def send_command(self, method: str, params: dict) -> dict:
            if method == "session.create":
                nonlocal created
                created += 1
                return {"session_id": "sess-auto"}
            if method == "session.resume":
                resumed.append(str(params["session_id"]))
                return {
                    "session": {"session_id": "sess-auto", "title": "New"},
                    "messages": [],
                }
            return {}

        def on_event(self, handler: Any) -> None:
            self.handler = handler

    class _FakeApp:
        def __init__(self) -> None:
            self._client = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = None
            self._replay_run_id: str | None = None
            self._history_loaded = False
            self._session_title = ""
            self._titled = False
            self._first_user_text = ""
            self._plan_review_pending = False
            self._input_runtime_mode = RuntimeMode.ACT
            self.mark_connected = 0
            self.history: list[list[dict[str, Any]]] = []

        def _update_header(self, _state: str) -> None:
            return None

        async def _refresh_authority(self) -> None:
            return None

        def _break_llm(self) -> None:
            return None

        def _append_history(self, messages: list[dict[str, Any]]) -> None:
            self.history.append(messages)

        def _prompt(self) -> None:
            return None

        def _mark_connected(self) -> None:
            self.mark_connected += 1

        def _mark_disconnected(self) -> None:
            return None

        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    count = 0

    def factory(_host: str, _port: int, *, auth_token: str | None = None) -> Any:
        nonlocal count
        client = _FakeSocket(count)
        count += 1
        return client

    conn = TuiConnection(
        app,
        None,
        host="127.0.0.1",
        port=9999,
        client_factory=factory,
    )
    task = asyncio.create_task(conn.run())
    try:
        while app.mark_connected < 2:
            await asyncio.sleep(0.05)
        assert created == 1
        assert resumed == ["sess-auto"]
        assert app._session_id == "sess-auto"
        # 首次连接创建会话后 history 已标记加载，重连不必再取历史
        assert app.history == []
        assert app._history_loaded is True
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证服务器推送事件经 on_event 回调回交给 App
# 设计：fake client 在 run_event_loop 中触发已注册 handler，断言事件到达 App 回调
async def test_connection_delivers_events_to_app_callback() -> None:
    received: list[dict[str, Any]] = []
    block = asyncio.Event()

    class _FakeSocket:
        def __init__(self) -> None:
            self.handler: Any = None

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def run_event_loop(self) -> None:
            # 等脚本注册 handler 后推送一条事件，随后保持连接存续
            while self.handler is None:
                await asyncio.sleep(0)
            await self.handler({"type": "llm.token", "token": "hi"})
            await block.wait()

        async def send_command(self, _method: str, _params: dict) -> dict:
            return {"session_id": "sess-evt"}

        def on_event(self, handler: Any) -> None:
            self.handler = handler

    class _FakeApp:
        def __init__(self) -> None:
            self._client = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = None
            self._replay_run_id: str | None = None
            self._history_loaded = True
            self._session_title = ""
            self._titled = False
            self._first_user_text = ""
            self._plan_review_pending = False
            self._input_runtime_mode = RuntimeMode.ACT

        def _update_header(self, _state: str) -> None:
            return None

        async def _refresh_authority(self) -> None:
            return None

        def _break_llm(self) -> None:
            return None

        def _append_history(self, _messages: list[dict[str, Any]]) -> None:
            return None

        def _prompt(self) -> None:
            return None

        def _mark_connected(self) -> None:
            return None

        def _mark_disconnected(self) -> None:
            return None

        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    conn = TuiConnection(
        app,
        received.append,
        host="127.0.0.1",
        port=9999,
        client_factory=lambda _h, _p, *, auth_token=None: _FakeSocket(),
    )
    task = asyncio.create_task(conn.run())
    try:
        while len(received) < 1:
            await asyncio.sleep(0.05)
        assert received[0] == {"type": "llm.token", "token": "hi"}
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
