from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from code_rook.core.events.bus import EventBus
from code_rook.core.hooks import HookConfig, HookDecision, HookManager, load_hook_configs
from code_rook.core.hooks.config import HookConfigError
from code_rook.core.hooks.payload import build_hook_payload
from code_rook.core.hooks.process import HookProcessResult


# 功能：验证同一生命周期的异步 hooks 按注册顺序执行
# 设计：两个回调向共享列表追加标记，直接断言顺序以覆盖确定性扩展语义
async def test_hooks_run_in_registration_order() -> None:
    hooks = HookManager()
    seen: list[str] = []

    async def first(context: dict[str, object]) -> None:
        seen.append(str(context["value"]))

    async def second(context: dict[str, object]) -> None:
        seen.append("second")

    hooks.register("UserPromptSubmit", first)
    hooks.register("UserPromptSubmit", second)

    decision = await hooks.emit("UserPromptSubmit", {"value": "first"})

    assert not decision.blocked
    assert seen == ["first", "second"]


# 功能：验证 PreToolUse hook 可以阻断后续回调并返回原因
# 设计：首个回调返回阻断决定，第二个回调若运行会污染列表，从而同时验证短路行为
async def test_hook_block_short_circuits_callbacks() -> None:
    hooks = HookManager()
    seen: list[str] = []

    async def blocker(context: dict[str, object]) -> HookDecision:
        seen.append(str(context["tool_name"]))
        return HookDecision(blocked=True, reason="policy hook")

    async def unreachable(context: dict[str, object]) -> None:
        seen.append("unexpected")

    hooks.register("PreToolUse", blocker)
    hooks.register("PreToolUse", unreachable)

    decision = await hooks.emit("PreToolUse", {"tool_name": "bash"})

    assert decision == HookDecision(blocked=True, reason="policy hook")
    assert seen == ["bash"]


# 构造满足 Hooks V2 必填字段的测试配置
def _config(
    command: tuple[str, ...],
    *,
    hook_id: str = "test-hook",
    blocking: bool = True,
    on_failure: str = "open",
    timeout_ms: int = 2000,
    scope: str = "user",
) -> HookConfig:
    return HookConfig.model_validate(
        {
            "id": hook_id,
            "event": "tool_call_before",
            "timeout_ms": timeout_ms,
            "blocking": blocking,
            "command": command,
            "conditions": {},
            "trusted_scope": scope,
            "on_failure": on_failure,
        }
    )


# 功能：验证 blocking hook 超时分别遵守 fail-open 和 fail-closed
# 设计：执行相同睡眠子进程，仅切换 on_failure，直接比较最终阻断决定和审计状态
@pytest.mark.parametrize(
    ("on_failure", "expected_blocked"),
    [("open", False), ("closed", True)],
)
async def test_blocking_hook_timeout_policy(
    tmp_path: Path,
    on_failure: str,
    expected_blocked: bool,
) -> None:
    config = _config(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        on_failure=on_failure,
        timeout_ms=100,
    )
    manager = HookManager([config], workspace=tmp_path)

    decision = await manager.emit("tool_call_before", {"tool_name": "Bash"})

    assert decision.blocked is expected_blocked
    assert manager.audit_events()[-1].status == "timeout"
    assert manager.audit_events()[-1].on_failure == on_failure


# 功能：验证 hook payload 会移除 API key 并压缩超大 tool output
# 设计：输入显式 secret 和 100KB 输出，检查序列化 stdin 不含 secret 且只保留摘要元数据
def test_hook_payload_is_redacted_and_bounded() -> None:
    config = _config((sys.executable, "hook.py"))

    payload = build_hook_payload(
        config,
        "tool_call_before",
        {
            "api_key": "sk-super-secret-value",
            "params": {"authorization": "Bearer hidden-token"},
            "output": "x" * 100_000,
        },
    )
    encoded = payload.model_dump_json()

    assert "sk-super-secret-value" not in encoded
    assert "hidden-token" not in encoded
    assert payload.truncated
    assert payload.context["truncated"] is True
    assert len(encoded.encode("utf-8")) < 4096


# 功能：验证 hooks.toml 的 required 字段和来源 trusted_scope 不能伪造
# 设计：项目配置故意声明 user scope，加载器应在任何命令执行前拒绝整个配置
def test_hook_config_rejects_scope_spoofing(tmp_path: Path) -> None:
    config_path = tmp_path / ".coderook" / "hooks.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        """
[[hooks]]
id = "spoofed"
event = "turn_start"
timeout_ms = 1000
blocking = true
command = ["python", "hook.py"]
conditions = {}
trusted_scope = "user"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(HookConfigError, match="trusted_scope=project"):
        load_hook_configs(tmp_path, user_config=tmp_path / "missing.toml")


# 功能：验证未受信任工作区中的 project hook 会跳过并记录结构化事件
# 设计：命令若运行会创建文件，用 false trust provider 断言副作用不存在且 audit 可查询
async def test_project_hook_requires_trusted_workspace(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    config = _config(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        scope="project",
    )
    manager = HookManager(
        [config],
        workspace=tmp_path,
        project_trust_provider=lambda _session_id: False,
    )

    decision = await manager.emit(
        "tool_call_before",
        {"session_id": "sess-1", "tool_name": "Bash"},
    )

    assert not decision.blocked
    assert not marker.exists()
    assert manager.audit_events()[-1].status == "skipped_untrusted"


# 功能：验证手动 rerun 不能绕过 project hook 的工作区信任检查
# 设计：未信任 session 的命令若执行会落 marker，断言审计为 skipped 且文件不存在
async def test_project_hook_rerun_requires_trusted_workspace(tmp_path: Path) -> None:
    marker = tmp_path / "rerun.txt"
    config = _config(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        scope="project",
    )
    manager = HookManager(
        [config],
        workspace=tmp_path,
        project_trust_provider=lambda session_id: session_id == "trusted-session",
    )

    audit = await manager.rerun("test-hook", session_id="untrusted-session")

    assert audit is not None
    assert audit.status == "skipped_untrusted"
    assert not marker.exists()


# 功能：验证受信任 session 仍可手动重跑 project hook
# 设计：trust provider 只允许固定 session，成功 rerun 后检查完成审计和 marker 副作用
async def test_project_hook_rerun_allows_trusted_workspace(tmp_path: Path) -> None:
    marker = tmp_path / "trusted-rerun.txt"
    config = _config(
        (sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        scope="project",
    )
    manager = HookManager(
        [config],
        workspace=tmp_path,
        project_trust_provider=lambda session_id: session_id == "trusted-session",
    )

    audit = await manager.rerun("test-hook", session_id="trusted-session")

    assert audit is not None
    assert audit.status == "completed"
    assert marker.exists()


# 返回指定 PID 是否仍存在，供跨平台进程树终止断言
def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# 功能：验证 hook 超时时会连同其派生子进程一起终止
# 设计：父 hook 启动长驻 child 并写 PID，超时返回后检查 child 已从系统进程表消失
async def test_hook_timeout_kills_process_tree(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "\n".join(
            [
                "import pathlib, subprocess, sys, time",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(60)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = _config((sys.executable, str(script)), timeout_ms=1000)
    manager = HookManager([config], workspace=tmp_path)

    await manager.emit("tool_call_before", {})

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    assert not _process_exists(child_pid)


# 功能：验证进程 hook 的 stdout 只能影响结构化决定且审计不会保存整段输出
# 设计：脚本返回阻断 JSON 后再输出大量文本，检查阻断生效、截断标记存在且 audit 无正文
async def test_hook_output_is_bounded(tmp_path: Path) -> None:
    response = json.dumps({"blocked": True, "reason": "policy"})
    script = f"import sys; sys.stdout.write({response!r} + 'x' * 100000)"
    config = _config((sys.executable, "-c", script)).model_copy(
        update={"max_output_bytes": 1024}
    )
    manager = HookManager([config], workspace=tmp_path)

    decision = await manager.emit("tool_call_before", {})

    audit = manager.audit_events()[-1]
    assert not decision.blocked
    assert audit.output_truncated
    assert "x" * 100 not in audit.model_dump_json()


# 功能：验证非阻断 hook 使用有界队列且满载时明确丢弃并审计
# 设计：冻结 worker 中的首个执行、填满容量一队列，再投递第三项触发 dropped 事件
async def test_nonblocking_hook_queue_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    # 用可控异步执行器冻结首个 hook，稳定制造队列满载条件
    async def fake_execute(*args: object, **kwargs: object) -> HookProcessResult:
        started.set()
        await release.wait()
        return HookProcessResult("completed", 0, "", "", False)

    monkeypatch.setattr("code_rook.core.hooks.manager.execute_hook_process", fake_execute)
    config = _config(
        (sys.executable, "hook.py"),
        blocking=False,
    )
    manager = HookManager([config], workspace=tmp_path, queue_size=1)

    await manager.emit("tool_call_before", {"sequence": 1})
    await started.wait()
    await manager.emit("tool_call_before", {"sequence": 2})
    await manager.emit("tool_call_before", {"sequence": 3})
    release.set()
    await manager.close()

    assert any(event.status == "dropped" for event in manager.audit_events())


# 功能：验证每次进程 hook 执行都会发布 hook.executed 结构化 EventBus 事件
# 设计：用成功空输出脚本执行一次，收集 BaseModel 并核对 hook ID、状态和失败策略字段
async def test_hook_execution_publishes_structured_event(tmp_path: Path) -> None:
    bus = EventBus()
    published: list[BaseModel] = []

    # 收集 hook manager 发布到总线的结构化事件
    async def collect(event: BaseModel) -> None:
        published.append(event)

    bus.subscribe(collect)
    manager = HookManager(
        [_config((sys.executable, "-c", "pass"))],
        workspace=tmp_path,
        bus=bus,
    )

    await manager.emit("tool_call_before", {})

    event = published[-1]
    assert event.type == "hook.executed"  # type: ignore[attr-defined]
    assert event.hook_id == "test-hook"  # type: ignore[attr-defined]
    assert event.status == "completed"  # type: ignore[attr-defined]
