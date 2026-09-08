from pathlib import Path

from code_rook.core.agent_runtime.prompt_templates import (
    expand_prompt_template,
    list_prompt_templates,
    parse_arguments,
    substitute_arguments,
)


# 功能：验证位置、默认值、切片及全量参数按 Pi 单次替换语义展开。
# 设计：把占位符放入实参证明不会递归执行，并覆盖 Windows 路径和引号分组。
def test_template_arguments_are_non_recursive() -> None:
    args = parse_arguments('"C:\\My Repo" \'$1\' final')
    assert args == ["C:\\My Repo", "$1", "final"]
    assert substitute_arguments("$1 | ${@:2:1} | ${4:-missing} | $ARGUMENTS", args) == (
        "C:\\My Repo | $1 | missing | C:\\My Repo $1 final"
    )
    assert substitute_arguments("${@:-none} ${1:-$2}", []) == "none $2"
    assert substitute_arguments("${@:0:0}", args) == ""


# 功能：验证 Markdown 模板去除元数据、按目录优先级加载且未知命令保持原样。
# 设计：真实临时目录中创建两个同名模板，证明用户模板优先及参数正确展开。
def test_template_files_expand_in_directory_order(tmp_path: Path) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    user.mkdir()
    project.mkdir()
    (user / "explain.md").write_text(
        "---\ndescription: explain\n---\nExplain $1 in ${2:-Chinese}.", encoding="utf-8"
    )
    (project / "explain.md").write_text("Project template", encoding="utf-8")
    assert expand_prompt_template('/explain "auth module"', [user, project]) == (
        "Explain auth module in Chinese."
    )
    assert expand_prompt_template("/unknown", [user, project]) == "/unknown"
    assert list_prompt_templates([user, project]) == [
        {"name": "explain", "description": "explain", "kind": "template"}
    ]


# 功能：模板说明和参数提示来自 YAML，命令发现不展开或丢弃正文占位符。
# 设计：用带 BOM 的 Windows 换行和折叠标量覆盖真实编辑文件，核对展示与执行一致。
def test_template_metadata_and_raw_description(tmp_path: Path) -> None:
    (tmp_path / "review.md").write_bytes(
        ('\ufeff---\r\ndescription: >-\r\n  Review changes\r\n  and tests\r\n'
         'argument-hint: "<path> [language]"\r\n---\r\nCheck $1 in ${2:-English}.').encode("utf-8")
    )
    (tmp_path / "simple.md").write_text("Inspect $1 carefully.", encoding="utf-8")
    assert list_prompt_templates([tmp_path]) == [
        {"name": "review", "description": "Review changes and tests", "kind": "template",
         "argument_hint": "<path> [language]"},
        {"name": "simple", "description": "Inspect $1 carefully.", "kind": "template"},
    ]
    assert expand_prompt_template('/review "auth module" Chinese', [tmp_path]) == (
        "Check auth module in Chinese."
    )


# 功能：空元数据正常去除，损坏模板不遮蔽下一个可用的同名模板。
# 设计：两个实际目录共享命令名称，比较发现和执行是否选取同一个合法模板。
def test_template_invalid_metadata_uses_next_candidate(tmp_path: Path) -> None:
    user, project = tmp_path / "user", tmp_path / "project"
    user.mkdir()
    project.mkdir()
    (user / "brief.md").write_text('---\ndescription: [broken\n---\nWrong', encoding="utf-8")
    (project / "brief.md").write_text('---\n---\nSummarize $1.', encoding="utf-8")
    assert list_prompt_templates([user, project])[0]["description"] == "Summarize $1."
    assert expand_prompt_template("/brief logs", [user, project]) == "Summarize logs."


# 功能：显式模板文件与目录可混合加载，同名优先级在补全和执行时一致。
# 设计：指定目录外的单文件并加入同名后备模板，验证名称取文件名且参数仍按原规则展开。
def test_explicit_template_file_matches_discovery(tmp_path: Path) -> None:
    single = tmp_path / "review.md"
    single.write_text("Review $1 with ${2:-tests}", encoding="utf-8")
    directory = tmp_path / "templates"
    directory.mkdir()
    (directory / "review.md").write_text("Wrong template", encoding="utf-8")
    (directory / "brief.md").write_text("Summarize $@", encoding="utf-8")
    sources = [single, directory]
    assert [entry["name"] for entry in list_prompt_templates(sources)] == ["review", "brief"]
    assert expand_prompt_template('/review "auth module"', sources) == "Review auth module with tests"
    assert expand_prompt_template("/brief one two", sources) == "Summarize one two"
