from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_rook.core.processes import (
    ProcessSupervisor,
    create_shell_process,
    sanitized_shell_environment,
    terminate_process_tree,
    wait_for_process_leader,
)


# 探测 POSIX PID 是否仍存在，供真实进程组回归测试等待内核完成回收
def _posix_pid_exists(pid: int) -> bool:
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


# 有界等待 POSIX PID 消失，避免因 init 回收时序造成脆弱的即时断言
async def _wait_for_posix_pid_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while _posix_pid_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
# 功能：验证 forget 会清除 leader 正常退出后仍留在已保存进程组中的后台后代
# 设计：让受管 shell 派生 sleep 后立即退出，先确认 PGID 与子 PID 存活，再 forget 并等待子进程消失
async def test_process_supervisor_forget_reaps_residual_posix_group() -> None:
    supervisor = ProcessSupervisor()
    process = await supervisor.start_shell(
        "sh -c 'sleep 30 & echo $!'",
        label="orphaned-shell",
    )
    assert process.stdout is not None
    child_pid = int((await process.stdout.readline()).strip())
    await asyncio.wait_for(wait_for_process_leader(process), timeout=2.0)

    record = supervisor.snapshot()[0]
    assert record.process_group_id == process.pid
    assert _posix_pid_exists(child_pid)

    supervisor.forget(process)

    assert await _wait_for_posix_pid_exit(child_pid)
    assert supervisor.snapshot() == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
# 功能：验证 terminate_process_tree 在 leader 已退出时仍按 PGID 终止后台后代
# 设计：使用未接入 supervisor 的新会话 shell 重现提前退出，再直接调用公共终止函数检查子 PID
async def test_terminate_process_tree_reaps_group_after_leader_exit() -> None:
    process = await create_shell_process("sh -c 'sleep 30 & echo $!'")
    assert process.stdout is not None
    child_pid = int((await process.stdout.readline()).strip())
    await asyncio.wait_for(wait_for_process_leader(process), timeout=2.0)
    assert _posix_pid_exists(child_pid)

    await terminate_process_tree(process, grace_seconds=0.2)

    assert await _wait_for_posix_pid_exit(child_pid)


# 功能：验证 Shell 环境白名单无条件删除 API Key、Git Token 与 SSH agent 句柄
# 设计：显式覆盖多类敏感变量并保留 PATH，直接检查过滤结果以覆盖大小写归一化
def test_sanitized_shell_environment_removes_credentials() -> None:
    environment = sanitized_shell_environment(
        {
            "PATH": "test-path",
            "OPENAI_API_KEY": "provider-secret",
            "GITHUB_TOKEN": "git-secret",
            "SSH_AUTH_SOCK": "agent-socket",
        }
    )

    assert environment["PATH"] == "test-path"
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert "SSH_AUTH_SOCK" not in environment


# 功能：验证通用受管 argv 子进程默认也不会继承 daemon 的敏感环境变量
# 设计：把诱饵 token 放入进程环境并由真实 Python 子进程读取，覆盖非 Shell 扩展启动路径
async def test_process_supervisor_scrubs_default_exec_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-secret")
    supervisor = ProcessSupervisor()
    process = await supervisor.start_exec(
        sys.executable,
        "-c",
        "import os;print(os.getenv('GITHUB_TOKEN','missing'))",
        label="environment-boundary",
        stdout=asyncio.subprocess.PIPE,
    )

    stdout, _stderr = await process.communicate()
    supervisor.forget(process)

    assert stdout.strip() == b"missing"


# 功能：验证真实一次性 Shell 进程也无法读取传入的敏感环境变量
# 设计：用当前 Python 从子进程读取测试 API Key，断言进程边界实际收到 missing
async def test_create_shell_process_scrubs_sensitive_environment() -> None:
    command = (
        f'"{sys.executable}" -c '
        '"import os;print(os.getenv(\'CODEROOK_TEST_API_KEY\',\'missing\'))"'
    )
    process = await create_shell_process(
        command,
        env={"CODEROOK_TEST_API_KEY": "must-not-leak"},
    )

    stdout, _stderr = await process.communicate()

    assert b"must-not-leak" not in stdout
    assert b"missing" in stdout


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
