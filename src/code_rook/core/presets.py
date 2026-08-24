from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.authority import AuthorityProfile, RuntimeMode, ToolAction
from code_rook.core.capabilities import CapabilityStability


class AgentPreset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    name: str = Field(min_length=1)
    stability: CapabilityStability
    prompt_layers: tuple[str, ...] = ()
    tool_allowlist: frozenset[str] | None = None
    provider_policy: str = "catalog"
    default_mode: RuntimeMode = RuntimeMode.ACT
    default_profile: AuthorityProfile = AuthorityProfile.ASK
    tool_program: bool = False
    worker_backend: str = "builtin"
    authority_ceiling: frozenset[ToolAction] = Field(
        default_factory=lambda: frozenset(ToolAction)
    )
    sandbox_requirement: str = "honest_degradation"

    # 返回 preset 全字段的确定性 SHA-256 摘要供 Session 冻结组合
    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


STANDARD_PRESET = AgentPreset(
    id="standard",
    name="Standard",
    stability=CapabilityStability.STABLE,
)

MINIMAL_PRESET = AgentPreset(
    id="minimal",
    name="Minimal",
    stability=CapabilityStability.STABLE,
    tool_allowlist=frozenset(
        {"file", "read_file", "edit_file", "str_replace_editor", "bash"}
    ),
)

TOOL_PROGRAM_PRESET = AgentPreset(
    id="tool-program",
    name="Tool Program",
    stability=CapabilityStability.LABS,
    tool_program=True,
)

AGENT_PRESETS: tuple[AgentPreset, ...] = (
    STANDARD_PRESET,
    MINIMAL_PRESET,
    TOOL_PROGRAM_PRESET,
)


# 按稳定 ID 返回内建 Agent Preset，未知值明确失败
def get_agent_preset(preset_id: str) -> AgentPreset:
    for preset in AGENT_PRESETS:
        if preset.id == preset_id:
            return preset
    raise KeyError(f"unknown agent preset: {preset_id}")
