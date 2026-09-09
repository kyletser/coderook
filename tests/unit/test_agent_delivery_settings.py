from __future__ import annotations

from pathlib import Path

from code_rook.core.agent_runtime.settings import (
    AgentDeliverySettings,
    AgentDeliverySettingsStore,
)
from code_rook.core.app import CoreApp
from code_rook.core.config import CodeRookConfig


# 功能：验证交付设置写入用户状态后可在下一次 Core 启动时恢复
# 设计：使用临时 JSON 文件执行真实原子保存与重新加载，断言两个模式作为一个快照往返
def test_agent_delivery_settings_roundtrip(tmp_path: Path) -> None:
    store = AgentDeliverySettingsStore(tmp_path / "agent-settings.json")
    expected = AgentDeliverySettings(steering_mode="all", follow_up_mode="one-at-a-time")

    store.save(expected)

    assert store.load(AgentDeliverySettings()) == expected


# 功能：验证设置命令会持久化并立即更新当前 Core 的纠偏和后续消息策略
# 设计：以真实 Core 交互管理器和最小 SessionManager 替身调用 handler，覆盖即时生效与返回协议
async def test_agent_settings_handler_applies_without_restart(tmp_path: Path) -> None:
    class _Sessions:
        # 记录 Core 下发的后续消息模式
        def __init__(self) -> None:
            self.follow_up_mode = ""

        # 接收活动 SessionManager 的即时模式更新
        def set_follow_up_mode(self, mode: str) -> None:
            self.follow_up_mode = mode

    app = CoreApp()
    app._config = CodeRookConfig()
    app._agent_settings_store = AgentDeliverySettingsStore(tmp_path / "agent-settings.json")
    sessions = _Sessions()
    app._sessions = sessions  # type: ignore[assignment]

    result = await app._agent_settings_set_handler(
        {"steering_mode": "all", "follow_up_mode": "all"}
    )
    app._interaction_manager.register_run("run-settings")
    assert app._interaction_manager.steer("run-settings", "first")
    assert app._interaction_manager.steer("run-settings", "second")

    assert result.settings.steering_mode == "all"
    assert result.settings.follow_up_mode == "all"
    assert app._interaction_manager.drain_steering("run-settings") == ["first", "second"]
    assert sessions.follow_up_mode == "all"
    assert app._agent_settings_store.load(AgentDeliverySettings()).follow_up_mode == "all"
