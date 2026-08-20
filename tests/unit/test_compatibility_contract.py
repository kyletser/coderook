from __future__ import annotations

from datetime import UTC, datetime

from code_rook.core.api.service import RuntimeApiService
from code_rook.core.compatibility import (
    HTTP_API_VERSION,
    RUNTIME_EVENT_SCHEMA_VERSION,
    STREAM_JSON_SCHEMA_VERSIONS,
)
from code_rook.core.headless import HeadlessEnvelope, HeadlessRunResult
from code_rook.core.runtime.models import RuntimeEventRecord


# 功能：验证公开 capabilities 与代码中的协议常量保持同一版本事实源
# 设计：无须构造 runtime 即可调用纯能力方法，固定 HTTP、SSE record 和 stream-json 三类协商值
async def test_runtime_capabilities_publish_supported_contract_versions() -> None:
    service = object.__new__(RuntimeApiService)

    capabilities = await service.capabilities()

    assert capabilities["api_version"] == HTTP_API_VERSION == "v1"
    assert capabilities["runtime_event_schema_version"] == RUNTIME_EVENT_SCHEMA_VERSION == 1
    assert capabilities["stream_json_schema_versions"] == list(
        STREAM_JSON_SCHEMA_VERSIONS
    ) == [1]
    assert "workspace_diff" in capabilities["features"]
    assert "permission_response" in capabilities["features"]


# 功能：验证 v1 durable event 与 headless envelope 的默认 schema 版本不会静默漂移
# 设计：实例化三种公开模型并对齐兼容常量，避免只更新文档或 capabilities 而遗漏真实序列化结果
def test_public_model_defaults_match_compatibility_constants() -> None:
    event = RuntimeEventRecord(
        thread_id="thread-contract",
        turn_id="turn-contract",
        seq=1,
        type="turn.started",
        payload={},
        ts=datetime(2026, 8, 20, tzinfo=UTC),
    )
    envelope = HeadlessEnvelope(
        kind="event",
        sequence=1,
        run_id="run-contract",
        type="run.started",
        payload={},
    )
    result = HeadlessRunResult(
        run_id="run-contract",
        status="success",
        exit_code=0,
    )

    assert event.schema_version == RUNTIME_EVENT_SCHEMA_VERSION
    assert envelope.schema_version in STREAM_JSON_SCHEMA_VERSIONS
    assert result.schema_version in STREAM_JSON_SCHEMA_VERSIONS
