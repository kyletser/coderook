from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SkillScope = Literal["builtin", "user", "project", "legacy"]
SkillTrust = Literal["builtin", "trusted", "untrusted"]
SkillIntegrity = Literal["verified", "mismatch", "unmanaged"]
SkillToolName = Annotated[str, Field(min_length=1, max_length=128)]


class SkillManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    description: str = Field(default="", max_length=2_048)
    allowed_tools: tuple[SkillToolName, ...] = ()


class SkillInstallMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[2] = 2
    source: str = Field(min_length=1)
    installed_at: str = Field(min_length=1)
    trust: SkillTrust
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest: SkillManifest
    system_prompt_template: str = Field(max_length=512 * 1_024)
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    expected_digest: str = ""
    source: str = Field(max_length=4_096)
    installed_at: str
    trust: SkillTrust
    scope: SkillScope
    path: str
    integrity: SkillIntegrity

    @property
    # 返回兼容旧调用方的 skill 名称
    def name(self) -> str:
        return self.manifest.name

    @property
    # 返回兼容旧调用方的 skill 描述
    def description(self) -> str:
        return self.manifest.description

    @property
    # 返回兼容旧调用方的可用工具列表副本
    def allowed_tools(self) -> list[str]:
        return list(self.manifest.allowed_tools)


class SkillInstallPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source: str
    target: str
    scope: Literal["user", "project"]
    trust: Literal["trusted", "untrusted"]
    digest: str
    files: tuple[str, ...]
    overwrite: bool


class SkillAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    scope: SkillScope
    trust: SkillTrust
    source: str
    path: str
    digest: str
    expected_digest: str
    integrity: SkillIntegrity
