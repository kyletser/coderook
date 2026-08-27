"""TUI 与 core 守护进程之间 socket 连接层的抽象。

`TuiConnection` 持有 `SocketClient` 及订阅 topics，负责连接、断线重连、
事件订阅与会话（创建/恢复）的生命周期。它通过回调把事件交回 App，而不直接
驱动 Textual 消息泵。连接期间通过 `self._app._client` 暴露当前客户端，供 App
的 IPC 动作（`send_command`）使用。
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, cast

from textual.widgets import Label

from code_rook.core.transport.socket_client import IpcError, SocketClient
from code_rook.tui.product import tr

log = logging.getLogger(__name__)

# 按 thread 订阅的持久事件，llm.token 与 session 生命周期由下方实时通道补齐
_THREAD_TOPICS = [
    "run.*",
    "step.*",
    "agent.*",
    "tool.*",
    "llm.reasoning",
    "llm.usage",
    "llm.route_selected",
    "llm.retry",
    "log.*",
    "permission.*",
    "context.*",
    "verification.*",
    "goal.*",
    "subagent.*",
    "skill.*",
    "plan.*",
    "strategy.*",
    "recovery.*",
    "user_question.*",
    "lsp.*",
    "background.*",
    "hook.executed",
]

# 仅订阅不写入 runtime ledger 的会话实时事件，分发前仍会校验 session/run 归属
_LIVE_TOPICS = ["session.*", "llm.token"]

# daemon 级事件不进入会话渲染器，避免打断当前流式块或改写 run 状态
_DAEMON_TOPICS = ["core.*", "audit.*"]

# 旧 --replay 使用原始 run 事件，同时补齐新增的 LLM 可观测 topic
_REPLAY_TOPICS = [*_THREAD_TOPICS, *_LIVE_TOPICS]

# session.resume 在活动 Turn 持锁时返回的稳定 JSON-RPC 错误码
_SESSION_BUSY_ERROR = -32012


@dataclass
class SessionUiState:
    active_run_id: str | None = None
    pending_permissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_question: dict[str, Any] | None = None
    pending_plan: dict[str, Any] | None = None


# 优先选择最近的非空会话；若只有空会话则复用最新一个，避免重复堆积
def _select_recent_session(sessions: object) -> str | None:
    if not isinstance(sessions, list):
        return None
    candidates = [
        item
        for item in sessions
        if isinstance(item, dict) and item.get("mode", "chat") == "chat"
    ]
    nonempty = [
        item
        for item in candidates
        if (
            isinstance(item.get("run_count"), int)
            and not isinstance(item.get("run_count"), bool)
            and item.get("run_count", 0) > 0
        )
        or item.get("last_run_id")
    ]
    selected = nonempty[0] if nonempty else (candidates[0] if candidates else None)
    if selected is None:
        return None
    session_id = selected.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


class TuiConnection:
    """封装 socket 连接循环，事件经回调回交给 App。"""

    def __init__(
        self,
        app: Any,
        on_event: Any,
        *,
        host: str,
        port: int,
        auth_token: str | None = None,
        client_factory: Any = None,
    ) -> None:
        # 初始化连接参数、事件回调和客户端工厂
        self._app = app
        self._on_event = on_event
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._client_factory = client_factory if client_factory is not None else SocketClient
        self._session_cursors: dict[str, int] = {}
        self._session_run_ids: dict[str, set[str]] = {}
        self._session_ui_states: dict[str, SessionUiState] = {}
        self._client_thread_subscriptions: dict[str, str] = {}
        self._activation_lock = asyncio.Lock()
        self._sessions_requiring_replay: set[str] = set()
        self._activating_session: str | None = None
        self._activation_buffer: dict[int, dict[str, Any]] = {}
        self._live_subscribed = False
        self._daemon_subscribed = False

    # 使用 App 当前界面语言生成连接层的用户可见文案
    def _text(self, key: str, **values: object) -> str:
        locale = getattr(self._app, "_locale", "en-US")
        return tr(key, locale, **values)

    # 暴露当前活跃客户端，App 通过它发送 IPC 命令
    @property
    def client(self) -> SocketClient | None:
        return cast(SocketClient | None, self._app._client)

    # 将已验证归属的会话事件交给 App 回调
    async def _deliver_event(self, event: dict[str, Any]) -> None:
        if not callable(self._on_event):
            return
        result = self._on_event(event)
        if inspect.isawaitable(result):
            await result

    # 将 daemon 事件交给独立可选回调，绝不进入会话渲染器
    async def _dispatch_daemon_event(self, event: dict[str, Any]) -> None:
        callback = getattr(self._app, "_handle_daemon_event", None)
        if not callable(callback):
            return
        result = callback(event)
        if inspect.isawaitable(result):
            await result

    # 从 runtime.event 恢复原始事件形状，补回渲染器需要的 run/session 标识
    @staticmethod
    def _adapt_runtime_event(event: dict[str, Any]) -> tuple[str, int, dict[str, Any]] | None:
        thread_id = event.get("thread_id")
        seq = event.get("seq")
        event_type = event.get("event_type")
        payload = event.get("payload")
        if (
            not isinstance(thread_id, str)
            or not thread_id
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or not isinstance(event_type, str)
            or not event_type
            or not isinstance(payload, dict)
        ):
            log.warning("ignored malformed runtime event: %r", event)
            return None
        adapted = dict(payload)
        adapted["type"] = event_type
        adapted.setdefault("session_id", thread_id)
        turn_id = event.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            run_id = (
                adapted.get("worker_run_id")
                if event_type.startswith("subagent.")
                else turn_id
            )
            if isinstance(run_id, str) and run_id:
                adapted.setdefault("run_id", run_id)
        timestamp = event.get("ts")
        if isinstance(timestamp, str) and timestamp:
            adapted.setdefault("ts", timestamp)
        return thread_id, seq, adapted

    # 把 durable 事件归并为可在会话切回时恢复的活动交互状态
    def _reduce_session_state(self, session_id: str, payload: dict[str, Any]) -> None:
        state = self._session_ui_states.setdefault(session_id, SessionUiState())
        event_type = str(payload.get("type", ""))
        run_id = payload.get("run_id")
        progress_event = event_type.startswith(
            ("agent.", "step.", "tool.", "llm.", "verification.")
        ) or event_type in {"run.started", "run.finished"}
        if progress_event and isinstance(run_id, str) and run_id:
            if (
                state.pending_question is not None
                and state.pending_question.get("run_id") == run_id
            ):
                state.pending_question = None
            if (
                state.pending_plan is not None
                and state.pending_plan.get("run_id") == run_id
            ):
                state.pending_plan = None
        if event_type == "run.started" and isinstance(run_id, str) and run_id:
            state.active_run_id = run_id
        elif event_type == "run.finished":
            if not isinstance(run_id, str) or state.active_run_id == run_id:
                state.active_run_id = None
                state.pending_permissions.clear()
                state.pending_question = None
                state.pending_plan = None
        elif event_type in {"session.interrupted", "session.closed"}:
            state.active_run_id = None
            state.pending_permissions.clear()
            state.pending_question = None
            state.pending_plan = None
        elif event_type == "permission.requested":
            tool_use_id = payload.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id:
                state.pending_permissions[tool_use_id] = dict(payload)
        elif event_type in {"permission.granted", "permission.denied"}:
            tool_use_id = payload.get("tool_use_id")
            if isinstance(tool_use_id, str):
                state.pending_permissions.pop(tool_use_id, None)
        elif event_type == "user_question.asked":
            state.pending_question = dict(payload)
        elif event_type == "plan.ready":
            state.pending_plan = dict(payload)
        elif event_type == "plan.resolved":
            pending = state.pending_plan
            if pending is not None and pending.get("run_id") == run_id:
                state.pending_plan = None

    # 激活历史时只重建最终仍待处理的交互控件，跳过已被后续事件解决的旧请求
    def _activation_event_is_visible(
        self,
        session_id: str,
        payload: dict[str, Any],
    ) -> bool:
        event_type = str(payload.get("type", ""))
        state = self._session_ui_states.get(session_id, SessionUiState())
        if event_type == "permission.requested":
            tool_use_id = payload.get("tool_use_id")
            return (
                isinstance(tool_use_id, str)
                and tool_use_id in state.pending_permissions
            )
        if event_type == "user_question.asked":
            return (
                state.pending_question is not None
                and state.pending_question.get("question_id")
                == payload.get("question_id")
            )
        if event_type == "plan.ready":
            return (
                state.pending_plan is not None
                and state.pending_plan.get("run_id") == payload.get("run_id")
            )
        if event_type == "plan.resolved":
            return False
        return True

    # 返回指定 session 当前归并状态的隔离副本
    def session_state(self, session_id: str) -> SessionUiState:
        state = self._session_ui_states.get(session_id, SessionUiState())
        return SessionUiState(
            active_run_id=state.active_run_id,
            pending_permissions={
                key: dict(value) for key, value in state.pending_permissions.items()
            },
            pending_question=(
                dict(state.pending_question) if state.pending_question is not None else None
            ),
            pending_plan=dict(state.pending_plan) if state.pending_plan is not None else None,
        )

    # 标记本地已提交的审批决定，避免切回会话时复活旧控件
    def resolve_permission(self, session_id: str, tool_use_id: str) -> None:
        state = self._session_ui_states.setdefault(session_id, SessionUiState())
        state.pending_permissions.pop(tool_use_id, None)

    # 标记本地已提交的问题回答，避免切回会话时复活旧控件
    def resolve_question(self, session_id: str, question_id: str) -> None:
        state = self._session_ui_states.setdefault(session_id, SessionUiState())
        pending = state.pending_question
        if pending is not None and pending.get("question_id") == question_id:
            state.pending_question = None

    # 在 Core 成功持久解决后同步清除匹配 run 的本地计划归并状态
    def resolve_plan(self, session_id: str, run_id: str) -> None:
        state = self._session_ui_states.setdefault(session_id, SessionUiState())
        pending = state.pending_plan
        if pending is not None and pending.get("run_id") == run_id:
            state.pending_plan = None

    # 判断会话是否因上次激活失败而必须重新回放后才能继续确认事件
    def session_requires_replay(self, session_id: str) -> bool:
        return session_id in self._sessions_requiring_replay

    # 按 session 游标去重；非当前事件只归并状态而不确认游标
    async def _dispatch_runtime_event(self, event: dict[str, Any]) -> None:
        adapted = self._adapt_runtime_event(event)
        if adapted is None:
            return
        thread_id, seq, payload = adapted
        if (
            thread_id in self._sessions_requiring_replay
            and thread_id != self._activating_session
        ):
            return
        previous = self._session_cursors.get(thread_id, 0)
        if seq <= previous:
            return
        self._reduce_session_state(thread_id, payload)
        if payload.get("type") == "run.started":
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                self._session_run_ids.setdefault(thread_id, set()).add(run_id)
        if thread_id == self._activating_session:
            self._activation_buffer[seq] = payload
            return
        if thread_id != getattr(self._app, "_session_id", None):
            return
        await self._deliver_event(payload)
        self._session_cursors[thread_id] = seq

    # 校验实时事件的 session/run 归属，防止全局通道污染当前会话
    def _live_event_belongs_to_current_session(self, event: dict[str, Any]) -> bool:
        session_id = getattr(self._app, "_session_id", None)
        if not isinstance(session_id, str) or not session_id:
            return False
        event_type = str(event.get("type", ""))
        if event_type.startswith("session."):
            return event.get("session_id") == session_id
        if event_type == "llm.token":
            run_id = event.get("run_id")
            return (
                isinstance(run_id, str)
                and run_id in self._session_run_ids.get(session_id, set())
            )
        return False

    # 事件分发：分离 daemon、durable thread、经归属校验的实时事件与旧 run replay
    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "runtime.event":
            await self._dispatch_runtime_event(event)
            return
        if event_type.startswith(("core.", "audit.")):
            await self._dispatch_daemon_event(event)
            return
        if self._live_event_belongs_to_current_session(event):
            await self._deliver_event(event)
            return
        replay_run_id = getattr(self._app, "_replay_run_id", None)
        if replay_run_id is not None and event.get("run_id") == replay_run_id:
            await self._deliver_event(event)

    # 记住 session 已知 run，使重连时的实时 token 能在 durable replay 前通过归属校验
    def _remember_session_run(self, session_id: str, run_id: object) -> None:
        if isinstance(run_id, str) and run_id:
            self._session_run_ids.setdefault(session_id, set()).add(run_id)

    # 为当前 socket 建立一次实时会话通道和独立 daemon 通道
    async def _subscribe_connection_events(self, client: Any) -> None:
        if not self._live_subscribed:
            await client.send_command(
                "event.subscribe",
                {"topics": list(_LIVE_TOPICS), "scope": "global"},
            )
            self._live_subscribed = True
        if not self._daemon_subscribed:
            await client.send_command(
                "event.subscribe",
                {"topics": list(_DAEMON_TOPICS), "scope": "global"},
            )
            self._daemon_subscribed = True

    # 为指定 session 建立带游标的 durable thread 订阅并返回服务端高水位
    async def subscribe_session(self, session_id: str) -> int:
        client = self.client
        if client is None or not session_id or session_id in self._client_thread_subscriptions:
            return self._session_cursors.get(session_id, 0)
        after_seq = self._session_cursors.get(session_id, 0)
        result = await client.send_command(
            "event.subscribe",
            {
                "topics": list(_THREAD_TOPICS),
                "thread_id": session_id,
                "after_seq": after_seq,
            },
        )
        subscription_id = result.get("subscription_id")
        if not isinstance(subscription_id, str) or not subscription_id:
            raise ValueError("event.subscribe returned malformed subscription_id")
        self._client_thread_subscriptions[session_id] = subscription_id
        last_seq = result.get("last_seq")
        if isinstance(last_seq, int) and not isinstance(last_seq, bool):
            return last_seq
        return self._session_cursors.get(session_id, 0)

    # 只取消指定 session 在当前 socket 上的 thread 订阅，保留 live 与 daemon 全局通道
    async def unsubscribe_session(self, session_id: str) -> bool:
        client = self.client
        subscription_id = self._client_thread_subscriptions.get(session_id)
        if client is None or subscription_id is None:
            return False
        result = await client.send_command(
            "event.unsubscribe",
            {"subscription_id": subscription_id},
        )
        removed = result.get("removed")
        if not isinstance(removed, bool):
            raise ValueError("event.unsubscribe returned malformed removed state")
        if self._client_thread_subscriptions.get(session_id) == subscription_id:
            self._client_thread_subscriptions.pop(session_id, None)
        return removed

    # 将 event.replay 记录恢复成 TUI 使用的 payload 形状
    @staticmethod
    def _adapt_replay_record(
        session_id: str,
        record: dict[str, Any],
    ) -> tuple[int, dict[str, Any]] | None:
        seq = record.get("seq")
        event_type = record.get("type")
        payload = record.get("payload")
        if (
            not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or not isinstance(event_type, str)
            or not isinstance(payload, dict)
        ):
            return None
        adapted = dict(payload)
        adapted["type"] = event_type
        adapted.setdefault("session_id", session_id)
        turn_id = record.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            run_id = adapted.get("worker_run_id") if event_type.startswith("subagent.") else turn_id
            if isinstance(run_id, str) and run_id:
                adapted.setdefault("run_id", run_id)
        timestamp = record.get("ts")
        if isinstance(timestamp, str) and timestamp:
            adapted.setdefault("ts", timestamp)
        return seq, adapted

    # 分页读取 session 游标后的 durable 事件并返回高水位
    async def _replay_session(self, session_id: str) -> int:
        client = self.client
        if client is None:
            return self._session_cursors.get(session_id, 0)
        cursor = self._session_cursors.get(session_id, 0)
        latest = cursor
        while True:
            result = await client.send_command(
                "event.replay",
                {"thread_id": session_id, "after_seq": cursor, "limit": 1000},
            )
            raw_events = result.get("events", [])
            events = raw_events if isinstance(raw_events, list) else []
            for raw in events:
                if not isinstance(raw, dict):
                    continue
                adapted = self._adapt_replay_record(session_id, raw)
                if adapted is None:
                    continue
                seq, payload = adapted
                cursor = max(cursor, seq)
                self._reduce_session_state(session_id, payload)
                if any(
                    fnmatch.fnmatch(str(payload.get("type", "")), topic)
                    for topic in _THREAD_TOPICS
                ):
                    self._activation_buffer[seq] = payload
            raw_latest = result.get("latest_seq")
            if isinstance(raw_latest, int) and not isinstance(raw_latest, bool):
                latest = max(latest, raw_latest)
            if not bool(result.get("has_more", False)) or not events:
                return max(latest, cursor)

    # 串行切换会话，避免并发激活覆盖共享缓冲区或遗留多个 thread 订阅
    async def activate_session(self, session_id: str, prepare: Any) -> SessionUiState:
        async with self._activation_lock:
            return await self._activate_session_locked(session_id, prepare)

    # 在激活锁内准备目标会话、补交缺失事件并只在渲染完成后确认游标
    async def _activate_session_locked(
        self,
        session_id: str,
        prepare: Any,
    ) -> SessionUiState:
        if self.client is None:
            return self.session_state(session_id)
        previous_session = getattr(self._app, "_session_id", None)
        previous_unsubscribed = False
        if (
            isinstance(previous_session, str)
            and previous_session
            and previous_session != session_id
        ):
            previous_unsubscribed = (
                previous_session in self._client_thread_subscriptions
            )
            await self.unsubscribe_session(previous_session)
        self._activating_session = session_id
        self._activation_buffer = {}
        high_water = self._session_cursors.get(session_id, 0)
        try:
            if session_id in self._client_thread_subscriptions:
                high_water = await self._replay_session(session_id)
            else:
                high_water = await self.subscribe_session(session_id)
            prepared = prepare()
            if inspect.isawaitable(prepared):
                await prepared
            while self._activation_buffer:
                pending = sorted(self._activation_buffer.items())
                self._activation_buffer = {}
                for seq, payload in pending:
                    if seq <= self._session_cursors.get(session_id, 0):
                        continue
                    if self._activation_event_is_visible(session_id, payload):
                        await self._deliver_event(payload)
                    self._session_cursors[session_id] = seq
            self._session_cursors[session_id] = max(
                self._session_cursors.get(session_id, 0),
                high_water,
            )
            self._sessions_requiring_replay.discard(session_id)
            return self.session_state(session_id)
        except (Exception, asyncio.CancelledError):
            self._sessions_requiring_replay.add(session_id)
            still_on_previous = (
                isinstance(previous_session, str)
                and getattr(self._app, "_session_id", None) == previous_session
            )
            try:
                await self.unsubscribe_session(session_id)
            except Exception:
                log.exception(
                    "failed to remove target thread subscription session_id=%s",
                    session_id,
                )
            if still_on_previous:
                if previous_unsubscribed and isinstance(previous_session, str):
                    try:
                        await self.subscribe_session(previous_session)
                    except Exception:
                        log.exception(
                            "failed to restore previous thread subscription session_id=%s",
                            previous_session,
                        )
            raise
        finally:
            self._activating_session = None
            self._activation_buffer = {}

    # 保留 --replay RUN_ID 的原始事件回放语义，但限定在指定 run scope
    async def _subscribe_legacy_replay(self, client: Any) -> None:
        replay_run_id = getattr(self._app, "_replay_run_id", None)
        if not isinstance(replay_run_id, str) or not replay_run_id:
            return
        await client.send_command(
            "event.subscribe",
            {
                "topics": list(_REPLAY_TOPICS),
                "scope": f"run:{replay_run_id}",
                "replay_from_run": replay_run_id,
            },
        )

    # 在真实 App 支持时显示去重的连接恢复建议，同时兼容测试替身
    def _show_connection_problem(self, kind: str, detail: str) -> None:
        callback = getattr(self._app, "_show_connection_problem", None)
        if callable(callback):
            callback(kind, detail)

    # 在 App 支持 Goal 控制面时恢复当前 session 的持久目标状态
    async def _refresh_goal_state(self) -> None:
        callback = getattr(self._app, "_refresh_goal_state", None)
        if not callable(callback):
            return
        result = callback()
        if inspect.isawaitable(result):
            await result

    # 尝试在线程中启动受管 Core，避免同步启动探针阻塞 Textual 事件循环
    async def _recover_core(self) -> tuple[bool, str]:
        callback = getattr(self._app, "_core_recovery", None)
        if not callable(callback):
            return False, self._text("connection.recovery_disabled")
        try:
            started = await asyncio.to_thread(callback)
        except Exception as exc:
            log.exception("automatic Core recovery failed")
            return False, self._text("connection.restart_failed", error=exc)
        action = self._text(
            "connection.core_started" if started else "connection.core_available"
        )
        return True, action

    # 通知真实 App 当前会话来自新建、首次恢复或断线重连，同时兼容测试替身
    def _show_session_ready(
        self,
        action: str,
        session_id: str,
        title: str,
        history_count: int | None,
    ) -> None:
        callback = getattr(self._app, "_show_session_ready", None)
        if callable(callback):
            callback(action, session_id, title, history_count)

    # 恢复空闲会话；活动会话持锁时改读权威 thread 投影并只读附着
    async def resume_or_attach_session(
        self,
        client: Any,
        session_id: str,
    ) -> tuple[dict[str, Any], bool]:
        try:
            resumed = await client.send_command(
                "session.resume",
                {"session_id": session_id},
            )
        except IpcError as exc:
            if exc.code != _SESSION_BUSY_ERROR:
                raise
            fetched = await client.send_command(
                "thread.get",
                {"thread_id": session_id},
            )
            raw_thread = fetched.get("thread")
            if not isinstance(raw_thread, dict):
                raise ValueError("thread.get returned malformed thread") from exc
            thread_id = raw_thread.get("id")
            if thread_id != session_id:
                raise ValueError("thread.get returned a different thread") from exc
            title = raw_thread.get("title", "")
            return {
                "session_id": session_id,
                "title": title if isinstance(title, str) else "",
                "status": raw_thread.get("status", "running"),
                "last_run_id": None,
            }, True
        raw_session = resumed.get("session")
        if not isinstance(raw_session, dict):
            raise ValueError("session.resume returned malformed session")
        resumed_id = raw_session.get("session_id")
        if resumed_id != session_id:
            raise ValueError("session.resume returned a different session")
        return dict(raw_session), False

    # 用权威 transcript 重建恢复会话并在 durable replay 后对账最终交互状态
    async def _restore_resumed_session(
        self,
        client: Any,
        session_id: str,
        title: str,
        *,
        reconnecting: bool,
    ) -> tuple[SessionUiState, int]:
        history = await client.send_command(
            "session.get_history",
            {"session_id": session_id},
        )
        raw_messages = history.get("messages", [])
        messages = raw_messages if isinstance(raw_messages, list) else []
        prepare_view = getattr(self._app, "_prepare_session_view", None)

        # 真实 App 原子清空旧流式视图并用完整 transcript 重建，测试替身保留兼容入口
        async def prepare() -> None:
            if callable(prepare_view):
                result = prepare_view(
                    session_id,
                    messages,
                    resume=True,
                    title=title,
                )
                if inspect.isawaitable(result):
                    await result
                return
            if not getattr(self._app, "_history_loaded", False) or reconnecting:
                self._app._append_history(messages)
            self._app._history_loaded = True

        state = await self.activate_session(session_id, prepare)
        reconcile = getattr(self._app, "_reconcile_session_state", None)
        if callable(reconcile):
            reconcile(state)
        restore_composer = getattr(self._app, "_restore_session_composer", None)
        if callable(restore_composer):
            restore_composer(session_id)
        return state, len(messages)

    # 关闭握手阶段创建的客户端；测试替身或旧实现没有 close 时安全跳过
    async def _close_client(self, client: Any) -> None:
        close = getattr(client, "close", None)
        if not callable(close):
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except OSError:
            log.debug("client close failed during connection cleanup", exc_info=True)

    # 管理 SocketClient 生命周期：连接、订阅事件、断线重连、会话恢复
    async def run(self) -> None:
        header = self._app.query_one("#header", Label)

        while True:
            client = self._client_factory(
                self._host, self._port, auth_token=self._auth_token
            )
            self._app._client = None
            try:
                await client.connect()
            except (ConnectionRefusedError, OSError):
                log.warning("connection refused %s:%s, retrying", self._host, self._port)
                await self._close_client(client)
                self._app._update_header("disconnected")
                self._app._mark_disconnected()
                recovered, detail = await self._recover_core()
                if recovered:
                    self._show_connection_problem("recovering", detail)
                    await asyncio.sleep(0.1)
                else:
                    self._show_connection_problem(
                        "unreachable",
                        self._text(
                            "connection.unreachable",
                            host=self._host,
                            port=self._port,
                            detail=detail,
                        ),
                    )
                    await asyncio.sleep(2)
                continue
            except IpcError as exc:
                log.error("IPC authentication failed: %s", exc)
                await self._close_client(client)
                header.update(
                    "[bold]CodeRook[/bold]  "
                    f"[red]{self._text('connection.authentication_failed')}[/red]"
                )
                self._app._mark_disconnected()
                self._show_connection_problem("authentication", str(exc))
                await asyncio.sleep(2)
                continue

            log.info("connected to %s:%s", self._host, self._port)
            self._app._client = client
            self._client_thread_subscriptions.clear()
            self._live_subscribed = False
            self._daemon_subscribed = False
            self._app._update_header("connecting")
            loop_task = asyncio.create_task(client.run_event_loop())

            client.on_event(self._dispatch_event)

            try:
                loop_task.add_done_callback(
                    lambda t: log.error("loop_task failed: %s", t.exception())
                    if not t.cancelled() and t.exception() is not None
                    else None
                )
                resume_session_id = self._app._resume_session_id
                reconnecting = bool(
                    resume_session_id is not None and self._app._history_loaded
                )
                if resume_session_id is None and getattr(
                    self._app,
                    "_continue_recent",
                    False,
                ):
                    recent = await client.send_command(
                        "session.list",
                        {"include_closed": False, "limit": 50},
                    )
                    resume_session_id = _select_recent_session(
                        recent.get("sessions", [])
                    )
                if resume_session_id is None:
                    created = await client.send_command("session.create", {"mode": "chat"})
                    self._app._session_id = str(created["session_id"])
                    self._app._resume_session_id = self._app._session_id
                    self._app._history_loaded = True
                    self._app._session_title = ""
                    self._app._titled = False
                    self._app._first_user_text = ""
                    log.info("session created session_id=%s", self._app._session_id)
                    self._show_session_ready(
                        "created",
                        self._app._session_id,
                        "",
                        0,
                    )
                else:
                    resumed_info, attached_active = await self.resume_or_attach_session(
                        client,
                        resume_session_id,
                    )
                    resumed_title = str(resumed_info.get("title", ""))
                    self._app._session_id = str(resumed_info["session_id"])
                    self._app._resume_session_id = self._app._session_id
                    self._app._session_title = resumed_title
                    self._app._titled = bool(
                        resumed_title and resumed_title != "Untitled"
                    )
                    self._remember_session_run(
                        self._app._session_id,
                        resumed_info.get("last_run_id"),
                    )
                    log.info(
                        "session %s session_id=%s",
                        "attached while active" if attached_active else "resumed",
                        self._app._session_id,
                    )
                    history_count: int | None = None
                await self._subscribe_connection_events(client)
                if resume_session_id is not None:
                    _state, history_count = await self._restore_resumed_session(
                        client,
                        self._app._session_id,
                        resumed_title,
                        reconnecting=reconnecting,
                    )
                    self._show_session_ready(
                        "reconnected" if reconnecting else "resumed",
                        self._app._session_id,
                        resumed_title,
                        history_count,
                    )
                else:
                    await self.subscribe_session(self._app._session_id)
                await self._subscribe_legacy_replay(client)
                await self._app._refresh_authority()
                await self._refresh_goal_state()
                self._app._mark_connected()
                await loop_task
            except (IpcError, KeyError, TypeError, ValueError, OSError) as e:
                header.update(
                    "[bold]CodeRook[/bold]  "
                    f"[red]{self._text('connection.setup_failed')}[/red]"
                )
                self._show_connection_problem("protocol", str(e))
            finally:
                if not loop_task.done():
                    loop_task.cancel()
                self._app._client = None
                self._app._session_id = None
                self._app._mark_disconnected()
                self._app._break_llm()
                await client.close()

            self._show_connection_problem(
                "disconnected",
                self._text("connection.closed", host=self._host, port=self._port),
            )

            self._app._update_header("disconnected")
            await asyncio.sleep(2)
