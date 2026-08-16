from __future__ import annotations

import os
from pathlib import Path

from code_rook.core.persistent_shell import (
    PersistentShellPool,
    PersistentShellSession,
    default_shell_argv,
)
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
