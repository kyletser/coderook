from __future__ import annotations

import asyncio
from typing import Any

import pytest

from code_rook.core.authority import RuntimeMode
from code_rook.core.transport.socket_client import IpcError
from code_rook.tui.connection import TuiConnection, _select_recent_session


# 功能：验证连接层用户可见故障文案跟随 App 的中英文 locale
# 设计：直接调用纯翻译入口覆盖变量插值，避免启动重连循环造成定时等待
def test_connection_product_text_follows_app_locale() -> None:
    class _LocalizedApp:
        _client = None
        _locale = "zh-CN"

    connection = TuiConnection(
        _LocalizedApp(),
        None,
        host="127.0.0.1",
        port=7437,
    )

    assert connection._text("connection.authentication_failed") == "身份验证失败"
    assert (
        connection._text(
            "connection.unreachable",
            host="127.0.0.1",
            port=7437,
            detail="自动恢复未启用",
        )
        == "无法连接 127.0.0.1:7437；自动恢复未启用"
    )


# 功能：验证裸启动优先恢复最近的非空会话，只有空会话时复用而不继续创建
# 设计：用按最近更新时间排序的最小 session 列表覆盖非空优先、空会话回退与空列表三条分支
def test_select_recent_session_prefers_nonempty_and_reuses_empty() -> None:
    sessions = [
        {"session_id": "empty-new", "run_count": 0, "last_run_id": None},
        {"session_id": "used", "run_count": 2, "last_run_id": "run-2"},
    ]

    assert _select_recent_session(sessions) == "used"
    assert _select_recent_session([sessions[0]]) == "empty-new"
    assert _select_recent_session([]) is None


# 功能：验证 daemon 尚未启动时首次拒绝连接会立即禁用输入并显示恢复建议
# 设计：让 fake client 在 connect 阶段抛出 ConnectionRefusedError，等首次状态回调后取消重试循环
async def test_initial_connection_refusal_marks_app_disconnected() -> None:
    attempted = asyncio.Event()
    closed = asyncio.Event()

    class _RefusingSocket:
        # 模拟目标端口没有监听并通知测试首次尝试已经发生
        async def connect(self) -> None:
            attempted.set()
            raise ConnectionRefusedError

        # 记录失败握手后的客户端资源清理
        async def close(self) -> None:
            closed.set()

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
        assert closed.is_set()
        assert app.problems == [
            (
                "unreachable",
                "cannot connect to 127.0.0.1:9999; automatic recovery is disabled",
            )
        ]
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
    thread_subscriptions: list[dict[str, Any]] = []
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
                if "thread_id" in params:
                    thread_subscriptions.append(dict(params))
                    if self.index == 0 and self.handler is not None:
                        await self.handler(
                            {
                                "type": "runtime.event",
                                "thread_id": "sess-fixed",
                                "turn_id": "run-fixed",
                                "seq": 7,
                                "event_type": "run.started",
                                "payload": {"goal": "resume"},
                                "ts": "2026-01-01T00:00:00+00:00",
                            }
                        )
                    return {"subscription_id": f"sub-{self.index}", "last_seq": 7}
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
        assert [params["after_seq"] for params in thread_subscriptions] == [0, 7]
        assert all(params["thread_id"] == "sess-fixed" for params in thread_subscriptions)
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证活动 Turn 的 SESSION_BUSY 不阻断重连订阅，并恢复可响应的审批与问题控件
# 设计：预置断线前游标和 active run，让 fake Core 对 resume 返回 busy，再从权威 thread 投影附着并回放两个交互事件，同时注入异会话事件验证隔离
async def test_busy_active_turn_reconnect_recovers_interactions_without_cross_session() -> None:
    block = asyncio.Event()
    connected = asyncio.Event()
    calls: list[tuple[str, dict[str, Any]]] = []
    rendered: list[dict[str, Any]] = []
    reconciled: list[object] = []
    responses: list[tuple[str, dict[str, Any]]] = []

    class _FakeSocket:
        # 初始化事件回调槽供订阅命令同步推送 durable replay
        def __init__(self) -> None:
            self.handler: Any = None

        # 模拟已建立并认证的稳定连接
        async def connect(self) -> None:
            return None

        # 关闭 fake socket 不需要释放真实网络资源
        async def close(self) -> None:
            return None

        # 保持重连后的事件循环存活直到断言完成
        async def run_event_loop(self) -> None:
            await block.wait()

        # 模拟 busy 恢复、权威 thread 查询、游标回放和交互响应
        async def send_command(
            self,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            calls.append((method, dict(params)))
            if method == "session.resume":
                raise IpcError(-32012, "session busy")
            if method == "thread.get":
                return {
                    "thread": {
                        "id": "sess-active",
                        "title": "Active task",
                        "status": "running",
                    }
                }
            if method == "session.get_history":
                return {"messages": [{"role": "user", "content": "continue"}]}
            if method == "event.subscribe" and "thread_id" in params:
                assert params["thread_id"] == "sess-active"
                assert params["after_seq"] == 1
                assert self.handler is not None
                events = [
                    {
                        "type": "runtime.event",
                        "thread_id": "sess-active",
                        "turn_id": "run-active",
                        "seq": 2,
                        "event_type": "permission.requested",
                        "payload": {
                            "tool_use_id": "tool-active",
                            "tool_name": "Bash",
                            "params": {"command": "git status"},
                            "param_preview": "git status",
                        },
                    },
                    {
                        "type": "runtime.event",
                        "thread_id": "sess-other",
                        "turn_id": "run-other",
                        "seq": 1,
                        "event_type": "permission.requested",
                        "payload": {
                            "tool_use_id": "tool-other",
                            "tool_name": "Bash",
                            "params": {"command": "git clean"},
                            "param_preview": "git clean",
                        },
                    },
                    {
                        "type": "runtime.event",
                        "thread_id": "sess-active",
                        "turn_id": "run-active",
                        "seq": 3,
                        "event_type": "user_question.asked",
                        "payload": {
                            "question_id": "question-active",
                            "question": "Continue?",
                            "header": "Decision",
                            "options": ["Yes"],
                        },
                    },
                ]
                for event in events:
                    await self.handler(event)
                return {"subscription_id": "sub-active", "last_seq": 3}
            if method in {"permission.respond", "user_question.respond"}:
                responses.append((method, dict(params)))
                return {}
            return {}

        # 注册连接层事件分发回调
        def on_event(self, handler: Any) -> None:
            self.handler = handler

    class _FakeApp:
        # 初始化断线前同一会话的最小 TUI 状态
        def __init__(self) -> None:
            self._client: Any = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = "sess-active"
            self._replay_run_id: str | None = None
            self._history_loaded = True
            self._session_title = "Active task"
            self._titled = True
            self._first_user_text = "continue"

        # 接受连接状态更新而不依赖真实 Header
        def _update_header(self, _state: str) -> None:
            return None

        # 模拟 authority 恢复成功
        async def _refresh_authority(self) -> None:
            return None

        # 标记重连链路已完成全部恢复动作
        def _mark_connected(self) -> None:
            connected.set()

        # 接受连接清理阶段的离线状态
        def _mark_disconnected(self) -> None:
            return None

        # 模拟清理断线前流式块
        def _break_llm(self) -> None:
            return None

        # 记录 transcript 重建并保持目标会话为当前会话
        async def _prepare_session_view(
            self,
            session_id: str,
            _messages: object,
            *,
            resume: bool,
            title: str,
        ) -> None:
            assert resume is True
            assert title == "Active task"
            self._session_id = session_id

        # 捕获 durable reducer 的最终活动交互状态
        def _reconcile_session_state(self, state: object) -> None:
            reconciled.append(state)

        # 模拟恢复该会话自己的 composer 草稿
        def _restore_session_composer(self, _session_id: str) -> None:
            return None

        # 提供连接层启动时查询的最小 Header 替身
        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                # 接受协议错误时可能产生的 Header 更新
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    client = _FakeSocket()
    connection = TuiConnection(
        app,
        rendered.append,
        host="127.0.0.1",
        port=9999,
        client_factory=lambda *_args, **_kwargs: client,
    )
    connection._session_cursors["sess-active"] = 1
    connection._reduce_session_state(
        "sess-active",
        {"type": "run.started", "run_id": "run-active"},
    )
    connection._remember_session_run("sess-active", "run-active")

    task = asyncio.create_task(connection.run())
    try:
        await asyncio.wait_for(connected.wait(), timeout=1)
        methods = [method for method, _params in calls]
        assert methods.index("session.resume") < methods.index("thread.get")
        assert methods.index("thread.get") < methods.index("event.subscribe")
        assert connection._session_cursors["sess-active"] == 3
        assert [event["type"] for event in rendered] == [
            "permission.requested",
            "user_question.asked",
        ]
        assert all(event["session_id"] == "sess-active" for event in rendered)
        state = connection.session_state("sess-active")
        assert state.active_run_id == "run-active"
        assert list(state.pending_permissions) == ["tool-active"]
        assert state.pending_question is not None
        assert reconciled and getattr(reconciled[-1], "active_run_id") == "run-active"

        assert app._client is not None
        await app._client.send_command(
            "permission.respond",
            {"tool_use_id": "tool-active", "decision": "allow_once"},
        )
        await app._client.send_command(
            "user_question.respond",
            {"question_id": "question-active", "answer": "Yes"},
        )
        connection.resolve_permission("sess-active", "tool-active")
        connection.resolve_question("sess-active", "question-active")

        assert [method for method, _params in responses] == [
            "permission.respond",
            "user_question.respond",
        ]
        resolved = connection.session_state("sess-active")
        assert resolved.pending_permissions == {}
        assert resolved.pending_question is None
        assert list(connection.session_state("sess-other").pending_permissions) == [
            "tool-other"
        ]
    finally:
        block.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
            if method == "event.subscribe" and "thread_id" in params:
                return {"subscription_id": f"sub-{self.index}", "last_seq": 0}
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
        # 重连必须重新拉取权威 transcript，确保断线期间完成的 assistant 正文可见
        assert app.history == [[]]
        assert app._history_loaded is True
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证 continue 模式优先恢复最近 session，而不是制造新的空会话
# 设计：fake socket 为 session.list 返回最近记录并保持连接，断言 resume 被调用且 create 未调用
async def test_continue_recent_resumes_latest_session() -> None:
    block = asyncio.Event()
    calls: list[tuple[str, dict[str, Any]]] = []
    session_notices: list[tuple[str, str, str, int | None]] = []

    class _FakeSocket:
        # 接收连接并保持事件循环存活到测试结束
        async def connect(self) -> None:
            return None

        # 关闭 fake socket 不需要释放外部资源
        async def close(self) -> None:
            return None

        # 阻塞模拟稳定连接
        async def run_event_loop(self) -> None:
            await block.wait()

        # 返回最近会话并记录连接层选择的 IPC 路径
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            if method == "session.list":
                return {"sessions": [{"session_id": "sess-latest"}]}
            if method == "session.resume":
                return {
                    "session": {"session_id": "sess-latest", "title": "Latest"}
                }
            if method == "session.get_history":
                return {"messages": []}
            if method == "event.subscribe" and "thread_id" in params:
                return {"subscription_id": "sub-latest", "last_seq": 0}
            return {}

        # 保存事件 handler 不需要主动推送事件
        def on_event(self, _handler: Any) -> None:
            return None

    class _FakeApp:
        # 初始化连接层依赖的最小 continue 状态
        def __init__(self) -> None:
            self._client = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = None
            self._continue_recent = True
            self._replay_run_id: str | None = None
            self._history_loaded = False
            self._session_title = ""
            self._titled = False
            self._first_user_text = ""
            self.connected = asyncio.Event()

        # 接受连接状态更新
        def _update_header(self, _state: str) -> None:
            return None

        # 模拟 authority 恢复
        async def _refresh_authority(self) -> None:
            return None

        # 接收恢复的历史消息
        def _append_history(self, _messages: list[dict[str, Any]]) -> None:
            return None

        # 标记连接已可用
        def _mark_connected(self) -> None:
            self.connected.set()

        # 记录连接层对最近会话恢复结果的产品提示
        def _show_session_ready(
            self,
            action: str,
            session_id: str,
            title: str,
            history_count: int | None,
        ) -> None:
            session_notices.append((action, session_id, title, history_count))

        # 标记连接关闭
        def _mark_disconnected(self) -> None:
            return None

        # 清理流式块
        def _break_llm(self) -> None:
            return None

        # 提供最小 header 控件替身
        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                # 接受 header 更新
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    app = _FakeApp()
    connection = TuiConnection(
        app,
        None,
        host="127.0.0.1",
        port=9999,
        client_factory=lambda *_args, **_kwargs: _FakeSocket(),
    )
    task = asyncio.create_task(connection.run())
    try:
        await asyncio.wait_for(app.connected.wait(), timeout=1)
        methods = [method for method, _params in calls]
        assert "session.list" in methods
        assert "session.resume" in methods
        assert "session.create" not in methods
        assert app._session_id == "sess-latest"
        assert session_notices == [("resumed", "sess-latest", "Latest", 0)]
        resume_index = methods.index("session.resume")
        subscriptions = [params for method, params in calls if method == "event.subscribe"]
        thread_subscription = next(params for params in subscriptions if "thread_id" in params)
        assert methods.index("event.subscribe") > resume_index
        assert thread_subscription["thread_id"] == "sess-latest"
        assert thread_subscription["after_seq"] == 0
        assert {
            "goal.*",
            "llm.reasoning",
            "llm.route_selected",
            "llm.retry",
        }.issubset(set(thread_subscription["topics"]))
    finally:
        block.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# 功能：验证受管 TUI 在 Core 拒绝连接后会自动启动服务并恢复连接
# 设计：首个 fake socket 拒绝连接、恢复回调置位，第二个保持在线，断言无需用户重启即可进入 ready
async def test_connection_refusal_recovers_managed_core() -> None:
    block = asyncio.Event()
    recovered = asyncio.Event()
    connected = asyncio.Event()
    problems: list[tuple[str, str]] = []
    attempts = 0

    class _FakeSocket:
        # 记录当前连接序号，第一次模拟崩溃后的端口拒绝
        def __init__(self, index: int) -> None:
            self.index = index

        # 首次连接失败，自动恢复后的连接成功
        async def connect(self) -> None:
            if self.index == 0:
                raise ConnectionRefusedError

        # fake 关闭不持有外部资源
        async def close(self) -> None:
            return None

        # 第二次连接保持存活直到测试结束
        async def run_event_loop(self) -> None:
            await block.wait()

        # 返回创建会话与连接初始化所需的最小响应
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "session.create":
                return {"session_id": "sess-recovered"}
            if method == "event.subscribe" and "thread_id" in params:
                return {"subscription_id": "sub-recovered", "last_seq": 0}
            return {}

        # 测试不主动推送事件
        def on_event(self, _handler: Any) -> None:
            return None

    class _FakeApp:
        # 初始化自动恢复与新会话所需状态
        def __init__(self) -> None:
            self._client = None
            self._session_id: str | None = None
            self._resume_session_id: str | None = None
            self._replay_run_id: str | None = None
            self._history_loaded = False
            self._session_title = ""
            self._titled = False
            self._first_user_text = ""
            self._core_recovery = self.recover

        # 模拟启动器成功派生新 Core
        def recover(self) -> bool:
            recovered.set()
            return True

        # 接收连接状态而不依赖 Textual
        def _update_header(self, _state: str) -> None:
            return None

        # 记录自动恢复提示
        def _show_connection_problem(self, kind: str, detail: str) -> None:
            problems.append((kind, detail))

        # 模拟会话权限恢复
        async def _refresh_authority(self) -> None:
            return None

        # 标记连接已经恢复
        def _mark_connected(self) -> None:
            connected.set()

        # 接收断线状态
        def _mark_disconnected(self) -> None:
            return None

        # fake 流式块无需清理
        def _break_llm(self) -> None:
            return None

        # 提供 header 替身
        def query_one(self, _selector: str, _cls: Any = None) -> Any:
            class _Header:
                # 接收 markup 更新
                def update(self, *_args: Any, **_kwargs: Any) -> None:
                    return None

            return _Header()

    # 每次连接构造递增序号，精确模拟一次失败后成功
    def factory(*_args: Any, **_kwargs: Any) -> _FakeSocket:
        nonlocal attempts
        socket = _FakeSocket(attempts)
        attempts += 1
        return socket

    app = _FakeApp()
    connection = TuiConnection(
        app,
        None,
        host="127.0.0.1",
        port=9999,
        client_factory=factory,
    )
    task = asyncio.create_task(connection.run())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        await asyncio.wait_for(connected.wait(), timeout=1)
        assert attempts == 2
        assert app._session_id == "sess-recovered"
        assert problems == [("recovering", "started a new Core")]
    finally:
        block.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# 功能：验证 durable run 归属建立后，同 run 的实时 token 经回调交给 App
# 设计：fake client 先推送 runtime run.started 再推送 llm.token，同时覆盖 wrapper 解包与实时通道校验
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
            await self.handler(
                {
                    "type": "runtime.event",
                    "thread_id": "sess-evt",
                    "turn_id": "run-evt",
                    "seq": 1,
                    "event_type": "run.started",
                    "payload": {"goal": "demo"},
                    "ts": "2026-01-01T00:00:00+00:00",
                }
            )
            await self.handler(
                {"type": "llm.token", "run_id": "run-evt", "token": "hi"}
            )
            await block.wait()

        async def send_command(self, method: str, params: dict) -> dict:
            if method == "session.create":
                return {"session_id": "sess-evt"}
            if method == "event.subscribe" and "thread_id" in params:
                return {"subscription_id": "sub-evt", "last_seq": 1}
            return {}

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
        while len(received) < 2:
            await asyncio.sleep(0.05)
        assert received[0]["type"] == "run.started"
        assert received[0]["run_id"] == "run-evt"
        assert received[1] == {"type": "llm.token", "run_id": "run-evt", "token": "hi"}
    finally:
        block.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# 功能：验证 runtime wrapper 按 thread 隔离、按 seq 去重并为渲染器恢复原始字段
# 设计：交错推送当前与外部 thread、重复 seq 及 token，断言只有当前 thread/run 事件通过
async def test_runtime_event_adapter_isolates_threads_and_deduplicates() -> None:
    received: list[dict[str, Any]] = []

    class _FakeApp:
        # 初始化当前会话与 replay 状态
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"
            self._replay_run_id = None

    connection = TuiConnection(
        _FakeApp(),
        received.append,
        host="127.0.0.1",
        port=9999,
    )
    current_started = {
        "type": "runtime.event",
        "thread_id": "sess-current",
        "turn_id": "run-current",
        "seq": 1,
        "event_type": "run.started",
        "payload": {"goal": "current"},
        "ts": "2026-01-01T00:00:00+00:00",
    }
    await connection._dispatch_event(current_started)
    await connection._dispatch_event(current_started)
    await connection._dispatch_event(
        {
            **current_started,
            "thread_id": "sess-other",
            "turn_id": "run-other",
            "payload": {"goal": "other"},
        }
    )
    await connection._dispatch_event(
        {"type": "llm.token", "run_id": "run-other", "token": "wrong"}
    )
    await connection._dispatch_event(
        {"type": "llm.token", "run_id": "run-current", "token": "right"}
    )

    assert [event["type"] for event in received] == ["run.started", "llm.token"]
    assert received[0]["session_id"] == "sess-current"
    assert received[0]["run_id"] == "run-current"
    assert received[1]["token"] == "right"
    assert connection._session_cursors == {"sess-current": 1}
    assert connection.session_state("sess-other").active_run_id == "run-other"


# 功能：验证 core 与 audit daemon 级事件只进入独立回调，不进入会话渲染器
# 设计：同时记录两类回调并推送两种全局事件，断言会话事件列表保持为空
async def test_daemon_events_use_independent_callback() -> None:
    rendered: list[dict[str, Any]] = []
    daemon_events: list[dict[str, Any]] = []

    class _FakeApp:
        # 初始化会话状态和 daemon 事件接收器
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"
            self._replay_run_id = None

        # 记录 daemon 事件而不改写 run 状态
        def _handle_daemon_event(self, event: dict[str, Any]) -> None:
            daemon_events.append(event)

    connection = TuiConnection(
        _FakeApp(),
        rendered.append,
        host="127.0.0.1",
        port=9999,
    )
    events = [
        {"type": "core.started", "listen_addr": "127.0.0.1:7437", "version": "1"},
        {
            "type": "audit.degraded",
            "source": "runtime_projection",
            "diagnostic_id": "AUD-123",
        },
    ]
    for event in events:
        await connection._dispatch_event(event)

    assert daemon_events == events
    assert rendered == []


# 功能：验证 session 切换只保留目标 thread 订阅，切回时按独立游标补交且不重不漏
# 设计：fake Core 维护 subscription_id 与 durable 事件日志，连续执行 A→B→A→B，并在会话离屏时追加事件，核对取消顺序、after_seq 和唯一渲染序列
async def test_session_switch_keeps_per_thread_cursors_without_cross_rendering() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    rendered: list[dict[str, Any]] = []
    event_log: dict[str, list[dict[str, Any]]] = {"sess-a": [], "sess-b": []}
    connection: TuiConnection

    # 构造带稳定 thread、seq 和可观察标签的 runtime 事件
    def runtime_event(session_id: str, seq: int, label: str) -> dict[str, Any]:
        return {
            "type": "runtime.event",
            "thread_id": session_id,
            "turn_id": f"run-{label}",
            "seq": seq,
            "event_type": "run.started",
            "payload": {"goal": label, "label": label},
            "ts": "2026-01-01T00:00:00+00:00",
        }

    class _FakeClient:
        # 初始化服务端活动订阅表和单调 subscription_id
        def __init__(self) -> None:
            self.active: dict[str, str] = {}
            self.next_subscription = 1

        # 模拟 typed subscribe/unsubscribe，并在订阅响应前完成 durable replay
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, dict(params)))
            if method == "event.subscribe":
                session_id = str(params["thread_id"])
                subscription_id = f"sub-{self.next_subscription}"
                self.next_subscription += 1
                self.active[subscription_id] = session_id
                after_seq = int(params["after_seq"])
                for event in event_log[session_id]:
                    if int(event["seq"]) > after_seq:
                        await connection._dispatch_event(event)
                latest = max(
                    (int(event["seq"]) for event in event_log[session_id]),
                    default=after_seq,
                )
                return {"subscription_id": subscription_id, "last_seq": latest}
            if method == "event.unsubscribe":
                subscription_id = str(params["subscription_id"])
                removed = self.active.pop(subscription_id, None) is not None
                return {"subscription_id": subscription_id, "removed": removed}
            raise AssertionError(method)

        # 将新 durable 事件保存并只推送给当前仍活动的 thread 订阅
        async def publish(self, event: dict[str, Any]) -> None:
            session_id = str(event["thread_id"])
            event_log[session_id].append(event)
            for subscribed_session in list(self.active.values()):
                if subscribed_session == session_id:
                    await connection._dispatch_event(event)

    class _FakeApp:
        # 初始化在 A 会话上的活动客户端
        def __init__(self, client: _FakeClient) -> None:
            self._client = client
            self._session_id = "sess-a"
            self._replay_run_id = None

    client = _FakeClient()
    app = _FakeApp(client)
    connection = TuiConnection(
        app,
        rendered.append,
        host="127.0.0.1",
        port=9999,
    )
    event_log["sess-a"].append(runtime_event("sess-a", 1, "a-1"))
    await connection.activate_session("sess-a", lambda: None)
    event_log["sess-b"].append(runtime_event("sess-b", 1, "b-1"))
    await connection.activate_session(
        "sess-b",
        lambda: setattr(app, "_session_id", "sess-b"),
    )
    await client.publish(runtime_event("sess-a", 2, "a-2"))
    await client.publish(runtime_event("sess-b", 2, "b-2"))
    await connection.activate_session(
        "sess-a",
        lambda: setattr(app, "_session_id", "sess-a"),
    )
    await client.publish(runtime_event("sess-b", 3, "b-3"))
    await connection.activate_session(
        "sess-b",
        lambda: setattr(app, "_session_id", "sess-b"),
    )

    subscriptions = [
        (str(params["thread_id"]), int(params["after_seq"]))
        for method, params in calls
        if method == "event.subscribe"
    ]
    unsubscriptions = [
        str(params["subscription_id"])
        for method, params in calls
        if method == "event.unsubscribe"
    ]
    assert subscriptions == [
        ("sess-a", 0),
        ("sess-b", 0),
        ("sess-a", 1),
        ("sess-b", 2),
    ]
    assert unsubscriptions == ["sub-1", "sub-2", "sub-3"]
    assert [event["label"] for event in rendered] == [
        "a-1",
        "b-1",
        "b-2",
        "a-2",
        "b-3",
    ]
    assert connection._session_cursors == {"sess-a": 2, "sess-b": 3}
    assert connection._client_thread_subscriptions == {"sess-b": "sub-4"}
    assert client.active == {"sub-4": "sess-b"}


# 功能：验证目标会话激活失败时撤销目标订阅并恢复原会话订阅
# 设计：让 prepare 在 target 已订阅后抛错，按 typed 调用序列和最终映射证明回滚不会遗留双 thread 订阅
async def test_session_switch_failure_restores_previous_thread_subscription() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class _FakeClient:
        # 初始化单调订阅编号和服务端活动订阅表
        def __init__(self) -> None:
            self.next_subscription = 1
            self.active: dict[str, str] = {}

        # 模拟成功订阅和按标识取消
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, dict(params)))
            if method == "event.subscribe":
                subscription_id = f"sub-{self.next_subscription}"
                self.next_subscription += 1
                self.active[subscription_id] = str(params["thread_id"])
                return {"subscription_id": subscription_id, "last_seq": 0}
            if method == "event.unsubscribe":
                subscription_id = str(params["subscription_id"])
                removed = self.active.pop(subscription_id, None) is not None
                return {"subscription_id": subscription_id, "removed": removed}
            raise AssertionError(method)

    class _FakeApp:
        # 保持原会话为当前视图，使激活异常触发回滚分支
        def __init__(self, client: _FakeClient) -> None:
            self._client = client
            self._session_id = "sess-a"

    client = _FakeClient()
    app = _FakeApp(client)
    connection = TuiConnection(app, None, host="127.0.0.1", port=9999)
    await connection.subscribe_session("sess-a")

    # 在目标订阅建立后模拟视图准备失败
    def fail_prepare() -> None:
        raise RuntimeError("view failed")

    with pytest.raises(RuntimeError, match="view failed"):
        await connection.activate_session("sess-b", fail_prepare)

    assert [method for method, _params in calls] == [
        "event.subscribe",
        "event.unsubscribe",
        "event.subscribe",
        "event.unsubscribe",
        "event.subscribe",
    ]
    assert connection._client_thread_subscriptions == {"sess-a": "sub-3"}
    assert client.active == {"sub-3": "sess-a"}


# 功能：验证目标视图已切换后事件交付失败不会让后续高序号越过未确认游标
# 设计：令 B 的第二条回放渲染失败且取消订阅也瞬时失败，再推送 seq=4 并重试激活，证明游标停在 1 且 2、3、4 均可完整补交
async def test_post_prepare_delivery_failure_requires_replay_before_cursor_can_advance() -> None:
    rendered: list[str] = []
    event_log: list[dict[str, Any]] = []
    connection: TuiConnection

    # 构造同时适用于 runtime push 与 event.replay 的持久事件
    def runtime_event(seq: int, label: str) -> dict[str, Any]:
        return {
            "type": "runtime.event",
            "thread_id": "sess-b",
            "turn_id": "run-b",
            "seq": seq,
            "event_type": "agent.decision",
            "payload": {"decision": "continue", "label": label},
            "ts": "2026-08-24T00:00:00+00:00",
        }

    class _FakeClient:
        # 初始化活动订阅并让目标取消首次失败以模拟连接边界竞态
        def __init__(self) -> None:
            self.next_subscription = 1
            self.active: dict[str, str] = {}
            self.fail_target_unsubscribe = True

        # 模拟 thread 订阅、显式回放和一次目标取消失败
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "event.subscribe":
                session_id = str(params["thread_id"])
                subscription_id = f"sub-{self.next_subscription}"
                self.next_subscription += 1
                self.active[subscription_id] = session_id
                after_seq = int(params["after_seq"])
                for event in event_log:
                    if int(event["seq"]) > after_seq:
                        await connection._dispatch_event(event)
                return {
                    "subscription_id": subscription_id,
                    "last_seq": max(
                        (int(event["seq"]) for event in event_log),
                        default=after_seq,
                    ),
                }
            if method == "event.unsubscribe":
                subscription_id = str(params["subscription_id"])
                if (
                    self.active.get(subscription_id) == "sess-b"
                    and self.fail_target_unsubscribe
                ):
                    self.fail_target_unsubscribe = False
                    raise RuntimeError("unsubscribe unavailable")
                removed = self.active.pop(subscription_id, None) is not None
                return {"subscription_id": subscription_id, "removed": removed}
            if method == "event.replay":
                after_seq = int(params["after_seq"])
                records = [
                    {
                        "seq": event["seq"],
                        "type": event["event_type"],
                        "turn_id": event["turn_id"],
                        "payload": event["payload"],
                        "ts": event["ts"],
                    }
                    for event in event_log
                    if int(event["seq"]) > after_seq
                ]
                return {
                    "events": records,
                    "latest_seq": max(
                        (int(event["seq"]) for event in event_log),
                        default=after_seq,
                    ),
                    "has_more": False,
                }
            raise AssertionError(method)

        # 追加实时事件并投递给仍然残留的目标订阅
        async def publish(self, event: dict[str, Any]) -> None:
            event_log.append(event)
            for session_id in list(self.active.values()):
                if session_id == event["thread_id"]:
                    await connection._dispatch_event(event)

    class _FakeApp:
        # 从 A 会话开始并允许 prepare 原子改写当前会话
        def __init__(self, client: _FakeClient) -> None:
            self._client = client
            self._session_id = "sess-a"
            self._replay_run_id = None

    client = _FakeClient()
    app = _FakeApp(client)

    # 在第二条目标事件上模拟视图渲染失败
    def fail_second(payload: dict[str, Any]) -> None:
        if payload.get("label") == "b-2":
            raise RuntimeError("view failed on b-2")
        rendered.append(str(payload["label"]))

    connection = TuiConnection(app, fail_second, host="127.0.0.1", port=9999)
    await connection.subscribe_session("sess-a")
    event_log.extend(
        [runtime_event(1, "b-1"), runtime_event(2, "b-2"), runtime_event(3, "b-3")]
    )

    with pytest.raises(RuntimeError, match="view failed on b-2"):
        await connection.activate_session(
            "sess-b",
            lambda: setattr(app, "_session_id", "sess-b"),
        )

    assert connection._session_cursors["sess-b"] == 1
    assert connection.session_requires_replay("sess-b")
    await client.publish(runtime_event(4, "b-4"))
    assert connection._session_cursors["sess-b"] == 1

    connection._on_event = lambda payload: rendered.append(str(payload["label"]))
    await connection.activate_session("sess-b", lambda: None)

    assert rendered == ["b-1", "b-2", "b-3", "b-4"]
    assert connection._session_cursors["sess-b"] == 4
    assert not connection.session_requires_replay("sess-b")


# 功能：验证非当前 session 的 active run、审批和问题状态仍会归并且可在本地响应后清除
# 设计：向后台 session 依次投递 run/permission/question，再调用显式 resolve 并检查状态不依赖渲染
async def test_inactive_session_interactions_are_reconciled_without_cursor_ack() -> None:
    class _FakeApp:
        # 初始化当前查看 session，目标事件属于另一个 session
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"
            self._replay_run_id = None

    connection = TuiConnection(_FakeApp(), None, host="127.0.0.1", port=9999)
    events = [
        (1, "run.started", {"goal": "background"}),
        (
            2,
            "permission.requested",
            {
                "tool_use_id": "tool-1",
                "tool_name": "Bash",
                "params": {"command": "git status"},
                "param_preview": "git status",
            },
        ),
        (
            3,
            "user_question.asked",
            {
                "question_id": "question-1",
                "question": "Continue?",
                "header": "Decision",
                "options": ["Yes"],
            },
        ),
    ]
    for seq, event_type, payload in events:
        await connection._dispatch_event(
            {
                "type": "runtime.event",
                "thread_id": "sess-background",
                "turn_id": "run-background",
                "seq": seq,
                "event_type": event_type,
                "payload": payload,
                "ts": "2026-08-24T00:00:00+00:00",
            }
        )

    state = connection.session_state("sess-background")
    assert state.active_run_id == "run-background"
    assert list(state.pending_permissions) == ["tool-1"]
    assert state.pending_question is not None
    assert "sess-background" not in connection._session_cursors

    connection.resolve_permission("sess-background", "tool-1")
    connection.resolve_question("sess-background", "question-1")
    resolved = connection.session_state("sess-background")
    assert resolved.pending_permissions == {}
    assert resolved.pending_question is None


# 功能：验证已确认游标内的重复审批事件不会在用户响应后复活交互状态
# 设计：当前 session 先交付并确认 permission，再本地 resolve 后重放同 seq，断言 reducer 保持空状态
async def test_confirmed_duplicate_cannot_revive_resolved_permission() -> None:
    class _FakeApp:
        # 初始化当前会话使首次审批事件真实确认游标
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"
            self._replay_run_id = None

    connection = TuiConnection(_FakeApp(), lambda _event: None, host="127.0.0.1", port=9999)
    event = {
        "type": "runtime.event",
        "thread_id": "sess-current",
        "turn_id": "run-current",
        "seq": 1,
        "event_type": "permission.requested",
        "payload": {
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
            "params": {"command": "git status"},
            "param_preview": "git status",
        },
        "ts": "2026-08-24T00:00:00+00:00",
    }

    await connection._dispatch_event(event)
    connection.resolve_permission("sess-current", "tool-1")
    await connection._dispatch_event(event)

    assert connection._session_cursors == {"sess-current": 1}
    assert connection.session_state("sess-current").pending_permissions == {}


# 功能：验证渲染回调失败时不会提前确认事件游标，修复后同一事件仍可补交
# 设计：让首次同步回调显式抛错，再替换为记录回调重放同 seq，断言游标只在成功交付后推进
async def test_runtime_event_cursor_advances_only_after_successful_delivery() -> None:
    rendered: list[dict[str, Any]] = []

    class _FakeApp:
        # 初始化当前会话使 durable 事件进入真实交付路径
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"
            self._replay_run_id = None

    # 模拟视图重建期间的瞬时渲染失败
    def _fail_delivery(_event: dict[str, Any]) -> None:
        raise RuntimeError("view unavailable")

    connection = TuiConnection(_FakeApp(), _fail_delivery, host="127.0.0.1", port=9999)
    event = {
        "type": "runtime.event",
        "thread_id": "sess-current",
        "turn_id": "run-current",
        "seq": 1,
        "event_type": "run.started",
        "payload": {"goal": "retry delivery"},
        "ts": "2026-08-24T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="view unavailable"):
        await connection._dispatch_event(event)
    assert "sess-current" not in connection._session_cursors

    connection._on_event = rendered.append
    await connection._dispatch_event(event)

    assert connection._session_cursors == {"sess-current": 1}
    assert [item["type"] for item in rendered] == ["run.started"]


# 功能：验证真实 run.finished→plan.ready 顺序保留待审计划且 plan.resolved 精确清除它
# 设计：先终结 run 再发布计划和 durable 决定，覆盖旧测试未模拟到的生产事件时序
def test_session_reducer_tracks_plan_until_durable_resolution() -> None:
    class _FakeApp:
        # 提供 reducer 所需的最小会话标识
        def __init__(self) -> None:
            self._client = None
            self._session_id = "sess-current"

    connection = TuiConnection(_FakeApp(), None, host="127.0.0.1", port=9999)
    connection._reduce_session_state(
        "sess-current",
        {
            "type": "user_question.asked",
            "session_id": "sess-current",
            "run_id": "run-1",
            "question_id": "question-1",
        },
    )
    connection._reduce_session_state(
        "sess-current",
        {"type": "agent.decision", "run_id": "run-1", "decision": "continue"},
    )
    connection._reduce_session_state(
        "sess-current",
        {"type": "run.finished", "run_id": "run-1", "status": "success"},
    )
    connection._reduce_session_state(
        "sess-current",
        {
            "type": "plan.ready",
            "session_id": "sess-current",
            "run_id": "run-1",
        },
    )

    state = connection.session_state("sess-current")
    assert state.pending_question is None
    assert state.pending_plan is not None

    connection._reduce_session_state(
        "sess-current",
        {
            "type": "plan.resolved",
            "session_id": "sess-current",
            "run_id": "run-1",
            "decision": "cancel",
        },
    )

    assert connection.session_state("sess-current").pending_plan is None


# 功能：验证重启回放不会在 run.finished 后复活已经 durable 解决的计划审阅控件
# 设计：使用生产真实 finished→ready→resolved 顺序，断言请求与墓碑均不渲染但游标完整确认
async def test_activation_replay_filters_resolved_interaction_controls() -> None:
    rendered: list[dict[str, Any]] = []

    class _FakeClient:
        # 返回一页包含已解决问题与计划的完整历史
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "event.replay"
            assert params["thread_id"] == "sess-current"
            return {
                "events": [
                    {
                        "seq": 1,
                        "type": "user_question.asked",
                        "turn_id": "run-1",
                        "payload": {"question_id": "q-1"},
                    },
                    {
                        "seq": 2,
                        "type": "agent.decision",
                        "turn_id": "run-1",
                        "payload": {"decision": "continue"},
                    },
                    {
                        "seq": 3,
                        "type": "run.finished",
                        "turn_id": "run-1",
                        "payload": {"status": "success"},
                    },
                    {
                        "seq": 4,
                        "type": "plan.ready",
                        "turn_id": "run-1",
                        "payload": {"request": "review", "plan": "one"},
                    },
                    {
                        "seq": 5,
                        "type": "plan.resolved",
                        "turn_id": "run-1",
                        "payload": {"decision": "cancel", "revision": ""},
                    },
                ],
                "latest_seq": 5,
                "has_more": False,
            }

    class _FakeApp:
        # 保存当前客户端和会话供激活路径使用
        def __init__(self) -> None:
            self._client = _FakeClient()
            self._session_id = "sess-current"

    app = _FakeApp()
    connection = TuiConnection(
        app,
        rendered.append,
        host="127.0.0.1",
        port=9999,
    )
    connection._client_thread_subscriptions["sess-current"] = "sub-current"

    state = await connection.activate_session("sess-current", lambda: None)

    assert [event["type"] for event in rendered] == [
        "agent.decision",
        "run.finished",
    ]
    assert state.pending_question is None
    assert state.pending_plan is None
    assert connection._session_cursors["sess-current"] == 5


# 功能：验证重连会重新读取权威 transcript 以补回断线期间完成的 assistant 正文
# 设计：把 history_loaded 预置为真并直接调用恢复入口，断言 reconnecting 仍交付完整消息
async def test_reconnect_refreshes_history_even_when_previous_view_was_loaded() -> None:
    assistant = {"role": "assistant", "content": "completed while offline"}

    class _FakeClient:
        # 返回断线期间新增的完整 assistant 历史
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "session.get_history":
                return {"messages": [assistant]}
            if method == "event.subscribe":
                return {"subscription_id": "sub-current", "last_seq": 0}
            raise AssertionError(method)

    class _FakeApp:
        # 记录被恢复入口交付的 transcript
        def __init__(self) -> None:
            self._client = _FakeClient()
            self._session_id = "sess-current"
            self._history_loaded = True
            self.history: list[list[dict[str, Any]]] = []

        # 捕获兼容视图入口收到的完整消息
        def _append_history(self, messages: list[dict[str, Any]]) -> None:
            self.history.append(messages)

    app = _FakeApp()
    connection = TuiConnection(app, None, host="127.0.0.1", port=9999)

    _state, count = await connection._restore_resumed_session(
        app._client,
        "sess-current",
        "Current",
        reconnecting=True,
    )

    assert count == 1
    assert app.history == [[assistant]]


# 功能：验证 --replay 保留历史 run 回放且不退回全局 run 订阅
# 设计：直接触发 replay 订阅，断言指定 replay_from_run 与 run scope 同时存在
async def test_legacy_replay_is_scoped_to_requested_run() -> None:
    calls: list[dict[str, Any]] = []

    class _FakeClient:
        # 记录 replay 订阅参数
        async def send_command(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert method == "event.subscribe"
            calls.append(dict(params))
            return {}

    class _FakeApp:
        # 初始化历史 run 标识
        def __init__(self) -> None:
            self._client = _FakeClient()
            self._session_id = "sess-replay"
            self._replay_run_id = "run-old"

    app = _FakeApp()
    connection = TuiConnection(app, None, host="127.0.0.1", port=9999)
    await connection._subscribe_legacy_replay(app._client)

    assert calls[0]["scope"] == "run:run-old"
    assert calls[0]["replay_from_run"] == "run-old"
    assert "llm.reasoning" in calls[0]["topics"]
