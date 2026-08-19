from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import pytest

from code_rook.core.processes import ProcessSupervisor


# 功能：验证 ProcessSupervisor 登记 argv 子进程并可在自然退出后显式回收记录
# 设计：启动只输出固定文本的 Python 子进程，检查快照标签、输出和 forget 后空表
async def test_process_supervisor_tracks_and_forgets_exec() -> None:
    supervisor = ProcessSupervisor()
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        "print('ready')",
        label="test-worker",
        stdout=asyncio.subprocess.PIPE,
    )

    stdout, _stderr = await process.communicate()
    records = supervisor.snapshot()
    usage = supervisor.forget(process)

    assert stdout == b"ready\r\n" or stdout == b"ready\n"
    assert records[0].pid == process.pid
    assert records[0].label == "test-worker"
    assert usage.wall_time_ms >= 0
    assert usage.process_count >= 1
    if os.name == "nt":
        assert usage.complete is True
        assert usage.samples >= 1
    assert supervisor.snapshot() == ()


@pytest.mark.skipif(
    os.name != "nt" and not (os.name == "posix" and os.path.isdir("/proc")),
    reason="resource sampler currently supports Windows and Linux",
)
# 功能：验证受管进程可在运行期间返回 CPU、峰值内存、进程数与采样完整性
# 设计：真实分配约 4MB 并短暂等待，使 Windows Job 或 Linux /proc 至少取得一个稳定样本
async def test_process_supervisor_samples_resource_usage() -> None:
    supervisor = ProcessSupervisor()
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        "import time; payload=bytearray(4*1024*1024); time.sleep(0.3)",
        label="resource-worker",
    )
    await asyncio.sleep(0.15)

    usage = supervisor.usage(process)
    await supervisor.terminate(process)

    assert usage.complete is True
    assert usage.samples >= 1
    assert usage.peak_memory_bytes > 0
    assert usage.process_count >= 1


# 功能：验证 ProcessSupervisor.close 会终止仍在运行的整个受管进程
# 设计：启动长等待 Python 进程后立即关闭监督器，断言根进程退出且登记表清空
async def test_process_supervisor_close_terminates_running_process() -> None:
    supervisor = ProcessSupervisor()
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        label="long-worker",
    )

    await supervisor.close()

    assert process.returncode is not None
    assert supervisor.snapshot() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object only")
# 功能：验证 Windows 受管根进程的后代会继承 Job Object 且随 supervisor 一起终止
# 设计：真实启动会再派生长驻子进程的 Python 根进程，关闭后用 tasklist 确认子 PID 不再存活
async def test_windows_job_object_kills_descendant_tree() -> None:
    supervisor = ProcessSupervisor()
    script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(child.pid, flush=True); time.sleep(30)"
    )
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        script,
        label="windows-job-tree",
        stdout=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None
    child_pid = int((await process.stdout.readline()).strip())
    assert supervisor.snapshot()[0].job_managed is True

    await supervisor.close()
    tasklist = subprocess.run(
        ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert f'"{child_pid}"' not in tasklist.stdout
