from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from code_rook.core.editing import content_hash
from code_rook.core.tools.builtin.read_file import ReadFileTool


def _read_result(content: str) -> tuple[dict, str]:
    metadata_line, marker, text = content.split("\n", maxsplit=2)
    assert metadata_line.startswith("[metadata] ")
    assert marker == "[content]"
    return json.loads(metadata_line.removeprefix("[metadata] ")), text


# 功能：验证读取存在的文件时返回完整内容且 is_error 为 False
# 设计：写临时文件后读取，断言 content 和 is_error，覆盖正常路径（happy path）
async def test_read_existing_file(tmp_path: Path) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("hello world", encoding="utf-8")
    result = await ReadFileTool(workspace_root=tmp_path).invoke({"path": str(f)})
    metadata, text = _read_result(result.content)
    assert not result.is_error
    assert text == "hello world"
    assert metadata["path"] == "hello.txt"
    assert metadata["content_hash"].startswith("sha256:")
    assert metadata["bytes"] == 11
    assert metadata["truncated"] is False


# 功能：验证文件不存在时抛出 FileNotFoundError 而非返回错误 ToolResult
# 设计：传入不存在的路径，确认 ReadFileTool 不吞掉异常，让调用方（invoke_tool）负责错误分类和事件发布
async def test_file_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await ReadFileTool(workspace_root=tmp_path).invoke(
            {"path": str(tmp_path / "missing.txt")}
        )


# 功能：验证包含 `..` 的路径被拒绝并抛出 PermissionError
# 设计：传入 `"../secret.txt"` 这种最典型的目录遍历形式，确认安全边界第一道防线有效
async def test_path_traversal_dotdot_raises() -> None:
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "../secret.txt"})


# 功能：验证多级路径中嵌入的 `..` 经过路径规范化后也被正确检测
# 设计：使用 `"subdir/../../etc/passwd"` 测试路径 resolve 后的深度遍历，确认单层 `..` 过滤不足以覆盖此情况
async def test_path_traversal_nested_raises() -> None:
    with pytest.raises(PermissionError):
        await ReadFileTool().invoke({"path": "subdir/../../etc/passwd"})


# 功能：验证超过 512KB 的文件被截断并在末尾追加 [truncated] 标记
# 设计：写 600KB 文件，断言内容以 x×512KB 开头、以 [truncated] 结尾，确认截断不破坏前缀内容
async def test_truncation_over_512kb(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_bytes(b"x" * (600 * 1024))
    result = await ReadFileTool(workspace_root=tmp_path).invoke({"path": str(f)})
    metadata, text = _read_result(result.content)
    assert not result.is_error
    assert text.endswith("[truncated]")
    assert metadata["truncated"] is True
    # Actual text content is exactly 512KB worth of 'x' chars
    assert text.startswith("x" * (512 * 1024))


# 功能：验证恰好等于 512KB 的文件不被截断（边界值：超过而非大于等于）
# 设计：boundary check，确认截断阈值为"严格超过 512KB"，防止 off-by-one 错误
async def test_exact_512kb_is_not_truncated(tmp_path: Path) -> None:
    f = tmp_path / "exact.txt"
    f.write_bytes(b"y" * (512 * 1024))
    result = await ReadFileTool(workspace_root=tmp_path).invoke({"path": str(f)})
    metadata, text = _read_result(result.content)
    assert not result.is_error
    assert not text.endswith("[truncated]")
    assert metadata["truncated"] is False
    assert len(text) == 512 * 1024


# 功能：验证空文件返回空字符串而非 None 或错误
# 设计：零字节文件确认 content="" 的正常返回，避免调用方（LLM prompt 组装）对空内容做额外 None 判断
async def test_empty_file_returns_empty_content(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="utf-8")
    result = await ReadFileTool(workspace_root=tmp_path).invoke({"path": str(f)})
    metadata, text = _read_result(result.content)
    assert not result.is_error
    assert metadata["bytes"] == 0
    assert text == ""


# 功能：验证按一基闭区间读取保留 UTF-8、CRLF、下一行及全文件编辑哈希
# 设计：读取文件中间两行并与原始字节哈希比较，防止范围哈希被误用于乐观编辑校验
async def test_read_line_range_preserves_full_file_hash(tmp_path: Path) -> None:
    raw = "一\r\n二\r\n三\r\n四".encode()
    (tmp_path / "source.py").write_bytes(raw)
    result = await ReadFileTool(workspace_root=tmp_path).invoke({
        "path": "source.py", "start_line": 2, "end_line": 3,
    })
    metadata, text = _read_result(result.content)
    assert text == "二\r\n三\r\n"
    assert metadata["content_hash"] == content_hash(raw)
    assert metadata["total_lines"] == 4 and metadata["next_line"] == 4
    assert metadata["start_line"] == 2 and metadata["end_line"] == 3
    assert not metadata["truncated"]


# 功能：验证只给起始行默认读取 200 行且能继续到文件末尾
# 设计：用 300 行冻结文件检查默认分页与超出末尾的闭区间截取，避免整文件反复进入上下文
async def test_read_start_line_defaults_to_bounded_page(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("line\n" * 300, encoding="utf-8")
    tool = ReadFileTool(workspace_root=tmp_path)
    first = await tool.invoke({"path": "large.py", "start_line": 1})
    metadata, text = _read_result(first.content)
    assert len(text.splitlines()) == 200 and metadata["next_line"] == 201
    last = await tool.invoke({"path": "large.py", "start_line": 201, "end_line": 900})
    metadata, text = _read_result(last.content)
    assert len(text.splitlines()) == 100 and metadata["next_line"] is None


# 功能：验证非法行范围不会伪装成成功的空内容
# 设计：覆盖零起点、反向区间与越过末尾三种输入，分别检查参数验证和读取结果
async def test_invalid_read_line_ranges_are_explicit(tmp_path: Path) -> None:
    (tmp_path / "short.py").write_text("one", encoding="utf-8")
    tool = ReadFileTool(workspace_root=tmp_path)
    for params in ({"start_line": 0}, {"start_line": 3, "end_line": 2}):
        with pytest.raises(ValidationError):
            await tool.invoke({"path": "short.py", **params})
    result = await tool.invoke({"path": "short.py", "start_line": 2})
    assert result.is_error and "beyond" in result.content
