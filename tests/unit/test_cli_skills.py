from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.cli.commands.skills import (
    cmd_skills_audit,
    cmd_skills_install,
    cmd_skills_list,
    cmd_skills_remove,
    cmd_skills_show,
)


# 创建 CLI 安装用的最小目录式 skill
def _source(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: cli-skill\ndescription: CLI test\n---\nDo $ARGUMENTS\n",
        encoding="utf-8",
    )
    return source


# 功能：验证 coderook skills install 默认只 preview，--yes 后才实际写入
# 设计：连续调用 CLI handler 并检查 stdout 与项目目标目录，覆盖确认边界而不启动 daemon
def test_cli_skills_install_preview_then_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_skills_install(
        str(source),
        scope="project",
        trust=True,
        confirmed=False,
        overwrite=False,
    )

    assert "Preview only" in capsys.readouterr().out
    target = tmp_path / ".coderook" / "skills" / "cli-skill"
    assert not target.exists()

    cmd_skills_install(
        str(source),
        scope="project",
        trust=True,
        confirmed=True,
        overwrite=False,
    )

    assert target.is_dir()
    assert "installed cli-skill" in capsys.readouterr().out


# 功能：验证 skills list/show/audit/remove 命令共享同一 provenance 状态
# 设计：安装后依次调用四个 handler，检查 manifest 可见、audit verified 且确认删除生效
def test_cli_skills_lifecycle_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path)
    monkeypatch.chdir(tmp_path)
    cmd_skills_install(
        str(source),
        scope="project",
        trust=False,
        confirmed=True,
        overwrite=False,
    )
    capsys.readouterr()

    cmd_skills_list()
    assert "cli-skill\tproject\tuntrusted\tverified" in capsys.readouterr().out
    cmd_skills_show("cli-skill")
    shown = capsys.readouterr().out
    assert '"source"' in shown
    assert "system_prompt_template" not in shown
    cmd_skills_audit()
    assert "cli-skill\tproject\tuntrusted\tverified" in capsys.readouterr().out
    cmd_skills_remove("cli-skill", scope="project", confirmed=True)
    assert not (tmp_path / ".coderook" / "skills" / "cli-skill").exists()


# 功能：验证 CLI audit 对被修改的受管 skill 返回非零退出码
# 设计：安装后篡改 SKILL.md，捕获 SystemExit 并检查 stderr 明确报告 digest mismatch
def test_cli_skills_audit_fails_on_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path)
    monkeypatch.chdir(tmp_path)
    cmd_skills_install(
        str(source),
        scope="project",
        trust=True,
        confirmed=True,
        overwrite=False,
    )
    capsys.readouterr()
    installed = tmp_path / ".coderook" / "skills" / "cli-skill" / "SKILL.md"
    installed.write_text(installed.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")

    with pytest.raises(SystemExit) as captured:
        cmd_skills_audit()

    assert captured.value.code == 1
    assert "digest mismatch" in capsys.readouterr().err
