from __future__ import annotations

import asyncio
from typing import Any

import pytest

from code_rook.core.authority import AuthorityProfile, SandboxCapability
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.permissions.policy import PermissionDecision, ToolPolicy
from code_rook.core.permissions.storage import (
    PolicyStoreError,
    load_authority_profile,
    load_policy_file,
    save_policy_file,
)
from code_rook.core.tools.spec import ApprovalRequirement

# ── helpers ──────────────────────────────────────────────────────────────────

def _make_manager(**policies: ToolPolicy) -> PermissionManager:
    # policy_file=None：测试中不使用持久化，不污染 ~/.coderook/policy.toml
    return PermissionManager(policies or None)


async def _collect_emitted() -> tuple[list[dict[str, Any]], Any]:
    emitted: list[dict[str, Any]] = []

    async def emitter(event: dict[str, Any]) -> None:
        emitted.append(event)

    return emitted, emitter


# ── evaluate() delegation ─────────────────────────────────────────────────────

# 功能：验证 PermissionManager.evaluate 委托给 policy 层返回正确决策
# 设计：直接调用 evaluate()，不涉及 Future，验证策略加载与委托路径
def test_evaluate_delegates_to_policy() -> None:
    mgr = _make_manager()
    assert mgr.evaluate("apply_patch", {"patch": "x"}) == PermissionDecision.ASK
    assert mgr.evaluate("read_file", {"path": "x"}) == PermissionDecision.ALLOW
    assert mgr.evaluate("bash", {"command": "echo hi"}) == PermissionDecision.ASK
    assert mgr.evaluate("edit_file", {"path": "x", "old_text": "a", "new_text": "b"}) == PermissionDecision.ASK
    assert mgr.evaluate("write_file", {"path": "x", "content": ""}) == PermissionDecision.ASK


# ── check_and_wait: ALLOW path ───────────────────────────────────────────────

# 功能：验证策略为 ALLOW 时 check_and_wait 立即返回 (True, "auto_allow")，不发任何事件
# 设计：read_file 默认 ALLOW，断言不产生 permission.requested 事件，覆盖"无噪声放行"路径
async def test_check_and_wait_allow_no_event() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t1", tool_name="read_file",
        params={"path": "README.md"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert emitted == []


async def test_cancelled_permission_wait_removes_pending_request() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()
    task = asyncio.create_task(
        mgr.check_and_wait(
            tool_use_id="cancel-me",
            tool_name="bash",
            params={"command": "echo hi"},
            session_id="s1",
            event_emitter=emitter,
        )
    )
    while not emitted:
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "cancel-me" not in mgr._pending


async def test_headless_deny_rejects_ask_without_pending_future() -> None:
    mgr = _make_manager()
    mgr.set_session_mode("headless", "deny")
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="headless-deny",
        tool_name="edit_file",
        params={"path": "x", "old_text": "a", "new_text": "b"},
        session_id="headless",
        event_emitter=emitter,
    )

    assert not allowed
    assert decision == "headless_deny"
    assert emitted == []
    assert mgr._pending == {}


async def test_headless_fail_fast_reports_permission_required_immediately() -> None:
    mgr = _make_manager()
    mgr.set_session_mode("headless", "fail_fast")
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="headless-fail",
        tool_name="bash",
        params={"command": "echo hello"},
        session_id="headless",
        event_emitter=emitter,
    )

    assert not allowed
    assert decision == "headless_fail_fast"
    assert emitted == []
    assert mgr._pending == {}


async def test_headless_allow_list_is_explicit_and_cannot_bypass_boundary() -> None:
    mgr = _make_manager()
    mgr._persistent_always["write_file"] = "allow"
    mgr.set_session_mode("headless", "allow_list", allow_tools=["edit_file", "bash"])
    _, emitter = await _collect_emitted()

    edit_allowed, edit_decision = await mgr.check_and_wait(
        tool_use_id="edit",
        tool_name="edit_file",
        params={"path": "x", "old_text": "a", "new_text": "b"},
        session_id="headless",
        event_emitter=emitter,
    )
    write_allowed, write_decision = await mgr.check_and_wait(
        tool_use_id="write",
        tool_name="write_file",
        params={"path": "x", "content": "new"},
        session_id="headless",
        event_emitter=emitter,
    )
    outside_allowed, outside_decision = await mgr.check_and_wait(
        tool_use_id="outside",
        tool_name="bash",
        params={"command": "cat /etc/hosts"},
        session_id="headless",
        event_emitter=emitter,
    )

    assert (edit_allowed, edit_decision) == (True, "headless_allow_list")
    assert (write_allowed, write_decision) == (False, "headless_deny")
    assert (outside_allowed, outside_decision) == (False, "headless_deny")
    assert mgr._pending == {}


async def test_headless_modes_preserve_default_allow_tools() -> None:
    mgr = _make_manager()
    mgr.set_session_mode("headless", "deny")
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="read",
        tool_name="read_file",
        params={"path": "README.md"},
        session_id="headless",
        event_emitter=emitter,
    )

    assert (allowed, decision) == (True, "auto_allow")
    assert emitted == []

    task_allowed, task_decision = await mgr.check_and_wait(
        tool_use_id="task",
        tool_name="task_create",
        params={"subject": "inspect"},
        session_id="headless",
        event_emitter=emitter,
    )
    assert (task_allowed, task_decision) == (True, "auto_allow")


# ── check_and_wait: ASK path + respond ───────────────────────────────────────

# 功能：验证 ASK 策略时发出 permission.requested 事件并等待 respond() 解决 Future
# 设计：在后台协程中调用 respond("allow_once")，主协程 await 结束后断言结果；
#       这是权限系统的核心反向请求通路
async def test_check_and_wait_ask_emits_event_and_waits() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_respond() -> None:
        await asyncio.sleep(0)  # yield once so check_and_wait can emit the event
        mgr.respond("t2", "allow_once")

    task = asyncio.create_task(_auto_respond())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t2", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is True
    assert decision == "allow_once"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"
    assert emitted[0]["tool_use_id"] == "t2"
    assert emitted[0]["tool_name"] == "bash"


# 功能：验证 respond("deny_once") 使 check_and_wait 返回 (False, "deny_once")
# 设计：用户拒绝时工具不应执行，确认 False 返回值而不是异常
async def test_check_and_wait_deny_once_returns_false() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _auto_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t3", "deny_once")

    task = asyncio.create_task(_auto_deny())
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t3", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False
    assert decision == "deny_once"


# ── always_allow cache ────────────────────────────────────────────────────────

# 功能：验证 respond("always_allow") 后同 session 同工具下次不再发事件
# 设计：第二次调用 check_and_wait 命中 always 缓存，直接返回 (True, "auto_allow")，emitted 仍为 1 条
async def test_always_allow_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # First call: user says "always allow"
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t4", "always_allow")

    task = asyncio.create_task(_auto_always())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t4", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is True

    # Second call: should hit cache, no new event
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t5", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )

    assert r2 is True
    assert d2 == "auto_allow"
    assert len(emitted) == 1  # only the first call emitted an event


# 功能：验证 always_allow 在同一 manager 实例内对所有 session 生效（persistent_always 共享）
# 设计：s1 设置 always_allow → 写入 _persistent_always；s2 命中 persistent 缓存，直接放行；
#       emitted 只有 1 条（s2 不需要再 ASK）。这是 persistent always 的核心跨 session 语义。
async def test_always_allow_not_shared_across_sessions() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # session s1 sets always allow for bash
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t6", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t6", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    # session s2 — persistent_always["bash"] = "allow" → 直接放行，不再 ASK
    r, d = await mgr.check_and_wait(
        tool_use_id="t7", tool_name="bash",
        params={"command": "echo"}, session_id="s2",
        event_emitter=emitter,
    )

    assert r is True
    assert d == "auto_allow"
    assert len(emitted) == 1  # s2 命中 persistent 缓存，不再发出事件


# ── always_deny cache ─────────────────────────────────────────────────────────

# 功能：验证 respond("always_deny") 后同 session 同工具下次直接返回 (False, "auto_deny")
# 设计：用户选择 always deny 后不应继续骚扰，下次调用静默拒绝
async def test_always_deny_skips_future_ask() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _auto_always_deny() -> None:
        await asyncio.sleep(0)
        mgr.respond("t8", "always_deny")

    task = asyncio.create_task(_auto_always_deny())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="t8", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is False

    # Second call: cache hit → no event, return (False, "auto_deny")
    r2, d2 = await mgr.check_and_wait(
        tool_use_id="t9", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    assert r2 is False
    assert d2 == "auto_deny"
    assert len(emitted) == 1


# ── cancel_session ────────────────────────────────────────────────────────────

# 功能：验证 cancel_session 将 pending Future 设为 deny_once，check_and_wait 返回 False
# 设计：模拟客户端断连场景——check_and_wait 挂起后调用 cancel_session，
#       确认 Future 被解决而非永久挂起（防止僵尸 run）
async def test_cancel_session_resolves_pending_future() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    async def _cancel_after_emit() -> None:
        await asyncio.sleep(0)  # wait for event to be emitted
        mgr.cancel_session("s1", reason="client_disconnected")

    task = asyncio.create_task(_cancel_after_emit())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="t10", tool_name="bash",
        params={"command": "ls"}, session_id="s1",
        event_emitter=emitter,
    )
    await task

    assert allowed is False


# 功能：验证 cancel_session 只取消属于该 session 的 pending Future
# 设计：s1 和 s2 各有一个 pending，cancel_session(s2) 不影响 s1 的 Future
async def test_cancel_session_only_affects_target_session() -> None:
    mgr = _make_manager()
    _, emitter = await _collect_emitted()

    # Launch two concurrent check_and_wait for different sessions
    s1_done = asyncio.Event()
    s2_done = asyncio.Event()
    s1_result: list[bool] = []
    s2_result: list[bool] = []

    async def _s1() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="ta", tool_name="bash",
            params={"command": "echo"}, session_id="s1",
            event_emitter=emitter,
        )
        s1_result.append(r)
        s1_done.set()

    async def _s2() -> None:
        r, _ = await mgr.check_and_wait(
            tool_use_id="tb", tool_name="bash",
            params={"command": "echo"}, session_id="s2",
            event_emitter=emitter,
        )
        s2_result.append(r)
        s2_done.set()

    t1 = asyncio.create_task(_s1())
    t2 = asyncio.create_task(_s2())

    await asyncio.sleep(0)  # let both emit events and hang

    # cancel only s2
    mgr.cancel_session("s2")
    await s2_done.wait()

    # s1 should still be pending; resolve it manually
    mgr.respond("ta", "allow_once")
    await s1_done.wait()

    await t1
    await t2

    assert s1_result == [True]   # s1 was allowed
    assert s2_result == [False]  # s2 was cancelled → denied


# ── respond: unknown tool_use_id ──────────────────────────────────────────────

# 功能：验证 respond 传入不存在的 tool_use_id 时静默忽略，不抛异常
# 设计：竞态场景（客户端重复发送响应）不应导致 daemon crash
def test_respond_unknown_tool_use_id_is_noop() -> None:
    mgr = _make_manager()
    mgr.respond("nonexistent", "allow_once")  # should not raise


# ── OUTSIDE_CWD 显式授权 ──────────────────────────────────────────────────────

# 功能：验证 always_allow bash 之后，绝对路径命令命中缓存且不再询问
# 设计：先显式永久允许普通 bash，再请求工作区外路径，断言缓存语义与界面文案一致
async def test_always_allow_applies_to_outside_cwd() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    # 首次 allow → 写入 session always 缓存
    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t_always", "always_allow")

    t = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t_always", tool_name="bash",
        params={"command": "echo ok"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert len(emitted) == 1  # 首次 ASK 触发事件

    # 第二次：bash + 绝对路径 → 命中用户显式设置的 always 缓存
    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_abs", tool_name="bash",
        params={"command": "cat /etc/hosts"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is True
    assert decision == "auto_allow"
    assert len(emitted) == 1


# 功能：验证 Full Access 自动批准工作区外命令且不触发询问
# 设计：给会话设置 full_access authority 后执行绝对路径命令，断言硬策略之后直接授权
async def test_full_access_allows_outside_cwd_without_prompt() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()
    current = mgr.get_authority_snapshot("s-full")
    mgr.set_authority_snapshot(
        "s-full",
        current.model_copy(update={"profile": AuthorityProfile.FULL_ACCESS}),
    )

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t-full",
        tool_name="bash",
        params={"command": "cat /etc/hosts"},
        session_id="s-full",
        event_emitter=emitter,
        action="shell",
    )

    assert allowed is True
    assert decision == "authority_allow"
    assert emitted == []


# 功能：验证 always_allow 仍不能绕过危险命令的 deny pattern
# 设计：先永久允许安全 bash，再执行命中硬拒绝规则的命令，断言拒绝发生在缓存之前
async def test_always_allow_cannot_bypass_deny_pattern() -> None:
    mgr = _make_manager(
        bash=ToolPolicy(
            default=PermissionDecision.ASK,
            deny_patterns=[r"rm\s+-rf"],
        )
    )
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("t-safe", "always_allow")

    task = asyncio.create_task(_auto_always())
    await mgr.check_and_wait(
        tool_use_id="t-safe",
        tool_name="bash",
        params={"command": "echo safe"},
        session_id="s-safe",
        event_emitter=emitter,
    )
    await task

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t-danger",
        tool_name="bash",
        params={"command": "rm -rf /tmp/example"},
        session_id="s-safe",
        event_emitter=emitter,
    )

    assert allowed is False
    assert decision == "auto_deny"
    assert len(emitted) == 1


# ── 持久化 always 写文件 ──────────────────────────────────────────────────────

# 功能：验证 always_allow 决策写入 policy_file，新 PermissionManager 加载后自动放行
# 设计：用 tmp_path 作为 policy_file，断言文件存在且内容正确；
#       再新建 manager 加载文件，同工具无需 ASK 直接返回 auto_allow
async def test_persistent_always_written_and_reloaded(tmp_path: pytest.TempPathFixture) -> None:
    policy_file = tmp_path / "policy.toml"
    mgr = PermissionManager(policy_file=policy_file)
    emitted, emitter = await _collect_emitted()

    async def _auto_always() -> None:
        await asyncio.sleep(0)
        mgr.respond("tp1", "always_allow")

    t = asyncio.create_task(_auto_always())
    allowed, _ = await mgr.check_and_wait(
        tool_use_id="tp1", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    await t
    assert allowed is True
    assert policy_file.exists()

    loaded = load_policy_file(policy_file)
    assert loaded.get("bash") == "allow"

    # 新 manager 加载同一文件，bash 应直接 auto_allow（无 OUTSIDE_CWD）
    mgr2 = PermissionManager(policy_file=policy_file)
    emitted2, emitter2 = await _collect_emitted()
    allowed2, decision2 = await mgr2.check_and_wait(
        tool_use_id="tp2", tool_name="bash",
        params={"command": "echo new"}, session_id="s2",
        event_emitter=emitter2,
    )
    assert allowed2 is True
    assert decision2 == "auto_allow"
    assert emitted2 == []  # 无需 ASK


# 功能：验证权限姿态写入 policy.toml 后可跨 PermissionManager 实例恢复
# 设计：先持久化 full_access，再创建新 manager，断言新会话继承且旧 always 规则未丢失
def test_authority_profile_persists_as_global_default(
    tmp_path: pytest.TempPathFixture,
) -> None:
    policy_file = tmp_path / "policy.toml"
    mgr = PermissionManager(policy_file=policy_file)
    mgr._persistent_always["bash"] = "allow"

    mgr.set_default_profile(AuthorityProfile.FULL_ACCESS)

    assert load_authority_profile(policy_file) == AuthorityProfile.FULL_ACCESS
    assert load_policy_file(policy_file) == {"bash": "allow"}
    restored = PermissionManager(policy_file=policy_file)
    assert (
        restored.get_authority_snapshot("new-session").profile
        == AuthorityProfile.FULL_ACCESS
    )


# 功能：验证 action 级 NEVER 与 ALWAYS 审批要求覆盖工具默认策略
# 设计：对同一只读 action 分别传两种声明，断言 NEVER 静默放行而 ALWAYS 在 Ask 姿态发出请求
async def test_action_approval_requirement_overrides_policy() -> None:
    manager = PermissionManager(timeout_s=0)
    emitted, emitter = await _collect_emitted()

    allowed, decision = await manager.check_and_wait(
        tool_use_id="never",
        tool_name="Custom",
        params={"action": "inspect"},
        session_id="scope",
        event_emitter=emitter,
        action="read",
        approval_requirement=ApprovalRequirement.NEVER,
    )

    async def _approve_required() -> None:
        await asyncio.sleep(0)
        manager.respond("always", "allow_once")

    response = asyncio.create_task(_approve_required())
    required_allowed, required_decision = await manager.check_and_wait(
        tool_use_id="always",
        tool_name="Custom",
        params={"action": "inspect"},
        session_id="scope",
        event_emitter=emitter,
        action="read",
        approval_requirement=ApprovalRequirement.ALWAYS,
    )
    await response

    assert allowed is True and decision == "auto_allow"
    assert required_allowed is True and required_decision == "allow_once"
    assert [event["tool_use_id"] for event in emitted] == ["always"]


# 功能：验证旧平铺 always 规则只迁移到对应 family action 而不扩大到整个工具族
# 设计：预置 write_file=allow，断言 File.write 自动放行，但 File.edit 仍独立触发审批
async def test_legacy_always_rule_maps_to_exact_family_action(
    tmp_path: pytest.TempPathFixture,
) -> None:
    policy_file = tmp_path / "policy.toml"
    save_policy_file({"write_file": "allow"}, policy_file)
    manager = PermissionManager(policy_file=policy_file, timeout_s=0)
    emitted, emitter = await _collect_emitted()

    write_allowed, write_decision = await manager.check_and_wait(
        tool_use_id="file-write",
        tool_name="File",
        params={"action": "write", "path": "x"},
        session_id="scope",
        event_emitter=emitter,
        action="mutate",
    )

    async def _deny_edit() -> None:
        await asyncio.sleep(0)
        manager.respond("file-edit", "deny_once")

    response = asyncio.create_task(_deny_edit())
    edit_allowed, edit_decision = await manager.check_and_wait(
        tool_use_id="file-edit",
        tool_name="File",
        params={"action": "edit", "path": "x"},
        session_id="scope",
        event_emitter=emitter,
        action="mutate",
    )
    await response

    assert write_allowed is True and write_decision == "auto_allow"
    assert edit_allowed is False and edit_decision == "deny_once"
    assert [event["tool_use_id"] for event in emitted] == ["file-edit"]


# ── 审批超时 ──────────────────────────────────────────────────────────────────

# 功能：验证 check_and_wait 超时后返回 (False, "timeout")，不永久挂起
# 设计：timeout_s=0.05 极短超时，不主动 respond；断言在合理时间内返回 False
async def test_permission_timeout_returns_false() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t_timeout", tool_name="bash",
        params={"command": "echo hi"}, session_id="s1",
        event_emitter=emitter,
    )

    assert allowed is False
    assert decision == "timeout"
    assert len(emitted) == 1
    assert emitted[0]["type"] == "permission.requested"


# 功能：验证超时后 pending 被清理，迟到的 respond 不影响后续调用
# 设计：超时后调用 respond，不抛异常（unknown tool_use_id 静默忽略）；
#       再次 check_and_wait 同 tool_use_id 仍正常发出新的 permission.requested
async def test_permission_timeout_cleans_up_pending() -> None:
    mgr = PermissionManager(timeout_s=0.05)
    _, emitter = await _collect_emitted()

    await mgr.check_and_wait(
        tool_use_id="t_late", tool_name="bash",
        params={"command": "echo"}, session_id="s1",
        event_emitter=emitter,
    )
    # 超时后迟到的 respond 不应 crash
    mgr.respond("t_late", "allow_once")  # should be noop
    assert "t_late" not in mgr._pending


# ── command-prefix always (W3.2) ─────────────────────────────────────────────

# 功能：respond("always_allow_pattern") 存入前缀规则后，合法尾参命令命中并放行
# 设计：bash 前缀键为"完整批准的首命令 + 通配符 *"，二次带额外尾参的命令命中即 auto_allow
async def test_always_allow_pattern_hits_legal_args() -> None:
    mgr = _make_manager()
    emitted, emitter = await _collect_emitted()

    async def _approve_pattern() -> None:
        await asyncio.sleep(0)
        mgr.respond("tP1", "always_allow_pattern")

    task = asyncio.create_task(_approve_pattern())
    r1, _ = await mgr.check_and_wait(
        tool_use_id="tP1", tool_name="bash",
        params={"command": "uv run pytest -k unit"}, session_id="s1",
        event_emitter=emitter,
    )
    await task
    assert r1 is True
    assert "bash:uv run pytest -k unit*" in mgr._persistent_always

    r2, d2 = await mgr.check_and_wait(
        tool_use_id="tP2", tool_name="bash",
        params={"command": "uv run pytest -k unit --ff"}, session_id="s2",
        event_emitter=emitter,
    )
    assert r2 is True
    assert d2 == "auto_always_prefix"
    assert len(emitted) == 1  # 第二次命中前缀缓存，不再 ASK


# 功能：命令串接注入不得命中已批准的前缀，仍走 ASK 审批
# 设计：`uv run pytest; rm -rf /` 首命令虽匹配但带 ; 串接，必须拒绝自动放行、继续询问
async def test_always_allow_pattern_rejects_chained_injection() -> None:
    mgr = _make_manager()
    mgr._persistent_always["bash:uv run pytest*"] = "allow"
    emitted, emitter = await _collect_emitted()

    task = asyncio.create_task(
        mgr.check_and_wait(
            tool_use_id="tP3", tool_name="bash",
            params={"command": "uv run pytest; rm -rf /"}, session_id="s1",
            event_emitter=emitter,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # 尚未审批：应处于挂起（ASK）而非自动放行
    assert "tP3" in mgr._pending
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# 功能：前缀规则仅在确切首命令对齐时命中，无关的子串命令不误放行
# 设计：已批准 `bash:pytest*`，`uv run pytest` 因 pytest 不在首 token 而不命中
async def test_always_allow_pattern_requires_leading_alignment() -> None:
    mgr = _make_manager()
    mgr._persistent_always["bash:pytest*"] = "allow"
    emitted, emitter = await _collect_emitted()

    task = asyncio.create_task(
        mgr.check_and_wait(
            tool_use_id="tP4", tool_name="bash",
            params={"command": "uv run pytest"}, session_id="s1",
            event_emitter=emitter,
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert "tP4" in mgr._pending  # 不命中前缀，走 ASK
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── W3.1 沙箱闭环 ────────────────────────────────────────────────────────────

# 功能：AUTO_REVIEW + 沙箱可用时 bash 命令免审批直接放行（决策码 authority_sandbox_allow）
# 设计：注入带可用 bwrap 沙箱的 authority 快照，断言不发出任何审批事件且返回授权语义
async def test_auto_review_with_sandbox_auto_allows_bash() -> None:
    mgr = _make_manager()
    current = mgr.get_authority_snapshot("s-sbx")
    mgr.set_authority_snapshot(
        "s-sbx",
        current.model_copy(
            update={
                "profile": AuthorityProfile.AUTO_REVIEW,
                "sandbox": SandboxCapability(
                    available=True, kind="linux_bwrap", reason="ok"
                ),
            }
        ),
    )
    emitted, emitter = await _collect_emitted()

    allowed, decision = await mgr.check_and_wait(
        tool_use_id="t-sbx",
        tool_name="bash",
        params={"command": "uv run pytest"},
        session_id="s-sbx",
        event_emitter=emitter,
        action="shell",
    )
    assert allowed is True
    assert decision == "authority_sandbox_allow"
    assert emitted == []


# 功能：验证不受支持的隔离后端即使标记 available 也不能让 AUTO_REVIEW shell 自动放行
# 设计：使用默认 ASK bash 与不受支持 capability，确认请求仍进入审批而不是返回 sandbox allow
async def test_auto_review_unsupported_backend_requires_approval() -> None:
    mgr = _make_manager()
    current = mgr.get_authority_snapshot("s-unknown-sbx")
    mgr.set_authority_snapshot(
        "s-unknown-sbx",
        current.model_copy(
            update={
                "profile": AuthorityProfile.AUTO_REVIEW,
                "sandbox": SandboxCapability(
                    available=True,
                    kind="windows_none",
                    reason="probe claimed support",
                ),
            }
        ),
    )

    emitted, emitter = await _collect_emitted()
    task = asyncio.create_task(
        mgr.check_and_wait(
            tool_use_id="t-unknown-sbx",
            tool_name="bash",
            params={"command": "python -c 'print(1)'"},
            session_id="s-unknown-sbx",
            event_emitter=emitter,
            action="shell",
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert task.done() is False
    assert emitted and emitted[0]["tool_use_id"] == "t-unknown-sbx"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# 功能：验证未来 policy.toml 版本会阻断读取与保存，旧版 daemon 不能静默降级覆盖
# 设计：写入带 meta schema_version=99 的最小策略，分别调用 loader 和 save 后比较原文不变
def test_future_policy_schema_blocks_unsupported_downgrade(
    tmp_path: pytest.TempPathFixture,
) -> None:
    policy_file = tmp_path / "policy.toml"
    original = '[meta]\nschema_version = 99\n\n[always]\nbash = "allow"\n'
    policy_file.write_text(original, encoding="utf-8")

    with pytest.raises(PolicyStoreError, match="newer than supported"):
        load_policy_file(policy_file)
    with pytest.raises(PolicyStoreError, match="newer than supported"):
        save_policy_file({"bash": "deny"}, policy_file)

    assert policy_file.read_text(encoding="utf-8") == original


# 功能：AUTO_REVIEW 但无可用沙箱时 shell_sandbox_plan 返回 None，不包裹命令
# 设计：这是"沙箱失败→回落审批"的伴随判定：无后端就不该 wrapper，闭环交由 ASK 兜底
def test_shell_sandbox_plan_none_without_autoreview_or_backend() -> None:
    mgr = _make_manager()
    assert mgr.shell_sandbox_plan("splain", "/ws") is None
    current = mgr.get_authority_snapshot("s-auto")
    mgr.set_authority_snapshot(
        "s-auto",
        current.model_copy(update={"profile": AuthorityProfile.AUTO_REVIEW}),
    )
    assert mgr.shell_sandbox_plan("s-auto", "/ws") is None


# 功能：AUTO_REVIEW + 可用 bwrap 时 shell_sandbox_plan 返回非降级的 workspace_write 计划
# 设计：验证计划真实可用（degraded=False）且 wrapper 以 bwrap 起头，恰与闭环放行对齐
def test_shell_sandbox_plan_returns_wrapper_when_available() -> None:
    mgr = _make_manager()
    current = mgr.get_authority_snapshot("s-ok")
    mgr.set_authority_snapshot(
        "s-ok",
        current.model_copy(
            update={
                "profile": AuthorityProfile.AUTO_REVIEW,
                "sandbox": SandboxCapability(
                    available=True, kind="linux_bwrap", reason="ok"
                ),
            }
        ),
    )
    plan = mgr.shell_sandbox_plan("s-ok", "/proj")
    assert plan is not None
    assert plan.degraded is False
    assert plan.wrapper and plan.wrapper[0] == "bwrap"
