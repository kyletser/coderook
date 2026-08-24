from __future__ import annotations

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from code_rook.core.api.auth import ApiTokenError, load_or_create_api_token


@pytest.mark.parametrize("environment_value", ["", "   ", "\t"])
# 功能：验证空或纯空白环境 token 不能关闭鉴权而会回落到安全用户文件
# 设计：参数化空白形态并连续加载同一路径，断言排他创建结果可稳定复用且不是空凭据
def test_blank_environment_token_falls_back_to_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_value: str,
) -> None:
    token_path = tmp_path / "state" / "api-token"
    monkeypatch.setenv("CODEROOK_API_TOKEN", environment_value)

    created = load_or_create_api_token(token_path)
    loaded = load_or_create_api_token(token_path)

    assert created == loaded
    assert len(created) >= 32
    assert token_path.read_text(encoding="utf-8") == created + "\n"
    if os.name != "nt":
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert token_path.stat().st_uid == os.getuid()


# 功能：验证并发 Core 只能排他创建一个完整 token 且所有调用方得到同一值
# 设计：用线程屏障同时触发两个真实文件加载器，覆盖 O_EXCL 与创建窗口重试而不依赖固定时序
def test_concurrent_token_creation_returns_one_complete_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "state" / "api-token"
    barrier = Barrier(2)
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    # 在同一屏障后调用生产加载器以放大排他创建竞争窗口
    def load() -> str:
        barrier.wait()
        return load_or_create_api_token(token_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(lambda _index: load(), range(2)))

    assert values[0] == values[1]
    assert token_path.read_text(encoding="utf-8") == values[0] + "\n"


# 功能：验证有效显式环境 token 优先于文件且不会产生任何磁盘凭据
# 设计：预置符合 Bearer 字符集的非空值并指向不存在路径，断言原值返回且父目录仍不存在
def test_explicit_environment_token_does_not_touch_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "missing" / "api-token"
    monkeypatch.setenv("CODEROOK_API_TOKEN", "explicit-api-token")

    token = load_or_create_api_token(token_path)

    assert token == "explicit-api-token"
    assert not token_path.parent.exists()


# 功能：验证非空环境 token 夹带空白时失败关闭而不是静默裁剪
# 设计：用首尾空白包裹的 token 调用文件入口，断言不会创建 fallback 文件或接受歧义凭据
def test_nonblank_environment_token_rejects_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "state" / "api-token"
    monkeypatch.setenv("CODEROOK_API_TOKEN", " explicit-api-token ")

    with pytest.raises(ApiTokenError, match="whitespace"):
        load_or_create_api_token(token_path)

    assert not token_path.exists()


@pytest.mark.parametrize("content", ["short\n", "x" * 32 + " extra\n", "令牌" * 20])
# 功能：验证既有 token 文件的长度、空白和字符格式不合规时全部失败关闭
# 设计：以 0600 普通文件隔离内容校验变量，覆盖短值、内嵌空白和非 Bearer 字符三类损坏
def test_existing_token_file_requires_strict_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    token_path = tmp_path / "api-token"
    token_path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        token_path.chmod(0o600)
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    with pytest.raises(ApiTokenError):
        load_or_create_api_token(token_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are not Windows ACLs")
# 功能：验证 POSIX 既有 token 文件必须由当前用户拥有并严格使用 0600
# 设计：创建内容合法但权限为 0644 的文件，只改变 mode 变量并断言加载器拒绝宽权限
def test_existing_token_file_rejects_broad_posix_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "api-token"
    token_path.write_text("x" * 32 + "\n", encoding="utf-8")
    token_path.chmod(0o644)
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    with pytest.raises(ApiTokenError, match="0600"):
        load_or_create_api_token(token_path)


# 功能：验证 token 路径为目录或重解析对象时不会被普通文本读取绕过
# 设计：用真实目录占据最终路径，覆盖所有平台都可执行的非普通文件失败边界
def test_existing_token_path_must_be_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_path = tmp_path / "api-token"
    token_path.mkdir()
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    with pytest.raises(ApiTokenError, match="regular file"):
        load_or_create_api_token(token_path)


# 功能：验证 token 文件符号链接在 no-follow 读取前即失败关闭
# 设计：若宿主允许创建链接则让链接指向合法 0600 文件，证明目标内容合法也不能绕过路径身份检查
def test_existing_token_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "real-token"
    target.write_text("x" * 32 + "\n", encoding="utf-8")
    if os.name != "nt":
        target.chmod(0o600)
    token_path = tmp_path / "api-token"
    try:
        token_path.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    with pytest.raises(ApiTokenError, match="regular file"):
        load_or_create_api_token(token_path)


# 功能：验证 token 直接父目录为符号链接或重解析点时拒绝访问
# 设计：把链接父目录指向当前用户控制的真实目录，证明边界关注路径身份而非目标可读性
def test_token_parent_symlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = tmp_path / "real-state"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-state"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlink creation")
    monkeypatch.delenv("CODEROOK_API_TOKEN", raising=False)

    with pytest.raises(ApiTokenError, match="parent must be a real directory"):
        load_or_create_api_token(linked_parent / "api-token")
