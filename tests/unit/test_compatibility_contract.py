from __future__ import annotations

from datetime import UTC, datetime

import code_rook
from code_rook.core.api.service import RuntimeApiService
from code_rook.core.app import CoreApp
from code_rook.core.authority import AuthoritySnapshot, SandboxCapability
from code_rook.core.compatibility import (
    HTTP_API_VERSION,
    RUNTIME_EVENT_SCHEMA_VERSION,
    STREAM_JSON_SCHEMA_VERSIONS,
)
from code_rook.core.headless import HeadlessEnvelope, HeadlessRunResult
from code_rook.core.permissions.manager import PermissionManager
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
    assert "bounded_goal_loop" in capabilities["features"]
    assert "base_subagents" in capabilities["features"]
    assert "skills" in capabilities["features"]
    assert "mcp_tools" in capabilities["features"]
    assert "memory" in capabilities["features"]
    assert capabilities["feature_flags"]["stable"]["durable_threads"] is True
    assert capabilities["feature_flags"]["stable"]["bounded_goal_loop"] is True
    assert capabilities["feature_flags"]["labs"]["fleet_workers"] is True
    assert isinstance(capabilities["labs_enabled"], bool)
    assert "skills_v2" not in capabilities["feature_flags"]["labs"]
    assert capabilities["feature_flags"]["internal"]["runtime_projection"] is True
    assert capabilities["sandbox"]["state"] in {
        "enforcement_available",
        "partial_enforcement",
        "degraded",
    }


# 功能：验证 IPC 与 HTTP capabilities 消费同一快照且明确报告 Windows 无强制沙箱
# 设计：向两条 facade 注入同一 windows_none authority，排除宿主平台探测差异后比较完整 JSON
async def test_ipc_and_http_capabilities_share_one_snapshot() -> None:
    sandbox = SandboxCapability(
        available=False,
        kind="windows_none",
        reason="no OS isolation backend",
    )
    manager = PermissionManager()
    manager.set_authority_snapshot(
        "__runtime_capabilities__",
        AuthoritySnapshot(sandbox=sandbox),
    )
    app = CoreApp()
    app._permission_manager = manager  # type: ignore[attr-defined]
    service = object.__new__(RuntimeApiService)
    service._permission_manager = manager  # type: ignore[attr-defined]

    ipc = await app._runtime_capabilities_handler({})  # type: ignore[attr-defined]
    http = await service.capabilities()

    assert ipc.model_dump(mode="json") == http
    assert ipc.version == code_rook.__version__
    assert ipc.sandbox.capability.kind == "windows_none"
    assert ipc.sandbox.windows_forced_sandbox == "unavailable"
    assert ipc.runtime_event_schema_version == RUNTIME_EVENT_SCHEMA_VERSION
    assert ipc.stream_json_schema_versions == list(STREAM_JSON_SCHEMA_VERSIONS)


# 功能：验证 capabilities 同时区分 Labs 能力可用性与当前启用状态
# 设计：直接向纯构建器注入开关，避免测试进程环境影响公开契约断言
def test_runtime_capabilities_report_labs_activation_separately() -> None:
    from code_rook.core.compatibility import build_runtime_capabilities

    disabled = build_runtime_capabilities("test", labs_enabled=False)
    enabled = build_runtime_capabilities("test", labs_enabled=True)

    assert disabled.feature_flags.labs["declarative_workflows"] is True
    assert disabled.labs_enabled is False
    assert enabled.labs_enabled is True


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
