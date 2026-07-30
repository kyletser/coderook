from __future__ import annotations

import ipaddress
import os
import secrets
import stat
import time
from pathlib import Path

_TOKEN_ENV = "CODEROOK_IPC_TOKEN"
_MIN_TOKEN_LENGTH = 32
_MAX_TOKEN_LENGTH = 512


class IpcTokenError(RuntimeError):
    """The local IPC credential is missing, unsafe, or malformed."""


def is_loopback_host(host: str) -> bool:
    candidate = host.strip().lower()
    if candidate == "localhost":
        return True
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def require_loopback_host(host: str) -> None:
    if not is_loopback_host(host):
        raise SystemExit(
            f"Refusing non-loopback Core host {host!r}; use 127.0.0.1, ::1, or localhost"
        )


def _validate_token(token: str, *, source: str) -> str:
    if token != token.strip() or any(char.isspace() for char in token):
        raise IpcTokenError(f"IPC token from {source} contains whitespace")
    if not (_MIN_TOKEN_LENGTH <= len(token) <= _MAX_TOKEN_LENGTH):
        raise IpcTokenError(
            f"IPC token from {source} must be {_MIN_TOKEN_LENGTH}-{_MAX_TOKEN_LENGTH} characters"
        )
    return token


def _read_token_file(path: Path) -> str:
    try:
        file_stat = path.lstat()
    except FileNotFoundError as exc:
        raise IpcTokenError(f"IPC token file does not exist: {path}") from exc
    if stat.S_ISLNK(file_stat.st_mode):
        raise IpcTokenError(f"IPC token file must not be a symlink: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise IpcTokenError(f"IPC token path is not a regular file: {path}")
    if file_stat.st_size > 4096:
        raise IpcTokenError(f"IPC token file is unexpectedly large: {path}")
    if hasattr(os, "getuid") and file_stat.st_uid != os.getuid():
        raise IpcTokenError(f"IPC token file is not owned by the current user: {path}")
    try:
        token = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise IpcTokenError(f"Cannot read IPC token file: {path}") from exc
    return _validate_token(token, source=str(path))


def read_ipc_token(path: Path) -> str:
    env_token = os.environ.get(_TOKEN_ENV)
    if env_token is not None:
        return _validate_token(env_token, source=_TOKEN_ENV)
    return _read_token_file(path.expanduser())


def _read_created_token(path: Path) -> str:
    # A second Core may observe the exclusive file between create() and fsync().
    for _ in range(20):
        try:
            return _read_token_file(path)
        except IpcTokenError:
            time.sleep(0.01)
    return _read_token_file(path)


def load_or_create_ipc_token(path: Path) -> str:
    env_token = os.environ.get(_TOKEN_ENV)
    if env_token is not None:
        return _validate_token(env_token, source=_TOKEN_ENV)

    path = path.expanduser()
    if path.is_symlink():
        raise IpcTokenError(f"IPC token file must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_created_token(path)
    except OSError as exc:
        raise IpcTokenError(f"Cannot create IPC token file: {path}") from exc

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(token + "\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError as exc:
            raise IpcTokenError(f"Cannot restrict IPC token permissions: {path}") from exc
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return token
