from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from code_rook.core.authority import RuntimeMode
from code_rook.core.background import BackgroundJobRegistry
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.openai_compatible import _to_openai_tools
from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.repository import (
    command_candidate_id,
    discover_test_commands,
    render_test_command,
)
from code_rook.core.runner import AgentRunner
from code_rook.core.task.manager import TaskManager
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.families import FileTool
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import ToolCaller, ToolCapability, ToolCatalogError
from code_rook.core.workspace import WorkspaceBoundary


# 从 File family schema 中提取按 oneOf 暴露的 action 名称
def _file_actions(schema: dict[str, object]) -> set[str]:
    input_schema = schema["input_schema"]
    assert isinstance(input_schema, dict)
    variants = input_schema["oneOf"]
    assert isinstance(variants, list)
    return {
        str(variant["properties"]["action"]["enum"][0])
        for variant in variants
    }


# 从 registry 的模型目录中返回 File schema
def _file_schema(registry: ToolRegistry) -> dict[str, object]:
    schemas = registry.tool_schemas()
    return next(schema for schema in schemas if schema["name"] == "File")


# 从 registry 的模型目录中返回 Git schema
def _git_schema(registry: ToolRegistry) -> dict[str, object]:
    schemas = registry.tool_schemas()
    return next(schema for schema in schemas if schema["name"] == "Git")


# 从 registry 的模型目录中返回 Run schema
def _run_schema(registry: ToolRegistry) -> dict[str, object]:
    schemas = registry.tool_schemas()
    return next(schema for schema in schemas if schema["name"] == "Run")


# 在隔离仓库中执行 Git 命令并返回标准输出
def _run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


# 按当前平台 shell 规则构造调用 Python 解释器的命令字符串
def _python_command(code: str) -> str:
    argv = [sys.executable, "-c", code]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


# 功能：验证默认 Runner 暴露原生文件工具，并保留 File family 供显式工具目录使用
# 设计：先检查默认小工具面，再显式选择 File 验证 family action 与兼容 backend 均未丢失
def test_runner_exposes_file_family_and_keeps_hidden_aliases(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert names == {"bash", "edit", "read", "write"}
    assert {
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "write_file",
        "edit_file",
        "apply_patch",
    }.isdisjoint(names)
    assert registry.get("read_file") is not None
    assert registry.get("apply_patch") is not None
    registry.set_model_tool_allowlist(frozenset({"File"}))
    assert _file_actions(_file_schema(registry)) == {
        "read",
        "list",
        "search_name",
        "search_content",
        "write",
        "edit",
        "patch",
    }


# 功能：验证默认根 Agent 工具面保持为四个原生编码工具且不超过显式上限
# 设计：通过 Runner 的独立装配器构建真实目录，固定模型首轮收到的小工具面
def test_runner_tool_assembly_keeps_default_surface_bounded(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    schemas = registry.tool_schemas()
    names = {str(schema["name"]) for schema in schemas}

    assert len(schemas) <= registry.model_tool_limit
    assert names == {"bash", "edit", "read", "write"}


# 功能：验证默认小工具面隐藏 Git family，但显式选择后仍保留五个只读 action
# 设计：同时检查默认目录、显式 family schema 和 git_diff replay backend 的兼容边界
def test_runner_exposes_git_family_and_hides_legacy_alias(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert "Git" not in names
    assert "git_diff" not in names
    assert registry.get("git_diff") is not None
    registry.set_model_tool_allowlist(frozenset({"Git"}))
    assert _file_actions(_git_schema(registry)) == {
        "status",
        "diff",
        "log",
        "show",
        "blame",
    }


# 功能：验证旧 git_diff whitelist 只映射为显式 Git.diff action
# 设计：构建单 action family，断言 schema 仍要求 action，避免 whitelist 绕过 action 权限
def test_legacy_git_whitelist_maps_to_explicit_diff_action(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        tool_whitelist=["git_diff"],
    )

    assert _file_actions(_git_schema(registry)) == {"diff"}
    try:
        registry.resolve_call("Git", {"path": "."})
    except ValueError as exc:
        assert "action is required" in str(exc)
    else:
        raise AssertionError("single-action Git family must require action")


# 功能：验证 Git family 五个只读 action 在真实仓库中都返回可用结果
# 设计：创建单提交并制造未提交改动，覆盖 status/diff/log/show/blame 的真实子进程适配路径
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_git_family_executes_all_read_actions(tmp_path: Path) -> None:
    _run_git(tmp_path, "init", "--initial-branch=main")
    _run_git(tmp_path, "config", "user.email", "coderook@example.invalid")
    _run_git(tmp_path, "config", "user.name", "CodeRook Test")
    target = tmp_path / "sample.txt"
    target.write_text("first\n", encoding="utf-8")
    _run_git(tmp_path, "add", "sample.txt")
    _run_git(tmp_path, "commit", "-m", "initial")
    target.write_text("first\nsecond\n", encoding="utf-8")
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    tool = registry.get("Git")
    assert tool is not None

    status = await tool.invoke({"action": "status"})
    diff = await tool.invoke({"action": "diff"})
    log = await tool.invoke({"action": "log", "limit": 1})
    show = await tool.invoke({"action": "show", "revision": "HEAD"})
    blame = await tool.invoke({"action": "blame", "path": "sample.txt"})

    assert not status.is_error and "sample.txt" in status.content
    assert not diff.is_error and '"diff"' in diff.content
    assert not log.is_error and "initial" in log.content
    assert not show.is_error and "initial" in show.content
    assert not blame.is_error and "filename sample.txt" in blame.content


# 功能：验证默认小工具面隐藏 Run family，但显式选择后保留 tests/verifiers
# 设计：检查默认模型目录、显式 action schema 和内部兼容实现表
def test_runner_exposes_run_family_and_hides_legacy_aliases(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert "Run" not in names
    assert {"run_tests", "run_verifiers"}.isdisjoint(names)
    assert registry.get("run_tests") is not None
    assert registry.get("run_verifiers") is not None
    registry.set_model_tool_allowlist(frozenset({"Run"}))
    assert _file_actions(_run_schema(registry)) == {"tests", "verifiers"}


# 功能：验证 Run.tests 与 Run.verifiers 真实执行并返回有界结构化结果
# 设计：使用当前 Python 解释器构造跨平台命令，覆盖单测试成功和并行 gate 的 pass/fail 汇总
async def test_run_family_executes_tests_and_verifiers(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    tool = registry.get("Run")
    assert tool is not None
    passing = _python_command("print('gate-ok')")
    failing = _python_command("raise SystemExit(2)")

    tests = await tool.invoke({"action": "tests", "command": passing})
    verifiers = await tool.invoke(
        {
            "action": "verifiers",
            "commands": [
                {"name": "pass", "command": passing},
                {"name": "fail", "command": failing},
            ],
        }
    )

    assert not tests.is_error and "gate-ok" in tests.content
    tests_payload = json.loads(tests.content)
    assert tests_payload["verification_eligible"] is False
    assert tests_payload["verification_reason"] == "missing_candidate_id"
    assert verifiers.is_error
    payload = json.loads(verifiers.content)
    assert payload["verdict"] == "fail"
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["verification_eligible"] is False


# 功能：验证只有 daemon 发现且 ID、命令完全匹配的项目候选可生成可信验证资格
# 设计：先运行任意 echo 风格命令确认失败关闭，再执行真实 manifest 候选并核对来源绑定
async def test_run_tests_verification_requires_exact_discovered_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n",
        encoding="utf-8",
    )
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    tool = registry.get("Run")
    assert tool is not None
    candidate = discover_test_commands(WorkspaceBoundary(tmp_path)).candidates[0]
    command = render_test_command(candidate)

    arbitrary = await tool.invoke(
        {"action": "tests", "command": _python_command("print('ok')")}
    )
    verified = await tool.invoke(
        {
            "action": "tests",
            "command": command,
            "candidate_id": command_candidate_id(candidate),
        }
    )

    assert json.loads(arbitrary.content)["verification_eligible"] is False
    verified_payload = json.loads(verified.content)
    assert verified.is_error is False
    assert verified_payload["verification_eligible"] is True
    assert verified_payload["gates"][0]["source"] == "pyproject.toml"


# 功能：验证测试进程执行期间修改 manifest 会使原候选资格立即失效
# 设计：让真实 pytest 用例改写候选来源，再依靠执行后重新发现断言不能沿用旧摘要认证
async def test_run_tests_rechecks_manifest_after_execution(tmp_path: Path) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(
        "[tool.pytest.ini_options]\naddopts = '-q'\n",
        encoding="utf-8",
    )
    (tmp_path / "test_mutate.py").write_text(
        "from pathlib import Path\n\n"
        "def test_mutate_manifest():\n"
        "    path = Path('pyproject.toml')\n"
        "    path.write_text(path.read_text(encoding='utf-8') + '# changed\\n', "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )
    candidate = discover_test_commands(WorkspaceBoundary(tmp_path)).candidates[0]
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    tool = runner._build_registry(TaskManager(tmp_path / ".tasks")).get("Run")
    assert tool is not None

    result = await tool.invoke(
        {
            "action": "tests",
            "command": render_test_command(candidate),
            "candidate_id": command_candidate_id(candidate),
        }
    )

    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["verification_eligible"] is False
    assert payload["verification_reason"] == "candidate_changed_during_execution"


# 功能：验证带后台 registry 的 Runner 可显式启用 Bash lifecycle family
# 设计：默认仍保持小工具面，再选择 Bash 检查 run/wait/interact/cancel 与兼容 backend
def test_runner_exposes_bash_lifecycle_family(tmp_path: Path) -> None:
    background = BackgroundJobRegistry(EventBus())
    runner = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
        background_registry=background,
    )
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        session_id="sess-bash",
        run_id="run-bash",
    )
    names = {str(schema["name"]) for schema in registry.tool_schemas()}
    assert "Bash" not in names
    registry.set_model_tool_allowlist(frozenset({"Bash"}))
    schema = next(item for item in registry.tool_schemas() if item["name"] == "Bash")

    assert {
        "background_start",
        "background_result",
        "background_interact",
        "background_cancel",
    }.isdisjoint(names)
    assert _file_actions(schema) == {"run", "wait", "interact", "cancel"}
    assert registry.get("bash") is not None
    assert registry.get("background_interact") is not None


# 功能：验证缺省 Bash.run 经真实参数校验和权限审批后才执行
# 设计：重放 Pilot 的 command-only 参数，仅替换 Shell 后端并分别允许与拒绝，避免执行历史命令
@pytest.mark.parametrize("decision", ["allow_once", "deny"])
async def test_bash_default_action_keeps_permission_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str,
) -> None:
    manager = PermissionManager(timeout_s=0)
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path, permission_manager=manager)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    backend = registry.get("Bash")
    assert backend is not None
    executed: list[dict[str, object]] = []

    # 捕获已通过审批的执行参数而不创建进程
    async def execute(params: dict[str, object]) -> ToolResult:
        executed.append(params)
        return ToolResult("command completed")

    monkeypatch.setattr(backend._shell, "invoke", execute)  # type: ignore[attr-defined]
    bus = EventBus()
    events: list[str] = []

    # 按参数化决策响应真实权限请求
    async def respond(event: object) -> None:
        events.append(str(getattr(event, "type", "")))
        if getattr(event, "type", "") == "permission.requested":
            manager.respond("implicit-run", decision)

    bus.subscribe(respond)
    call = ToolCallBlock("implicit-run", "Bash", {"command": "echo probe"})
    resolved = registry.resolve_call("Bash", call.input)
    assert resolved.permission_scope.key == "Bash.run"
    result = await invoke_tool(registry, call, bus, "run", permission_manager=manager,
                               session_id="session")
    assert "permission.requested" in events
    assert bool(executed) == (decision == "allow_once")
    assert result.is_error == (decision == "deny")
    assert call.input == {"command": "echo probe"}  # 保留原始模型调用以便重放


# 功能：验证 Bash 缺省动作在三种线协议共用目录中明确声明且不扩大隐藏权限
# 设计：检查原始和 OpenAI 扁平 Schema，并用 Plan 与 action allowlist 拒绝同一命令
def test_bash_default_schema_and_frozen_action_restriction(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    registry.set_model_tool_allowlist(frozenset({"Bash"}))
    schema = next(item for item in registry.tool_schemas() if item["name"] == "Bash")
    variant = schema["input_schema"]["oneOf"][0]
    assert variant["required"] == ["command"]
    assert variant["properties"]["action"]["default"] == "run"
    parameters = _to_openai_tools([schema])[0]["function"]["parameters"]
    assert parameters["required"] == ["command"]
    assert parameters["properties"]["action"]["default"] == "run"
    registry.set_model_action_allowlist({"Bash": frozenset({"wait"})})
    with pytest.raises(ToolCatalogError, match="hidden"):
        registry.resolve_call("Bash", {"command": "echo probe"})
    plan = runner._build_registry(TaskManager(tmp_path / ".plan"), runtime_mode=RuntimeMode.PLAN)
    with pytest.raises(ToolCatalogError):
        plan.resolve_call("Bash", {"command": "echo probe"})
    with pytest.raises(ToolCatalogError):
        plan.resolve_call("Bash", {"action": "unknown", "command": "echo probe"})


# 功能：验证 Bash family 可完成 background run、interact 与 wait 生命周期
# 设计：用等待 stdin 的真实 Python 子进程串联三个 action，证明 family 不只是展示层别名
async def test_bash_family_background_lifecycle(tmp_path: Path) -> None:
    background = BackgroundJobRegistry(EventBus())
    runner = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
        background_registry=background,
    )
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        session_id="sess-bash",
        run_id="run-bash",
    )
    tool = registry.get("Bash")
    assert tool is not None
    command = subprocess.list2cmdline(
        [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"]
    )

    started = await tool.invoke(
        {"action": "run", "command": command, "background": True, "timeout": 10}
    )
    job_id = started.content.split("job_id=", 1)[1].split(".", 1)[0]
    interaction = await tool.invoke(
        {
            "action": "interact",
            "job_id": job_id,
            "stdin": "family-input\n",
            "close_stdin": True,
        }
    )
    waited = await tool.invoke(
        {"action": "wait", "job_id": job_id, "timeout": 10}
    )

    assert not interaction.is_error
    assert not waited.is_error
    assert "family-input" in waited.content


# 功能：验证 Plan Mode 不暴露任何执行代码的 Run action
# 设计：构建真实 Plan registry，确认 family 和旧 alias 都不进入模型目录但 replay 实现仍保留
def test_plan_registry_hides_run_family(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        runtime_mode=RuntimeMode.PLAN,
    )
    names = {str(schema["name"]) for schema in registry.tool_schemas()}

    assert "Run" not in names
    assert "run_tests" not in names
    assert "Repository" in names
    assert registry.get("Run") is not None
    assert registry.get("run_tests") is not None


# 功能：验证旧工具 whitelist 被准确转换为 File action 子集
# 设计：只允许 read_file 和 grep，断言 File 仍可见但仅暴露 read/search_content 两个 action
def test_legacy_whitelist_maps_to_file_action_subset(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        tool_whitelist=["read_file", "grep"],
    )

    assert _file_actions(_file_schema(registry)) == {"read", "search_content"}
    assert registry.get("read_file") is not None
    assert registry.get("grep") is not None
    assert registry.get("write_file") is None


# 功能：验证 Plan Mode 在同一个 File family 中只保留四个只读 action
# 设计：构建真实 Plan registry 并检查 oneOf，确保写 backend 存在也无法通过目录解析调用
def test_plan_registry_filters_file_mutations_by_action(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(
        TaskManager(tmp_path / ".tasks"),
        runtime_mode=RuntimeMode.PLAN,
    )

    assert _file_actions(_file_schema(registry)) == {
        "read",
        "list",
        "search_name",
        "search_content",
    }
    denied = registry.get("File")
    assert denied is not None
    try:
        registry.resolve_call("File", {"action": "write", "path": "x", "content": "y"})
    except ValueError as exc:
        assert "unavailable in plan mode" in str(exc)
    else:
        raise AssertionError("Plan must reject File.write")


# 功能：验证 File family 的读写 action 真实分派到现有 backend
# 设计：通过 invoke_tool 走 schema、catalog 和事件完整路径，写入后再读取并检查原工具元数据
async def test_file_family_executes_real_read_and_write_actions(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    bus = EventBus()

    written = await invoke_tool(
        registry,
        ToolCallBlock(
            id="write-1",
            name="File",
            input={"action": "write", "path": "sample.txt", "content": "hello"},
        ),
        bus,
        "run-1",
    )
    read = await invoke_tool(
        registry,
        ToolCallBlock(
            id="read-1",
            name="File",
            input={"action": "read", "path": "sample.txt"},
        ),
        bus,
        "run-1",
    )

    assert not written.is_error
    assert not read.is_error
    assert "[content]\nhello" in read.content


# 功能：验证旧平铺 alias 对模型 fail closed，但 replay/internal caller 仍可兼容执行
# 设计：对同一 read_file 调用分别使用默认 model caller 和 internal caller，固定迁移期边界
async def test_legacy_file_alias_is_internal_only(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("legacy", encoding="utf-8")
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    call = ToolCallBlock(
        id="legacy-read",
        name="read_file",
        input={"path": "sample.txt"},
    )

    denied = await invoke_tool(registry, call, EventBus(), "run-1")
    allowed = await invoke_tool(
        registry,
        call,
        EventBus(),
        "run-1",
        caller=ToolCaller.REPLAY,
    )

    assert denied.is_error
    assert denied.error_type == "schema_error"
    assert "model is not allowed" in denied.content
    assert not allowed.is_error
    assert "legacy" in allowed.content


# 功能：验证 Git、Run 和 Bash 的旧平铺调用可由 transcript replay 执行
# 设计：以真实 registry 和 REPLAY caller 调用三个隐藏 alias，区分兼容可执行与模型不可见
@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_legacy_git_run_and_bash_aliases_replay(tmp_path: Path) -> None:
    _run_git(tmp_path, "init", "--initial-branch=main")
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    command = _python_command("print('replayed')")

    git_result = await invoke_tool(
        registry,
        ToolCallBlock(id="old-git", name="git_diff", input={"path": "."}),
        EventBus(),
        "run-replay",
        caller=ToolCaller.REPLAY,
    )
    tests_result = await invoke_tool(
        registry,
        ToolCallBlock(
            id="old-tests",
            name="run_tests",
            input={"command": command},
        ),
        EventBus(),
        "run-replay",
        caller=ToolCaller.REPLAY,
    )
    bash_result = await invoke_tool(
        registry,
        ToolCallBlock(id="old-bash", name="bash", input={"command": "printf replayed"}),
        EventBus(),
        "run-replay",
        caller=ToolCaller.REPLAY,
    )

    assert not git_result.is_error
    assert not tests_result.is_error and "replayed" in tests_result.content
    assert not bash_result.is_error and "replayed" in bash_result.content


# 功能：验证 File.read 依据 action capability 自动放行且不产生审批请求
# 设计：使用真实 PermissionManager 和 EventBus，确认 family 名称没有让只读 action 退化成未知工具 ASK
async def test_file_read_uses_action_level_permission(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("readable", encoding="utf-8")
    manager = PermissionManager()
    runner = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
        permission_manager=manager,
    )
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    bus = EventBus()
    events: list[str] = []

    # 收集权限和工具事件类型
    async def collect(event: object) -> None:
        events.append(str(getattr(event, "type", "")))

    bus.subscribe(collect)  # type: ignore[arg-type]
    result = await invoke_tool(
        registry,
        ToolCallBlock(
            id="family-read",
            name="File",
            input={"action": "read", "path": "sample.txt"},
        ),
        bus,
        "run-1",
        permission_manager=manager,
        session_id="sess-1",
    )

    assert not result.is_error
    assert "permission.requested" not in events


# 功能：验证 Ask 姿态下 File.write 仍产生审批并在允许后执行
# 设计：异步响应真实 pending Future，断言 action capability 没有因 family 聚合而把写操作当只读放行
async def test_file_write_still_requires_approval_in_ask_mode(tmp_path: Path) -> None:
    manager = PermissionManager(timeout_s=0)
    runner = AgentRunner(
        CodeRookConfig(),
        workspace_root=tmp_path,
        permission_manager=manager,
    )
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))
    bus = EventBus()
    events: list[str] = []

    # 收集权限和工具事件类型
    async def collect(event: object) -> None:
        event_type = str(getattr(event, "type", ""))
        events.append(event_type)
        if event_type == "permission.requested":
            manager.respond("family-write", "allow_once")

    bus.subscribe(collect)  # type: ignore[arg-type]
    result = await invoke_tool(
        registry,
        ToolCallBlock(
            id="family-write",
            name="File",
            input={"action": "write", "path": "out.txt", "content": "ok"},
        ),
        bus,
        "run-1",
        permission_manager=manager,
        session_id="sess-1",
    )

    assert not result.is_error
    assert "permission.requested" in events
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "ok"


# 功能：验证 File action 为读取声明共享 claim、为写入和 patch 声明独占 claim
# 设计：比较同一路径 read/write 与全工作区 patch，给并行调度提供可判定资源证据
def test_file_family_declares_action_level_resource_claims(tmp_path: Path) -> None:
    runner = AgentRunner(CodeRookConfig(), workspace_root=tmp_path)
    registry = runner._build_registry(TaskManager(tmp_path / ".tasks"))

    read = registry.resource_claims("File", {"action": "read", "path": "same.txt"})
    write = registry.resource_claims(
        "File",
        {"action": "write", "path": "same.txt", "content": "x"},
    )
    patch = registry.resource_claims("File", {"action": "patch", "patch": "diff"})

    assert read[0].resource == write[0].resource == "workspace:same.txt"
    assert read[0].capability == ToolCapability.READ
    assert not read[0].exclusive
    assert write[0].capability == ToolCapability.WRITE
    assert write[0].exclusive
    assert patch[0].resource == "workspace:/**"
    assert patch[0].exclusive


class _UnusedBackend(BaseTool):
    name = "unused"
    description = "unused"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    # 返回固定结果以满足 BaseTool 契约
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="unused")


# 功能：验证 File family 拒绝没有任何已知 backend 的错误装配
# 设计：传入无关 backend 名称，确保配置错误在 runner 启动阶段失败而不是模型调用时才暴露
def test_file_family_requires_known_backend(tmp_path: Path) -> None:
    try:
        FileTool(WorkspaceBoundary(tmp_path), {"unused": _UnusedBackend()})
    except ValueError as exc:
        assert "at least one backend" in str(exc)
    else:
        raise AssertionError("File family must reject an empty action map")
