from __future__ import annotations

import asyncio

from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import InteractionManager
from code_rook.core.tools.builtin.ask_user_question import AskUserQuestionTool


# 功能：验证结构化问题会发布 typed 事件并等待对应答案
# 设计：先启动真实 ask Future，从事件取得动态 question_id 后回答，覆盖挂起、恢复和清理完整路径
async def test_question_waits_for_matching_answer() -> None:
    bus = EventBus()
    events: list[BaseModel] = []

    # 收集结构化问题事件供测试取得 question_id
    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    manager = InteractionManager(bus)
    task = asyncio.create_task(
        manager.ask(
            run_id="run-1",
            session_id="sess-1",
            question="选择数据库？",
            header="数据库",
            options=["SQLite", "PostgreSQL"],
            multi_select=False,
        )
    )
    await asyncio.sleep(0)

    event = events[0]
    question_id = str(getattr(event, "question_id"))
    assert getattr(event, "type") == "user_question.asked"
    assert getattr(event, "options") == ["SQLite", "PostgreSQL"]
    assert manager.answer(question_id, "SQLite")
    assert await task == "SQLite"
    assert not manager.answer(question_id, "PostgreSQL")


# 功能：验证提问工具把用户答案作为工具结果返回给模型
# 设计：运行真实工具 invoke 并由事件订阅者即时回答，证明 schema 工具与交互管理器正确连通
async def test_ask_user_question_tool_returns_answer() -> None:
    bus = EventBus()
    manager = InteractionManager(bus)

    # 在问题发布时立即回答，避免测试依赖定时轮询
    async def answer(event: BaseModel) -> None:
        manager.answer(str(getattr(event, "question_id")), "保留兼容性")

    bus.subscribe(answer)
    tool = AskUserQuestionTool(manager, "sess-1", "run-1")

    result = await tool.invoke(
        {
            "question": "是否保留旧接口？",
            "header": "兼容性",
            "options": ["保留兼容性", "直接移除"],
        }
    )

    assert not result.is_error
    assert result.content == "User answer: 保留兼容性"


# 功能：验证运行中纠偏只接受活动 run，并按到达顺序一次性取走
# 设计：覆盖未注册、已注册、空内容和注销四种边界，确保消息不会串到其他或后续 run
def test_steering_queue_is_scoped_to_active_run() -> None:
    manager = InteractionManager(EventBus())

    assert not manager.steer("run-1", "change direction")
    manager.register_run("run-1")
    assert manager.steer("run-1", "first")
    assert manager.steer("run-1", "second")
    assert not manager.steer("run-1", "   ")
    assert manager.drain_steering("run-1") == ["first", "second"]
    assert manager.drain_steering("run-1") == []

    manager.steer("run-1", "discard me")
    manager.unregister_run("run-1")
    assert manager.drain_steering("run-1") == []
    assert not manager.steer("run-1", "too late")
