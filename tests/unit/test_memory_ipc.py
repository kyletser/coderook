from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from code_rook.core.app import CoreApp


# 功能：验证 Memory typed IPC 覆盖 add/edit/pin/expire/delete 和设置完整生命周期
# 设计：在隔离工作区直接调用 Core handler 并读取每次权威返回，避免 mock 掩盖落盘与过期语义
async def test_memory_ipc_governance_roundtrip(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    app = CoreApp()

    added = await app._memory_add_handler(
        {
            "name": "test command",
            "description": "quality gate",
            "memory_type": "project",
            "body": "Run pytest.",
            "source_session_id": "sess-1",
        }
    )
    memory_id = added.memory.id
    edited = await app._memory_edit_handler(
        {"memory_id": memory_id, "body": "Run uv run pytest."}
    )
    pinned = await app._memory_pin_handler(
        {"memory_id": memory_id, "pinned": True}
    )
    expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    expiring = await app._memory_expire_handler(
        {"memory_id": memory_id, "expires_at": expires_at}
    )
    settings = await app._memory_settings_set_handler({"auto_save": "off"})
    listed = await app._memory_list_handler({"include_expired": True})
    deleted = await app._memory_delete_handler({"memory_id": memory_id})

    assert edited.memory.body == "Run uv run pytest."
    assert edited.memory.source_session_id == "sess-1"
    assert pinned.memory.pinned is True
    assert expiring.memory.expires_at is not None
    assert settings.settings.auto_save == "off"
    assert listed.settings.auto_save == "off"
    assert [item.id for item in listed.memories] == [memory_id]
    assert deleted.deleted is True


# 功能：验证 memory.list 默认返回 prompt 策略且可选择隐藏过期记录
# 设计：先经 typed add 创建记录再设过去时间，对比 active-only 与审计列表两个投影
async def test_memory_list_can_hide_expired_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    app = CoreApp()
    added = await app._memory_add_handler({"name": "old", "body": "Old rule."})
    memory_id = added.memory.id
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await app._memory_expire_handler(
        {"memory_id": memory_id, "expires_at": past}
    )

    active = await app._memory_list_handler({"include_expired": False})
    audit = await app._memory_list_handler({"include_expired": True})

    assert active.settings.auto_save == "prompt"
    assert active.memories == []
    assert audit.memories[0].expired is True
