"""TUI 控件集合。

本包存放从 ``code_rook.tui.app`` 拆分出来的独立控件与它们的共享辅助函数，
避免 ``app.py`` 与各控件模块之间产生循环导入。
"""

from __future__ import annotations

import json
from typing import Any


# 截断长文本并附加省略号
def _preview(s: str, n: int) -> str:
    return s[:n] + "…" if len(s) > n else s


# 把工具参数序列化为紧凑缩进的 JSON 文本
def _params_str(params: dict[str, Any]) -> str:
    return json.dumps(params, ensure_ascii=False, indent=2)


# 工具在各状态下使用的动作文案对（未完成 / 完成）
_TOOL_ACTIONS: dict[str, tuple[str, str]] = {
    "apply_patch": ("正在应用补丁", "已应用补丁"),
    "background_cancel": ("正在停止后台任务", "已停止后台任务"),
    "background_list": ("正在查看后台任务", "已查看后台任务"),
    "background_run": ("正在启动后台命令", "已启动后台命令"),
    "background_status": ("正在检查后台任务", "已检查后台任务"),
    "bash": ("正在执行命令", "已执行命令"),
    "checkpoint_list": ("正在加载恢复点", "已加载恢复点"),
    "checkpoint_rewind": ("正在恢复检查点", "已恢复检查点"),
    "edit_file": ("正在修改", "已修改"),
    "git_diff": ("正在检查工作区改动", "已检查工作区改动"),
    "glob": ("正在搜索", "已搜索"),
    "grep": ("正在搜索", "已搜索"),
    "list_dir": ("正在查看目录", "已查看目录"),
    "memory_forget": ("正在删除项目记忆", "已删除项目记忆"),
    "memory_save": ("正在保存项目记忆", "已保存项目记忆"),
    "memory_search": ("正在搜索项目记忆", "已搜索项目记忆"),
    "note_save": ("正在保存笔记", "已保存笔记"),
    "read_file": ("正在读取", "已读取"),
    "read_image": ("正在读取图片", "已读取图片"),
    "spawn_agent": ("正在启动子代理", "已启动子代理"),
    "task_claim": ("正在认领任务", "已认领任务"),
    "task_create": ("正在创建任务", "已创建任务"),
    "task_list": ("正在加载任务", "已加载任务"),
    "task_update": ("正在更新任务", "已更新任务"),
    "web_fetch": ("正在抓取网页", "已抓取网页"),
    "web_search": ("正在搜索网页", "已搜索网页"),
    "write_file": ("正在写入", "已写入"),
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
def _tool_target(tool_name: str, params: dict[str, Any]) -> str:
    if tool_name == "File":
        action = str(params.get("action", ""))
        if action in {"search_name", "search_content"}:
            pattern = str(params.get("pattern", ""))
            path = str(params.get("path", "."))
            return _preview(f"{pattern} in {path}" if pattern else path, 110)
        if action == "patch":
            return "workspace patch"
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
            return _preview(str(params.get("command", "tests")), 110)
        commands = params.get("commands", [])
        if isinstance(commands, list):
            return f"{len(commands)} verification gates"
        return "verification gates"
    if tool_name == "agent":
        action = str(params.get("action", "status"))
        if action == "start":
            return _preview(str(params.get("description", "worker")), 110)
        return _preview(str(params.get("worker_id", "workers")), 110)
    if tool_name == "Bash":
        action = str(params.get("action", "run"))
        if action == "run":
            return _preview(str(params.get("command", "")), 110)
        return _preview(str(params.get("job_id", "background job")), 110)
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
) -> str:
    if tool_name == "File":
        action = str(params.get("action", ""))
        file_actions = {
            "read": ("正在读取", "已读取"),
            "list": ("正在查看目录", "已查看目录"),
            "search_name": ("正在按名称搜索", "已完成名称搜索"),
            "search_content": ("正在搜索内容", "已完成内容搜索"),
            "write": ("正在写入", "已写入"),
            "edit": ("正在修改", "已修改"),
            "patch": ("正在应用补丁", "已应用补丁"),
        }
        actions = file_actions.get(action, ("正在操作文件", "已完成文件操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Git":
        action = str(params.get("action", ""))
        git_actions = {
            "status": ("正在检查 Git 状态", "已检查 Git 状态"),
            "diff": ("正在检查 Git 改动", "已检查 Git 改动"),
            "log": ("正在读取提交记录", "已读取提交记录"),
            "show": ("正在查看提交", "已查看提交"),
            "blame": ("正在追溯代码行", "已追溯代码行"),
        }
        actions = git_actions.get(action, ("正在读取 Git", "已读取 Git"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Run":
        action = str(params.get("action", ""))
        run_actions = {
            "tests": ("正在运行测试", "已运行测试"),
            "verifiers": ("正在运行验证", "已运行验证"),
        }
        actions = run_actions.get(action, ("正在运行检查", "已运行检查"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "Bash":
        action = str(params.get("action", "run"))
        bash_actions = {
            "run": ("正在执行命令", "已执行命令"),
            "wait": ("正在等待后台任务", "已检查后台任务"),
            "interact": ("正在发送后台输入", "已发送后台输入"),
            "cancel": ("正在停止后台任务", "已停止后台任务"),
        }
        actions = bash_actions.get(action, ("正在操作命令", "已完成命令操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    if tool_name == "agent":
        action = str(params.get("action", "status"))
        agent_actions = {
            "start": ("正在启动 Worker", "已启动 Worker"),
            "status": ("正在检查 Worker", "已检查 Worker"),
            "peek": ("正在查看 Worker 进度", "已查看 Worker 进度"),
            "wait": ("正在等待 Worker", "已等待 Worker"),
            "cancel": ("正在停止 Worker", "已停止 Worker"),
            "followup": ("正在发送 Worker 指令", "已发送 Worker 指令"),
        }
        actions = agent_actions.get(action, ("正在操作 Worker", "已完成 Worker 操作"))
        label = actions[1 if finished else 0]
        return f"{label} {_tool_target(tool_name, params)}".rstrip()
    actions = _TOOL_ACTIONS.get(
        tool_name,
        (f"正在执行 {tool_name}", f"已完成 {tool_name}"),
    )
    action = actions[1 if finished else 0]
    target = _tool_target(tool_name, params)
    return f"{action} {target}".rstrip()