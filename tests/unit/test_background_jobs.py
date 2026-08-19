from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel

from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.events.bus import EventBus
from code_rook.core.tools.builtin.background import (
    BackgroundCancelTool,
    BackgroundInteractTool,
    BackgroundListTool,
    BackgroundResultTool,
    BackgroundStartTool,
)


# 功能：验证后台命令跨工具调用保存结果并发布开始、结束事件
# 设计：使用短 Python 命令和真实 asyncio 子进程，轮询 registry 后同时断言输出与事件顺序
async def test_background_job_completes_and_emits_events() -> None:
    bus = EventBus()
    events: list[BaseModel] = []

    async def collect(event: BaseModel) -> None:
        events.append(event)

    bus.subscribe(collect)
    registry = BackgroundJobRegistry(bus)
    command = f'"{sys.executable}" -c "print(12345)"'
    started = await BackgroundStartTool(registry, "sess-1", "run-1").invoke(
        {"command": command, "timeout": 10}
    )
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]

    for _ in range(100):
        job = registry.get(job_id)
        if job is not None and job.status != "running":
            break
        await asyncio.sleep(0.01)

    result = await BackgroundResultTool(registry).invoke({"job_id": job_id})
    payload = json.loads(result.content)

    assert payload["status"] == "completed"
    assert "12345" in payload["output"]
    assert [event.type for event in events] == ["background.started", "background.finished"]  # type: ignore[attr-defined]


# 功能：验证后台任务列表按 session 隔离，取消后状态稳定为 cancelled
# 设计：启动长睡眠命令后从对应 session 列表获取 ID，再通过取消工具验证进程清理路径
async def test_background_list_and_cancel_are_session_scoped() -> None:
    registry = BackgroundJobRegistry(EventBus())
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    started = await BackgroundStartTool(registry, "sess-a", "run-a").invoke(
        {"command": command, "timeout": 20}
    )
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]

    listed = await BackgroundListTool(registry, "sess-a").invoke({})
    other = await BackgroundListTool(registry, "sess-b").invoke({})
    cancelled = await BackgroundCancelTool(registry).invoke({"job_id": job_id})

    assert json.loads(listed.content)[0]["job_id"] == job_id
    assert json.loads(other.content) == []
    assert not cancelled.is_error
    assert registry.get(job_id).status == "cancelled"  # type: ignore[union-attr]


# 功能：验证后台 shell 可接收后续 stdin，并由 wait 返回最终输出
# 设计：启动阻塞读取一行的 Python 子进程，再通过独立 interact 调用写入并关闭 stdin
async def test_background_job_interact_and_wait() -> None:
    registry = BackgroundJobRegistry(EventBus())
    command = subprocess.list2cmdline(
        [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"]
    )
    started = await BackgroundStartTool(registry, "sess-i", "run-i").invoke(
        {"command": command, "timeout": 10}
    )
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]

    interaction = await BackgroundInteractTool(registry).invoke(
        {"job_id": job_id, "stdin": "hello-background\n", "close_stdin": True}
    )
    result = await BackgroundResultTool(registry).invoke(
        {"job_id": job_id, "wait": True, "timeout": 10}
    )
    payload = json.loads(result.content)

    assert not interaction.is_error
    assert payload["status"] == "completed"
    assert "hello-background" in payload["output"]


# 功能：验证后台命令继承当前工具装配的工作目录而不是 daemon 启动目录
# 设计：在隔离目录运行打印 cwd 的真实短进程，并同时核对任务元数据与进程输出
async def test_background_job_uses_explicit_workspace_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    registry = BackgroundJobRegistry(EventBus())
    command = subprocess.list2cmdline(
        [sys.executable, "-c", "import pathlib; print(pathlib.Path.cwd())"]
    )
    tool = BackgroundStartTool(registry, "sess-cwd", "run-cwd", cwd=workspace)

    started = await tool.invoke({"command": command, "timeout": 10})
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]
    result = await BackgroundResultTool(registry).invoke(
        {"job_id": job_id, "wait": True, "timeout": 10}
    )
    payload = json.loads(result.content)

    assert payload["cwd"] == str(workspace.resolve())
    assert str(workspace.resolve()) in payload["output"]
