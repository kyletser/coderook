from __future__ import annotations

import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.authority import (
    RuntimeMode,
    SandboxCapability,
    detect_sandbox_capability,
)
from code_rook.core.features import labs_enabled as process_labs_enabled

HTTP_API_VERSION = "v1"
RUNTIME_EVENT_SCHEMA_VERSION = 1
STREAM_JSON_SCHEMA_VERSIONS = (1,)

STABLE_FEATURE_FLAGS = {
    "durable_threads": True,
    "durable_turns": True,
    "event_cursor_replay": True,
    "sse_cursor_replay": True,
    "turn_receipts": True,
    "interrupt": True,
    "steer": True,
    "permission_response": True,
    "workspace_diff": True,
    "provider_catalog": True,
    "configuration_readiness": True,
    "checkpoints": True,
    "change_center": True,
    "bounded_goal_loop": True,
    "base_subagents": True,
    "skills": True,
    "mcp_tools": True,
    "memory": True,
}
LABS_FEATURE_FLAGS = {
    "fleet_workers": True,
    "declarative_workflows": True,
    "hooks_v2": True,
    "mcp_resources_prompts": True,
    "vscode_experimental": True,
}
INTERNAL_FEATURE_FLAGS = {
    "runtime_projection": True,
    "trace_degraded_state": True,
    "wire_protocol_generation": True,
}


class RuntimeFeatureFlags(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stable: dict[str, bool] = Field(default_factory=dict)
    labs: dict[str, bool] = Field(default_factory=dict)
    internal: dict[str, bool] = Field(default_factory=dict)


class RuntimeSandboxStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    platform: str
    capability: SandboxCapability
    state: Literal["enforcement_available", "degraded"]
    windows_forced_sandbox: Literal["available", "unavailable", "not_applicable"]


# 返回旧调用方省略新增字段时使用的完整 feature flag 默认快照
def _default_feature_flags() -> RuntimeFeatureFlags:
    return RuntimeFeatureFlags(
        stable=dict(STABLE_FEATURE_FLAGS),
        labs=dict(LABS_FEATURE_FLAGS),
        internal=dict(INTERNAL_FEATURE_FLAGS),
    )


# 返回当前宿主平台的默认 sandbox capability 状态
def _default_sandbox_status() -> RuntimeSandboxStatus:
    target_platform = sys.platform
    detected = detect_sandbox_capability(platform=target_platform)
    windows_status: Literal["available", "unavailable", "not_applicable"] = (
        "available"
        if target_platform == "win32" and detected.available
        else "unavailable"
        if target_platform == "win32"
        else "not_applicable"
    )
    return RuntimeSandboxStatus(
        platform=target_platform,
        capability=detected,
        state="enforcement_available" if detected.available else "degraded",
        windows_forced_sandbox=windows_status,
    )


# 返回 stream-json 当前支持版本副本，避免调用方修改全局 tuple
def _default_stream_json_versions() -> list[int]:
    return list(STREAM_JSON_SCHEMA_VERSIONS)


class RuntimeCapabilitiesSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    api_version: str = HTTP_API_VERSION
    runtime_modes: list[RuntimeMode]
    features: list[str]
    feature_flags: RuntimeFeatureFlags = Field(default_factory=_default_feature_flags)
    labs_enabled: bool = False
    sandbox: RuntimeSandboxStatus = Field(default_factory=_default_sandbox_status)
    runtime_event_schema_version: int = RUNTIME_EVENT_SCHEMA_VERSION
    stream_json_schema_versions: list[int] = Field(
        default_factory=_default_stream_json_versions
    )


# 从协议常量和实际 sandbox 探测构建 IPC/HTTP 共用的能力快照
def build_runtime_capabilities(
    version: str,
    *,
    sandbox: SandboxCapability | None = None,
    platform_name: str | None = None,
    labs_enabled: bool | None = None,
) -> RuntimeCapabilitiesSnapshot:
    target_platform = (
        platform_name
        or ("win32" if sandbox is not None and sandbox.kind == "windows_none" else None)
        or sys.platform
    )
    detected = sandbox or detect_sandbox_capability(platform=target_platform)
    windows_status: Literal["available", "unavailable", "not_applicable"] = (
        "available"
        if target_platform == "win32" and detected.available
        else "unavailable"
        if target_platform == "win32"
        else "not_applicable"
    )
    flags = _default_feature_flags()
    return RuntimeCapabilitiesSnapshot(
        version=version,
        runtime_modes=list(RuntimeMode),
        features=[name for name, enabled in flags.stable.items() if enabled],
        feature_flags=flags,
        labs_enabled=(
            process_labs_enabled() if labs_enabled is None else labs_enabled
        ),
        sandbox=RuntimeSandboxStatus(
            platform=target_platform,
            capability=detected,
            state="enforcement_available" if detected.available else "degraded",
            windows_forced_sandbox=windows_status,
        ),
        stream_json_schema_versions=list(STREAM_JSON_SCHEMA_VERSIONS),
    )
