from __future__ import annotations

from code_rook.tui.commands import BUILTIN_SLASH_COMMANDS, match_slash_command


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
        ("plan", "只读规划并审阅后再实施：/plan [任务]"),
        ("mode", "查看或切换工作模式：plan|act|operate"),
        ("permissions", "查看或切换权限模式"),
        ("trust", "查看或授予/撤销工作区信任"),
        ("sandbox", "查看 OS 隔离能力（仅探测）"),
        ("tasks", "查看最近一次 run 的任务"),
        ("workers", "查看全部持久 Worker 与 Fleet"),
        ("workflow", "查看、启动或检查 workflow"),
        ("diff", "查看工作区改动"),
        ("rewind", "从安全恢复点回滚文件"),
        ("context", "查看上下文占用与用量"),
        ("cost", "查看本会话成本分解与缓存节省"),
        ("turn", "检查 route、用量、审批与收据"),
        ("skills", "列出、查看、安装或删除 skills"),
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