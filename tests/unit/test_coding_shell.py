import asyncio
import os
from pathlib import Path

import pytest

from code_rook.core.agent_runtime.shell import CodingShellTool, OutputAccumulator, bash_executable
from code_rook.core.processes import mark_agent_process_environment


# 功能：大输出保留完整字节，UTF-8 跨块不乱码且显示尾部有界。
# 设计：用小预算提前触发落盘，把中文字符分拆输入并验证最终文件与输入一致。
def test_output_accumulator_spills_and_decodes(tmp_path: Path) -> None:
    output = OutputAccumulator(tmp_path, max_bytes=30, max_lines=3)
    content = ("中文 output\n" * 20).encode()
    for index in range(0, len(content), 5):
        output.append(content[index : index + 5])
    output.close()
    assert output.path is not None
    assert output.path.read_bytes() == content
    assert "�" not in output.tail
    assert len(output.tail.encode()) <= 30
    assert len(output.tail.splitlines()) <= 3


# 检查当前测试机是否具备可运行的 Bash，缺失时不依赖外部安装。
def _require_bash() -> None:
    try:
        bash_executable()
    except RuntimeError:
        pytest.skip("Bash not installed")


# 功能：入口进程覆盖外层 Agent 标记，使所有后续子进程识别 CodeRook。
# 设计：临时设置冲突标记后调用统一入口函数，核对通用与产品专属变量。
def test_agent_process_markers_identify_coderook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_AGENT", "outer-agent")
    monkeypatch.delenv("CODEROOK_CODING_AGENT", raising=False)

    mark_agent_process_environment()

    assert os.environ["AI_AGENT"] == "coderook"
    assert os.environ["CODEROOK_CODING_AGENT"] == "true"


# 功能：新 Shell 使用 Bash 语法且能返回成功输出与非零退出状态。
# 设计：运行真实本地 Shell 的 printf 和 exit，不调用模型或网络。
async def test_coding_shell_uses_bash(tmp_path: Path) -> None:
    _require_bash()
    tool = CodingShellTool(tmp_path, None, None)
    result = await tool.invoke({"command": "value=hello; printf '%s' \"$value\""})
    assert result.content == "hello"
    assert not result.is_error
    failed = await tool.invoke({"command": "printf failure; exit 7"})
    assert failed.is_error
    assert "[exit 7]" in failed.content
    assert failed.process_usage == {"exit_code": 7}
    assert failed.failure_category == "command_failed"


# 功能：Agent Bash 可读取本轮冻结的会话与模型身份，方便模型回答运行时问题。
# 设计：直接注入非敏感元数据并执行真实 Bash，同时确认未显式允许的密钥变量不可见。
async def test_coding_shell_exposes_session_metadata(tmp_path: Path) -> None:
    _require_bash()
    tool = CodingShellTool(
        tmp_path,
        None,
        None,
        session_environment={
            "CODEROOK_SESSION_ID": "sess-runtime",
            "CODEROOK_PROVIDER": "aliyun",
            "CODEROOK_MODEL": "qwen3.8-flash",
            "CODEROOK_REASONING_LEVEL": "high",
            "CODEROOK_API_KEY": "must-not-leak",
        },
    )
    result = await tool.invoke(
        {
            "command": (
                "printf '%s|%s|%s|%s|%s' "
                '"$CODEROOK_SESSION_ID" "$CODEROOK_PROVIDER" "$CODEROOK_MODEL" '
                '"$CODEROOK_REASONING_LEVEL" "$CODEROOK_API_KEY"'
            )
        }
    )
    assert result.content == "sess-runtime|aliyun|qwen3.8-flash|high|"
    assert not result.is_error


# 功能：显式超时保留已产生的输出，不受旧 120 秒 schema 上限限制。
# 设计：超时前打印一行并短暂睡眠，检查输出和状态；单独验证长超时参数。
async def test_shell_timeout_preserves_output(tmp_path: Path) -> None:
    from code_rook.core.agent_runtime.shell import ShellParams

    _require_bash()
    assert ShellParams(command="build", timeout=900).timeout == 900
    assert ShellParams(command="build").timeout is None
    result = await CodingShellTool(tmp_path, None, None).invoke(
        {"command": "printf started; sleep 10", "timeout": 0.5}
    )
    assert result.error_type == "timeout"
    assert result.failure_category == "timed_out"
    assert "started" in result.content


# 功能：用户取消可以结束没有默认超时的命令。
# 设计：在真实 Shell 睡眠期间取消并限制测试等待时间，确认没有吞掉取消信号。
async def test_shell_can_be_cancelled(tmp_path: Path) -> None:
    _require_bash()
    task = asyncio.create_task(
        CodingShellTool(tmp_path, None, None).invoke({"command": "sleep 30"})
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=8)


# 功能：实际 Shell 超量输出的完整文件能通过默认 read 工具继续读取。
# 设计：使用 2100 行触发尾部截断，再从落盘文件读取开头，覆盖两个默认工具的衔接。
async def test_shell_full_output_is_readable(tmp_path: Path) -> None:
    from code_rook.core.agent_runtime.tools import ReadTool
    from code_rook.core.workspace import WorkspaceBoundary

    _require_bash()
    result = await CodingShellTool(tmp_path, None, None).invoke(
        {"command": "for ((i=1;i<=2100;i++)); do printf 'line %s\\n' \"$i\"; done"}
    )
    assert not result.is_error
    assert "Output truncated" in result.content
    logs = list((tmp_path / ".coderook" / "tool-output").glob("shell-*.log"))
    assert len(logs) == 1
    page = await ReadTool(WorkspaceBoundary(tmp_path)).invoke({"path": str(logs[0]), "limit": 2})
    assert page.content.startswith("line 1\nline 2\n")
