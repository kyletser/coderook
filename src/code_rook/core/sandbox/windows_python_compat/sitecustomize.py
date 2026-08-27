from __future__ import annotations

import os
import secrets
import tempfile
from os import PathLike

_ORIGINAL_MKDTEMP = tempfile.mkdtemp


# 在 Windows ACL 沙箱中创建继承父 DACL 的临时目录，避免 Python 0o700 显式 ACL 丢失 capability ACE
def _inheriting_mkdtemp(
    suffix: str | None = None,
    prefix: str | None = None,
    dir: str | PathLike[str] | None = None,
) -> str:
    if os.environ.get("CODEROOK_WINDOWS_ACL") != "1":
        return _ORIGINAL_MKDTEMP(suffix=suffix, prefix=prefix, dir=dir)
    raw_dir = os.fspath(dir) if dir is not None else tempfile.gettempdir()
    normalized_suffix = suffix or ""
    normalized_prefix = prefix if prefix is not None else "tmp"
    for _attempt in range(100):
        candidate = os.path.join(
            raw_dir,
            f"{normalized_prefix}{secrets.token_hex(8)}{normalized_suffix}",
        )
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("no usable temporary directory name found")


if os.name == "nt" and os.environ.get("CODEROOK_WINDOWS_ACL") == "1":
    setattr(tempfile, "mkdtemp", _inheriting_mkdtemp)
