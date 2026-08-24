from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from code_rook.core.memory import MemoryStore
from code_rook.core.tools.builtin.memory import (
    MemoryEditTool,
    MemoryExpireTool,
    MemoryForgetTool,
    MemoryPinTool,
    MemorySaveTool,
    MemorySearchTool,
)


# 功能：验证项目记忆可保存、召回，并在索引中保留可审计 ID
# 设计：使用临时目录写真实文件，同时用中文查询覆盖中英文词法切分路径
def test_memory_store_save_search_and_index(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    record = store.save(
        name="测试命令",
        description="项目测试流程",
        mem_type="project",
        body="修改代码后运行 uv run pytest。",
        source_session_id="sess-1",
        source_run_id="run-1",
    )

    found = store.search("如何运行项目测试", limit=5)

    assert found == [record]
    index = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert record.id in index
    assert "测试命令" in index


# 功能：验证同名记忆更新时保持 ID 和创建时间，避免索引产生重复事实
# 设计：连续保存同名记录并断言只有一条文件记录，覆盖确定性覆盖语义
def test_memory_store_updates_same_name(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    first = store.save(
        name="package manager",
        description="old",
        mem_type="feedback",
        body="Use npm.",
    )
    second = store.save(
        name="package manager",
        description="new",
        mem_type="feedback",
        body="Use pnpm.",
    )

    assert second.id == first.id
    assert second.created_at == first.created_at
    assert [item.body for item in store.list_all()] == ["Use pnpm."]


# 功能：验证 memory 工具完成保存、检索和删除的完整生命周期
# 设计：直接调用真实工具并解析 search JSON，覆盖参数校验、来源写入和删除结果
async def test_memory_tools_roundtrip(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    save_result = await MemorySaveTool(store, "sess-1", "run-1").invoke(
        {
            "name": "lint command",
            "description": "quality gate",
            "type": "project",
            "body": "Run uv run ruff check .",
        }
    )
    memory_id = save_result.content.split("=", 1)[1]

    search_result = await MemorySearchTool(store).invoke({"query": "lint quality"})
    payload = json.loads(search_result.content)
    forget_result = await MemoryForgetTool(store).invoke({"memory_id": memory_id})

    assert payload[0]["source_run_id"] == "run-1"
    assert forget_result.content == f"forgot memory_id={memory_id}"
    assert store.list_all() == []


# 功能：验证长期规则必须确认后才会保存，并在落盘前脱敏 API Key
# 设计：同一提示先以默认未确认调用再显式确认，证明宽泛关键词不会触发静默写入
def test_explicit_memory_requires_confirmation_and_redacts_secrets(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    prompt = "记住以后使用 uv，密钥是 sk-secretvalue123456"

    skipped = store.remember_explicit_prompt(prompt, source_run_id="run-secret")

    record = store.remember_explicit_prompt(
        prompt,
        source_run_id="run-secret",
        confirmed=True,
    )

    assert skipped is None
    assert record is not None
    assert "sk-secretvalue123456" not in record.body
    assert "[REDACTED]" in record.body
    assert record.source_run_id == "run-secret"


# 功能：验证自动记忆可关闭且非法设置按关闭处理
# 设计：先检查默认 prompt，再持久化 off 并注入损坏文件，覆盖正常设置与 fail-closed 读取
def test_memory_auto_save_settings_are_persistent_and_fail_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")

    assert store.load_settings().auto_save == "prompt"
    assert store.set_auto_save("off").auto_save == "off"
    assert MemoryStore(tmp_path / "memory").load_settings().auto_save == "off"

    store.settings_path.write_text('{"auto_save":"invalid"}', encoding="utf-8")

    assert store.load_settings().auto_save == "off"


# 功能：验证记忆支持 edit、pin、expire 且过期项不会进入默认召回
# 设计：通过公开工具修改真实记录，先设未来时间再设过去时间，兼顾排序和审计保留语义
async def test_memory_governance_edit_pin_and_expire(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    record = store.save(
        name="runtime",
        description="old",
        mem_type="project",
        body="Use the old command.",
    )
    edit = await MemoryEditTool(store).invoke(
        {
            "memory_id": record.id,
            "description": "verified command",
            "body": "Use uv run pytest.",
        }
    )
    pin = await MemoryPinTool(store).invoke(
        {"memory_id": record.id, "pinned": True}
    )
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    expire = await MemoryExpireTool(store).invoke(
        {"memory_id": record.id, "expires_at": future}
    )

    active = store.list_all()
    assert edit.is_error is False
    assert pin.content.startswith("pinned")
    assert expire.is_error is False
    assert active[0].body == "Use uv run pytest."
    assert active[0].pinned is True

    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await MemoryExpireTool(store).invoke(
        {"memory_id": record.id, "expires_at": past}
    )

    assert store.list_all() == []
    assert store.get(record.id) is not None
