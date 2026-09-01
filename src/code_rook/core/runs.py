from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

RUNS_DIR = Path("~/.coderook/runs").expanduser()
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")


# 校验 run_id 仅含路径安全字符，禁止回放与写入路径逃逸状态根目录
def validate_run_id(run_id: str) -> str:
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError(f"invalid run id: {run_id!r}")
    return run_id


# 返回指定 run_id 对应的目录路径
def run_dir(run_id: str) -> Path:
    return RUNS_DIR / validate_run_id(run_id)


# 返回指定 run_id 的事件日志文件路径
def events_file(run_id: str) -> Path:
    return run_dir(run_id) / "events.jsonl"


# 生成格式为 YYYYMMDD-HHMMSS-xxxxxx 的唯一 run ID
def new_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}-{suffix}"


# 创建 run 目录（含父级）并返回路径
def ensure_run_dir(run_id: str) -> Path:
    path = run_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
