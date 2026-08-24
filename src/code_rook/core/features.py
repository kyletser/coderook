from __future__ import annotations

import os
from collections.abc import Mapping

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


# 判断当前进程是否由维护者显式开启实验性功能
def labs_enabled(environ: Mapping[str, str] | None = None) -> bool:
    source = os.environ if environ is None else environ
    return source.get("CODEROOK_LABS", "").strip().casefold() in _TRUE_VALUES
