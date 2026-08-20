from __future__ import annotations

import sys
from pathlib import Path

from examples.automated_fix import build_command as build_fix_command
from examples.read_only_review import build_command as build_review_command

from code_rook.core.hooks import HookManager, load_hook_configs
from code_rook.core.mcp.client import McpClient
from code_rook.core.skills import SkillLoader, SkillManager

_ROOT = Path(__file__).resolve().parents[2]
_MCP_SERVER = _ROOT / "examples" / "mcp_echo_server.py"
_SKILL_SOURCE = _ROOT / "examples" / "skills" / "focused-fix"
_HOOK_SCRIPT = _ROOT / "examples" / "hooks" / "guard_sensitive_files.py"
_HOOK_CONFIG = _ROOT / "examples" / "hooks" / "hooks.toml"


# 功能：验证只读示例仅显式放行读取、搜索和 diff 工具
# 设计：检查构造后的 argv 而不调用模型，确保文档示例可在普通 CI 中确定性验证
def test_read_only_review_example_has_no_write_allowlist() -> None:
    command = build_review_command("review")

    assert "edit_file" not in command
    assert "apply_patch" not in command
    assert command.count("--allow-tool") == 5
    assert "Repository" in command
    assert "stream-json" in command


# 功能：验证自动修复示例显式声明编辑、验证和有限提问策略
# 设计：直接检查 argv 中的关键工具与 preset 参数，避免启动 daemon 或消耗模型费用
def test_automated_fix_example_declares_controlled_tools() -> None:
    command = build_fix_command("fix")

    assert "edit_file" in command
    assert "apply_patch" in command
    assert "Bash.run" in command
    assert command[command.index("--question-mode") + 1] == "preset"


# 功能：验证最小 MCP 示例能被真实 CodeRook stdio 客户端发现并调用
# 设计：启动标准库子进程完成 initialize、tools/list 和 tools/call，覆盖真实换行帧与进程回收
async def test_mcp_echo_example_roundtrip() -> None:
    client = McpClient()
    await client.connect_stdio(sys.executable, [str(_MCP_SERVER)])
    try:
        tools = await client.list_tools()
        result = await client.call_tool("echo", {"text": "hello"})
    finally:
        await client.close()

    assert [tool.name for tool in tools] == ["echo"]
    assert result == "hello"


# 功能：验证 focused-fix 示例能经受管安装、digest 校验并替换调用参数
# 设计：使用真实 SkillManager 安装到隔离项目，再由 SkillLoader 解析和渲染，覆盖用户复制的完整路径
def test_focused_fix_skill_example_installs_and_renders(tmp_path: Path) -> None:
    user_dir = tmp_path / "user-skills"
    manager = SkillManager(tmp_path, user_skills_dir=user_dir)

    installed = manager.install(
        str(_SKILL_SOURCE),
        scope="project",
        trust="trusted",
        confirmed=True,
    )
    loaded = SkillLoader(tmp_path, user_skills_dir=user_dir).resolve("focused-fix")

    assert installed.integrity == "verified"
    assert loaded is not None
    assert loaded.integrity == "verified"
    assert loaded.allowed_tools == ["Repository", "File", "Git", "Run"]
    rendered = SkillLoader(tmp_path, user_skills_dir=user_dir).render_prompt(
        loaded,
        "fix auth regression",
    )
    assert "fix auth regression" in rendered
    assert "$ARGUMENTS" not in rendered


# 功能：验证敏感文件 Hook 示例能从真实项目配置加载并由子进程阻断或放行 File 调用
# 设计：复用示例 TOML，只把 Python 命令替换为当前解释器，连续执行 .env 与源码目标覆盖双分支
async def test_sensitive_file_hook_example_blocks_and_allows(tmp_path: Path) -> None:
    config_dir = tmp_path / ".coderook"
    config_dir.mkdir()
    (config_dir / "hooks.toml").write_text(
        _HOOK_CONFIG.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    configs = load_hook_configs(
        tmp_path,
        user_config=tmp_path / "missing-user-hooks.toml",
    )
    config = configs[0].model_copy(
        update={"command": (sys.executable, str(_HOOK_SCRIPT))}
    )
    manager = HookManager(
        [config],
        workspace=tmp_path,
        project_trust_provider=lambda _session_id: True,
    )

    blocked = await manager.emit(
        "tool_call_before",
        {
            "session_id": "sess-example",
            "tool_name": "File",
            "params": {"action": "write", "path": ".env"},
        },
    )
    allowed = await manager.emit(
        "tool_call_before",
        {
            "session_id": "sess-example",
            "tool_name": "File",
            "params": {"action": "edit", "path": "src/app.py"},
        },
    )

    assert blocked.blocked
    assert ".env" in blocked.reason
    assert not allowed.blocked
    assert [event.status for event in manager.audit_events()] == [
        "blocked",
        "completed",
    ]
