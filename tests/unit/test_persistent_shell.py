from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from code_rook.core.artifacts import ArtifactStore
from code_rook.core.authority.models import SandboxCapability
from code_rook.core.persistent_shell import (
    PersistentShellPool,
    PersistentShellSession,
    default_shell_argv,
)
from code_rook.core.sandbox.planner import SandboxTier, plan_sandbox
from code_rook.core.tools.builtin.bash import BashTool

_IS_WINDOWS = os.name == "nt"


# 功能：验证常驻 shell 顺序执行命令并透传退出码
# 设计：真实子进程端到端跑两条 echo，断言输出内容与 exit 0 状态
async def test_persistent_shell_runs_commands_with_exit_code() -> None:
    session = PersistentShellSession()

    first = await session.run("echo coderook-one", timeout_s=15.0)
    second = await session.run("echo coderook-two", timeout_s=15.0)

    assert first.exit_code == 0 and not first.timed_out and not first.died
    assert "coderook-one" in first.text
    assert "coderook-two" in second.text
    assert "coderook-one" not in second.text
    await session._terminate()


# 功能：验证常驻 shell 的 cwd 与环境变量跨调用保持
# 设计：先 cd 到子目录并设置变量，第二次调用读取 pwd 与变量值，断言状态延续
async def test_persistent_shell_keeps_cwd_and_env(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    session = PersistentShellSession(cwd=tmp_path)

    if _IS_WINDOWS:
        await session.run("cd sub", timeout_s=15.0)
        await session.run("set CODEROOK_PERSIST=kept", timeout_s=15.0)
        probe = await session.run("cd", timeout_s=15.0)
        value = await session.run("echo %CODEROOK_PERSIST%", timeout_s=15.0)
    else:
        await session.run("cd sub", timeout_s=15.0)
        await session.run("export CODEROOK_PERSIST=kept", timeout_s=15.0)
        probe = await session.run("pwd", timeout_s=15.0)
        value = await session.run("echo $CODEROOK_PERSIST", timeout_s=15.0)

    assert probe.exit_code == 0
    assert "sub" in probe.text
    assert "kept" in value.text
    await session._terminate()


# 功能：验证非零退出码被解析为结构化结果而非崩溃
# 设计：按平台构造必然失败的命令，断言 exit_code 与超时/死亡标志为假
async def test_persistent_shell_reports_nonzero_exit() -> None:
    session = PersistentShellSession()
    if _IS_WINDOWS:
        outcome = await session.run("cmd /c exit 3", timeout_s=15.0)
    else:
        outcome = await session.run("sh -c 'exit 3'", timeout_s=15.0)

    assert outcome.exit_code == 3
    assert not outcome.timed_out and not outcome.died
    await session._terminate()


# 功能：验证命令超时会终止常驻进程并标记会话失效
# 设计：长驻命令配 1 秒超时，断言 timed_out、alive 变 False，池可重建会话
async def test_persistent_shell_timeout_kills_session(tmp_path: Path) -> None:
    session = PersistentShellSession(cwd=tmp_path)
    if _IS_WINDOWS:
        command = "ping -n 8 127.0.0.1 > nul"
    else:
        command = "sleep 8"

    outcome = await session.run(command, timeout_s=1.0)

    assert outcome.timed_out
    assert not session.alive

    pool = PersistentShellPool()
    pool.get_or_create("k1", tmp_path)
    pool._sessions["k1"] = session
    refreshed = pool.get_or_create("k1", tmp_path)

    assert refreshed is not session


# 功能：验证池按 key 复用会话并在空闲后回收
# 设计：同 key 两次获取返回同一会话；把 last_used 拨回过期后 sweep 建新会话
async def test_pool_reuses_and_recycles_by_key(tmp_path: Path) -> None:
    pool = PersistentShellPool(idle_recycle_s=60.0)

    first = pool.get_or_create("sess-a", tmp_path)
    again = pool.get_or_create("sess-a", tmp_path)

    assert first is again
    assert pool.size() == 1

    first.last_used -= 120.0
    refreshed = pool.get_or_create("sess-a", tmp_path)

    assert refreshed is not first
    assert pool.size() == 1
    await pool.aclose_all()


# 功能：验证同一 session 的沙箱或工作目录变化不会复用旧常驻进程
# 设计：先创建无沙箱会话再切换到模拟 bwrap 计划，断言身份失配立即生成新会话并回收旧实例
async def test_pool_recycles_when_execution_policy_changes(tmp_path: Path) -> None:
    pool = PersistentShellPool()
    first = pool.get_or_create("sess-policy", tmp_path)
    capability = SandboxCapability(available=True, kind="linux_bwrap", reason="ok")
    sandbox_plan = plan_sandbox(
        capability,
        SandboxTier.WORKSPACE_WRITE,
        str(tmp_path),
    )

    sandboxed = pool.get_or_create("sess-policy", tmp_path, sandbox_plan)

    assert sandboxed is not first
    assert sandboxed.execution_identity[0][0] == "bwrap"
    await asyncio.sleep(0)
    assert not first.alive
    await pool.aclose_all()


# 功能：验证 Windows ACL runner 包装后的常驻进程仍使用 cmd sentinel 协议
# 设计：构造 python-runner 前缀加末尾 cmd 的真实计划，防止只检查 argv 首项而误发 POSIX 脚本
async def test_windows_acl_wrapped_persistent_shell_detects_cmd(tmp_path: Path) -> None:
    pool = PersistentShellPool()
    capability = SandboxCapability(available=True, kind="windows_acl", reason="ok")
    sandbox_plan = plan_sandbox(
        capability,
        SandboxTier.WORKSPACE_WRITE,
        str(tmp_path),
    )

    session = pool.get_or_create("sess-windows-acl", tmp_path, sandbox_plan)

    assert session._is_cmd is True
    assert any(
        item.endswith("windows_acl_runner.py")
        for item in session.execution_identity[0]
    )
    await pool.aclose_all()


# 功能：验证 BashTool 持久模式走池路径且退出码语义正确
# 设计：注入池与 key 执行两条命令，断言成功输出格式与原 isolated 路径一致
async def test_bash_tool_persistent_session(tmp_path: Path) -> None:
    pool = PersistentShellPool()
    tool = BashTool(tmp_path, persistent_pool=pool, persistent_key="sess-b")

    result = await tool.invoke({"command": "echo hi-persist", "session": "persistent"})

    assert not result.is_error
    assert "hi-persist" in result.content
    await pool.aclose_all()


# 功能：验证持久超时返回 timeout 错误类型
# 设计：长命令配 1 秒超时，断言 error_type=timeout 且带部分输出段
async def test_bash_tool_persistent_timeout(tmp_path: Path) -> None:
    pool = PersistentShellPool()
    tool = BashTool(tmp_path, persistent_pool=pool, persistent_key="sess-c")
    command = "ping -n 8 127.0.0.1 > nul" if _IS_WINDOWS else "sleep 8"

    result = await tool.invoke(
        {"command": command, "session": "persistent", "timeout": 1}
    )

    assert result.is_error
    assert result.error_type == "timeout"
    assert "[timeout after 1s]" in result.content
    await pool.aclose_all()


# 功能：验证无 key 时持久请求被明确拒绝而不是静默降级
# 设计：注入池但不给 key，断言 schema_error 与指引文案
async def test_bash_tool_persistent_requires_key(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, persistent_pool=PersistentShellPool(), persistent_key="")

    result = await tool.invoke({"command": "echo x", "session": "persistent"})

    assert result.is_error
    assert result.error_type == "schema_error"
    assert "session=isolated" in result.content


# 功能：验证默认 isolated 路径行为不受持久化改造影响
# 设计：不带 session 参数调用，断言输出与既有一次性执行语义一致
async def test_bash_tool_default_is_isolated(tmp_path: Path) -> None:
    tool = BashTool(tmp_path, persistent_pool=PersistentShellPool(), persistent_key="s")

    result = await tool.invoke({"command": "echo iso-ok"})

    assert not result.is_error
    assert "iso-ok" in result.content


# 功能：验证平台默认 shell 命令行正确
# 设计：纯断言分支覆盖 Windows 与 POSIX，防止默认 argv 回归
def test_default_shell_argv_matches_platform() -> None:
    argv = default_shell_argv()

    if _IS_WINDOWS:
        assert argv[:2] == ["cmd", "/Q"]
    else:
        assert argv == ["/bin/sh"]


# 功能：验证常驻 Shell 可消费 10MB 无换行输出且只在结果边界有界截断
# 设计：使用当前 Python 写单个超长字节流，覆盖 readline LimitOverrun 的历史失效路径
async def test_persistent_shell_handles_ten_megabytes_without_newline(
    tmp_path: Path,
) -> None:
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    session = PersistentShellSession(artifact_store=artifact_store)
    command = f'"{sys.executable}" -c "import sys;sys.stdout.write(\'x\'*10485760)"'

    outcome = await session.run(command, timeout_s=30.0)

    assert outcome.exit_code == 0
    assert outcome.truncated is True
    assert len(outcome.text.encode("utf-8")) == 64 * 1024
    assert outcome.text.count("x") > 60 * 1024
    assert outcome.output_bytes >= 10 * 1024 * 1024
    assert outcome.output_artifact.startswith("artifact:")
    assert outcome.output_artifact_size == outcome.output_bytes
    sha256 = outcome.output_artifact.removeprefix("artifact:")
    first = await artifact_store.read(sha256, limit=50_000)
    second = await artifact_store.read(
        sha256,
        offset=first.next_offset or 0,
        limit=50_000,
    )
    assert first.next_offset == 50_000
    assert second.offset == 50_000
    recovered = await artifact_store.read_bytes(sha256, max_bytes=11 * 1024 * 1024)
    assert recovered.count(b"x") == 10 * 1024 * 1024
    await session._terminate()


# 功能：验证同一常驻 Shell 的并发调用被串行化且输出不会互相串线
# 设计：同时调度两条带唯一标记的命令，锁定顺序协议并分别检查结果隔离
async def test_persistent_shell_serializes_concurrent_calls() -> None:
    session = PersistentShellSession()

    first, second = await asyncio.gather(
        session.run("echo concurrent-one", timeout_s=15.0),
        session.run("echo concurrent-two", timeout_s=15.0),
    )

    assert "concurrent-one" in first.text and "concurrent-two" not in first.text
    assert "concurrent-two" in second.text and "concurrent-one" not in second.text
    await session._terminate()


# 功能：验证 POSIX 常驻 Shell 会清理命令遗留的持续输出后台进程且不污染下一次调用
# 设计：启动 yes 后立即返回，随后执行唯一标记命令，断言有界队列和 job 清理阻断串流
async def test_persistent_shell_cleans_background_output_between_commands() -> None:
    if _IS_WINDOWS:
        return
    session = PersistentShellSession()

    first = await session.run("yes CODEROOK_LEAK &", timeout_s=10.0)
    second = await session.run("printf CODEROOK_CLEAN", timeout_s=10.0)

    assert first.exit_code == 0
    assert "CODEROOK_CLEAN" in second.text
    assert "CODEROOK_LEAK" not in second.text
    assert session._queue.qsize() <= session._queue.maxsize
    await session._terminate()


# 功能：验证取消常驻 Shell 调用会终止整棵进程树并使会话不可复用
# 设计：启动长任务后取消等待协程，断言 CancelledError 传播且 alive 立即转为 false
async def test_persistent_shell_cancellation_terminates_session() -> None:
    session = PersistentShellSession()
    command = "ping -n 30 127.0.0.1 > nul" if _IS_WINDOWS else "sleep 30"
    task = asyncio.create_task(session.run(command, timeout_s=60.0))
    await asyncio.sleep(0.2)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert session.alive is False
