from __future__ import annotations

from pathlib import Path

from code_rook.core.input_context import (
    augment_file_references,
    extract_file_reference_tokens,
    list_workspace_file_references,
    resolve_file_references,
)


# 功能：从自然任务文本中提取有限数量的 @文件 标记并清理句末标点
# 设计：同时使用中英文标点和普通正文，验证解析不把非引用词误收为路径
def test_extract_file_reference_tokens() -> None:
    assert extract_file_reference_tokens(
        '比较 @src/app.py，、@README.md. 和 @"design notes.md" 然后总结'
    ) == [
        "src/app.py",
        "README.md",
        "design notes.md",
    ]


# 功能：文件引用只解析工作区内的精确路径或唯一模糊匹配
# 设计：创建精确文件、唯一模糊文件和越界文件，覆盖正常补全与目录逃逸拒绝
def test_resolve_file_references_stays_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("readme", encoding="utf-8")
    source = workspace / "src"
    source.mkdir()
    (source / "service.py").write_text("service", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    assert resolve_file_references(
        workspace,
        ["README.md", "service", "../secret.txt"],
    ) == ["README.md", "src/service.py"]


# 功能：模型输入追加用户显式引用的文件内容并保留含空格路径
# 设计：以显式参数传入含空格路径，同时断言模型获得正文而界面仍可使用原始输入
def test_augment_file_references_preserves_explicit_path_with_spaces(
    tmp_path: Path,
) -> None:
    target = tmp_path / "design notes.md"
    target.write_text("PRIVATE FULL CONTENT", encoding="utf-8")

    result = augment_file_references(
        "总结 @design notes.md",
        "总结 @design notes.md",
        tmp_path,
        explicit_references=["design notes.md"],
    )

    assert '\"design notes.md\"' in result
    assert "PRIVATE FULL CONTENT" in result
    assert "truncated=\"false\"" in result
    assert "reference data" in result


# 功能：超大文件引用只进入有界摘要并明确告知模型已截断
# 设计：构造超过单文件限额的首尾标记，断言头部可见、尾部不可见且 truncated 为 true
def test_augment_file_references_truncates_large_files(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("BEGIN\n" + ("x" * 30_000) + "\nEND", encoding="utf-8")

    result = augment_file_references(
        "inspect @large.txt",
        "inspect @large.txt",
        tmp_path,
    )

    assert "BEGIN" in result
    assert "END" not in result
    assert "truncated=\"true\"" in result


# 功能：TUI 文件候选跳过依赖、缓存和 CodeRook 内部目录并保持稳定相对路径
# 设计：在可见目录和三类忽略目录放置同名文件，直接核对候选清单只含用户源码
def test_list_workspace_file_references_skips_generated_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("app", encoding="utf-8")
    for directory in (".git", ".coderook", "node_modules"):
        path = tmp_path / directory
        path.mkdir()
        (path / "hidden.py").write_text("hidden", encoding="utf-8")

    assert list_workspace_file_references(tmp_path) == ["src/app.py"]
