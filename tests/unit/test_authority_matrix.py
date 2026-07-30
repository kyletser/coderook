from __future__ import annotations

import asyncio

import pytest

from code_rook.core.authority import (
    AuthorityDecision,
    AuthorityProfile,
    AuthoritySnapshot,
    RuntimeMode,
    ToolAction,
    WorkspaceTrust,
    evaluate_action,
    narrow_child_authority,
)
from code_rook.core.permissions.manager import PermissionManager


@pytest.mark.parametrize(
    ("mode", "profile", "action", "expected"),
    [
        (RuntimeMode.PLAN, AuthorityProfile.ASK, ToolAction.READ, AuthorityDecision.ALLOW),
        (RuntimeMode.PLAN, AuthorityProfile.FULL_ACCESS, ToolAction.MUTATE, AuthorityDecision.DENY),
        (RuntimeMode.PLAN, AuthorityProfile.FULL_ACCESS, ToolAction.SHELL, AuthorityDecision.DENY),
        (RuntimeMode.ACT, AuthorityProfile.ASK, ToolAction.MUTATE, AuthorityDecision.ASK),
        (RuntimeMode.OPERATE, AuthorityProfile.ASK, ToolAction.MUTATE, AuthorityDecision.ASK),
        (
            RuntimeMode.ACT,
            AuthorityProfile.AUTO_REVIEW,
            ToolAction.MUTATE,
            AuthorityDecision.ALLOW,
        ),
        (
            RuntimeMode.OPERATE,
            AuthorityProfile.AUTO_REVIEW,
            ToolAction.SHELL,
            AuthorityDecision.ASK,
        ),
        (
            RuntimeMode.ACT,
            AuthorityProfile.FULL_ACCESS,
            ToolAction.EXTERNAL,
            AuthorityDecision.ALLOW,
        ),
    ],
)
# 功能：验证 Mode 与 AuthorityProfile 的组合不会互相隐式提权
# 设计：用表驱动覆盖三种 mode 和三种 posture 的关键边界，特别固定 Plan 与 Operate 语义
def test_authority_matrix(
    mode: RuntimeMode,
    profile: AuthorityProfile,
    action: ToolAction,
    expected: AuthorityDecision,
) -> None:
    result = evaluate_action(
        AuthoritySnapshot(mode=mode, profile=profile),
        action,
    )

    assert result.decision == expected


# 功能：验证未知 capability 和 task scope 外 action 默认拒绝
# 设计：分别传入未知字符串和被 scope 排除的已知 action，覆盖两类 fail-closed 路径
def test_unknown_and_out_of_scope_actions_fail_closed() -> None:
    snapshot = AuthoritySnapshot(allowed_actions=frozenset({ToolAction.READ}))

    assert evaluate_action(snapshot, "future_admin").decision == AuthorityDecision.DENY
    assert evaluate_action(snapshot, ToolAction.MUTATE).decision == AuthorityDecision.DENY


# 功能：验证 child authority 只能取父权限、profile ceiling 与 task scope 的交集
# 设计：让 child 请求 Full Access、trusted 和全 action，断言低权限父级仍决定最终上限
def test_child_authority_cannot_elevate_parent() -> None:
    parent = AuthoritySnapshot(
        mode=RuntimeMode.PLAN,
        profile=AuthorityProfile.ASK,
        workspace_trust=WorkspaceTrust.UNTRUSTED,
        allowed_actions=frozenset({ToolAction.READ}),
    )

    child = narrow_child_authority(
        parent,
        profile_ceiling=AuthorityProfile.FULL_ACCESS,
        allowed_actions=frozenset(ToolAction),
        requested_mode=RuntimeMode.OPERATE,
        requested_trust=WorkspaceTrust.TRUSTED,
    )

    assert child.mode == RuntimeMode.PLAN
    assert child.profile == AuthorityProfile.ASK
    assert child.workspace_trust == WorkspaceTrust.UNTRUSTED
    assert child.allowed_actions == frozenset({ToolAction.READ})


# 功能：验证 Plan 的 authority 拒绝优先于历史 always-allow 权限缓存
# 设计：预置 mutation 工具的持久放行记录，再显式传 action，确认权限层在读取缓存前 fail closed
async def test_plan_denial_cannot_be_bypassed_by_permission_cache() -> None:
    manager = PermissionManager()
    manager._persistent_always["edit_file"] = "allow"
    manager.set_authority_snapshot(
        "session-plan",
        AuthoritySnapshot(
            mode=RuntimeMode.PLAN,
            profile=AuthorityProfile.FULL_ACCESS,
        ),
    )

    # 在拒绝路径中不应调用事件发送器
    async def emit_unexpected(_event: dict[str, object]) -> None:
        pytest.fail("Plan denial must not request approval")

    allowed, decision = await manager.check_and_wait(
        tool_use_id="tool-1",
        tool_name="edit_file",
        params={"path": "x", "old_text": "a", "new_text": "b"},
        session_id="session-plan",
        event_emitter=emit_unexpected,
        action=ToolAction.MUTATE,
    )

    assert not allowed
    assert decision == "authority_denied"


# 功能：验证 Operate + Ask 的写操作仍然进入人工审批而不是自动放行
# 设计：用异步 responder 完成真实 pending Future，并断言 permission.requested 事件确实产生
async def test_operate_ask_mutation_still_requests_approval() -> None:
    manager = PermissionManager()
    manager.set_authority_snapshot(
        "session-operate",
        AuthoritySnapshot(
            mode=RuntimeMode.OPERATE,
            profile=AuthorityProfile.ASK,
        ),
    )
    emitted: list[dict[str, object]] = []

    # 收集权限请求事件
    async def collect(event: dict[str, object]) -> None:
        emitted.append(event)

    # 在权限请求挂起后模拟用户单次允许
    async def respond() -> None:
        await asyncio.sleep(0)
        manager.respond("tool-operate", "allow_once")

    response = asyncio.create_task(respond())
    allowed, decision = await manager.check_and_wait(
        tool_use_id="tool-operate",
        tool_name="edit_file",
        params={"path": "x", "old_text": "a", "new_text": "b"},
        session_id="session-operate",
        event_emitter=collect,
        action=ToolAction.MUTATE,
    )
    await response

    assert allowed
    assert decision == "allow_once"
    assert [event["type"] for event in emitted] == ["permission.requested"]
