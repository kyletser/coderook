from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from code_rook.cli.commands import web
from code_rook.core.config import CodeRookConfig


# 功能：验证从受保护目录打开 Web 时复用当前 Core 工作区而不切换到欢迎区重启
# 设计：用最小项目注册表替身触发欢迎区重定向，并精确断言启动器收到 reuse_existing 标志
def test_web_from_protected_directory_reuses_running_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    welcome = tmp_path / "welcome"
    source.mkdir()
    welcome.mkdir()
    monkeypatch.chdir(source)
    registry = SimpleNamespace(
        is_protected_workspace=lambda _path: True,
        prepare_welcome_workspace=lambda: welcome,
    )
    ensure = MagicMock()
    monkeypatch.setattr(web, "ProjectRegistry", lambda: registry)
    monkeypatch.setattr(web, "ensure_core_running", ensure)
    monkeypatch.setattr(web, "_request_launch_url", AsyncMock(return_value="http://local/"))

    assert web.cmd_web(CodeRookConfig(), no_open=True) == 0
    assert Path.cwd() == welcome
    ensure.assert_called_once_with(
        CodeRookConfig(),
        env_file=None,
        reuse_existing=True,
    )
