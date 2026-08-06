from __future__ import annotations

import ipaddress
import os
import secrets
from pathlib import Path

_API_TOKEN_ENV = "CODEROOK_API_TOKEN"


# 判断监听主机是否明确限制在本机回环地址
def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


# 验证非回环监听必须配置 bearer token
def validate_api_binding(host: str, token: str) -> None:
    if not is_loopback_host(host) and not token:
        raise ValueError("non-loopback API binding requires CODEROOK_API_TOKEN")


# 使用常量时间比较验证 Authorization bearer header
def bearer_authorized(header: str | None, token: str) -> bool:
    if not token:
        return True
    prefix = "Bearer "
    if header is None or not header.startswith(prefix):
        return False
    return secrets.compare_digest(header[len(prefix) :], token)


# 读取或排他创建 0600 的 API token 文件；环境变量优先于文件
def load_or_create_api_token(path: Path) -> str:
    env_token = os.environ.get(_API_TOKEN_ENV)
    if env_token is not None:
        return env_token
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError(f"API token file must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        raise ValueError(f"API token file is empty: {path}") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(token + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(path, 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return token
