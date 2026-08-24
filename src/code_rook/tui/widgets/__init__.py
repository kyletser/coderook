"""TUI 控件集合。

本包存放从 ``code_rook.tui.app`` 拆分出来的独立控件与它们的共享辅助函数，
避免 ``app.py`` 与各控件模块之间产生循环导入。
"""

from __future__ import annotations

import json
from typing import Any

from code_rook.tui.product import tr


# 截断长文本并附加省略号
def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 把工具参数序列化为紧凑缩进的 JSON 文本
def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 工具名到集中式动作文案键的映射
_TOOL_ACTION_KEYS: dict[str, str] = {
    "apply_patch": "patch",
    "background_cancel": "background_cancel",
    "background_list": "background_list",
    "background_run": "background_run",
    "background_status": "background_status",
    "bash": "command",
    "checkpoint_list": "checkpoint_list",
    "checkpoint_rewind": "checkpoint_rewind",
    "edit_file": "edit",
    "git_diff": "workspace_diff",
    "glob": "search",
    "grep": "search",
    "list_dir": "list",
    "memory_forget": "memory_forget",
    "memory_save": "memory_save",
    "memory_search": "memory_search",
    "note_save": "note_save",
    "read_file": "read",
    "read_image": "read_image",
    "spawn_agent": "spawn_agent",
    "task_claim": "task_claim",
    "task_create": "task_create",
    "task_list": "task_list",
    "task_update": "task_update",
    "web_fetch": "web_fetch",
    "web_search": "web_search",
    "write_file": "write",
}

_FILE_ACTION_KEYS = {
    "read": "read",
    "list": "list",
    "search_name": "search_name",
    "search_content": "search_content",
    "write": "write",
    "edit": "edit",
    "patch": "patch",
}
_GIT_ACTION_KEYS = {
    "status": "git_status",
    "diff": "git_diff",
    "log": "git_log",
    "show": "git_show",
    "blame": "git_blame",
}
_RUN_ACTION_KEYS = {"tests": "run_tests", "verifiers": "run_verifiers"}
_BASH_ACTION_KEYS = {
    "run": "command",
    "wait": "background_wait",
    "interact": "background_interact",
    "cancel": "background_cancel",
}
_AGENT_ACTION_KEYS = {
    "start": "worker_start",
    "status": "worker_status",
    "peek": "worker_peek",
    "wait": "worker_wait",
    "cancel": "worker_cancel",
    "followup": "worker_followup",
}


# 从工具参数中提取最适合摘要展示的关键字段
def _param_summary(tool_name: str, params: dict[str, Any], max_len: int = 72) -> str:
    keys_by_tool = {
        "apply_patch": ("patch",),
        "checkpoint_list": (),
        "checkpoint_rewind": ("checkpoint_id",),
        "read_file": ("path",),
        "read_image": ("path",),
        "edit_file": ("path",),
        "write_file": ("path",),
        "list_dir": ("path", "max_depth"),
        "glob": ("pattern", "path"),
        "git_diff": ("scope", "path"),
        "grep": ("pattern", "path", "glob"),
        "bash": ("command",),
        "web_fetch": ("url",),
        "web_search": ("query",),
        "note_save": ("content",),
        "task_claim": ("task_id", "owner"),
        "task_create": ("subject",),
        "task_update": ("task_id", "status"),
    }
    keys = keys_by_tool.get(tool_name, ())
    parts = [f"{key}={params[key]!r}" for key in keys if key in params]
    if not parts:
        parts = [f"{key}={value!r}" for key, value in list(params.items())[:2]]
    return _preview(", ".join(parts), max_len)


# 把工具参数转换成接近自然语言的紧凑目标文本
def _tool_target(
    tool_name: str,
    params: dict[str, Any],
    *,
    locale: str = "zh-CN",
) -> str:
    if tool_name == "File":
        action = str(params.get("action", ""))
        if action in {"search_name", "search_content"}:
            pattern = str(params.get("pattern", ""))
            path = str(params.get("path", "."))
            return _preview(f"{pattern} in {path}" if pattern else path, 110)
        if action == "patch":
            return tr("tool.target.workspace_patch", locale)
        return _preview(str(params.get("path", ".")), 110)
    if tool_name == "Git":
        action = str(params.get("action", ""))
        if action == "show":
            revision = str(params.get("revision", ""))
            path = str(params.get("path", "."))
            return _preview(f"{revision} · {path}".strip(" ·"), 110)
        if action == "blame":
            return _preview(str(params.get("path", ".")), 110)
        return _preview(str(params.get("path", ".")), 110)
    if tool_name == "Run":
        action = str(params.get("action", ""))
        if action == "tests":
            return _preview(
                str(params.get("command", tr("tool.target.tests", locale))),
                110,
            )
        commands = params.get("commands", [])
        if isinstance(commands, list):
            return tr("tool.target.verification_count", locale, count=len(commands))
        return tr("tool.target.verification", locale)
    if tool_name == "agent":
        action = str(params.get("action", "status"))
        if action == "start":
            return _preview(
                str(params.get("description", tr("tool.target.worker", locale))),
                110,
            )
        return _preview(
            str(params.get("worker_id", tr("tool.target.workers", locale))),
            110,
        )
    if tool_name == "Bash":
        action = str(params.get("action", "run"))
        if action == "run":
            return _preview(str(params.get("command", "")), 110)
        return _preview(
            str(params.get("job_id", tr("tool.target.background_job", locale))),
            110,
        )
    if tool_name == "bash":
        return _preview(str(params.get("command", "")), 110)
    if tool_name in {"read_file", "edit_file", "write_file", "list_dir", "read_image"}:
        return _preview(str(params.get("path", ".")), 110)
    if tool_name in {"glob", "grep"}:
        pattern = str(params.get("pattern", ""))
        path = str(params.get("path", "."))
        target = f"{pattern} in {path}" if pattern else path
        return _preview(target, 110)
    if tool_name == "checkpoint_rewind":
        return _preview(str(params.get("checkpoint_id", "")), 110)
    if tool_name == "web_fetch":
        return _preview(str(params.get("url", "")), 110)
    if tool_name == "web_search":
        return _preview(str(params.get("query", "")), 110)
    if tool_name.startswith("background_"):
        value = params.get("command", params.get("job_id", ""))
        return _preview(str(value), 110)
    if tool_name.startswith("task_"):
        value = params.get("subject", params.get("task_id", ""))
        return _preview(str(value), 110)
    if tool_name == "spawn_agent":
        value = params.get("description", params.get("goal", ""))
        return _preview(str(value), 110)
    return _param_summary(tool_name, params, max_len=110)


# 生成工具运行中或完成后的自然动作文案
def _tool_action_text(
    tool_name: str,
    params: dict[str, Any],
    *,
    finished: bool,
    locale: str = "zh-CN",
) -> str:
    if tool_name == "File":
        action_key = _FILE_ACTION_KEYS.get(str(params.get("action", "")), "file")
    elif tool_name == "Git":
        action_key = _GIT_ACTION_KEYS.get(str(params.get("action", "")), "git")
    elif tool_name == "Run":
        action_key = _RUN_ACTION_KEYS.get(str(params.get("action", "")), "run")
    elif tool_name == "Bash":
        action_key = _BASH_ACTION_KEYS.get(
            str(params.get("action", "run")),
            "command_operation",
        )
    elif tool_name == "agent":
        action_key = _AGENT_ACTION_KEYS.get(
            str(params.get("action", "status")),
            "worker_operation",
        )
    else:
        action_key = _TOOL_ACTION_KEYS.get(tool_name, "generic")
    state = "finished" if finished else "running"
    label = tr(f"tool.action.{action_key}.{state}", locale, tool=tool_name)
    target = _tool_target(tool_name, params, locale=locale)
    return f"{label} {target}".rstrip()
