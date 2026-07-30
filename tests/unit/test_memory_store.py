from __future__ import annotations

import json
from pathlib import Path

from code_rook.core.memory import MemoryStore
from code_rook.core.tools.builtin.memory import (
    MemoryForgetTool,
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


# 功能：验证明确长期规则会自动记忆，同时 API Key 在落盘前被脱敏
# 设计：提示同时包含中文记忆触发词和 sk- 密钥，检查记录正文而非仅检查返回值
def test_explicit_memory_redacts_secrets(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")

    record = store.remember_explicit_prompt(
        "记住以后使用 uv，密钥是 sk-secretvalue123456",
        source_run_id="run-secret",
    )

    assert record is not None
    assert "sk-secretvalue123456" not in record.body
    assert "[REDACTED]" in record.body
    assert record.source_run_id == "run-secret"
