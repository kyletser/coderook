from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeMode(StrEnum):
    PLAN = "plan"
    ACT = "act"
    OPERATE = "operate"


class AuthorityProfile(StrEnum):
    ASK = "ask"
    AUTO_REVIEW = "auto_review"
    FULL_ACCESS = "full_access"


class WorkspaceTrust(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"


class ToolAction(StrEnum):
    READ = "read"
    MUTATE = "mutate"
    SHELL = "shell"
    EXTERNAL = "external"


class SandboxCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    kind: Literal["none", "windows_none", "linux_bwrap", "macos_seatbelt"]
    reason: str


# 返回未探测平台时使用的保守 sandbox 状态
def _default_sandbox() -> SandboxCapability:
    return SandboxCapability(
        available=False,
        kind="none",
        reason="sandbox capability has not been detected",
    )


# 返回默认允许进入 authority 评估的全部已知 action
def _default_actions() -> frozenset[ToolAction]:
    return frozenset(ToolAction)


class AuthoritySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: RuntimeMode = RuntimeMode.ACT
    profile: AuthorityProfile = AuthorityProfile.ASK
    workspace_trust: WorkspaceTrust = WorkspaceTrust.UNTRUSTED
    sandbox: SandboxCapability = Field(default_factory=_default_sandbox)
    allowed_actions: frozenset[ToolAction] = Field(default_factory=_default_actions)
