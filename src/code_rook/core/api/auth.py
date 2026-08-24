from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
import time
from pathlib import Path

_API_TOKEN_ENV = "CODEROOK_API_TOKEN"
_MIN_FILE_TOKEN_LENGTH = 32
_MAX_TOKEN_LENGTH = 512
_MAX_TOKEN_FILE_BYTES = 4096
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]+$")
_REPARSE_POINT = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


class ApiTokenError(ValueError):
    pass


# 判断监听主机是否明确限制在本机回环地址
def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


# 判断 token 是否为不含空白的非空配置值
def _token_is_configured(token: str) -> bool:
    return (
        bool(token)
        and token == token.strip()
        and not any(character.isspace() for character in token)
    )


# 验证非回环监听必须配置有效 bearer token
def validate_api_binding(host: str, token: str) -> None:
    if not is_loopback_host(host) and not _token_is_configured(token):
        raise ValueError("non-loopback API binding requires CODEROOK_API_TOKEN")


# 使用常量时间比较验证 Authorization bearer header，空 expected 始终失败关闭
def bearer_authorized(header: str | None, token: str) -> bool:
    if not _token_is_configured(token):
        return False
    prefix = "Bearer "
    if header is None or not header.startswith(prefix):
        return False
    presented = header[len(prefix) :]
    return bool(presented) and secrets.compare_digest(presented, token)


# 判断 Windows 文件状态是否带重解析点标记
def _is_reparse_point(snapshot: os.stat_result) -> bool:
    attributes = int(getattr(snapshot, "st_file_attributes", 0))
    return bool(_REPARSE_POINT and attributes & _REPARSE_POINT)


# 比较两个文件状态是否仍指向同一文件系统对象
def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


# 比较同一文件对象的长度和修改时间是否保持不变
def _same_snapshot(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_identity(first, second)
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


# 返回当前平台可用的用户标识，Windows 无 POSIX uid 时返回空
def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if callable(getuid) else None


# 验证 token 只包含 Bearer 安全字符并满足来源对应的长度下限
def _validate_token(token: str, *, source: str, minimum_length: int) -> str:
    if not _token_is_configured(token):
        raise ApiTokenError(f"API token from {source} contains whitespace or is empty")
    if not minimum_length <= len(token) <= _MAX_TOKEN_LENGTH:
        raise ApiTokenError(
            f"API token from {source} must be {minimum_length}-{_MAX_TOKEN_LENGTH} characters"
        )
    if _TOKEN_PATTERN.fullmatch(token) is None:
        raise ApiTokenError(f"API token from {source} contains unsupported characters")
    return token


# 把空或纯空白环境值视为未配置，非空值按显式凭据格式校验
def _environment_token() -> str | None:
    raw = os.environ.get(_API_TOKEN_ENV)
    if raw is None or not raw.strip():
        return None
    return _validate_token(raw, source=_API_TOKEN_ENV, minimum_length=1)


# 验证 token 父目录为稳定、当前用户控制且不可由其他 POSIX 用户写入的真实目录
def _prepare_parent(path: Path) -> os.stat_result:
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshot = os.lstat(parent)
    except OSError as exc:
        raise ApiTokenError(f"Cannot prepare API token parent directory: {parent}") from exc
    if (
        stat.S_ISLNK(snapshot.st_mode)
        or _is_reparse_point(snapshot)
        or not stat.S_ISDIR(snapshot.st_mode)
    ):
        raise ApiTokenError(f"API token parent must be a real directory: {parent}")
    current_uid = _current_uid()
    if current_uid is not None:
        if snapshot.st_uid != current_uid:
            raise ApiTokenError(f"API token parent is not owned by the current user: {parent}")
        if stat.S_IMODE(snapshot.st_mode) & 0o022:
            raise ApiTokenError(f"API token parent must not be group/world writable: {parent}")
    return snapshot


# 确认 token 操作期间父目录没有被替换或改成不安全对象
def _require_unchanged_parent(path: Path, before: os.stat_result) -> None:
    try:
        after = os.lstat(path.parent)
    except OSError as exc:
        raise ApiTokenError(f"API token parent changed during access: {path.parent}") from exc
    if (
        not _same_identity(before, after)
        or stat.S_ISLNK(after.st_mode)
        or _is_reparse_point(after)
        or not stat.S_ISDIR(after.st_mode)
    ):
        raise ApiTokenError(f"API token parent changed during access: {path.parent}")


# 验证现有 token 文件为当前用户拥有的普通文件并在 POSIX 上严格使用 0600
def _validate_file_snapshot(path: Path, snapshot: os.stat_result) -> None:
    if (
        stat.S_ISLNK(snapshot.st_mode)
        or _is_reparse_point(snapshot)
        or not stat.S_ISREG(snapshot.st_mode)
    ):
        raise ApiTokenError(f"API token path must be a real regular file: {path}")
    if snapshot.st_size > _MAX_TOKEN_FILE_BYTES:
        raise ApiTokenError(f"API token file is unexpectedly large: {path}")
    current_uid = _current_uid()
    if current_uid is not None:
        if snapshot.st_uid != current_uid:
            raise ApiTokenError(f"API token file is not owned by the current user: {path}")
        if stat.S_IMODE(snapshot.st_mode) != 0o600:
            raise ApiTokenError(f"API token file must have POSIX mode 0600: {path}")


# 以 no-follow 描述符读取现有 token 并复核路径、父目录和文件身份
def _read_token_file(path: Path, parent_before: os.stat_result) -> str:
    try:
        before = os.lstat(path)
    except FileNotFoundError as exc:
        raise ApiTokenError(f"API token file does not exist: {path}") from exc
    except OSError as exc:
        raise ApiTokenError(f"Cannot inspect API token file: {path}") from exc
    _validate_file_snapshot(path, before)
    flags = (
        os.O_RDONLY
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOINHERIT", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ApiTokenError(f"Cannot open API token file safely: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        _validate_file_snapshot(path, opened)
        if not _same_identity(before, opened):
            raise ApiTokenError(f"API token file changed while opening: {path}")
        data = os.read(descriptor, _MAX_TOKEN_FILE_BYTES + 1)
        finished = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ApiTokenError(f"API token file changed while reading: {path}") from exc
    _validate_file_snapshot(path, after)
    if not _same_snapshot(before, opened) or not _same_snapshot(opened, finished):
        raise ApiTokenError(f"API token file changed while reading: {path}")
    if not _same_snapshot(finished, after):
        raise ApiTokenError(f"API token file changed after reading: {path}")
    _require_unchanged_parent(path, parent_before)
    if len(data) > _MAX_TOKEN_FILE_BYTES:
        raise ApiTokenError(f"API token file is unexpectedly large: {path}")
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise ApiTokenError(f"API token file is not valid UTF-8: {path}") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    return _validate_token(text, source=str(path), minimum_length=_MIN_FILE_TOKEN_LENGTH)


# 在并发创建窗口短暂重试读取，避免把尚未 fsync 的排他文件误判为损坏
def _read_created_token(path: Path, parent_before: os.stat_result) -> str:
    failure: ApiTokenError | None = None
    for _attempt in range(20):
        try:
            return _read_token_file(path, parent_before)
        except ApiTokenError as exc:
            failure = exc
            time.sleep(0.01)
    assert failure is not None
    raise failure


# 仅在路径仍指向本次创建对象时清理失败的 token 文件
def _remove_created_token(path: Path, created: os.stat_result) -> None:
    try:
        current = os.lstat(path)
        if (
            _same_identity(created, current)
            and stat.S_ISREG(current.st_mode)
            and not _is_reparse_point(current)
        ):
            path.unlink()
    except OSError:
        return


# 把 token 完整写入排他创建的描述符并在 POSIX 上从描述符设置 0600
def _write_created_token(
    path: Path,
    descriptor: int,
    token: str,
) -> os.stat_result:
    created = os.fstat(descriptor)
    if not stat.S_ISREG(created.st_mode) or _is_reparse_point(created):
        raise ApiTokenError(f"Created API token path is not a regular file: {path}")
    current_uid = _current_uid()
    if current_uid is not None:
        fchmod = getattr(os, "fchmod", None)
        if not callable(fchmod):
            raise ApiTokenError("POSIX API token permissions cannot be enforced")
        fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    payload = (token + "\n").encode("utf-8")
    offset = 0
    while offset < len(payload):
        offset += os.write(descriptor, payload[offset:])
    os.fsync(descriptor)
    finished = os.fstat(descriptor)
    _validate_file_snapshot(path, finished)
    return finished


# 读取非空显式环境 token，否则安全加载或排他创建用户级 API token 文件
def load_or_create_api_token(path: Path) -> str:
    env_token = _environment_token()
    if env_token is not None:
        return env_token
    path = path.expanduser().absolute()
    parent_before = _prepare_parent(path)
    if os.path.lexists(path):
        return _read_created_token(path, parent_before)
    token = secrets.token_urlsafe(32)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_BINARY", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_NOINHERIT", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_created_token(path, parent_before)
    except OSError as exc:
        raise ApiTokenError(f"Cannot create API token file: {path}") from exc
    created = os.fstat(descriptor)
    try:
        finished = _write_created_token(path, descriptor, token)
    except BaseException:
        os.close(descriptor)
        _remove_created_token(path, created)
        raise
    os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise ApiTokenError(f"Created API token file disappeared: {path}") from exc
    _validate_file_snapshot(path, after)
    if not _same_snapshot(finished, after):
        raise ApiTokenError(f"Created API token file changed before use: {path}")
    _require_unchanged_parent(path, parent_before)
    return token
