from __future__ import annotations

from code_rook.core.hooks import HookDecision, HookManager


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
