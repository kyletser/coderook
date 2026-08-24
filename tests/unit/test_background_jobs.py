from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.events.bus import EventBus
from code_rook.core.tools.builtin.background import (
    BackgroundCancelTool,
    BackgroundInteractTool,
    BackgroundListTool,
    BackgroundResultTool,
    BackgroundStartTool,
)


# 判断 POSIX PID 是否仍可运行，并在 Linux 上忽略等待父系统回收的僵尸条目
def _posix_pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.is_file():
        try:
            raw = stat_path.read_text(encoding="utf-8")
        except OSError:
            return False
        close_paren = raw.rfind(")")
        if close_paren >= 0 and raw[close_paren + 2 :].split()[0] == "Z":
            return False
    return True


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

    result = await BackgroundResultTool(registry, "sess-1").invoke({"job_id": job_id})
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
    cancelled = await BackgroundCancelTool(registry, "sess-a").invoke({"job_id": job_id})

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

    interaction = await BackgroundInteractTool(registry, "sess-i").invoke(
        {"job_id": job_id, "stdin": "hello-background\n", "close_stdin": True}
    )
    result = await BackgroundResultTool(registry, "sess-i").invoke(
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
    result = await BackgroundResultTool(registry, "sess-cwd").invoke(
        {"job_id": job_id, "wait": True, "timeout": 10}
    )
    payload = json.loads(result.content)

    assert payload["cwd"] == str(workspace.resolve())
    assert str(workspace.resolve()) in payload["output"]


# 功能：验证后台进程运行期间即可查询到已刷新的增量输出
# 设计：子进程先 flush 标记再阻塞读 stdin，轮询到标记后确认状态仍为 running 再释放进程
async def test_background_job_streams_output_while_running() -> None:
    registry = BackgroundJobRegistry(EventBus())
    command = subprocess.list2cmdline(
        [
            sys.executable,
            "-c",
            "import sys; print('early', flush=True); sys.stdin.readline(); print('late')",
        ]
    )
    job = registry.start(command, 10, "sess-stream", "run-stream")

    for _ in range(100):
        if "early" in job.output:
            break
        await asyncio.sleep(0.01)

    assert job.status == "running"
    assert "early" in job.output
    assert await registry.interact(job.id, "continue\n", close_stdin=True)
    completed = await registry.wait(job.id, 10)
    assert completed is not None
    assert completed.status == "completed"
    assert "late" in completed.output


# 功能：验证高输出后台进程保留有界预览并把完整字节流存为可分页 artifact
# 设计：将预览缩到 1 KiB 后输出 200 KiB，逐页读取内容寻址句柄以证明截断内容没有丢失
async def test_background_job_output_uses_bounded_ring(tmp_path: Path) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    registry = BackgroundJobRegistry(
        EventBus(),
        max_output_bytes=1024,
        artifact_store=artifact_store,
    )
    command = subprocess.list2cmdline(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"]
    )
    job = registry.start(command, 10, "sess-noisy", "run-noisy")

    completed = await registry.wait(job.id, 10)

    assert completed is not None
    assert completed.status == "completed"
    assert completed.output_bytes == 200000
    assert completed.output_truncated
    assert completed.output.startswith("[earlier output truncated]\n")
    assert len(completed.output.encode("utf-8")) <= 1400
    assert completed.output_artifact.startswith("artifact:")
    assert completed.output_artifact_size == 200000

    sha256 = completed.output_artifact.removeprefix("artifact:")
    offset = 0
    recovered: list[str] = []
    while True:
        page = await artifact_store.read(sha256, offset=offset, limit=50_000)
        recovered.append(page.content)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert "".join(recovered) == "x" * 200000


# 功能：验证 result、interact、cancel 都不能凭 job_id 跨 session 操作后台任务
# 设计：其他 session 依次尝试三种入口并确认任务仍运行，最后由 owner 取消完成清理
async def test_background_job_operations_reject_cross_session_access() -> None:
    registry = BackgroundJobRegistry(EventBus())
    command = f'"{sys.executable}" -c "import time; time.sleep(10)"'
    started = await BackgroundStartTool(registry, "sess-owner", "run-owner").invoke(
        {"command": command, "timeout": 20}
    )
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]

    result = await BackgroundResultTool(registry, "sess-other").invoke(
        {"job_id": job_id}
    )
    interaction = await BackgroundInteractTool(registry, "sess-other").invoke(
        {"job_id": job_id, "stdin": "intrusion\n"}
    )
    cancellation = await BackgroundCancelTool(registry, "sess-other").invoke(
        {"job_id": job_id}
    )

    assert result.is_error
    assert interaction.is_error
    assert cancellation.is_error
    assert registry.get(job_id) is not None
    assert registry.get(job_id).status == "running"  # type: ignore[union-attr]
    owner_cancel = await BackgroundCancelTool(registry, "sess-owner").invoke(
        {"job_id": job_id}
    )
    assert not owner_cancel.is_error


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
# 功能：验证后台 shell 的 leader 正常退出时 registry 仍会回收同组的后台后代
# 设计：执行典型 sh 后台 sleep 并从结果提取 PID，要求任务正常完成且 PID 在返回前后有界消失
async def test_background_job_reaps_descendant_after_leader_exit() -> None:
    registry = BackgroundJobRegistry(EventBus())
    job = registry.start(
        "sh -c 'sleep 30 & echo $!'",
        5,
        "sess-orphan",
        "run-orphan",
    )

    completed = await registry.wait(job.id, 5)

    assert completed is not None
    assert completed.status == "completed"
    child_pid = int(completed.output.strip().splitlines()[0])
    child_exists = True
    for _ in range(100):
        if not _posix_pid_running(child_pid):
            child_exists = False
            break
        await asyncio.sleep(0.02)

    assert child_exists is False
