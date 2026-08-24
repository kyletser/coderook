from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

import pytest

from code_rook.tui.commands import (
    BUILTIN_SLASH_COMMANDS,
    _cmd_goal,
    _cmd_workers,
    _parse_goal_create_args,
    _parse_worker_start,
    match_slash_command,
    visible_slash_commands,
)


# 功能：Worker start 命令解析角色、route、模型、预算和显式写入范围
# 设计：混合引号 prompt 与重复 scope 参数，断言默认只读只会被真实写 claim 关闭
def test_parse_worker_start_contract() -> None:
    read_only = _parse_worker_start(
        '--profile reviewer --route fast --model coder "inspect repo"'
    )
    writing = _parse_worker_start(
        '--budget 500 --file src/a.py --write-root tests "fix bug"'
    )

    assert read_only["prompt"] == "inspect repo"
    assert read_only["read_only"] is True
    assert read_only["profile"] == "reviewer"
    assert read_only["route_id"] == "fast"
    assert read_only["model"] == "coder"
    assert writing["read_only"] is False
    assert writing["exact_files"] == ["src/a.py"]
    assert writing["write_roots"] == ["tests"]
    assert writing["token_budget"] == 500


# 功能：验证 match_slash_command 精确匹配内建命令名与带参数形式，且不误吞未知前缀
# 设计：分别断言 /x 与 /x arg 命中同一命令，/xarg 与尾随无空格的不匹配
def test_match_slash_command_matches_name_and_args() -> None:
    by_name = {cmd.name: cmd for cmd in BUILTIN_SLASH_COMMANDS}
    assert len(by_name) == len(BUILTIN_SLASH_COMMANDS)  # 无重名

    for name in by_name:
        assert match_slash_command(f"/{name}") is by_name[name]
        assert match_slash_command(f"/{name} some args") is by_name[name]

    assert match_slash_command("/providerx") is None
    assert match_slash_command("/plan") is not None
    assert match_slash_command("/") is None
    assert match_slash_command("") is None


# 功能：验证内建命令注册表覆盖补全弹窗所需的全部历史命令，补全列表与现状逐条一致
# 设计：把旧版硬编码补全列表与注册表比对，杜绝"补全与分发两处维护"回退为不一致
def test_builtin_commands_cover_previous_completion_list() -> None:
    previous = [
        ("help", "显示键位与全部命令"),
        ("sessions", "打开会话选择器（输入即过滤）"),
        ("new", "新建会话"),
        ("rename", "重命名当前会话：/rename <标题>"),
        ("fork", "复制当前会话为分支：/fork [标题]"),
        ("export", "导出当前会话：/export [md|json]"),
        ("delete", "删除当前会话（需 --yes 确认）"),
        ("provider", "查看或切换 Provider route"),
        ("model", "查看或切换模型"),
        ("doctor", "诊断活动 Provider route"),
        ("config", "更换 LLM API、模型或密钥"),
        ("compact", "手动压缩上下文"),
        ("copy", "复制上一条回复"),
        ("history", "当前工作区输入历史：查看、开关或清空"),
        ("language", "切换界面语言：中文或 English"),
        ("attachments", "查看或移除待发送图片"),
        ("plan", "只读规划并审阅后再实施：/plan [任务]"),
        ("review", "只读复审当前改动：/review [关注点]"),
        ("goal", "持续执行并管理持久目标"),
        ("mode", "查看或切换工作模式：plan|act|operate"),
        ("permissions", "查看或切换权限模式"),
        ("trust", "查看或授予/撤销工作区信任"),
        ("sandbox", "查看 OS 隔离能力（仅探测）"),
        ("tasks", "查看最近一次 run 的任务"),
        ("workers", "查看、审查或应用持久 Worker"),
        ("workflow", "查看、启动或检查 workflow"),
        ("changes", "打开可导航的改动中心"),
        ("diff", "查看工作区改动"),
        ("stage", "选择文件加入 Git index（需 --yes）"),
        ("commit", "从已 stage 改动创建本地 commit（需 --yes）"),
        ("rewind", "预览并二次确认安全恢复点"),
        ("context", "查看上下文占用与用量"),
        ("cost", "查看本会话成本分解与缓存节省"),
        ("turn", "检查 route、用量、审批与收据"),
        ("skills", "列出、查看、安装或删除 skills"),
        ("mcp", "查看 MCP server 状态与工具"),
        ("hooks", "查看 hook 配置与执行记录"),
        ("memory", "查看、编辑并控制项目记忆"),
        ("jobs", "后台任务中心：查看/取消"),
        ("artifacts", "查看产物或执行引用感知 GC"),
    ]
    actual = [(cmd.name, cmd.description) for cmd in BUILTIN_SLASH_COMMANDS]
    assert actual == previous


# 功能：验证每个命令是否提供可调用的 handler 且 need_connection 取值合法
# 设计：调用 handler 前先以 None 占位探测函数签名，仅验证结构而非执行行为
def test_builtin_commands_all_have_handlers() -> None:
    for cmd in BUILTIN_SLASH_COMMANDS:
        assert callable(cmd.handler)
        assert isinstance(cmd.needs_connection, bool)
        assert cmd.name and cmd.description


# 功能：验证 Labs 命令默认不进入补全列表，显式启用后才出现
# 设计：比较同一注册表的稳定视图和完整视图，保留直接匹配作为旧命令兼容入口
def test_labs_commands_are_hidden_until_explicitly_enabled() -> None:
    stable = {command.name for command in visible_slash_commands()}
    enabled = {
        command.name for command in visible_slash_commands(labs_enabled=True)
    }

    assert {"workflow", "hooks"}.isdisjoint(stable)
    assert {"workflow", "hooks"}.issubset(enabled)
    assert match_slash_command("/workflow") is not None


# 功能：验证 Goal 创建选项会映射为完整 typed IPC 参数并保留重复完成标准
# 设计：组合全部边界选项、约束与 `--` 位置分隔符，直接断言模型校验后的参数字典
def test_parse_goal_create_args_builds_typed_ipc_payload() -> None:
    objective, params = _parse_goal_create_args(
        '--no-auto-continue --max-auto-turns 5 --max-wall-seconds 900 '
        '--token-budget 12000 --criterion "tests pass" '
        '--criterion "lint passes" --constraint "do not push" -- Ship release',
        "sess-goal",
    )

    assert objective == "Ship release"
    assert params == {
        "session_id": "sess-goal",
        "objective": "Ship release",
        "token_budget": 12000,
        "auto_continue": False,
        "max_auto_turns": 5,
        "max_wall_seconds": 900,
        "constraints": ["do not push"],
        "completion_criteria": ["tests pass", "lint passes"],
        "start": True,
    }
    windows_objective, _windows_params = _parse_goal_create_args(
        "修复 C:\\repo\\src\\app.py",
        "sess-goal",
    )
    assert windows_objective == "修复 C:\\repo\\src\\app.py"


# 功能：验证 Goal 创建拒绝未知、重复、越界、非整数、空目标和未闭合引号
# 设计：逐项经过同一解析入口并核对用户可定位的错误片段，避免非法值到达 Core
def test_parse_goal_create_args_rejects_invalid_boundaries() -> None:
    cases = [
        ("--unknown x ship", "未知 Goal 参数"),
        ("--token-budget 1 --token-budget 2 ship", "只能指定一次"),
        ("--max-auto-turns 0 ship", "greater than or equal to 1"),
        ("--max-auto-turns 1.5 ship", "必须是正整数"),
        ("--max-wall-seconds 86401 ship", "less than or equal to 86400"),
        ("--auto-continue --no-auto-continue ship", "只能指定一次"),
        ("--token-budget 10", "objective"),
        ('--criterion "unfinished', "参数引号不完整"),
    ]

    for raw, message in cases:
        with pytest.raises(ValueError, match=message):
            _parse_goal_create_args(raw, "sess-goal")


# 功能：验证 Goal 命令把边界参数交给启动 worker，并要求取消操作显式确认
# 设计：用最小 App 替身捕获 begin 调用和提示，覆盖参数化创建、无确认取消与确认后的 clear 映射
async def test_goal_command_creation_and_cancel_contract() -> None:
    class _TextArea:
        # 初始化 Goal 命令需要清空的输入框文本
        def __init__(self) -> None:
            self.text = ""

    class _App:
        # 初始化连接、会话和 Goal 动作捕获槽
        def __init__(self) -> None:
            self._client = object()
            self._session_id = "sess-goal"
            self._busy = False
            self.begins: list[tuple[object, ...]] = []
            self.actions: list[tuple[str, str, str]] = []
            self.notices: list[str] = []
            self.pending: list[Coroutine[Any, Any, None]] = []

        # 捕获 Goal 创建进入运行态时的参数与原始草稿
        def _begin_goal_command(
            self,
            _text_area: Any,
            action: str,
            objective: str,
            *,
            command_params: dict[str, object] | None,
            draft: str,
        ) -> None:
            self.begins.append((action, objective, command_params, draft))

        # 捕获非创建 Goal IPC 动作
        async def _do_goal_command(
            self,
            action: str,
            value: str,
            *,
            draft: str,
        ) -> None:
            self.actions.append((action, value, draft))

        # 收集命令提示文本
        def _append(self, widget: Any) -> None:
            self.notices.append(str(widget.render()))

        # 保存 Textual worker 协程供测试显式等待
        def run_worker(
            self,
            coroutine: Coroutine[Any, Any, None],
            *,
            name: str,
            exclusive: bool,
        ) -> None:
            del name, exclusive
            self.pending.append(coroutine)

    app = _App()
    text_area = _TextArea()
    command = (
        '/goal create --token-budget 500 --max-auto-turns 2 '
        '--max-wall-seconds 60 --criterion "tests pass" -- fix parser'
    )
    await _cmd_goal(app, text_area, command)  # type: ignore[arg-type]

    action, objective, params, draft = app.begins[-1]
    assert (action, objective, draft) == ("create", "fix parser", command)
    assert isinstance(params, dict)
    assert params["token_budget"] == 500
    assert params["max_auto_turns"] == 2
    assert params["max_wall_seconds"] == 60
    assert params["completion_criteria"] == ["tests pass"]

    await _cmd_goal(app, text_area, "/goal cancel")  # type: ignore[arg-type]
    assert not app.pending
    assert "/goal cancel --yes" in app.notices[-1]

    await _cmd_goal(app, text_area, "/goal cancel --yes")  # type: ignore[arg-type]
    await app.pending.pop()
    assert app.actions[-1] == ("clear", "", "/goal cancel --yes")


# 功能：验证 Worker review 直接生成摘要，apply 必须携带同一摘要和末尾 --yes
# 设计：用最小 App/TextArea 替身捕获异步动作，分别覆盖审查、缺确认提示、合法应用与非法摘要拒绝
async def test_workers_review_and_apply_command_contract() -> None:
    digest = "a" * 64

    class _TextArea:
        # 初始化命令处理所需的输入框状态
        def __init__(self) -> None:
            self.text = ""
            self.disabled = False
            self.border_title = ""

    class _App:
        # 初始化连接状态与命令捕获容器
        def __init__(self) -> None:
            self._client = object()
            self._session_id = "sess-1"
            self.actions: list[tuple[object, ...]] = []
            self.notices: list[str] = []
            self.pending: list[Coroutine[Any, Any, None]] = []

        # 捕获命令提示文本
        def _append(self, widget: Any) -> None:
            self.notices.append(str(widget.render()))

        # 记录 review 动作参数
        async def _do_worker_review(
            self,
            worker_id: str,
            approved: bool,
            *,
            confirmed: bool = False,
            expected_digest: str = "",
        ) -> None:
            self.actions.append(
                ("review", worker_id, approved, confirmed, expected_digest)
            )

        # 记录 apply 动作参数
        async def _do_worker_apply(self, worker_id: str, expected_digest: str) -> None:
            self.actions.append(("apply", worker_id, expected_digest))

        # 保存 Textual worker 协程供测试显式等待
        def run_worker(
            self,
            coroutine: Coroutine[Any, Any, None],
            *,
            name: str,
            exclusive: bool,
        ) -> None:
            del name, exclusive
            self.pending.append(coroutine)

    app = _App()
    text_area = _TextArea()

    await _cmd_workers(app, text_area, "/workers review worker-1")  # type: ignore[arg-type]
    await app.pending.pop()
    assert app.actions == [("review", "worker-1", True, False, "")]

    await _cmd_workers(  # type: ignore[arg-type]
        app,
        text_area,
        f"/workers review worker-1 approve {digest} --yes",
    )
    await app.pending.pop()
    assert app.actions[-1] == ("review", "worker-1", True, True, digest)

    await _cmd_workers(  # type: ignore[arg-type]
        app,
        text_area,
        f"/workers apply worker-1 {digest}",
    )
    assert not app.pending
    assert f"/workers apply worker-1 {digest} --yes" in app.notices[-1]

    await _cmd_workers(  # type: ignore[arg-type]
        app,
        text_area,
        f"/workers apply worker-1 {digest} --yes",
    )
    await app.pending.pop()
    assert app.actions[-1] == ("apply", "worker-1", digest)

    await _cmd_workers(  # type: ignore[arg-type]
        app,
        text_area,
        "/workers apply worker-1 NOT-A-DIGEST --yes",
    )
    assert not app.pending
    assert "64 位小写十六进制" in app.notices[-1]
