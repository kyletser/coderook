#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import PurePosixPath
from typing import Any

_WRITE_ACTIONS = {"write", "edit", "patch"}
_SENSITIVE_NAMES = {
    ".env",
    ".git-credentials",
    "api-token",
    "credentials.json",
    "ipc-token",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}


# 统一 Windows 与 POSIX 路径分隔符并提取文件名
def _file_name(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).name.casefold()


# 判断单个目标路径是否属于常见凭据或环境文件
def _is_sensitive_path(value: str) -> bool:
    name = _file_name(value)
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or any(name.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)
    )


# 从 File family 参数中找出将被修改的敏感目标
def _sensitive_target(params: dict[str, Any]) -> str | None:
    action = str(params.get("action", ""))
    if action not in _WRITE_ACTIONS:
        return None
    path = str(params.get("path", ""))
    if path and _is_sensitive_path(path):
        return path
    if action == "patch":
        patch = str(params.get("patch", params.get("patch_text", "")))
        for line in patch.splitlines():
            if line.startswith(("+++ ", "--- ")):
                candidate = line[4:].removeprefix("a/").removeprefix("b/")
                if _is_sensitive_path(candidate):
                    return candidate
    return None


# 将 Hooks V2 stdin payload 转换为结构化阻断决定
def evaluate(payload: dict[str, Any]) -> dict[str, object]:
    if payload.get("schema_version") != 2 or payload.get("event") != "tool_call_before":
        return {"blocked": False, "reason": "unsupported hook payload"}
    context = payload.get("context")
    if not isinstance(context, dict) or context.get("tool_name") != "File":
        return {"blocked": False, "reason": "not a File tool call"}
    params = context.get("params")
    if not isinstance(params, dict):
        return {"blocked": False, "reason": "File params are missing"}
    target = _sensitive_target(params)
    if target is None:
        return {"blocked": False, "reason": "target is not sensitive"}
    return {
        "blocked": True,
        "reason": f"example policy blocks writes to sensitive file: {target}",
    }


# 运行不依赖 CodeRook daemon 的示例策略自检
def _self_test() -> None:
    base = {
        "schema_version": 2,
        "event": "tool_call_before",
        "context": {"tool_name": "File"},
    }
    blocked = dict(base)
    blocked["context"] = {
        "tool_name": "File",
        "params": {"action": "write", "path": ".env"},
    }
    allowed = dict(base)
    allowed["context"] = {
        "tool_name": "File",
        "params": {"action": "edit", "path": "src/app.py"},
    }
    assert evaluate(blocked)["blocked"] is True
    assert evaluate(allowed)["blocked"] is False


# 解析 stdin 并只向 stdout 写一个 HookDecision JSON 对象
def main() -> int:
    parser = argparse.ArgumentParser(description="Block File writes to common secret paths.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        print("sensitive-file hook self-test passed")
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(json.dumps({"blocked": True, "reason": f"invalid hook payload: {exc}"}))
        return 0
    if not isinstance(payload, dict):
        print(json.dumps({"blocked": True, "reason": "hook payload must be an object"}))
        return 0
    print(json.dumps(evaluate(payload), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
