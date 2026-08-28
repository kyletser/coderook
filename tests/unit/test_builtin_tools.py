from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from code_rook.core.processes import ProcessSupervisor, _decode_shell_output
from code_rook.core.tools.builtin import bash as bash_module
from code_rook.core.tools.builtin.bash import BashTool
from code_rook.core.tools.builtin.list_dir import ListDirTool
from code_rook.core.tools.builtin.write_file import WriteFileTool
from code_rook.core.tools.execution_metadata import tool_invocation

# ── bash ──────────────────────────────────────────────────────────────────────


# 判断 POSIX PID 是否仍可运行，并在 Linux 上把已终止僵尸视为完成回收
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


# 功能：验证成功命令的 stdout 出现在 ToolResult.content 中，is_error 为 False
# 设计：用 echo 命令避免外部依赖，直接比较输出内容，无需 mock
@pytest.mark.asyncio
async def test_bash_success_stdout() -> None:
    result = await BashTool().invoke({"command": "echo hello"})
    assert not result.is_error
    assert "hello" in result.content


# 功能：验证一次性 Bash 在完成前报告有界实时输出尾部
# 设计：通过 tool_invocation 注入内存 progress 回调执行真实 echo，避免依赖 daemon 或模型
@pytest.mark.asyncio
async def test_bash_reports_live_output_tail() -> None:
    progress: list[tuple[str, int]] = []

    # 收集 Bash 分块读取路径报告的尾部与累计字节数
    async def collect(output_tail: str, total_bytes: int) -> None:
        progress.append((output_tail, total_bytes))

    with tool_invocation("tool-live", progress=collect):
        result = await BashTool().invoke({"command": "echo live-output"})

    assert result.is_error is False
    assert progress
    assert "live-output" in progress[-1][0]
    assert progress[-1][1] >= len("live-output")


@pytest.mark.asyncio
# 功能：验证受管 Bash 把 CPU、内存、进程数与 wall-time 写入 ToolResult
# 设计：使用真实 ProcessSupervisor 执行短命令，检查结构化证据而不依赖平台具体数值
async def test_bash_reports_process_usage() -> None:
    supervisor = ProcessSupervisor()

    result = await BashTool(process_supervisor=supervisor).invoke({"command": "echo usage"})

    assert result.process_usage is not None
    assert int(result.process_usage["process_count"]) >= 1
    assert int(result.process_usage["wall_time_ms"]) >= 0
    assert supervisor.snapshot() == ()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
# 功能：验证 isolated shell 不会把 leader 退出后仍运行的后台任务遗留给 daemon
# 设计：执行典型 sh 后台 sleep 并回显 PID，要求工具成功返回且返回前该 PID 已被进程组回收
async def test_bash_isolated_reaps_background_descendant() -> None:
    result = await BashTool().invoke(
        {"command": "sh -c 'sleep 30 & echo $!'", "timeout": 5}
    )

    assert not result.is_error
    child_pid = int(result.content.strip().splitlines()[0])
    child_exists = True
    for _ in range(100):
        if not _posix_pid_running(child_pid):
            child_exists = False
            break
        await asyncio.sleep(0.02)

    assert child_exists is False


# 功能：验证非零退出码时 is_error=True 且 content 包含退出码标注
# 设计：`exit 2` 是最简单的非零退出；不依赖任何外部命令行为
@pytest.mark.asyncio
async def test_bash_nonzero_exit_is_error() -> None:
    result = await BashTool().invoke({"command": "exit 2"})
    assert result.is_error
    assert "[exit 2]" in result.content


# 功能：验证命令超时后 is_error=True，error_type 为 "timeout"
# 设计：timeout=1s 搭配 sleep 2 必然超时；验证 error_type 而非 content，避免超时消息格式耦合
@pytest.mark.asyncio
async def test_bash_timeout() -> None:
    command = f'"{sys.executable}" -c "import time; time.sleep(5)"'
    result = await BashTool().invoke({"command": command, "timeout": 1})
    assert result.is_error
    assert result.error_type == "timeout"


# 功能：验证一次性 Bash 的同一超时预算同时覆盖输出读取和进程退出等待
# 设计：模拟 stdout 已 EOF 但进程仍迟迟不退出，断言 deadline 到达后终止进程树而非等待成功
@pytest.mark.asyncio
async def test_bash_timeout_covers_wait_after_stdout_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminated: list[object] = []

    process = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"")),
        wait=AsyncMock(return_value=0),
        returncode=None,
    )

    # 返回可精确控制 EOF 与退出时序的伪进程
    async def _spawn(*_args: object, **_kwargs: object) -> object:
        asyncio.get_running_loop().call_later(
            1.2,
            setattr,
            process,
            "returncode",
            0,
        )
        return process

    # 记录超时路径是否确实请求终止完整进程树
    async def _terminate(target: object) -> None:
        terminated.append(target)

    monkeypatch.setattr(bash_module, "spawn_sandboxed_shell", _spawn)
    monkeypatch.setattr(bash_module, "terminate_process_tree", _terminate)

    result = await BashTool().invoke({"command": "ignored", "timeout": 1})

    assert result.is_error is True
    assert result.error_type == "timeout"
    assert terminated == [process]


# 功能：验证 stderr 被合并到 stdout 输出中
# 设计：只写 stderr 的命令（>&2 echo），输出应该出现在合并后的 content 里
@pytest.mark.asyncio
async def test_bash_stderr_merged() -> None:
    result = await BashTool().invoke({"command": "echo err >&2"})
    assert not result.is_error
    assert "err" in result.content


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OEM code page only")
# 功能：验证 Windows 原生命令的 OEM 编码输出可直接作为工具结果返回
# 设计：从当前 OEM 页选择可解码的高位单字节，避免假定运行器代码页一定能编码中文
def test_bash_decodes_windows_oem_output() -> None:
    encoded: bytes | None = None
    expected = ""
    for value in range(0x80, 0x100):
        candidate = bytes([value])
        try:
            expected = candidate.decode("oem")
        except UnicodeDecodeError:
            continue
        encoded = candidate
        break
    if encoded is None:
        pytest.skip("current OEM code page has no standalone high-byte character")
    assert _decode_shell_output(encoded) == expected


# ── write_file ────────────────────────────────────────────────────────────────


# 功能：验证 write_file 写入文件后内容可以被读取，返回字节数
# 设计：写入临时目录，断言文件存在且内容一致；用 tmp_path fixture 自动清理
@pytest.mark.asyncio
async def test_write_file_creates_and_returns_size(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    result = await WriteFileTool(workspace_root=tmp_path).invoke(
        {"path": str(target), "content": "hello world"}
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["bytes_written"] == 11
    assert payload["additions"] == 1
    assert payload["deletions"] == 0
    assert target.read_text() == "hello world"


# 功能：验证 write_file 自动创建不存在的父目录
# 设计：路径包含两层不存在的子目录，确认写入后目录结构被创建
@pytest.mark.asyncio
async def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "file.txt"
    result = await WriteFileTool(workspace_root=tmp_path).invoke(
        {"path": str(target), "content": "x"}
    )
    assert not result.is_error
    assert target.exists()


# 功能：验证 write_file 拒绝包含 .. 的路径并抛出 PermissionError
# 设计：.. 路径遍历与 read_file 遵循相同规则，用相同的断言模式保持一致性
@pytest.mark.asyncio
async def test_write_file_rejects_traversal() -> None:
    with pytest.raises(PermissionError):
        await WriteFileTool().invoke({"path": "../secret.txt", "content": "x"})


# ── list_dir ──────────────────────────────────────────────────────────────────


# 功能：验证 list_dir 输出包含目录中的文件名
# 设计：在 tmp_path 创建已知结构，断言文件名出现在 content 中；不约束格式细节
@pytest.mark.asyncio
async def test_list_dir_shows_files(tmp_path: Path) -> None:
    (tmp_path / "foo.py").write_text("x")
    (tmp_path / "bar.md").write_text("y")
    result = await ListDirTool(workspace_root=tmp_path).invoke({"path": str(tmp_path)})
    assert not result.is_error
    assert "foo.py" in result.content
    assert "bar.md" in result.content


# 功能：验证 list_dir 按 max_depth 限制递归深度（depth=1 时不展示孙级目录内容）
# 设计：创建 parent/child/grandchild 三层，depth=1 时 grandchild 不应出现在输出中
@pytest.mark.asyncio
async def test_list_dir_respects_max_depth(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    grandchild = child / "grandchild"
    grandchild.mkdir()
    (grandchild / "deep.txt").write_text("x")

    result = await ListDirTool(workspace_root=tmp_path).invoke(
        {"path": str(tmp_path), "max_depth": 1}
    )
    assert not result.is_error
    assert "child" in result.content
    assert "deep.txt" not in result.content


# 功能：验证对不存在的路径 list_dir 抛出 FileNotFoundError
# 设计：直接传入不存在的路径字符串，预期抛出标准异常（invocation.py 捕获后返回 error ToolResult）
@pytest.mark.asyncio
async def test_list_dir_missing_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ListDirTool(workspace_root=tmp_path).invoke({"path": "missing"})


# 功能：验证 list_dir 拒绝包含 .. 的路径
# 设计：与 read_file 和 write_file 保持一致的安全规则
@pytest.mark.asyncio
async def test_list_dir_rejects_traversal() -> None:
    with pytest.raises(PermissionError):
        await ListDirTool().invoke({"path": "../"})


# 功能：验证 list_dir 展示但不跟随指向工作区外目录的符号链接
# 设计：外部目录放置唯一秘密文件，经目录链接挂入工作区后断言名称可见而秘密内容不可枚举
@pytest.mark.asyncio
async def test_list_dir_does_not_follow_external_directory_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "external-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this platform")

    result = await ListDirTool(workspace_root=workspace).invoke({"path": ".", "max_depth": 4})

    assert "external-link@ [outside workspace]" in result.content
    assert "outside-secret.txt" not in result.content
