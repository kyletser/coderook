from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from code_rook.core.skills.loader import (
    _METADATA_FILE,
    _parse_skill_file,
    digest_skill_path,
)
from code_rook.core.skills.models import (
    Skill,
    SkillAuditRecord,
    SkillInstallMetadata,
    SkillInstallPreview,
)

InstallScope = Literal["user", "project"]
InstallTrust = Literal["trusted", "untrusted"]


class SkillManagerError(ValueError):
    pass


class SkillConfirmationRequired(SkillManagerError):
    # 保存安装 preview，调用方可在用户确认后用相同参数重试
    def __init__(self, preview: SkillInstallPreview) -> None:
        super().__init__("skill installation requires preview confirmation")
        self.preview = preview


# 返回当前 UTC 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 验证目标是指定 skill 根目录的直接子项
def _validate_target(base: Path, target: Path) -> None:
    resolved_base = base.resolve()
    resolved_target = target.resolve()
    if resolved_target.parent != resolved_base:
        raise SkillManagerError(f"skill target escapes managed directory: {target}")


# 仅删除已验证位于 managed skill 根目录内的文件或目录
def _remove_target(base: Path, target: Path) -> None:
    _validate_target(base, target)
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)


class SkillManager:
    # 初始化项目和用户 skill 根目录，所有项目写入固定落到 .coderook/skills
    def __init__(
        self,
        project_root: Path,
        *,
        user_skills_dir: Path | None = None,
    ) -> None:
        self._project_root = project_root.resolve()
        self._user_dir = user_skills_dir or Path("~/.coderook/skills").expanduser()

    # 返回 scope 唯一允许写入的受管目录
    def _base(self, scope: InstallScope) -> Path:
        return (
            self._project_root / ".coderook" / "skills"
            if scope == "project"
            else self._user_dir
        )

    # 解析本地安装源为主说明文件，并拒绝符号链接和缺失 SKILL.md
    def _source_entry(self, source: str) -> Path:
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise SkillManagerError(f"skill source not found: {source}")
        if path.is_symlink():
            raise SkillManagerError(f"skill source must not be a symbolic link: {source}")
        if path.is_file():
            if path.suffix.lower() != ".md":
                raise SkillManagerError("skill source file must be Markdown")
            return path
        entry = path / "SKILL.md"
        if not entry.is_file():
            raise SkillManagerError(f"skill directory is missing SKILL.md: {source}")
        for child in path.rglob("*"):
            if child.is_symlink():
                raise SkillManagerError(f"skill source contains a symbolic link: {child}")
        return entry

    # 构造安装 preview，不创建目标目录或修改任何 skill
    def preview_install(
        self,
        source: str,
        *,
        scope: InstallScope = "project",
        trust: InstallTrust = "untrusted",
    ) -> SkillInstallPreview:
        entry = self._source_entry(source)
        skill = _parse_skill_file(entry, scope)
        base = self._base(scope)
        target = base / skill.name
        _validate_target(base, target)
        source_root = entry.parent if entry.name == "SKILL.md" else entry
        files = (
            tuple(
                sorted(
                    child.relative_to(source_root).as_posix()
                    for child in source_root.rglob("*")
                    if child.is_file() and child.name != _METADATA_FILE
                )
            )
            if source_root.is_dir()
            else ("SKILL.md",)
        )
        return SkillInstallPreview(
            name=skill.name,
            source=str(entry.parent if entry.name == "SKILL.md" else entry),
            target=str(target),
            scope=scope,
            trust=trust,
            digest=digest_skill_path(entry),
            files=files,
            overwrite=target.exists(),
        )

    # 经 preview 明确确认后原子安装 skill，并在落盘后重新验证 digest
    def install(
        self,
        source: str,
        *,
        scope: InstallScope = "project",
        trust: InstallTrust = "untrusted",
        confirmed: bool = False,
        overwrite: bool = False,
    ) -> Skill:
        preview = self.preview_install(source, scope=scope, trust=trust)
        if not confirmed:
            raise SkillConfirmationRequired(preview)
        base = self._base(scope)
        target = Path(preview.target)
        if target.exists() and not overwrite:
            raise SkillManagerError(f"skill already exists: {preview.name}")
        base.mkdir(parents=True, exist_ok=True)
        staging = base / f".{preview.name}.install-{uuid.uuid4().hex}"
        backup = base / f".{preview.name}.backup-{uuid.uuid4().hex}"
        _validate_target(base, staging)
        _validate_target(base, backup)
        source_entry = self._source_entry(source)
        source_root = source_entry.parent if source_entry.name == "SKILL.md" else source_entry
        try:
            if source_root.is_dir():
                shutil.copytree(
                    source_root,
                    staging,
                    ignore=shutil.ignore_patterns(_METADATA_FILE),
                )
            else:
                staging.mkdir()
                shutil.copy2(source_root, staging / "SKILL.md")
            staged_entry = staging / "SKILL.md"
            staged_digest = digest_skill_path(staged_entry)
            if staged_digest != preview.digest:
                raise SkillManagerError(
                    f"skill digest changed during install: {preview.digest} -> {staged_digest}"
                )
            metadata = SkillInstallMetadata(
                source=preview.source,
                installed_at=_now(),
                trust=trust,
                digest=staged_digest,
            )
            (staging / _METADATA_FILE).write_text(
                json.dumps(metadata.model_dump(mode="json"), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            if target.exists():
                target.replace(backup)
            staging.replace(target)
            if backup.exists():
                _remove_target(base, backup)
        except Exception:
            if staging.exists():
                _remove_target(base, staging)
            if backup.exists() and not target.exists():
                backup.replace(target)
            raise
        installed = _parse_skill_file(target / "SKILL.md", scope)
        if installed.integrity != "verified":
            raise SkillManagerError(f"installed skill failed digest verification: {installed.name}")
        return installed

    # 从受管 project/user 目录删除指定 skill，兼容目录永远只读
    def remove(
        self,
        name: str,
        *,
        scope: InstallScope = "project",
        confirmed: bool = False,
    ) -> None:
        if not confirmed:
            raise SkillManagerError("skill removal requires explicit confirmation")
        base = self._base(scope)
        target = base / name
        _validate_target(base, target)
        if not target.exists():
            flat_target = base / f"{name}.md"
            _validate_target(base, flat_target)
            if not flat_target.exists():
                raise SkillManagerError(f"skill not found in {scope} scope: {name}")
            target = flat_target
        _remove_target(base, target)

    # 返回最终优先级下的完整 skill 列表
    def list_all(self) -> list[Skill]:
        from code_rook.core.skills.loader import SkillLoader

        return SkillLoader(
            self._project_root,
            user_skills_dir=self._user_dir,
        ).list_all_skills()

    # 返回单个 skill 的 manifest 和 provenance，mismatch 仍可见
    def show(self, name: str) -> Skill | None:
        return next((skill for skill in self.list_all() if skill.name == name), None)

    # 审计全部生效 skill 的 digest 与 trust 状态
    def audit(self) -> list[SkillAuditRecord]:
        from code_rook.core.skills.loader import SkillLoader

        return SkillLoader(
            self._project_root,
            user_skills_dir=self._user_dir,
        ).audit()
