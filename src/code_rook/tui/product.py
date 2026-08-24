"""TUI 产品状态卡、可翻译文案与运行证据归并器。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.markup import escape
from textual.widgets import Static

_TEXT: dict[str, dict[str, str]] = {
    "zh": {
        "readiness.unconfigured.title": "欢迎使用 CodeRook",
        "readiness.unconfigured.body": "浏览会话和管理功能已经可用；开始编码前需要配置模型路由。",
        "readiness.configuration_invalid.title": "模型路由配置损坏",
        "readiness.configuration_invalid.body": (
            "活动路由记录无法安全加载；有效路由已保留，但开始任务前需要重新选择或修复活动路由。"
        ),
        "readiness.credential_missing.title": "模型凭据待补全",
        "readiness.credential_missing.body": "活动路由存在，但本机找不到它需要的凭据。",
        "readiness.endpoint_unreachable.title": "本地模型服务不可达",
        "readiness.endpoint_unreachable.body": "配置已保存，但本地模型端点当前无法连接。",
        "readiness.provider_unverified.title": "模型路由尚未验证",
        "readiness.provider_unverified.body": (
            "凭据已就绪，但当前 route/model 还没有匹配的基础 Doctor 收据。"
        ),
        "readiness.actions.unconfigured": "下一步  /config 配置模型 · /provider 查看路由",
        "readiness.actions.configuration_invalid": (
            "下一步  /provider 检查路由 · /config 重新选择活动模型"
        ),
        "readiness.actions.credential_missing": "下一步  /config 补充凭据 · /doctor 诊断",
        "readiness.actions.endpoint_unreachable": "下一步  启动本地模型服务 · /doctor 复查",
        "readiness.actions.provider_unverified": (
            "下一步  /doctor 验证 · 提交任务时也会执行轻量探测"
        ),
        "readiness.route": "路由 {route} · 模型 {model}",
        "readiness.prompt": "请先完成模型配置 · 草稿已保留",
        "error.title": "操作未完成",
        "error.category": "类别 {category}",
        "error.diagnostic": "诊断 ID {diagnostic_id}",
        "error.action.connection": "确认 Core 正在运行；界面会自动重连。",
        "error.action.authentication": "重启 Core，确保客户端与 daemon 使用同一 IPC token。",
        "error.action.protocol": "运行 coderook core restart；若仍失败请附诊断 ID 报告问题。",
        "error.action.submission": "原输入已保留；连接恢复后可再次提交。",
        "error.action.session": "重新打开 /sessions；若仍失败请附诊断 ID 报告问题。",
        "error.action.inspection": "稍后重试；运行详情仍可通过 /turn 查看。",
        "error.action.audit": (
            "审计持久化不可用，修改类工具已暂停；"
            "运行 coderook doctor runtime --repair 后重启 Core。"
        ),
        "error.action.generic": "重试该操作；若仍失败请附诊断 ID 报告问题。",
        "history.status": "输入历史：{status} · 仅当前工作区",
        "history.on": "已开启当前工作区输入历史。",
        "history.off": "已关闭当前工作区输入历史；已有记录未删除。",
        "history.clear": "已清空当前工作区输入历史。",
        "history.enabled": "开启",
        "history.disabled": "关闭",
        "history.usage": "用法：/history status|on|off|clear",
        "review.visible": "复审当前改动",
        "language.current": "界面语言：{language}",
        "language.changed": "界面语言已切换为 {language}；新状态卡与提示立即生效。",
        "language.usage": "用法：/language zh-CN|en-US",
        "shell.banner_hint": "输入消息开始对话 · /help 查看命令 · Ctrl+P 命令面板 · Ctrl+Q 退出",
        "shell.connecting": "正在连接",
        "shell.connected": "输入任务或按 Ctrl+P",
        "shell.plan": "规划模式",
        "shell.review_plan": "审阅上方计划",
        "shell.running": "执行中 · Enter 补充 · Ctrl+C 取消",
        "shell.planning": "规划中 · Enter 补充 · Ctrl+C 取消",
        "shell.disconnected": "连接已断开 · 正在重试",
        "shell.cancelled": "任务已取消",
        "shell.prompt.attachments": "附件 {count}",
        "connection.recovery_disabled": "自动恢复未启用",
        "connection.restart_failed": "自动重启失败：{error}",
        "connection.core_started": "已启动新的 Core",
        "connection.core_available": "Core 已恢复可用",
        "connection.unreachable": "无法连接 {host}:{port}；{detail}",
        "connection.authentication_failed": "身份验证失败",
        "connection.setup_failed": "连接初始化失败",
        "connection.closed": "到 {host}:{port} 的连接已关闭",
        "common.current": "当前",
        "common.more": "还有 {count} 项",
        "input.config.intro": "输入 API Key 后按 Enter，CodeRook 将探测该账号的可用模型。",
        "input.config.empty": "API Key 不能为空。",
        "input.config.discovering": "正在探测可用模型…",
        "input.config.retry": "Enter 重试 · Esc 返回",
        "input.config.hint": "Enter 探测模型 · Esc 返回",
        "completion.no_match": "没有匹配命令",
        "completion.usage": "用法：/{name} {usage}",
        "completion.hint": "↑↓ 导航 · Tab 补全 · Enter 执行/补全 · Esc 关闭",
        "question.custom": "输入自定义答案",
        "question.prompt": "请回答上方问题",
        "question.multi_hint": "↑↓ 移动 · Space 切换 · Enter 确认",
        "question.single_hint": "↑↓ 移动 · Enter 选择",
        "rewind.title": "恢复",
        "rewind.hint": "↑↓ 移动 · Enter 预览恢复 · Esc 关闭",
        "rewind.choose": "选择要恢复的 checkpoint",
        "rewind.safety": "恢复会拒绝覆盖 checkpoint 之后再次修改的文件",
        "plan_review.title": "计划已就绪",
        "plan_review.hint": "↑↓ 移动 · Enter 选择 · Esc 取消",
        "plan_review.question": "计划已完成，下一步？",
        "plan_review.approve": "批准并实施",
        "plan_review.approve_detail": "退出 Plan Mode，按当前权限逐项执行",
        "plan_review.revise": "继续规划",
        "plan_review.revise_detail": "输入反馈后再次进行只读分析",
        "plan_review.cancel": "取消",
        "plan_review.cancel_detail": "保留计划但不执行任何改动",
        "selector.sessions.title": "会话",
        "selector.sessions.hint": "输入即过滤 · ↑↓ 移动 · Enter 打开 · Esc 关闭",
        "selector.sessions.empty": "没有保存的 chat 会话。",
        "selector.sessions.filter": "过滤：{query} · 命中 {matched}/{total}",
        "selector.sessions.no_match": "没有匹配的会话，退格修改过滤词",
        "selector.sessions.untitled": "未命名会话",
        "selector.models.title": "模型",
        "selector.models.hint": "输入即搜索 · ↑↓ 移动 · Enter 切换 · Esc 关闭",
        "selector.models.empty": "没有已配置模型。使用 /model add <model-id> 添加。",
        "selector.models.capabilities": "route 能力：{labels}",
        "selector.models.search": "搜索：{query} · 命中 {matched}/{total}",
        "selector.models.no_match": "没有匹配的模型，退格修改搜索词",
        "selector.models.custom": "使用 /model add <model-id> 添加自定义模型",
        "selector.provider.title": "API Provider",
        "selector.provider.hint": "↑↓ 移动 · Enter 继续 · Esc 关闭",
        "permission.title": "需要批准",
        "permission.wants": "CodeRook 希望{action}",
        "permission.context.command": "命令",
        "permission.context.target": "目标",
        "permission.context.checkpoint": "CHECKPOINT",
        "permission.context.task": "任务",
        "permission.context.request": "请求",
        "permission.no_details": "没有更多详情",
        "permission.action.bash": "执行 shell 命令",
        "permission.action.write_file": "写入文件",
        "permission.action.edit_file": "编辑文件",
        "permission.action.apply_patch": "应用工作区改动",
        "permission.action.checkpoint_rewind": "恢复工作区改动",
        "permission.action.agent": "管理持久 Worker",
        "permission.action.spawn_agent": "启动子 Agent",
        "permission.action.other": "使用 {tool}",
        "permission.choice.allow_once": "仅本次允许",
        "permission.choice.always_allow": "始终允许",
        "permission.choice.deny_once": "拒绝",
        "permission.choice.always_deny": "始终拒绝",
        "permission.choice.always_allow_pattern": "始终允许此模式",
        "permission.choice.once_detail": "仅限本次请求",
        "permission.choice.remember_allow": "以后会话也记住允许",
        "permission.choice.deny_detail": "跳过本次请求",
        "permission.choice.remember_deny": "以后会话也记住拒绝",
        "permission.choice.pattern_detail": "记住该命令前缀",
        "permission.hunks": "HUNKS",
        "permission.all_or_nothing": "不可拆分",
        "permission.more_hunks": "其余 hunk 已选择但未显示",
        "permission.diff_truncated": "diff 已截断，请批准或拒绝以继续",
        "permission.allow_question": "允许这项操作吗？",
        "permission.hint": "↑↓ 导航 · Enter 选择 · Esc 拒绝",
        "permission.hunk_hint": "Tab 切换 hunk/操作 · Space 切换 hunk · {base}",
        "permission.pending": "等待批准",
        "permission.allowed": "已允许",
        "permission.denied": "已拒绝",
        "permission.decision.allow_once": "仅本次允许",
        "permission.decision.always_allow": "始终允许",
        "permission.decision.always_allow_pattern": "始终允许（命令模式）",
        "permission.decision.deny_once": "已拒绝",
        "permission.decision.always_deny": "始终拒绝",
        "permission.decision.timeout": "已超时",
        "permission_mode.title": "权限",
        "permission_mode.hint": "↑↓ 移动 · Enter 选择 · Esc 关闭",
        "permission_mode.question": "选择后续消息使用的权限模式",
        "permission_mode.ask": "询问后修改",
        "permission_mode.ask_detail": "文件修改、命令和外部操作按策略确认",
        "permission_mode.accept_edits": "自动接受修改",
        "permission_mode.accept_edits_detail": "工作区文件修改自动执行，命令和外部操作仍确认",
        "permission_mode.full_access": "全自动执行",
        "permission_mode.full_access_detail": (
            "本机命令、修改和外部操作自动批准，Plan Mode 与工具边界仍生效"
        ),
        "permission_mode.cycle_hint": "也可用 Shift+Tab 在三种权限姿态间循环",
        "stream.thinking": "思考中",
        "stream.thought": "深度思考",
        "stream.failed_action": "执行失败：{action}",
        "stream.error": "错误",
        "stream.response": "响应",
        "stream.failed": "失败",
        "stream.success": "成功",
        "stream.no_output": "（无输出）",
        "stream.run_command.one": "运行了命令",
        "stream.run_command.many": "运行了 {count} 条命令",
        "stream.read.one": "读取了文件",
        "stream.read.many": "读取了 {count} 个位置",
        "stream.search.one": "搜索了内容",
        "stream.search.many": "进行了 {count} 次搜索",
        "stream.web.one": "搜索了网页",
        "stream.web.many": "搜索了 {count} 次网页",
        "stream.edit.one": "编辑了文件",
        "stream.edit.many": "编辑了 {count} 个文件",
        "stream.used_tool": "使用了 {name}",
        "stream.tools": "执行工具",
        "stream.action.running": "正在使用 {tool}：{target}",
        "stream.action.finished": "已使用 {tool}：{target}",
        "tool.target.workspace_patch": "工作区补丁",
        "tool.target.tests": "测试",
        "tool.target.verification_count": "{count} 项验证门禁",
        "tool.target.verification": "验证门禁",
        "tool.target.worker": "Worker",
        "tool.target.workers": "Workers",
        "tool.target.background_job": "后台任务",
        "tool.action.patch.running": "正在应用补丁",
        "tool.action.patch.finished": "已应用补丁",
        "tool.action.background_cancel.running": "正在停止后台任务",
        "tool.action.background_cancel.finished": "已停止后台任务",
        "tool.action.background_list.running": "正在查看后台任务",
        "tool.action.background_list.finished": "已查看后台任务",
        "tool.action.background_run.running": "正在启动后台命令",
        "tool.action.background_run.finished": "已启动后台命令",
        "tool.action.background_status.running": "正在检查后台任务",
        "tool.action.background_status.finished": "已检查后台任务",
        "tool.action.command.running": "正在执行命令",
        "tool.action.command.finished": "已执行命令",
        "tool.action.checkpoint_list.running": "正在加载恢复点",
        "tool.action.checkpoint_list.finished": "已加载恢复点",
        "tool.action.checkpoint_rewind.running": "正在恢复检查点",
        "tool.action.checkpoint_rewind.finished": "已恢复检查点",
        "tool.action.edit.running": "正在修改",
        "tool.action.edit.finished": "已修改",
        "tool.action.workspace_diff.running": "正在检查工作区改动",
        "tool.action.workspace_diff.finished": "已检查工作区改动",
        "tool.action.search.running": "正在搜索",
        "tool.action.search.finished": "已搜索",
        "tool.action.list.running": "正在查看目录",
        "tool.action.list.finished": "已查看目录",
        "tool.action.memory_forget.running": "正在删除项目记忆",
        "tool.action.memory_forget.finished": "已删除项目记忆",
        "tool.action.memory_save.running": "正在保存项目记忆",
        "tool.action.memory_save.finished": "已保存项目记忆",
        "tool.action.memory_search.running": "正在搜索项目记忆",
        "tool.action.memory_search.finished": "已搜索项目记忆",
        "tool.action.note_save.running": "正在保存笔记",
        "tool.action.note_save.finished": "已保存笔记",
        "tool.action.read.running": "正在读取",
        "tool.action.read.finished": "已读取",
        "tool.action.read_image.running": "正在读取图片",
        "tool.action.read_image.finished": "已读取图片",
        "tool.action.spawn_agent.running": "正在启动子代理",
        "tool.action.spawn_agent.finished": "已启动子代理",
        "tool.action.task_claim.running": "正在认领任务",
        "tool.action.task_claim.finished": "已认领任务",
        "tool.action.task_create.running": "正在创建任务",
        "tool.action.task_create.finished": "已创建任务",
        "tool.action.task_list.running": "正在加载任务",
        "tool.action.task_list.finished": "已加载任务",
        "tool.action.task_update.running": "正在更新任务",
        "tool.action.task_update.finished": "已更新任务",
        "tool.action.web_fetch.running": "正在抓取网页",
        "tool.action.web_fetch.finished": "已抓取网页",
        "tool.action.web_search.running": "正在搜索网页",
        "tool.action.web_search.finished": "已搜索网页",
        "tool.action.write.running": "正在写入",
        "tool.action.write.finished": "已写入",
        "tool.action.search_name.running": "正在按名称搜索",
        "tool.action.search_name.finished": "已完成名称搜索",
        "tool.action.search_content.running": "正在搜索内容",
        "tool.action.search_content.finished": "已完成内容搜索",
        "tool.action.file.running": "正在操作文件",
        "tool.action.file.finished": "已完成文件操作",
        "tool.action.git_status.running": "正在检查 Git 状态",
        "tool.action.git_status.finished": "已检查 Git 状态",
        "tool.action.git_diff.running": "正在检查 Git 改动",
        "tool.action.git_diff.finished": "已检查 Git 改动",
        "tool.action.git_log.running": "正在读取提交记录",
        "tool.action.git_log.finished": "已读取提交记录",
        "tool.action.git_show.running": "正在查看提交",
        "tool.action.git_show.finished": "已查看提交",
        "tool.action.git_blame.running": "正在追溯代码行",
        "tool.action.git_blame.finished": "已追溯代码行",
        "tool.action.git.running": "正在读取 Git",
        "tool.action.git.finished": "已读取 Git",
        "tool.action.run_tests.running": "正在运行测试",
        "tool.action.run_tests.finished": "已运行测试",
        "tool.action.run_verifiers.running": "正在运行验证",
        "tool.action.run_verifiers.finished": "已运行验证",
        "tool.action.run.running": "正在运行检查",
        "tool.action.run.finished": "已运行检查",
        "tool.action.background_wait.running": "正在等待后台任务",
        "tool.action.background_wait.finished": "已检查后台任务",
        "tool.action.background_interact.running": "正在发送后台输入",
        "tool.action.background_interact.finished": "已发送后台输入",
        "tool.action.command_operation.running": "正在操作命令",
        "tool.action.command_operation.finished": "已完成命令操作",
        "tool.action.worker_start.running": "正在启动 Worker",
        "tool.action.worker_start.finished": "已启动 Worker",
        "tool.action.worker_status.running": "正在检查 Worker",
        "tool.action.worker_status.finished": "已检查 Worker",
        "tool.action.worker_peek.running": "正在查看 Worker 进度",
        "tool.action.worker_peek.finished": "已查看 Worker 进度",
        "tool.action.worker_wait.running": "正在等待 Worker",
        "tool.action.worker_wait.finished": "已等待 Worker",
        "tool.action.worker_cancel.running": "正在停止 Worker",
        "tool.action.worker_cancel.finished": "已停止 Worker",
        "tool.action.worker_followup.running": "正在发送 Worker 指令",
        "tool.action.worker_followup.finished": "已发送 Worker 指令",
        "tool.action.worker_operation.running": "正在操作 Worker",
        "tool.action.worker_operation.finished": "已完成 Worker 操作",
        "tool.action.generic.running": "正在执行 {tool}",
        "tool.action.generic.finished": "已完成 {tool}",
        "event.llm.retry": "正在重试模型响应 {kind} #{attempt}",
        "event.agent.stuck": "已停止重复动作 · {count} 次相同结果",
        "event.goal.continue": "Goal 将自动继续下一轮",
        "event.goal.paused": "Goal 已暂停，等待用户确认",
        "event.goal.ended": "Goal 本轮结束",
        "event.goal.resume": "继续执行：/goal resume",
        "event.run.cancelled": "已取消 · {steps} {unit}",
        "event.run.failed": "已失败 · {steps} {unit}",
        "event.run.model_guidance": (
            "检查 /doctor、/provider 或 /config；修复路由后可重新提交任务。"
        ),
        "event.permission.denied": "审批超时或连接断开，{tool} 已按拒绝处理",
        "event.diagnostics.passed": "诊断通过",
        "event.diagnostics.issues": "诊断发现 {count} 条问题",
        "event.diagnostics.degraded": "诊断降级 {status}",
        "manage.artifacts.title": "Artifacts",
        "manage.artifacts.summary": "{count} item(s) · {total} total · {reclaimable} reclaimable",
        "manage.artifacts.kept": "kept",
        "manage.artifacts.candidate": "candidate",
        "manage.artifacts.recent": "recent",
        "manage.artifacts.more": "还有 {count} 个 artifact",
        "manage.artifacts.hint": "使用 /artifacts gc [days] 预览，追加 --yes 确认删除。",
        "manage.gc.preview": "Artifact GC preview",
        "manage.gc.summary": "{count} item(s)，{reclaimable}",
        "manage.gc.no_delete": "未删除任何文件；确认请输入 /artifacts gc --yes",
        "manage.gc.completed": "Artifact GC completed",
        "manage.gc.receipt": "收据：{path}",
        "manage.mcp.title": "MCP servers",
        "manage.mcp.empty": "当前没有配置 MCP server。",
        "manage.mcp.tools": "{count} tool(s)",
        "manage.mcp.hint": "使用 /mcp <name> 展开工具清单。",
        "manage.mcp.no_tools": "该 server 未发现工具。",
        "manage.hooks.title": "Hooks",
        "manage.hooks.empty": "当前没有配置 hook。",
        "manage.hooks.recent": "最近执行",
        "manage.hooks.hint": "使用 /hooks rerun <id> --yes 手动重跑。",
        "manage.memory.title": "Project memory",
        "manage.memory.auto": "Agent 自动保存：{mode}（prompt=每次审批，off=关闭）",
        "manage.memory.empty": "当前项目没有记忆条目。",
        "manage.memory.expired": "已过期",
        "manage.memory.hint": (
            "/memory add <name> :: <body> · edit <id> :: <body> · pin|unpin <id> · "
            "expire <id> <ISO|never> · auto prompt|off · /memory delete <id> --yes"
        ),
        "manage.jobs.title": "后台任务",
        "manage.jobs.empty": "当前没有后台任务。",
        "manage.jobs.hint": "使用 /jobs show <id> 查看增量输出，/jobs cancel <id> --yes 取消。",
        "manage.jobs.missing": "未找到该任务。",
        "manage.jobs.item_title": "Job {id}",
        "manage.jobs.no_output": "[无输出]",
        "manage.workers.title": "Subagents / workers",
        "manage.workers.empty": "没有并行子代理。",
        "manage.workers.hint": "使用 /jobs cancel <worker_id> --yes 取消子代理。",
        "turn.title": "Turn 检查器",
        "turn.goal": "目标={goal}",
        "turn.workers": (
            "Worker={active}/{total} 活跃/总数 · 成本={cost} · "
            "待审批={pending} · 失败={failure}"
        ),
        "turn.route": "route={route} · model={model} · wire={wire}",
        "turn.authority": "模式={mode} · 权限={authority} · 信任={trust} · 沙箱={sandbox}",
        "turn.usage": "用量 {usage} · 成本={cost}",
        "turn.processes": (
            "进程={count} · CPU={cpu}ms · 峰值内存={memory}B · "
            "完整样本={complete}/{total}"
        ),
        "turn.tools": "工具={tools} · 审批={asked}/{allowed}/{denied}（请求/允许/拒绝）",
        "turn.verification": "验证 {value}",
        "turn.context_selection": "上下文选择 {value}",
        "turn.unavailable": "不可用：{items}",
        "turn.error": "错误={error}",
        "app.permission.busy": "当前 run 或计划审阅完成后再切换权限模式",
        "app.mode.busy": "当前 run 或计划审阅完成后再切换工作模式",
        "app.cancel.confirm": "再次 Ctrl+C 确认取消当前任务 · Ctrl+Q 退出 TUI",
        "app.cancel.running": "正在取消 {run_id}…",
        "app.copy.empty": "暂无可复制的回复",
        "app.copy.done": "已复制上一条回复",
        "app.compaction.title": "上下文已压缩",
        "app.compaction.summary": (
            "触发=手动 · 摘要={summary} · 保留={messages} 条/{tokens} tokens · "
            "节省≈{saved} · 质量={quality}"
        ),
        "app.compaction.file": "摘要文件：{path}",
        "app.permission.changed": "权限模式 · {label}",
        "app.mode.changed": "工作模式 · {mode}",
        "app.trust.changed": "工作区信任 · {trust}",
        "app.sandbox.title": "沙箱",
        "app.sandbox.available": (
            "OS 强制隔离后端可用；每次命令的实际隔离计划与结果记录在 receipt 中。"
        ),
        "app.sandbox.unavailable": (
            "当前没有 OS 强制隔离；危险动作继续走 ASK 审批和工作区边界，"
            "但这些机制不等同于系统沙箱。"
        ),
        "app.worker.review_title": "Worker {worker} 审查结果",
        "app.worker.patch_title": "待审查完整补丁（含未跟踪文件）",
        "app.worker.review_confirm": "确认上述全部内容后运行：{command}",
        "app.worker.applied": "Worker {worker} 的改动已应用到当前工作区。",
        "app.worker.not_committed": "未创建提交，未推送。",
        "app.mcp.not_found": "未找到 MCP server：{name}",
        "app.memory.auto": "Agent 自动记忆已设为 {mode}",
        "app.memory.action": "记忆 {action} 完成：{id}",
        "app.memory.deleted": "已删除记忆 {id}",
        "app.memory.missing": "未找到记忆 {id}",
        "app.changes.staged": "已 stage · {count} files · {files}",
        "app.changes.review_stage_first": (
            "请先用 /diff 审查并 /stage 选定文件，再确认本地 commit。"
        ),
        "app.changes.committed": "本地 commit 已创建 · {commit} · {subject}",
        "app.changes.commit_meta": "{count} files · hooks skipped · 未执行 push",
        "app.rewind.empty": "当前会话没有可恢复的 checkpoint。",
        "app.rewind.preview": "Rewind 预览 · {checkpoint}",
        "app.rewind.preview_meta": (
            "paths={paths}\nrestorable={restorable}\nalready={already}\nconflicts={conflicts}"
        ),
        "app.rewind.conflicts": "存在冲突，预览已失效；请先处理冲突后重新选择。",
        "app.rewind.confirm": "确认执行：/rewind --yes",
        "app.rewind.no_pending": "没有可确认的 Rewind 预览；请先运行 /rewind 并选择 checkpoint。",
        "app.rewind.done": "已恢复 {checkpoint} · restored={restored} · already={already}",
        "app.provider.routes": "Provider routes",
        "app.provider.none": "尚未配置 Provider route，请使用 /config 添加。",
        "app.provider.no_active": "尚无活动 route，请先使用 /config 配置 Provider。",
        "app.provider.active_route": "活动 route · {route}/{model}",
        "app.provider.active_model": "活动模型 · {route}/{model}",
        "app.provider.doctor": "Provider doctor",
        "app.provider.capabilities": "能力：{capabilities}",
        "app.provider.configured": "Provider 已配置 · {route}/{model}",
        "app.session.renamed": "会话已重命名 · {title}",
        "app.session.exported": "会话已导出 · {path}",
        "app.session.created": "新会话",
        "app.session.resumed": "会话已恢复",
        "app.session.reconnected": "会话已重连",
        "app.session.ready": "会话已就绪",
        "app.session.history": "{count} 条历史消息",
        "app.session.export_exists": (
            "导出目标已存在，未覆盖：{path}\n"
            "确认目标无误后输入 /export {format} --force --yes"
        ),
        "app.steer.sent": "补充要求已发送",
        "app.steer.failed": "纠偏发送失败 · 输入已恢复",
        "app.answer.sent": "回答已发送 · Agent 继续执行",
        "app.answer.sending": "正在发送回答",
        "app.plan.invalid_session": "计划所属会话已失效，未执行",
        "app.plan.feedback": "规划模式 · 输入反馈",
        "app.plan.cancelled": "计划已取消，未执行改动",
        "app.core.recovered": "Core 已恢复 · 正在重连当前会话并续接事件。",
        "app.startup.sandbox_degraded": (
            "当前没有 OS 强制隔离；危险动作继续走 ASK 审批和工作区边界，"
            "但这些机制不等同于系统沙箱。"
        ),
        "app.startup.labs": "Labs 已开启 · Fleet、Workflow 与 Hooks 仍属实验性。",
        "app.labs.disabled": (
            "Labs 功能默认关闭 · 如需承担实验性恢复与权限风险，"
            "请设置 CODEROOK_LABS=1 后重启。"
        ),
        "app.submit.image_default": "请分析附加图片。",
        "app.submit.starting": "run 正在启动；当前输入已保留，稍后再按 Enter 发送",
        "app.submit.busy": "Agent 忙碌或未连接，请稍后再试",
        "app.skills.invalid": "skills 参数错误：{error}",
        "app.skills.title": "Skills",
        "app.skills.empty": "没有 skill。",
        "app.skills.manifest": "Skill manifest",
        "app.skills.installed": "已安装 skill {name}",
        "app.skills.removed": "已删除 {scope} skill {name}",
        "app.skills.audit": "Skill audit",
        "app.skills.usage": (
            "用法：/skills list|show <name>|install <path> [--scope user|project] "
            "[--trust] [--yes]|remove <name> [--scope user|project] --yes|audit"
        ),
        "app.skills.preview": "安装预览（尚未写入）",
        "app.skills.confirm": "确认后在相同命令末尾添加 --yes",
        "app.goal.resume_label": "继续当前 Goal",
        "app.goal.title": "Goal",
        "app.goals.title": "Goals",
        "app.goals.empty": "没有 Goal。",
        "app.goal.invalid_payload": "Goal 数据无效",
        "app.goal.draft_reconnected": "连接中 · Goal 输入已恢复",
        "app.goal.empty": "当前 session 没有未完成 Goal。",
        "app.goal.draft_failed": "Goal 发送失败 · 输入已恢复",
        "app.goal.incomplete": "未完成标准",
        "app.goal.all_evidenced": "全部完成标准已有证据覆盖",
        "app.goal.no_criteria": "未设置完成标准；自动续跑会暂停",
        "app.goal.evidence": "已有证据",
        "app.goal.pause_reason": "暂停/阻塞原因",
        "app.goal.resume_confirm": "需要用户确认后才能继续：/goal resume",
        "app.tasks.empty": "当前会话最近一次 run 没有任务。",
        "app.tasks.title": "Tasks",
        "app.tasks.blocked": "blocked by {items}",
        "app.worker.started": "Worker {worker} 已启动",
        "app.worker.retried": "Worker {worker} 已启动重试",
        "app.worker.empty": "当前没有持久 Worker。",
        "app.worker.events": "Worker {worker} events",
        "app.worker.no_events": "没有新的持久事件。",
        "app.worker.followup": "已发送 followup 给 {worker}",
        "app.worker.review_pending": "{status}；尚未应用",
        "app.worker.review_rejected": "{status}；已拒绝，未执行 apply/merge",
        "app.provider.switch_hint": (
            "切换：/provider <route-id>；thinking 档位在 routes.json 中配置"
        ),
        "app.session.deleted": "会话已删除 · 已创建新会话",
        "app.draft.reconnected": "连接中 · 原输入已恢复",
        "app.draft.failed": "发送失败 · 原输入已恢复",
        "app.draft.steer_failed": "连接中 · 纠偏输入已恢复",
        "app.plan.approve_task": "批准计划并开始实施",
        "app.cost.title": "Cost · Runtime 持久会话",
        "app.cost.known_subtotal": "已知小计 {cost} · 另有未配置单价的模型用量",
        "app.cost.total": "总计 {cost}",
        "app.cost.note": (
            "金额为可解释参考价；未知模型不会被当作 $0。"
            "可在 ~/.coderook/pricing.toml 配置覆盖。"
        ),
        "cmd.usage": "用法：{usage}",
        "cmd.core_disconnected": "Core 未连接",
        "cmd.core_busy": "Core 未连接或任务运行中，稍后再试",
        "cmd.loading": "正在加载 {target}",
        "cmd.session.creating": "正在创建会话",
        "cmd.session.renaming": "正在重命名会话",
        "cmd.session.forking": "正在复制会话",
        "cmd.session.exporting": "正在导出会话",
        "cmd.session.export_usage": "用法：/export [md|json] [--force --yes]",
        "cmd.session.deleting": "正在删除会话",
        "cmd.session.delete_confirm": (
            "将删除当前会话 {session} 及其全部历史；确认请输入 /delete --yes"
        ),
        "cmd.provider.busy": "当前任务运行中，结束后再切换 Provider（Ctrl+C 可取消任务）",
        "cmd.model.busy": "当前任务运行中，结束后再切换模型（Ctrl+C 可取消任务）",
        "cmd.model.select": "选择模型",
        "cmd.doctor.busy": "当前任务运行中，结束后再运行诊断（Ctrl+C 可取消任务）",
        "cmd.doctor.running": "正在诊断 Provider",
        "cmd.config.busy": "当前任务运行中，结束后再修改 LLM 配置（Ctrl+C 可取消任务）",
        "cmd.config.select": "选择 API 平台",
        "cmd.rewind.checking": "正在校验 Rewind 预览",
        "cmd.review.request": (
            "作为资深代码审查者审查当前工作区改动。检查 durable turn 证据、diff、测试、"
            "安全边界及未验证项。保持只读，不修改文件。"
        ),
        "cmd.goal.invalid": "Goal 创建参数无效：{error}",
        "cmd.goal.quote": "参数引号不完整：{error}",
        "cmd.goal.auto_once": "--auto-continue 与 --no-auto-continue 只能指定一次",
        "cmd.goal.missing_value": "{option} 缺少值",
        "cmd.goal.missing_valid_value": "{option} 缺少有效值",
        "cmd.goal.once": "{option} 只能指定一次",
        "cmd.goal.positive": "{option} 必须是正整数",
        "cmd.goal.unknown": "未知 Goal 参数：{option}",
        "cmd.goal.invalid_value": "参数无效",
        "cmd.goal.clear_confirm": "这会取消当前 Turn 并终结 Goal；确认请输入 /goal {action} --yes",
        "cmd.goal.busy": "当前 Goal 或 turn 正在运行，请先暂停",
        "cmd.mode.current": "工作模式 · {mode} · 用法：/mode plan|act|operate",
        "cmd.permissions.select": "选择权限模式",
        "cmd.memory.delete_confirm": "将删除记忆 {id}；确认请输入 /memory delete {id} --yes",
        "cmd.memory.deleting": "正在删除记忆",
        "cmd.memory.running": "正在执行 memory {action}",
        "cmd.artifacts.days": "days 必须在 0..3650",
        "cmd.artifacts.gc": "正在处理 artifact GC",
        "cmd.stage.busy": "Core 未连接或任务运行中，不能 stage",
        "cmd.stage.placeholder": "<文件路径>",
        "cmd.stage.confirm": (
            "Stage 只会处理明确选择的当前改动：{paths}\n"
            "确认请输入 /stage <path...> --yes"
        ),
        "cmd.stage.running": "正在 stage 已选择改动",
        "cmd.commit.busy": "Core 未连接或任务运行中，不能 commit",
        "cmd.commit.placeholder": "<提交主题>",
        "cmd.commit.confirm": (
            "将从已 stage 改动创建本地 commit（不会 push，且跳过仓库 hooks）：{subject}\n"
            "确认请输入 /commit <提交主题> --yes"
        ),
        "cmd.commit.running": "正在创建本地 commit",
        "cmd.worker.invalid": "Worker 参数无效：{error}",
        "cmd.worker.missing_arg": "{option} 缺少参数",
        "cmd.worker.budget_positive": "--budget 必须是正整数",
        "cmd.worker.unknown_arg": "未知 Worker start 参数：{option}",
        "cmd.worker.empty_prompt": "Worker prompt 不能为空",
        "cmd.worker.loading": "正在加载 Workers",
        "cmd.worker.starting": "正在启动 Worker",
        "cmd.worker.status": "正在读取 Worker 状态",
        "cmd.worker.retry_confirm": "确认请输入 /workers retry {worker} --yes",
        "cmd.worker.retrying": "正在重试 Worker",
        "cmd.worker.events": "正在读取 Worker 事件",
        "cmd.worker.followup": "正在发送 Worker 指令",
        "cmd.worker.cancel_confirm": "确认请输入 /workers cancel {worker} --yes",
        "cmd.worker.cancelling": "正在取消 Worker",
        "cmd.worker.reviewing": "正在审查 Worker handoff",
        "cmd.worker.digest_required": "批准必须携带刚才完整预览返回的 64 位 digest。",
        "cmd.worker.review_confirm": (
            "审查不会自动合入。确认请输入 "
            "/workers review {worker} {decision} --yes"
        ),
        "cmd.worker.review_recording": "正在记录 Worker 审查",
        "cmd.worker.digest_invalid": (
            "Worker digest 必须是 review 返回的 64 位小写十六进制值。"
        ),
        "cmd.worker.apply_confirm": (
            "该操作会把已审查改动应用到当前工作区。确认请输入 "
            "/workers apply {worker} {digest} --yes"
        ),
        "cmd.worker.applying": "正在应用 Worker handoff",
        "cmd.worker.usage": (
            "用法：/workers [start [--profile P] [--route R] [--model M] [--budget N] "
            "[--file PATH | --write-root PATH] <prompt> | status <id> | retry <id> --yes | "
            "peek <id> [cursor] | followup <id> <message> | cancel <id> --yes | "
            "review <id> [approve|reject] [digest] --yes | apply <id> <digest> --yes]"
        ),
        "cmd.jobs.loading": "正在加载后台任务",
        "cmd.jobs.output": "正在加载任务输出",
        "cmd.jobs.cancel_confirm": "将取消任务 {id}，确认请输入 /jobs cancel {id} --yes",
        "cmd.jobs.cancelling": "正在取消任务",
        "cmd.jobs.usage": "用法：/jobs [show <id> | cancel <id> --yes]",
        "cmd.workflow.loading": "正在加载 {command}",
        "cmd.hooks.rerun_usage": "用法：/hooks rerun <hook_id> --yes",
        "cmd.hooks.rerun_confirm": (
            "将重跑 hook {id}，确认请输入 /hooks rerun {id} --yes"
        ),
        "cmd.hooks.rerunning": "正在重跑 hook",
        "cmd.hooks.usage": "用法：/hooks 或 /hooks rerun <hook_id> --yes",
        "cmd.hooks.loading": "正在加载 hooks",
        "app.workflow.started": "Workflow 已启动",
        "help.keys": "键位",
        "help.commands": "命令",
        "help.enter": "发送消息；Shift/Alt+Enter 或 Ctrl+J 换行",
        "help.history": "空输入时回溯输入历史",
        "help.mode": "循环工作模式 Act → Operate → Plan",
        "help.permission": "循环权限姿态 ask → auto-review → full-access",
        "help.cancel": "复制选择；无选择时再次按下取消当前任务",
        "help.copy": "复制选择或上一条回复",
        "help.scroll": "跳回日志底部",
        "help.quit": "退出 TUI；会话仍保留",
        "help.palette": "打开分类命令面板",
        "help.footer": "输入 / 可补全；未匹配的 /名称 会作为 skill 发送给 Agent",
        "header.repo": "仓库",
        "header.session": "会话",
        "header.route": "路由",
        "header.model": "模型",
        "header.permission": "权限",
        "header.trust": "信任",
        "header.goal": "目标",
        "header.state.ready": "就绪",
        "header.state.running": "执行中",
        "header.state.planning": "规划中",
        "header.state.plan": "规划",
        "header.state.plan ready": "待审阅",
        "header.state.disconnected": "已断开",
        "header.state.connecting": "连接中",
        "palette.title": "命令面板",
        "palette.search": "搜索：{query}",
        "palette.search.empty": "输入以过滤",
        "palette.no_results": "没有匹配命令",
        "palette.more": "… 还有 {count} 项，继续输入以缩小范围",
        "palette.hint": "↑/↓ 导航 · Enter 选择 · Esc 关闭",
        "palette.category.task": "任务",
        "palette.category.session": "会话",
        "palette.category.review": "审查",
        "palette.category.model": "模型",
        "palette.category.security": "安全",
        "palette.category.extension": "扩展",
        "palette.category.labs": "Labs",
        "changes.title": "改动中心",
        "changes.file_count": "{count} 个文件",
        "changes.empty": "工作区没有改动。",
        "changes.files": "文件",
        "changes.navigation": "j/k 文件 · n/p hunk",
        "changes.conflict": "冲突",
        "changes.more_files": "… 还有 {count} 个文件",
        "changes.no_hunk": "没有文本 hunk（未跟踪、二进制或 diff 已截断）。",
        "changes.hunk_position": "hunk {position}/{total}",
        "changes.more_hunk_lines": "… 还有 {count} 行",
        "changes.conflicts_block": "冲突阻止完成：",
        "changes.verification_failed": "验证失败，不能报告全部完成。",
        "changes.verification_unavailable": "未验证：没有 durable verification receipt。",
        "changes.unverified_count": "未验证：{count} 个改动文件缺少通过证据。",
        "changes.fully_verified": "所有改动文件都有通过的验证证据。",
        "changes.diff_truncated": "Diff 已截断；请缩小路径后重新审查。",
        "changes.verification": "验证",
        "changes.none_recorded": "没有记录",
        "changes.verification_mapping": "命令 → 结果 → 路径",
        "changes.scope_unavailable": "作用范围未知",
        "changes.more_verifications": "… 还有 {count} 条验证结果",
        "changes.close_hint": "Esc 关闭 · j/k 文件 · n/p hunk · /stage 与 /commit 仍需显式 --yes",
        "attachments.title": "待发送附件",
        "attachments.empty": "当前没有待发送图片。",
        "attachments.item": "#{index} {width}x{height} · {size} · {digest}",
        "attachments.added": "已附加图片 #{index} · {width}x{height} · {digest}",
        "attachments.limit": "每条消息最多附加 8 张图片",
        "attachments.too_large": "图片超过 2 MiB，请先压缩或裁剪",
        "attachments.removed": "已移除附件 #{index}。",
        "attachments.cleared": "已清空待发送附件。",
        "attachments.usage": "用法：/attachments [remove <序号>|clear]",
        "command.help": "显示键位与全部命令",
        "command.sessions": "打开会话选择器（输入即过滤）",
        "command.new": "新建会话",
        "command.rename": "重命名当前会话：/rename <标题>",
        "command.fork": "复制当前会话为分支：/fork [标题]",
        "command.export": "导出当前会话：/export [md|json]",
        "command.delete": "删除当前会话（需 --yes 确认）",
        "command.provider": "查看或切换 Provider route",
        "command.model": "查看或切换模型",
        "command.doctor": "诊断活动 Provider route",
        "command.config": "更换 LLM API、模型或密钥",
        "command.compact": "手动压缩上下文",
        "command.copy": "复制上一条回复",
        "command.history": "当前工作区输入历史：查看、开关或清空",
        "command.language": "切换界面语言：中文或 English",
        "command.attachments": "查看或移除待发送图片",
        "command.plan": "只读规划并审阅后再实施：/plan [任务]",
        "command.review": "只读复审当前改动：/review [关注点]",
        "command.goal": "持续执行并管理持久目标",
        "command.mode": "查看或切换工作模式：plan|act|operate",
        "command.permissions": "查看或切换权限模式",
        "command.trust": "查看或授予/撤销工作区信任",
        "command.sandbox": "查看 OS 隔离能力（仅探测）",
        "command.tasks": "查看最近一次 run 的任务",
        "command.workers": "查看、审查或应用持久 Worker",
        "command.workflow": "查看、启动或检查 workflow",
        "command.changes": "打开可导航的改动中心",
        "command.diff": "查看工作区改动",
        "command.stage": "选择文件加入 Git index（需 --yes）",
        "command.commit": "从已 stage 改动创建本地 commit（需 --yes）",
        "command.rewind": "预览并二次确认安全恢复点",
        "command.context": "查看上下文占用与用量",
        "command.cost": "查看本会话成本分解与缓存节省",
        "command.turn": "检查 route、用量、审批与收据",
        "command.skills": "列出、查看、安装或删除 skills",
        "command.mcp": "查看 MCP server 状态与工具",
        "command.hooks": "查看 hook 配置与执行记录",
        "command.memory": "查看、编辑并控制项目记忆",
        "command.jobs": "后台任务中心：查看/取消",
        "command.artifacts": "查看产物或执行引用感知 GC",
        "usage.model": "<模型 ID>|add <模型 ID>",
        "usage.attachments": "remove <序号>|clear",
        "usage.goal": (
            "create [边界参数] <目标>|status|list|pause|resume|edit|complete|cancel --yes"
        ),
        "usage.commit": "<提交主题> --yes",
        "configuration.discovery_failed": (
            "模型发现失败 · 诊断 ID {diagnostic_id}。请检查凭据与网络后重试。"
        ),
        "result.title.success": "任务完成",
        "result.title.failed": "任务失败",
        "result.title.interrupted": "任务已中断",
        "result.title.incomplete": "任务未完整结束",
        "result.title.content_filtered": "模型内容被过滤",
        "result.title.transport_error": "模型传输中断",
        "result.title.unknown": "任务结束",
        "result.meta": "耗时 {duration} · {steps} steps",
        "result.route": "路由 {route} · 模型 {model} · 成本 {cost}",
        "result.changes": "改动 {count} 个文件：{files}",
        "result.changes.none": "改动 0 个文件",
        "result.changes.unknown": "改动文件：证据不足",
        "result.line_stats.known": "行数 +{additions}/-{deletions}",
        "result.line_stats.partial": "已知行数 +{additions}/-{deletions} · 总计 unknown",
        "result.line_stats.unknown": "行数 unknown",
        "result.verification.pass": "验证通过 · {passed}/{total} gates",
        "result.verification.fail": "验证失败 · {passed}/{total} gates",
        "result.verification.unknown": "验证证据不足",
        "result.unverified": "未验证：{items}",
        "result.failure": "失败分类：{failure}",
        "result.actions": "入口  /changes 改动中心 · /review 复审 · /rewind 恢复 · /turn {turn}",
    },
    "en": {
        "readiness.unconfigured.title": "Welcome to CodeRook",
        "readiness.unconfigured.body": (
            "Sessions and management are ready. Configure a model route before coding."
        ),
        "readiness.configuration_invalid.title": "Model route configuration is invalid",
        "readiness.configuration_invalid.body": (
            "The active route could not be loaded safely. Valid routes were preserved, "
            "but you must select or repair an active route before starting a task."
        ),
        "readiness.credential_missing.title": "Model credential required",
        "readiness.credential_missing.body": (
            "The active route exists, but its credential cannot be resolved."
        ),
        "readiness.endpoint_unreachable.title": "Local model endpoint unavailable",
        "readiness.endpoint_unreachable.body": (
            "The route is saved, but its local endpoint cannot be reached."
        ),
        "readiness.provider_unverified.title": "Model route not verified",
        "readiness.provider_unverified.body": (
            "Credentials are present, but this route/model has no matching basic Doctor receipt."
        ),
        "readiness.actions.unconfigured": "Next  /config configure · /provider inspect routes",
        "readiness.actions.configuration_invalid": (
            "Next  /provider inspect routes · /config select an active model"
        ),
        "readiness.actions.credential_missing": "Next  /config add credential · /doctor diagnose",
        "readiness.actions.endpoint_unreachable": "Next  start the local service · /doctor retry",
        "readiness.actions.provider_unverified": (
            "Next  /doctor verify · task submission can also run a lightweight probe"
        ),
        "readiness.route": "Route {route} · model {model}",
        "readiness.prompt": "Configure a model first · draft preserved",
        "error.title": "Operation not completed",
        "error.category": "Category {category}",
        "error.diagnostic": "Diagnostic ID {diagnostic_id}",
        "error.action.connection": (
            "Check that Core is running; this view will reconnect automatically."
        ),
        "error.action.authentication": "Restart Core so the client and daemon share the IPC token.",
        "error.action.protocol": (
            "Run coderook core restart; report the diagnostic ID if it persists."
        ),
        "error.action.submission": "Your draft is preserved; submit again after reconnecting.",
        "error.action.session": "Open /sessions again; report the diagnostic ID if it persists.",
        "error.action.inspection": "Retry later; /turn still exposes durable run details.",
        "error.action.audit": (
            "Mutating tools are paused. Run coderook doctor runtime --repair, then restart Core."
        ),
        "error.action.generic": "Retry the operation; report the diagnostic ID if it persists.",
        "history.status": "Input history: {status} · current workspace only",
        "history.on": "Input history enabled for this workspace.",
        "history.off": "Input history disabled; existing entries were not deleted.",
        "history.clear": "Input history cleared for this workspace.",
        "history.enabled": "enabled",
        "history.disabled": "disabled",
        "history.usage": "Usage: /history status|on|off|clear",
        "review.visible": "Review current changes",
        "language.current": "UI language: {language}",
        "language.changed": "UI language changed to {language}; new cards and prompts use it now.",
        "language.usage": "Usage: /language zh-CN|en-US",
        "shell.banner_hint": "Describe a task · /help commands · Ctrl+P palette · Ctrl+Q quit",
        "shell.connecting": "Connecting",
        "shell.connected": "Describe a task or press Ctrl+P",
        "shell.plan": "Plan mode",
        "shell.review_plan": "Review the plan above",
        "shell.running": "Running · Enter steer · Ctrl+C cancel",
        "shell.planning": "Planning · Enter steer · Ctrl+C cancel",
        "shell.disconnected": "Disconnected · reconnecting",
        "shell.cancelled": "Task cancelled",
        "shell.prompt.attachments": "Attachments {count}",
        "connection.recovery_disabled": "automatic recovery is disabled",
        "connection.restart_failed": "automatic restart failed: {error}",
        "connection.core_started": "started a new Core",
        "connection.core_available": "Core became available",
        "connection.unreachable": "cannot connect to {host}:{port}; {detail}",
        "connection.authentication_failed": "authentication failed",
        "connection.setup_failed": "connection setup failed",
        "connection.closed": "connection to {host}:{port} closed",
        "common.current": "current",
        "common.more": "{count} more",
        "input.config.intro": (
            "Enter an API key and press Enter. CodeRook will discover the models "
            "available to this account."
        ),
        "input.config.empty": "API key cannot be empty.",
        "input.config.discovering": "Discovering available models…",
        "input.config.retry": "Enter retry · Esc back",
        "input.config.hint": "Enter discover models · Esc back",
        "completion.no_match": "no matching commands",
        "completion.usage": "usage: /{name} {usage}",
        "completion.hint": "↑↓ navigate · Tab complete · Enter run/complete · Esc dismiss",
        "question.custom": "Enter a custom answer",
        "question.prompt": "Answer the question above",
        "question.multi_hint": "↑↓ move · Space toggle · Enter confirm",
        "question.single_hint": "↑↓ move · Enter select",
        "rewind.title": "Rewind",
        "rewind.hint": "↑↓ move · Enter preview · Esc close",
        "rewind.choose": "Choose a checkpoint to restore",
        "rewind.safety": "Restore refuses to overwrite files changed again after the checkpoint",
        "plan_review.title": "Plan ready",
        "plan_review.hint": "↑↓ move · Enter select · Esc cancel",
        "plan_review.question": "The plan is ready. What next?",
        "plan_review.approve": "Approve and implement",
        "plan_review.approve_detail": "Exit Plan Mode and execute under the current permissions",
        "plan_review.revise": "Continue planning",
        "plan_review.revise_detail": "Provide feedback for another read-only analysis",
        "plan_review.cancel": "Cancel",
        "plan_review.cancel_detail": "Keep the plan without applying changes",
        "selector.sessions.title": "Sessions",
        "selector.sessions.hint": "Type to filter · ↑↓ move · Enter open · Esc close",
        "selector.sessions.empty": "No saved chat sessions.",
        "selector.sessions.filter": "Filter: {query} · {matched}/{total} matches",
        "selector.sessions.no_match": "No matching sessions; press Backspace to edit the filter",
        "selector.sessions.untitled": "Untitled",
        "selector.models.title": "Models",
        "selector.models.hint": "Type to search · ↑↓ move · Enter switch · Esc close",
        "selector.models.empty": "No configured models. Use /model add <model-id>.",
        "selector.models.capabilities": "route capabilities: {labels}",
        "selector.models.search": "Search: {query} · {matched}/{total} matches",
        "selector.models.no_match": "No matching models; press Backspace to edit the search",
        "selector.models.custom": "Add a custom option with /model add <model-id>",
        "selector.provider.title": "API Provider",
        "selector.provider.hint": "↑↓ move · Enter continue · Esc close",
        "permission.title": "Approval required",
        "permission.wants": "CodeRook wants to {action}",
        "permission.context.command": "COMMAND",
        "permission.context.target": "TARGET",
        "permission.context.checkpoint": "CHECKPOINT",
        "permission.context.task": "TASK",
        "permission.context.request": "REQUEST",
        "permission.no_details": "No additional details",
        "permission.action.bash": "run a shell command",
        "permission.action.write_file": "write a file",
        "permission.action.edit_file": "edit a file",
        "permission.action.apply_patch": "apply workspace changes",
        "permission.action.checkpoint_rewind": "rewind workspace changes",
        "permission.action.agent": "manage a durable worker",
        "permission.action.spawn_agent": "start a subagent",
        "permission.action.other": "use {tool}",
        "permission.choice.allow_once": "Allow once",
        "permission.choice.always_allow": "Always allow",
        "permission.choice.deny_once": "Deny",
        "permission.choice.always_deny": "Always deny",
        "permission.choice.always_allow_pattern": "Always allow pattern",
        "permission.choice.once_detail": "this request only",
        "permission.choice.remember_allow": "remember for future sessions",
        "permission.choice.deny_detail": "skip this request",
        "permission.choice.remember_deny": "remember for future sessions",
        "permission.choice.pattern_detail": "remember this command prefix",
        "permission.hunks": "HUNKS",
        "permission.all_or_nothing": "all-or-nothing",
        "permission.more_hunks": "additional hunks are selected but not shown",
        "permission.diff_truncated": "diff truncated, approve/deny to continue",
        "permission.allow_question": "Allow this action?",
        "permission.hint": "↑↓ navigate · Enter select · Esc deny",
        "permission.hunk_hint": "Tab hunks/actions · Space toggle hunk · {base}",
        "permission.pending": "approval required",
        "permission.allowed": "allowed",
        "permission.denied": "denied",
        "permission.decision.allow_once": "allowed once",
        "permission.decision.always_allow": "always allowed",
        "permission.decision.always_allow_pattern": "always allowed (pattern)",
        "permission.decision.deny_once": "denied",
        "permission.decision.always_deny": "always denied",
        "permission.decision.timeout": "timed out",
        "permission_mode.title": "Permissions",
        "permission_mode.hint": "↑↓ move · Enter select · Esc close",
        "permission_mode.question": "Choose the permission mode for subsequent messages",
        "permission_mode.ask": "Ask before changes",
        "permission_mode.ask_detail": (
            "Confirm file changes, commands, and external actions according to policy"
        ),
        "permission_mode.accept_edits": "Auto-accept edits",
        "permission_mode.accept_edits_detail": (
            "Apply workspace edits automatically; still confirm commands and external actions"
        ),
        "permission_mode.full_access": "Full access",
        "permission_mode.full_access_detail": (
            "Auto-approve local commands, edits, and external actions; Plan Mode and "
            "tool boundaries still apply"
        ),
        "permission_mode.cycle_hint": "Shift+Tab also cycles through the three permission modes",
        "stream.thinking": "Thinking",
        "stream.thought": "Thought process",
        "stream.failed_action": "Failed: {action}",
        "stream.error": "Error",
        "stream.response": "Response",
        "stream.failed": "Failed",
        "stream.success": "Succeeded",
        "stream.no_output": "(no output)",
        "stream.run_command.one": "Ran a command",
        "stream.run_command.many": "Ran {count} commands",
        "stream.read.one": "Read a file",
        "stream.read.many": "Read {count} locations",
        "stream.search.one": "Searched content",
        "stream.search.many": "Ran {count} searches",
        "stream.web.one": "Searched the web",
        "stream.web.many": "Ran {count} web searches",
        "stream.edit.one": "Edited a file",
        "stream.edit.many": "Edited {count} files",
        "stream.used_tool": "Used {name}",
        "stream.tools": "Ran tools",
        "stream.action.running": "Running {tool}: {target}",
        "stream.action.finished": "Completed {tool}: {target}",
        "tool.target.workspace_patch": "workspace patch",
        "tool.target.tests": "tests",
        "tool.target.verification_count": "{count} verification gates",
        "tool.target.verification": "verification gates",
        "tool.target.worker": "worker",
        "tool.target.workers": "workers",
        "tool.target.background_job": "background job",
        "tool.action.patch.running": "Applying patch",
        "tool.action.patch.finished": "Applied patch",
        "tool.action.background_cancel.running": "Stopping background job",
        "tool.action.background_cancel.finished": "Stopped background job",
        "tool.action.background_list.running": "Checking background jobs",
        "tool.action.background_list.finished": "Checked background jobs",
        "tool.action.background_run.running": "Starting background command",
        "tool.action.background_run.finished": "Started background command",
        "tool.action.background_status.running": "Checking background job",
        "tool.action.background_status.finished": "Checked background job",
        "tool.action.command.running": "Running command",
        "tool.action.command.finished": "Ran command",
        "tool.action.checkpoint_list.running": "Loading checkpoints",
        "tool.action.checkpoint_list.finished": "Loaded checkpoints",
        "tool.action.checkpoint_rewind.running": "Restoring checkpoint",
        "tool.action.checkpoint_rewind.finished": "Restored checkpoint",
        "tool.action.edit.running": "Editing",
        "tool.action.edit.finished": "Edited",
        "tool.action.workspace_diff.running": "Checking workspace changes",
        "tool.action.workspace_diff.finished": "Checked workspace changes",
        "tool.action.search.running": "Searching",
        "tool.action.search.finished": "Searched",
        "tool.action.list.running": "Listing directory",
        "tool.action.list.finished": "Listed directory",
        "tool.action.memory_forget.running": "Deleting project memory",
        "tool.action.memory_forget.finished": "Deleted project memory",
        "tool.action.memory_save.running": "Saving project memory",
        "tool.action.memory_save.finished": "Saved project memory",
        "tool.action.memory_search.running": "Searching project memory",
        "tool.action.memory_search.finished": "Searched project memory",
        "tool.action.note_save.running": "Saving note",
        "tool.action.note_save.finished": "Saved note",
        "tool.action.read.running": "Reading",
        "tool.action.read.finished": "Read",
        "tool.action.read_image.running": "Reading image",
        "tool.action.read_image.finished": "Read image",
        "tool.action.spawn_agent.running": "Starting subagent",
        "tool.action.spawn_agent.finished": "Started subagent",
        "tool.action.task_claim.running": "Claiming task",
        "tool.action.task_claim.finished": "Claimed task",
        "tool.action.task_create.running": "Creating task",
        "tool.action.task_create.finished": "Created task",
        "tool.action.task_list.running": "Loading tasks",
        "tool.action.task_list.finished": "Loaded tasks",
        "tool.action.task_update.running": "Updating task",
        "tool.action.task_update.finished": "Updated task",
        "tool.action.web_fetch.running": "Fetching web page",
        "tool.action.web_fetch.finished": "Fetched web page",
        "tool.action.web_search.running": "Searching the web",
        "tool.action.web_search.finished": "Searched the web",
        "tool.action.write.running": "Writing",
        "tool.action.write.finished": "Wrote",
        "tool.action.search_name.running": "Searching by name",
        "tool.action.search_name.finished": "Searched by name",
        "tool.action.search_content.running": "Searching content",
        "tool.action.search_content.finished": "Searched content",
        "tool.action.file.running": "Operating on file",
        "tool.action.file.finished": "Completed file operation",
        "tool.action.git_status.running": "Checking Git status",
        "tool.action.git_status.finished": "Checked Git status",
        "tool.action.git_diff.running": "Checking Git changes",
        "tool.action.git_diff.finished": "Checked Git changes",
        "tool.action.git_log.running": "Reading commit history",
        "tool.action.git_log.finished": "Read commit history",
        "tool.action.git_show.running": "Inspecting commit",
        "tool.action.git_show.finished": "Inspected commit",
        "tool.action.git_blame.running": "Tracing code lines",
        "tool.action.git_blame.finished": "Traced code lines",
        "tool.action.git.running": "Reading Git data",
        "tool.action.git.finished": "Read Git data",
        "tool.action.run_tests.running": "Running tests",
        "tool.action.run_tests.finished": "Ran tests",
        "tool.action.run_verifiers.running": "Running verification",
        "tool.action.run_verifiers.finished": "Ran verification",
        "tool.action.run.running": "Running checks",
        "tool.action.run.finished": "Ran checks",
        "tool.action.background_wait.running": "Waiting for background job",
        "tool.action.background_wait.finished": "Checked background job",
        "tool.action.background_interact.running": "Sending background input",
        "tool.action.background_interact.finished": "Sent background input",
        "tool.action.command_operation.running": "Operating command",
        "tool.action.command_operation.finished": "Completed command operation",
        "tool.action.worker_start.running": "Starting Worker",
        "tool.action.worker_start.finished": "Started Worker",
        "tool.action.worker_status.running": "Checking Worker",
        "tool.action.worker_status.finished": "Checked Worker",
        "tool.action.worker_peek.running": "Inspecting Worker progress",
        "tool.action.worker_peek.finished": "Inspected Worker progress",
        "tool.action.worker_wait.running": "Waiting for Worker",
        "tool.action.worker_wait.finished": "Waited for Worker",
        "tool.action.worker_cancel.running": "Stopping Worker",
        "tool.action.worker_cancel.finished": "Stopped Worker",
        "tool.action.worker_followup.running": "Sending Worker instruction",
        "tool.action.worker_followup.finished": "Sent Worker instruction",
        "tool.action.worker_operation.running": "Operating Worker",
        "tool.action.worker_operation.finished": "Completed Worker operation",
        "tool.action.generic.running": "Running {tool}",
        "tool.action.generic.finished": "Completed {tool}",
        "event.llm.retry": "Retrying model response {kind} #{attempt}",
        "event.agent.stuck": "Stopped repeated action · {count} identical results",
        "event.goal.continue": "Goal will continue automatically",
        "event.goal.paused": "Goal paused for user confirmation",
        "event.goal.ended": "Goal turn ended",
        "event.goal.resume": "Continue with: /goal resume",
        "event.run.cancelled": "Cancelled after {steps} {unit}",
        "event.run.failed": "Failed after {steps} {unit}",
        "event.run.model_guidance": (
            "Check /doctor, /provider, or /config, then resubmit after fixing the route."
        ),
        "event.permission.denied": (
            "Approval timed out or disconnected; {tool} was denied"
        ),
        "event.diagnostics.passed": "Diagnostics passed",
        "event.diagnostics.issues": "Diagnostics found {count} issue(s)",
        "event.diagnostics.degraded": "Diagnostics degraded: {status}",
        "manage.artifacts.title": "Artifacts",
        "manage.artifacts.summary": "{count} item(s) · {total} total · {reclaimable} reclaimable",
        "manage.artifacts.kept": "kept",
        "manage.artifacts.candidate": "candidate",
        "manage.artifacts.recent": "recent",
        "manage.artifacts.more": "{count} more artifact(s)",
        "manage.artifacts.hint": "Use /artifacts gc [days] to preview; append --yes to delete.",
        "manage.gc.preview": "Artifact GC preview",
        "manage.gc.summary": "{count} item(s), {reclaimable}",
        "manage.gc.no_delete": "No files were deleted; confirm with /artifacts gc --yes",
        "manage.gc.completed": "Artifact GC completed",
        "manage.gc.receipt": "receipt: {path}",
        "manage.mcp.title": "MCP servers",
        "manage.mcp.empty": "No MCP servers are configured.",
        "manage.mcp.tools": "{count} tool(s)",
        "manage.mcp.hint": "Use /mcp <name> to inspect its tools.",
        "manage.mcp.no_tools": "No tools were discovered for this server.",
        "manage.hooks.title": "Hooks",
        "manage.hooks.empty": "No hooks are configured.",
        "manage.hooks.recent": "Recent executions",
        "manage.hooks.hint": "Use /hooks rerun <id> --yes to rerun manually.",
        "manage.memory.title": "Project memory",
        "manage.memory.auto": "Agent auto-save: {mode} (prompt=confirm each time, off=disabled)",
        "manage.memory.empty": "This project has no memory entries.",
        "manage.memory.expired": "expired",
        "manage.memory.hint": (
            "/memory add <name> :: <body> · edit <id> :: <body> · pin|unpin <id> · "
            "expire <id> <ISO|never> · auto prompt|off · /memory delete <id> --yes"
        ),
        "manage.jobs.title": "Background jobs",
        "manage.jobs.empty": "There are no background jobs.",
        "manage.jobs.hint": (
            "Use /jobs show <id> for incremental output or "
            "/jobs cancel <id> --yes to cancel."
        ),
        "manage.jobs.missing": "Job not found.",
        "manage.jobs.item_title": "Job {id}",
        "manage.jobs.no_output": "[no output]",
        "manage.workers.title": "Subagents / workers",
        "manage.workers.empty": "There are no parallel subagents.",
        "manage.workers.hint": "Use /jobs cancel <worker_id> --yes to cancel a subagent.",
        "turn.title": "Turn Inspector",
        "turn.goal": "goal={goal}",
        "turn.workers": (
            "workers={active}/{total} active/total · cost={cost} · "
            "pending approvals={pending} · failure={failure}"
        ),
        "turn.route": "route={route} · model={model} · wire={wire}",
        "turn.authority": "mode={mode} · authority={authority} · trust={trust} · sandbox={sandbox}",
        "turn.usage": "usage {usage} · cost={cost}",
        "turn.processes": (
            "processes={count} · cpu={cpu}ms · peak memory={memory}B · "
            "samples={complete}/{total} complete"
        ),
        "turn.tools": "tools={tools} · approvals={asked}/{allowed}/{denied} (asked/allowed/denied)",
        "turn.verification": "verification {value}",
        "turn.context_selection": "context selection {value}",
        "turn.unavailable": "unavailable: {items}",
        "turn.error": "error={error}",
        "app.permission.busy": (
            "Wait for the current run or plan review before changing permissions"
        ),
        "app.mode.busy": "Wait for the current run or plan review before changing modes",
        "app.cancel.confirm": "Press Ctrl+C again to cancel the task · Ctrl+Q quits the TUI",
        "app.cancel.running": "Cancelling {run_id}…",
        "app.copy.empty": "No response is available to copy",
        "app.copy.done": "Copied the last response",
        "app.compaction.title": "Context compacted",
        "app.compaction.summary": (
            "trigger=manual · summary={summary} · retained={messages} msgs/{tokens} tokens · "
            "saved≈{saved} · quality={quality}"
        ),
        "app.compaction.file": "summary file: {path}",
        "app.permission.changed": "Permission mode · {label}",
        "app.mode.changed": "Working mode · {mode}",
        "app.trust.changed": "Workspace trust · {trust}",
        "app.sandbox.title": "Sandbox",
        "app.sandbox.available": (
            "An OS isolation backend is available; each command records its actual sandbox "
            "plan and result in the receipt."
        ),
        "app.sandbox.unavailable": (
            "No OS isolation backend is available. Risky actions still require ASK approval "
            "and workspace-boundary checks, but these are not an OS sandbox."
        ),
        "app.worker.review_title": "Worker {worker} review",
        "app.worker.patch_title": "Full patch to review (including untracked files)",
        "app.worker.review_confirm": "After reviewing all content, run: {command}",
        "app.worker.applied": "Worker {worker} changes were applied to the current workspace.",
        "app.worker.not_committed": "No commit was created and nothing was pushed.",
        "app.mcp.not_found": "MCP server not found: {name}",
        "app.memory.auto": "Agent auto-memory set to {mode}",
        "app.memory.action": "Memory {action} completed: {id}",
        "app.memory.deleted": "Deleted memory {id}",
        "app.memory.missing": "Memory {id} not found",
        "app.changes.staged": "Staged · {count} files · {files}",
        "app.changes.review_stage_first": (
            "Review with /diff and select files with /stage before confirming a local commit."
        ),
        "app.changes.committed": "Local commit created · {commit} · {subject}",
        "app.changes.commit_meta": "{count} files · hooks skipped · not pushed",
        "app.rewind.empty": "This session has no restorable checkpoint.",
        "app.rewind.preview": "Rewind preview · {checkpoint}",
        "app.rewind.preview_meta": (
            "paths={paths}\nrestorable={restorable}\nalready={already}\nconflicts={conflicts}"
        ),
        "app.rewind.conflicts": (
            "Conflicts invalidate this preview; resolve them and choose the checkpoint again."
        ),
        "app.rewind.confirm": "Confirm with: /rewind --yes",
        "app.rewind.no_pending": (
            "There is no Rewind preview to confirm; run /rewind and choose a checkpoint first."
        ),
        "app.rewind.done": "Restored {checkpoint} · restored={restored} · already={already}",
        "app.provider.routes": "Provider routes",
        "app.provider.none": "No Provider route is configured. Use /config to add one.",
        "app.provider.no_active": "No route is active. Use /config to configure a Provider.",
        "app.provider.active_route": "Active route · {route}/{model}",
        "app.provider.active_model": "Active model · {route}/{model}",
        "app.provider.doctor": "Provider doctor",
        "app.provider.capabilities": "capabilities: {capabilities}",
        "app.provider.configured": "Provider configured · {route}/{model}",
        "app.session.renamed": "Session renamed · {title}",
        "app.session.exported": "Session exported · {path}",
        "app.session.created": "New session",
        "app.session.resumed": "Session resumed",
        "app.session.reconnected": "Session reconnected",
        "app.session.ready": "Session ready",
        "app.session.history": "{count} history message(s)",
        "app.session.export_exists": (
            "Export target already exists and was not overwritten: {path}\n"
            "After checking the exact target, confirm with /export {format} --force --yes"
        ),
        "app.steer.sent": "Steer sent",
        "app.steer.failed": "Steer failed · draft restored",
        "app.answer.sent": "Answer sent · Agent resumed",
        "app.answer.sending": "Sending answer",
        "app.plan.invalid_session": "The plan belongs to an invalid session and was not executed",
        "app.plan.feedback": "Plan mode · enter feedback",
        "app.plan.cancelled": "Plan cancelled; no changes were applied",
        "app.core.recovered": "Core recovered · reconnecting the session and resuming events.",
        "app.startup.sandbox_degraded": (
            "No OS-enforced isolation is available. Risky actions still require ASK approval "
            "and workspace-boundary checks, but those controls are not an OS sandbox."
        ),
        "app.startup.labs": "Labs enabled · Fleet, Workflow, and Hooks remain experimental.",
        "app.labs.disabled": (
            "Labs are disabled by default · set CODEROOK_LABS=1 and restart only if you "
            "accept the experimental recovery and permission semantics."
        ),
        "app.submit.image_default": "Analyze the attached image.",
        "app.submit.starting": (
            "The run is starting; your input was preserved. Press Enter again in a moment."
        ),
        "app.submit.busy": "The Agent is busy or disconnected; try again shortly",
        "app.skills.invalid": "Invalid skills arguments: {error}",
        "app.skills.title": "Skills",
        "app.skills.empty": "No skills.",
        "app.skills.manifest": "Skill manifest",
        "app.skills.installed": "Installed skill {name}",
        "app.skills.removed": "Removed {scope} skill {name}",
        "app.skills.audit": "Skill audit",
        "app.skills.usage": (
            "Usage: /skills list|show <name>|install <path> [--scope user|project] "
            "[--trust] [--yes]|remove <name> [--scope user|project] --yes|audit"
        ),
        "app.skills.preview": "Install preview (nothing written yet)",
        "app.skills.confirm": "Append --yes to the same command to confirm",
        "app.goal.resume_label": "Resume the current Goal",
        "app.goal.title": "Goal",
        "app.goals.title": "Goals",
        "app.goals.empty": "No goals.",
        "app.goal.invalid_payload": "Invalid Goal payload",
        "app.goal.draft_reconnected": "Connecting · Goal input restored",
        "app.goal.empty": "This session has no unfinished Goal.",
        "app.goal.draft_failed": "Goal submission failed · input restored",
        "app.goal.incomplete": "Incomplete criteria",
        "app.goal.all_evidenced": "Every completion criterion has supporting evidence",
        "app.goal.no_criteria": "No completion criteria are set; auto-continue will pause",
        "app.goal.evidence": "Evidence",
        "app.goal.pause_reason": "Pause/block reason",
        "app.goal.resume_confirm": "User confirmation is required: /goal resume",
        "app.tasks.empty": "The latest run in this session has no tasks.",
        "app.tasks.title": "Tasks",
        "app.tasks.blocked": "blocked by {items}",
        "app.worker.started": "Worker {worker} started",
        "app.worker.retried": "Worker {worker} retry started",
        "app.worker.empty": "There are no durable Workers.",
        "app.worker.events": "Worker {worker} events",
        "app.worker.no_events": "There are no new durable events.",
        "app.worker.followup": "Sent followup to {worker}",
        "app.worker.review_pending": "{status}; not applied",
        "app.worker.review_rejected": "{status}; rejected without apply/merge",
        "app.provider.switch_hint": (
            "Switch with /provider <route-id>; configure thinking level in routes.json"
        ),
        "app.session.deleted": "Session deleted · a new session was created",
        "app.draft.reconnected": "Connecting · original input restored",
        "app.draft.failed": "Submission failed · original input restored",
        "app.draft.steer_failed": "Connecting · steer input restored",
        "app.plan.approve_task": "Approve the plan and begin implementation",
        "app.cost.title": "Cost · durable Runtime session",
        "app.cost.known_subtotal": (
            "Known subtotal {cost} · some model usage has no configured price"
        ),
        "app.cost.total": "Total {cost}",
        "app.cost.note": (
            "Amounts are explainable reference estimates; unknown models are not treated as "
            "$0. Configure overrides in ~/.coderook/pricing.toml."
        ),
        "cmd.usage": "Usage: {usage}",
        "cmd.core_disconnected": "Core is not connected",
        "cmd.core_busy": "Core is disconnected or a task is running; try again later",
        "cmd.loading": "Loading {target}",
        "cmd.session.creating": "Creating session",
        "cmd.session.renaming": "Renaming session",
        "cmd.session.forking": "Forking session",
        "cmd.session.exporting": "Exporting session",
        "cmd.session.export_usage": "Usage: /export [md|json] [--force --yes]",
        "cmd.session.deleting": "Deleting session",
        "cmd.session.delete_confirm": (
            "This deletes session {session} and all history; confirm with /delete --yes"
        ),
        "cmd.provider.busy": "Wait for the current task before switching Provider (Ctrl+C cancels)",
        "cmd.model.busy": "Wait for the current task before switching models (Ctrl+C cancels)",
        "cmd.model.select": "Select model",
        "cmd.doctor.busy": "Wait for the current task before running diagnostics (Ctrl+C cancels)",
        "cmd.doctor.running": "Diagnosing Provider",
        "cmd.config.busy": (
            "Wait for the current task before changing LLM configuration (Ctrl+C cancels)"
        ),
        "cmd.config.select": "Select API platform",
        "cmd.rewind.checking": "Checking Rewind preview",
        "cmd.review.request": (
            "Review the current workspace changes as a senior code reviewer. Inspect the "
            "durable turn evidence, diff, tests, safety boundaries, and unverified items. "
            "Remain read-only and do not modify files."
        ),
        "cmd.goal.invalid": "Invalid Goal parameters: {error}",
        "cmd.goal.quote": "Unclosed argument quote: {error}",
        "cmd.goal.auto_once": (
            "--auto-continue and --no-auto-continue may be specified only once"
        ),
        "cmd.goal.missing_value": "{option} is missing a value",
        "cmd.goal.missing_valid_value": "{option} is missing a valid value",
        "cmd.goal.once": "{option} may be specified only once",
        "cmd.goal.positive": "{option} must be a positive integer",
        "cmd.goal.unknown": "Unknown Goal argument: {option}",
        "cmd.goal.invalid_value": "Invalid value",
        "cmd.goal.clear_confirm": (
            "This cancels the current Turn and ends the Goal; "
            "confirm with /goal {action} --yes"
        ),
        "cmd.goal.busy": "Pause the active Goal or turn first",
        "cmd.mode.current": "Working mode · {mode} · Usage: /mode plan|act|operate",
        "cmd.permissions.select": "Select permission mode",
        "cmd.memory.delete_confirm": (
            "This deletes memory {id}; confirm with /memory delete {id} --yes"
        ),
        "cmd.memory.deleting": "Deleting memory",
        "cmd.memory.running": "Running memory action {action}",
        "cmd.artifacts.days": "days must be between 0 and 3650",
        "cmd.artifacts.gc": "Processing artifact GC",
        "cmd.stage.busy": "Core is disconnected or a task is running; cannot stage",
        "cmd.stage.placeholder": "<file path>",
        "cmd.stage.confirm": (
            "Stage only the explicitly selected current changes: {paths}\n"
            "Confirm with /stage <path...> --yes"
        ),
        "cmd.stage.running": "Staging selected changes",
        "cmd.commit.busy": "Core is disconnected or a task is running; cannot commit",
        "cmd.commit.placeholder": "<commit subject>",
        "cmd.commit.confirm": (
            "Create a local commit from staged changes (not pushed; repository hooks skipped): "
            "{subject}\nConfirm with /commit <subject> --yes"
        ),
        "cmd.commit.running": "Creating local commit",
        "cmd.worker.invalid": "Invalid Worker arguments: {error}",
        "cmd.worker.missing_arg": "{option} is missing an argument",
        "cmd.worker.budget_positive": "--budget must be a positive integer",
        "cmd.worker.unknown_arg": "Unknown Worker start argument: {option}",
        "cmd.worker.empty_prompt": "Worker prompt cannot be empty",
        "cmd.worker.loading": "Loading Workers",
        "cmd.worker.starting": "Starting Worker",
        "cmd.worker.status": "Loading Worker status",
        "cmd.worker.retry_confirm": "Confirm with /workers retry {worker} --yes",
        "cmd.worker.retrying": "Retrying Worker",
        "cmd.worker.events": "Loading Worker events",
        "cmd.worker.followup": "Sending Worker instruction",
        "cmd.worker.cancel_confirm": "Confirm with /workers cancel {worker} --yes",
        "cmd.worker.cancelling": "Cancelling Worker",
        "cmd.worker.reviewing": "Reviewing Worker handoff",
        "cmd.worker.digest_required": (
            "Approval requires the 64-character digest returned by the full preview."
        ),
        "cmd.worker.review_confirm": (
            "Review does not merge automatically. Confirm with "
            "/workers review {worker} {decision} --yes"
        ),
        "cmd.worker.review_recording": "Recording Worker review",
        "cmd.worker.digest_invalid": (
            "Worker digest must be the 64-character lowercase hexadecimal value from review."
        ),
        "cmd.worker.apply_confirm": (
            "This applies the reviewed changes to the current workspace. Confirm with "
            "/workers apply {worker} {digest} --yes"
        ),
        "cmd.worker.applying": "Applying Worker handoff",
        "cmd.worker.usage": (
            "Usage: /workers [start [--profile P] [--route R] [--model M] [--budget N] "
            "[--file PATH | --write-root PATH] <prompt> | status <id> | retry <id> --yes | "
            "peek <id> [cursor] | followup <id> <message> | cancel <id> --yes | "
            "review <id> [approve|reject] [digest] --yes | apply <id> <digest> --yes]"
        ),
        "cmd.jobs.loading": "Loading background jobs",
        "cmd.jobs.output": "Loading job output",
        "cmd.jobs.cancel_confirm": (
            "This cancels job {id}; confirm with /jobs cancel {id} --yes"
        ),
        "cmd.jobs.cancelling": "Cancelling job",
        "cmd.jobs.usage": "Usage: /jobs [show <id> | cancel <id> --yes]",
        "cmd.workflow.loading": "Loading {command}",
        "cmd.hooks.rerun_usage": "Usage: /hooks rerun <hook_id> --yes",
        "cmd.hooks.rerun_confirm": (
            "This reruns hook {id}; confirm with /hooks rerun {id} --yes"
        ),
        "cmd.hooks.rerunning": "Rerunning hook",
        "cmd.hooks.usage": "Usage: /hooks or /hooks rerun <hook_id> --yes",
        "cmd.hooks.loading": "Loading hooks",
        "app.workflow.started": "Workflow started",
        "help.keys": "Keys",
        "help.commands": "Commands",
        "help.enter": "Send; Shift/Alt+Enter or Ctrl+J inserts a newline",
        "help.history": "Recall workspace input history when the composer is empty",
        "help.mode": "Cycle Act → Operate → Plan",
        "help.permission": "Cycle ask → auto-review → full-access",
        "help.cancel": "Copy selection; press again with no selection to cancel",
        "help.copy": "Copy the selection or previous response",
        "help.scroll": "Jump to the bottom of the timeline",
        "help.quit": "Quit the TUI; sessions are preserved",
        "help.palette": "Open the categorized command palette",
        "help.footer": "Type / for completion; unknown /names are sent as skills",
        "header.repo": "repo",
        "header.session": "session",
        "header.route": "route",
        "header.model": "model",
        "header.permission": "permission",
        "header.trust": "trust",
        "header.goal": "goal",
        "header.state.ready": "ready",
        "header.state.running": "running",
        "header.state.planning": "planning",
        "header.state.plan": "plan",
        "header.state.plan ready": "review",
        "header.state.disconnected": "disconnected",
        "header.state.connecting": "connecting",
        "palette.title": "Command Palette",
        "palette.search": "Search: {query}",
        "palette.search.empty": "type to filter",
        "palette.no_results": "No matching commands",
        "palette.more": "… {count} more; keep typing to narrow",
        "palette.hint": "↑/↓ navigate · Enter select · Esc close",
        "palette.category.task": "Tasks",
        "palette.category.session": "Sessions",
        "palette.category.review": "Review",
        "palette.category.model": "Models",
        "palette.category.security": "Security",
        "palette.category.extension": "Extensions",
        "palette.category.labs": "Labs",
        "changes.title": "Change Center",
        "changes.file_count": "{count} file(s)",
        "changes.empty": "No workspace changes.",
        "changes.files": "Files",
        "changes.navigation": "j/k file · n/p hunk",
        "changes.conflict": "CONFLICT",
        "changes.more_files": "… {count} more file(s)",
        "changes.no_hunk": "No textual hunk (untracked, binary, or truncated diff).",
        "changes.hunk_position": "hunk {position}/{total}",
        "changes.more_hunk_lines": "… {count} more line(s)",
        "changes.conflicts_block": "Conflicts block completion:",
        "changes.verification_failed": "Verification failed; do not report completion.",
        "changes.verification_unavailable": "Unverified: no durable verification receipt.",
        "changes.unverified_count": "Unverified: {count} changed file(s) lack passing evidence.",
        "changes.fully_verified": "All changed files have passing verification evidence.",
        "changes.diff_truncated": "Diff truncated; narrow the path before review.",
        "changes.verification": "Verification",
        "changes.none_recorded": "none recorded",
        "changes.verification_mapping": "command → result → paths",
        "changes.scope_unavailable": "scope unavailable",
        "changes.more_verifications": "… {count} more verification result(s)",
        "changes.close_hint": (
            "Esc close · j/k files · n/p hunks · /stage and /commit still require --yes"
        ),
        "attachments.title": "Pending attachments",
        "attachments.empty": "No pending images.",
        "attachments.item": "#{index} {width}x{height} · {size} · {digest}",
        "attachments.added": "Attached image #{index} · {width}x{height} · {digest}",
        "attachments.limit": "A message can include at most 8 images",
        "attachments.too_large": "Image exceeds 2 MiB; compress or crop it first",
        "attachments.removed": "Removed attachment #{index}.",
        "attachments.cleared": "Cleared pending attachments.",
        "attachments.usage": "Usage: /attachments [remove <number>|clear]",
        "command.help": "Show shortcuts and commands",
        "command.sessions": "Open the searchable session picker",
        "command.new": "Create a session",
        "command.rename": "Rename the current session",
        "command.fork": "Fork the current session",
        "command.export": "Export the current session",
        "command.delete": "Delete the current session (--yes)",
        "command.provider": "Inspect or switch provider routes",
        "command.model": "Inspect or switch models",
        "command.doctor": "Diagnose the active provider route",
        "command.config": "Configure API, model, or credential",
        "command.compact": "Compact context",
        "command.copy": "Copy the last response",
        "command.history": "Manage workspace input history",
        "command.language": "Switch the UI language",
        "command.attachments": "Inspect or remove pending images",
        "command.plan": "Plan read-only, then review before acting",
        "command.review": "Review current changes read-only",
        "command.goal": "Run and manage a durable goal",
        "command.mode": "Inspect or switch work mode",
        "command.permissions": "Inspect or switch permissions",
        "command.trust": "Inspect or change workspace trust",
        "command.sandbox": "Inspect OS isolation",
        "command.tasks": "Inspect tasks from the latest run",
        "command.workers": "Inspect, review, or apply workers",
        "command.workflow": "Inspect or start workflows",
        "command.changes": "Open the navigable Change Center",
        "command.diff": "Open Change Center (compatibility alias)",
        "command.stage": "Stage selected files (--yes)",
        "command.commit": "Create a local commit (--yes)",
        "command.rewind": "Preview and confirm a safe rewind",
        "command.context": "Inspect context and usage",
        "command.cost": "Inspect session cost",
        "command.turn": "Inspect route, usage, approvals, and receipt",
        "command.skills": "List or manage skills",
        "command.mcp": "Inspect MCP servers and tools",
        "command.hooks": "Inspect hook configuration and runs",
        "command.memory": "Inspect or manage project memory",
        "command.jobs": "Background job center",
        "command.artifacts": "Inspect artifacts or run reference-aware GC",
        "usage.model": "<model ID>|add <model ID>",
        "usage.attachments": "remove <number>|clear",
        "usage.goal": (
            "create [boundary options] <objective>|status|list|pause|resume|edit|"
            "complete|cancel --yes"
        ),
        "usage.commit": "<commit subject> --yes",
        "configuration.discovery_failed": (
            "Model discovery failed · diagnostic ID {diagnostic_id}. "
            "Check credentials and network access, then retry."
        ),
        "result.title.success": "Task completed",
        "result.title.failed": "Task failed",
        "result.title.interrupted": "Task interrupted",
        "result.title.incomplete": "Task incomplete",
        "result.title.content_filtered": "Model content filtered",
        "result.title.transport_error": "Model transport interrupted",
        "result.title.unknown": "Task finished",
        "result.meta": "Duration {duration} · {steps} steps",
        "result.route": "Route {route} · model {model} · cost {cost}",
        "result.changes": "Changed {count} files: {files}",
        "result.changes.none": "Changed 0 files",
        "result.changes.unknown": "Changed files: evidence unavailable",
        "result.line_stats.known": "Lines +{additions}/-{deletions}",
        "result.line_stats.partial": "Known lines +{additions}/-{deletions} · totals unknown",
        "result.line_stats.unknown": "Lines unknown",
        "result.verification.pass": "Verification passed · {passed}/{total} gates",
        "result.verification.fail": "Verification failed · {passed}/{total} gates",
        "result.verification.unknown": "Verification evidence unavailable",
        "result.unverified": "Unverified: {items}",
        "result.failure": "Failure class: {failure}",
        "result.actions": "Open  /changes · /review · /rewind · /turn {turn}",
    },
}


# 把系统或用户提供的语言标签收敛为产品支持的规范语言
def normalize_locale(value: str | None) -> str:
    normalized = str(value or "").strip().replace("_", "-").casefold()
    if normalized == "en" or normalized.startswith("en-"):
        return "en-US"
    return "zh-CN"


# 返回用户级 TUI 偏好文件位置，不把界面状态写进仓库
def locale_settings_path(state_root: Path | None = None) -> Path:
    root = state_root or (Path.home() / ".coderook")
    return root / "ui.json"


# 从用户偏好文件读取规范语言，损坏或未知内容按未设置处理
def load_saved_locale(path: Path | None = None) -> str | None:
    target = path or locale_settings_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("locale")
    if not isinstance(raw, str) or raw not in {"zh-CN", "en-US"}:
        return None
    return raw


# 原子保存用户选择的规范语言并保留同文件中的其他偏好
def save_locale(value: str, path: Path | None = None) -> str:
    locale = normalize_locale(value)
    target = path or locale_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = {}
    payload = dict(existing) if isinstance(existing, dict) else {}
    payload["locale"] = locale
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return locale


# 从显式设置和标准区域环境变量中选择首次启动语言
def detect_locale(
    environment: dict[str, str] | None = None,
    *,
    settings_path: Path | None = None,
) -> str:
    env = os.environ if environment is None else environment
    configured = env.get("CODEROOK_LOCALE")
    if configured:
        return normalize_locale(configured)
    saved = load_saved_locale(settings_path) if settings_path is not None else None
    if environment is None and settings_path is None:
        saved = load_saved_locale()
    if saved is not None:
        return saved
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = env.get(name)
        if value:
            return normalize_locale(value.split(".", 1)[0])
    return "zh-CN"


# 按语言和键读取集中式产品文案，缺失翻译时回退中文
def tr(key: str, locale: str = "zh", **values: object) -> str:
    language = "en" if normalize_locale(locale) == "en-US" else "zh"
    template = _TEXT[language].get(key, _TEXT["zh"].get(key, key))
    return template.format(**values)


# 从错误类别与原始异常生成可关联但不暴露正文的短诊断编号
def diagnostic_id(category: str, error: BaseException | str | None = None) -> str:
    material = f"{category}:{type(error).__name__}:{error!s}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:10].upper()


class ReadinessCard(Static):
    """非阻塞配置状态卡；不会自动打开配置界面。"""

    # 根据统一 readiness 快照生成首次启动或提交前提示
    def __init__(self, readiness: Any, *, locale: str = "zh") -> None:
        status = str(getattr(readiness, "status", "unconfigured"))
        if status not in {
            "unconfigured",
            "configuration_invalid",
            "credential_missing",
            "endpoint_unreachable",
            "provider_unverified",
        }:
            status = "unconfigured"
        lines = [
            f"[bold yellow]{escape(tr(f'readiness.{status}.title', locale))}[/bold yellow]",
            f"[dim]{escape(tr(f'readiness.{status}.body', locale))}[/dim]",
        ]
        route = str(getattr(readiness, "route_id", "") or "")
        model = str(getattr(readiness, "model", "") or "")
        if route or model:
            lines.append(
                "[dim]"
                + escape(
                    tr(
                        "readiness.route",
                        locale,
                        route=route or "-",
                        model=model or "-",
                    )
                )
                + "[/dim]"
            )
        lines.append(f"[cyan]{escape(tr(f'readiness.actions.{status}', locale))}[/cyan]")
        super().__init__("\n".join(lines), classes="product-card readiness-card")


class SafeErrorCard(Static):
    """只展示错误类别、恢复动作和诊断 ID，不渲染原异常。"""

    # 构建脱敏错误卡并选择对应恢复动作
    def __init__(
        self,
        category: str,
        diagnostic: str,
        *,
        action: str | None = None,
        locale: str = "zh",
    ) -> None:
        action_key = action or category
        if f"error.action.{action_key}" not in _TEXT["zh"]:
            action_key = "generic"
        lines = [
            f"[bold red]{escape(tr('error.title', locale))}[/bold red]",
            "[dim]"
            + escape(tr("error.category", locale, category=category))
            + " · "
            + escape(tr("error.diagnostic", locale, diagnostic_id=diagnostic))
            + "[/dim]",
            f"[cyan]{escape(tr(f'error.action.{action_key}', locale))}[/cyan]",
        ]
        super().__init__("\n".join(lines), classes="product-card error-card")


@dataclass
class RunResult:
    """面向结果卡的统一、可测试视图模型。"""

    run_id: str
    status: str
    duration: str
    steps: int
    route: str
    model: str
    cost: str
    files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    line_stats_unknown: bool = True
    verification_status: str = "unknown"
    verification_passed: int = 0
    verification_total: int = 0
    unverified: list[str] = field(default_factory=list)
    failure: str = ""


@dataclass
class _RunEvidence:
    """事件流中的临时运行证据，最终由 durable receipt 覆盖。"""

    started_at: str = ""
    finished_at: str = ""
    route: str = ""
    model: str = ""
    steps: int = 0
    verification: list[dict[str, Any]] = field(default_factory=list)


# 把 ISO 时间戳转换成 datetime，解析失败时返回 None
def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# 把起止时间格式化为紧凑时长
def _format_duration(started: object, finished: object) -> str:
    start = _parse_time(started)
    end = _parse_time(finished)
    if start is None or end is None:
        return "unknown"
    seconds = max((end - start).total_seconds(), 0.0)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder}s"


# 从验证收据中归并总数、通过数和最终状态
def _verification_summary(
    evidence: list[dict[str, Any]],
) -> tuple[str, int, int]:
    if not evidence:
        return "unknown", 0, 0
    total = 0
    passed = 0
    failed = False
    unknown = False
    for item in evidence:
        gate_count = int(item.get("gate_count", 0) or 0)
        item_passed = int(item.get("passed", 0) or 0)
        total += max(gate_count, item_passed)
        passed += item_passed
        verdict = str(item.get("verdict", item.get("status", ""))).casefold()
        failed = failed or verdict in {"fail", "failed", "error"}
        unknown = unknown or verdict in {"", "unavailable", "timeout", "truncated"}
    if failed:
        return "fail", passed, total
    if unknown:
        return "unknown", passed, total
    return "pass", passed, total


# 只允许稳定分类码进入结果卡，任意异常正文统一折叠为 runtime_failure
def _safe_failure_class(value: object) -> str:
    failure = str(value or "").strip()
    if not failure:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", failure) is None:
        return "runtime_failure"
    return failure


class RunEvidenceReducer:
    """先归并实时事件，再让 durable turn receipt 成为最终权威。"""

    # 初始化按 run 隔离的临时证据表
    def __init__(self) -> None:
        self._runs: dict[str, _RunEvidence] = {}

    # 消费一次运行事件，忽略没有 run_id 的 daemon 或 session 事件
    def consume(self, event: dict[str, Any]) -> None:
        run_id = event.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return
        state = self._runs.setdefault(run_id, _RunEvidence())
        event_type = str(event.get("type", ""))
        if event_type == "run.started":
            state.started_at = str(event.get("ts", ""))
        elif event_type == "run.finished":
            state.finished_at = str(event.get("ts", ""))
            state.steps = int(event.get("steps", 0) or 0)
        elif event_type == "llm.route_selected":
            state.route = str(event.get("route_id", ""))
            state.model = str(event.get("model", ""))
        elif event_type.startswith("verification."):
            state.verification.append(dict(event))

    # 丢弃已离开会话的临时运行证据，避免延迟结果卡跨会话出现
    def discard(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    # 用 inspector 的 durable receipt 覆盖实时证据并生成最终结果模型
    def finalize(
        self,
        finish_event: dict[str, Any],
        inspection: dict[str, Any] | None = None,
    ) -> RunResult:
        run_id = str(finish_event.get("run_id", ""))
        state = self._runs.pop(run_id, _RunEvidence())
        payload = inspection or {}
        turn_raw = payload.get("turn", {})
        receipt_raw = payload.get("receipt", {})
        turn = dict(turn_raw) if isinstance(turn_raw, dict) else {}
        receipt = dict(receipt_raw) if isinstance(receipt_raw, dict) else {}
        route_raw = receipt.get("route") or turn.get("route") or {}
        route = dict(route_raw) if isinstance(route_raw, dict) else {}
        outcome_raw = str(
            receipt.get("outcome") or finish_event.get("outcome") or ""
        ).casefold()
        if outcome_raw:
            status = {
                "completed": "success",
                "tool_use": "incomplete",
                "length": "incomplete",
                "incomplete": "incomplete",
                "content_filtered": "content_filtered",
                "failed": "failed",
                "cancelled": "interrupted",
                "transport_error": "transport_error",
            }.get(outcome_raw, "unknown")
        else:
            status_raw = str(
                receipt.get("status")
                or turn.get("status")
                or finish_event.get("status", "")
            ).casefold()
            status = {
                "completed": "success",
                "success": "success",
                "failed": "failed",
                "error": "failed",
                "interrupted": "interrupted",
                "cancelled": "interrupted",
            }.get(status_raw, "unknown")
        verification_raw = receipt.get("verification", [])
        verification = (
            [dict(item) for item in verification_raw if isinstance(item, dict)]
            if isinstance(verification_raw, list)
            else []
        )
        if not verification:
            verification = list(state.verification)
        verification_status, passed, total = _verification_summary(verification)
        unavailable_raw = receipt.get("unavailable", [])
        unavailable = (
            [str(item) for item in unavailable_raw] if isinstance(unavailable_raw, list) else []
        )
        if inspection is None:
            unavailable = ["route", "cost", "files_changed"]
        if verification_status == "unknown" and "verification" not in unavailable:
            unavailable.append("verification")
        changes_raw = receipt.get("changes", [])
        changes = (
            [dict(item) for item in changes_raw if isinstance(item, dict)]
            if isinstance(changes_raw, list)
            else []
        )
        files_raw = receipt.get("files_changed", [])
        files = (
            [str(item.get("path", "")) for item in changes if item.get("path")]
            if changes
            else ([str(item) for item in files_raw] if isinstance(files_raw, list) else [])
        )
        known_additions = sum(
            int(item["additions"])
            for item in changes
            if isinstance(item.get("additions"), int)
            and not isinstance(item.get("additions"), bool)
        )
        known_deletions = sum(
            int(item["deletions"])
            for item in changes
            if isinstance(item.get("deletions"), int)
            and not isinstance(item.get("deletions"), bool)
        )
        line_stats_unknown = "change_line_stats" in unavailable or not changes or any(
            not isinstance(item.get(field), int) or isinstance(item.get(field), bool)
            for item in changes
            for field in ("additions", "deletions")
        )
        failure = _safe_failure_class(
            receipt.get("error_classification")
            or receipt.get("failure_category")
            or finish_event.get("failure_category")
            or finish_event.get("reason")
        )
        cost_raw = receipt.get("cost", "unknown")
        cost = str(cost_raw if cost_raw not in {None, ""} else "unknown")
        return RunResult(
            run_id=run_id,
            status=status,
            duration=_format_duration(
                receipt.get("started_at") or state.started_at,
                receipt.get("finished_at") or state.finished_at or finish_event.get("ts"),
            ),
            steps=int(finish_event.get("steps", state.steps) or 0),
            route=str(route.get("route_id") or state.route or "unknown"),
            model=str(route.get("model") or state.model or "unknown"),
            cost=cost,
            files=files,
            additions=known_additions,
            deletions=known_deletions,
            line_stats_unknown=line_stats_unknown,
            verification_status=verification_status,
            verification_passed=passed,
            verification_total=total,
            unverified=unavailable,
            failure=failure,
        )


# 截断卡片中的文件或分类文本，保持 80 列终端仍可扫读
def _clip(value: str, limit: int = 56) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


class RunResultCard(Static):
    """展示一次运行的结果、证据缺口和后续审查入口。"""

    # 把统一结果模型渲染为不依赖宽屏的多行卡片
    def __init__(self, result: RunResult, *, locale: str = "zh") -> None:
        title_key = f"result.title.{result.status}"
        color = {
            "success": "green",
            "failed": "red",
            "interrupted": "yellow",
            "incomplete": "yellow",
            "content_filtered": "yellow",
            "transport_error": "red",
        }.get(result.status, "cyan")
        lines = [
            f"[bold {color}]{escape(tr(title_key, locale))}[/bold {color}]  "
            f"[dim]{escape(result.run_id)}[/dim]",
            escape(
                tr(
                    "result.meta",
                    locale,
                    duration=result.duration,
                    steps=result.steps,
                )
            ),
            escape(
                tr(
                    "result.route",
                    locale,
                    route=_clip(result.route, 24),
                    model=_clip(result.model, 28),
                    cost=result.cost,
                )
            ),
        ]
        if "files_changed" in result.unverified:
            lines.append(escape(tr("result.changes.unknown", locale)))
        elif result.files:
            files = _clip(", ".join(result.files))
            lines.append(
                escape(
                    tr(
                        "result.changes",
                        locale,
                        count=len(result.files),
                        files=files,
                    )
                )
            )
        else:
            lines.append(escape(tr("result.changes.none", locale)))
        if result.line_stats_unknown:
            stats_key = (
                "result.line_stats.partial"
                if result.additions or result.deletions
                else "result.line_stats.unknown"
            )
        else:
            stats_key = "result.line_stats.known"
        lines.append(
            escape(
                tr(
                    stats_key,
                    locale,
                    additions=result.additions,
                    deletions=result.deletions,
                )
            )
        )
        lines.append(
            escape(
                tr(
                    f"result.verification.{result.verification_status}",
                    locale,
                    passed=result.verification_passed,
                    total=result.verification_total,
                )
            )
        )
        if result.unverified:
            unverified = tr(
                "result.unverified",
                locale,
                items=_clip(", ".join(result.unverified)),
            )
            lines.append(f"[yellow]{escape(unverified)}[/yellow]")
        if result.failure:
            lines.append(
                f"[red]{escape(tr('result.failure', locale, failure=_clip(result.failure)))}[/red]"
            )
        lines.append(f"[cyan]{escape(tr('result.actions', locale, turn=result.run_id))}[/cyan]")
        super().__init__("\n".join(lines), classes="product-card result-card")
