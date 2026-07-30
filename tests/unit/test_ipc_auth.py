from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from code_rook.core.config import CodeRookConfig, _apply_toml, get_config
from code_rook.core.transport.auth import (
    IpcTokenError,
    is_loopback_host,
    load_or_create_ipc_token,
    read_ipc_token,
    require_loopback_host,
)


def test_loopback_host_validation_is_strict() -> None:
    for host in ("127.0.0.1", "127.12.34.56", "::1", "[::1]", "localhost"):
        assert is_loopback_host(host)
        require_loopback_host(host)

    for host in ("0.0.0.0", "::", "192.168.1.10", "example.local", "localhost."):
        assert not is_loopback_host(host)
        with pytest.raises(SystemExit, match="non-loopback"):
            require_loopback_host(host)


def test_token_file_is_stable_private_and_not_a_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEROOK_IPC_TOKEN", raising=False)
    path = tmp_path / "private" / "ipc-token"

    first = load_or_create_ipc_token(path)
    second = load_or_create_ipc_token(path)

    assert first == second == read_ipc_token(path)
    assert len(first) >= 32
    assert path.read_text(encoding="utf-8") == first + "\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_environment_token_is_used_without_writing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "e" * 43
    path = tmp_path / "ipc-token"
    monkeypatch.setenv("CODEROOK_IPC_TOKEN", token)

    assert load_or_create_ipc_token(path) == token
    assert read_ipc_token(path) == token
    assert not path.exists()


def test_token_reader_rejects_malformed_and_symlink_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEROOK_IPC_TOKEN", raising=False)
    malformed = tmp_path / "malformed"
    malformed.write_text("short\n", encoding="utf-8")
    with pytest.raises(IpcTokenError, match="32-512"):
        read_ipc_token(malformed)

    target = tmp_path / "target"
    target.write_text("t" * 43 + "\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    if not os.path.islink(link):
        pytest.skip("symlink was not created on this platform/sandbox")
    with pytest.raises(IpcTokenError, match="symlink"):
        read_ipc_token(link)


def test_ipc_token_file_config_supports_toml_and_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CodeRookConfig()
    _apply_toml(config, {"core": {"ipc_token_file": "from-toml"}})
    assert config.ipc_token_file == "from-toml"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEROOK_IPC_TOKEN_FILE", "from-env")
    loaded = get_config()
    assert loaded.ipc_token_file == "from-env"
