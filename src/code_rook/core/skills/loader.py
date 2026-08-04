from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from code_rook.core.skills.models import (
    Skill,
    SkillAuditRecord,
    SkillInstallMetadata,
    SkillIntegrity,
    SkillManifest,
    SkillScope,
)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_METADATA_FILE = ".coderook-skill.json"


class SkillError(ValueError):
    pass


class SkillIntegrityError(SkillError):
    pass


@dataclass(frozen=True)
class _SkillCandidate:
    path: Path
    scope: SkillScope


# 返回文件修改时间的 UTC ISO 文本
def _installed_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


# 返回 skill 内容根，目录式 skill 包含除安装元数据外的全部文件
def _content_root(path: Path) -> Path:
    return path.parent if path.name == "SKILL.md" else path


# 计算单文件或目录 skill 的确定性 SHA-256
def digest_skill_path(path: Path) -> str:
    digest = hashlib.sha256()
    root = _content_root(path)
    if root.is_file():
        digest.update(root.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(root.read_bytes())
        return f"sha256:{digest.hexdigest()}"
    files = sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and item.name != _METADATA_FILE
    )
    for item in files:
        if item.is_symlink():
            raise SkillError(f"skill contains a symbolic link: {item}")
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


# 读取受管 skill 的安装元数据；缺失表示只读兼容或手工维护 skill
def _read_metadata(path: Path) -> SkillInstallMetadata | None:
    root = _content_root(path)
    metadata_path = root / _METADATA_FILE if root.is_dir() else None
    if metadata_path is None or not metadata_path.is_file():
        return None
    try:
        return SkillInstallMetadata.model_validate_json(
            metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise SkillError(f"invalid skill metadata {metadata_path}: {exc}") from exc


# 解析 Markdown frontmatter 中的 manifest 字段和正文
def _parse_manifest(path: Path) -> tuple[SkillManifest, str]:
    text = path.read_text(encoding="utf-8")
    name = path.parent.name if path.name == "SKILL.md" else path.stem
    description = ""
    allowed_tools: list[str] = []
    body = text
    match = _FRONTMATTER_RE.match(text)
    if match:
        front = match.group(1)
        body = text[match.end():]
        lines = front.splitlines()
        index = 0
        in_allowed_tools = False
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:"):].strip().strip('"').strip("'")
                in_allowed_tools = False
            elif stripped.startswith("description:"):
                value = stripped[len("description:"):].strip().strip('"').strip("'")
                in_allowed_tools = False
                if value in (">", "|"):
                    fold = value == ">"
                    parts: list[str] = []
                    index += 1
                    while index < len(lines) and lines[index].startswith((" ", "\t")):
                        parts.append(lines[index].strip())
                        index += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                description = value
            elif stripped.startswith("allowed_tools:"):
                in_allowed_tools = True
            elif in_allowed_tools and stripped.startswith("- "):
                allowed_tools.append(stripped[2:].strip())
            else:
                in_allowed_tools = False
            index += 1
    return (
        SkillManifest(
            name=name,
            description=description,
            allowed_tools=tuple(allowed_tools),
        ),
        body.strip(),
    )


# 解析 skill 并附加 manifest、digest、source、installed_at 和 trust provenance
def _parse_skill_file(path: Path, scope: SkillScope = "project") -> Skill:
    manifest, body = _parse_manifest(path)
    digest = digest_skill_path(path)
    metadata = _read_metadata(path)
    expected_digest = metadata.digest if metadata is not None else ""
    integrity: SkillIntegrity = (
        "verified"
        if scope == "builtin" or (metadata is not None and metadata.digest == digest)
        else "mismatch"
        if metadata is not None
        else "unmanaged"
    )
    trust = (
        "builtin"
        if scope == "builtin"
        else metadata.trust
        if metadata is not None
        else "untrusted"
    )
    source = metadata.source if metadata is not None else f"{scope}:{path}"
    installed_at = metadata.installed_at if metadata is not None else _installed_time(path)
    return Skill(
        manifest=manifest,
        system_prompt_template=body,
        digest=digest,
        expected_digest=expected_digest,
        source=source,
        installed_at=installed_at,
        trust=trust,
        scope=scope,
        path=str(path.resolve()),
        integrity=integrity,
    )


class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    # 绑定项目根目录和可注入用户目录，确保发现优先级不依赖进程 cwd
    def __init__(
        self,
        project_root: Path | None = None,
        *,
        user_skills_dir: Path | None = None,
    ) -> None:
        self._project_root = (project_root or Path.cwd()).resolve()
        self._user_dir = user_skills_dir or Path("~/.coderook/skills").expanduser()

    # 返回解析优先级顺序：project > user > builtin > legacy read-only
    def _candidate_groups(self) -> list[tuple[Path, SkillScope]]:
        return [
            (self._project_root / ".coderook" / "skills", "project"),
            (self._user_dir, "user"),
            (self._BUILTIN_DIR, "builtin"),
            (self._project_root / ".claude" / "skills", "legacy"),
            (self._project_root / ".codex" / "skills", "legacy"),
            (self._project_root / ".agents" / "skills", "legacy"),
        ]

    # 返回目录中扁平和目录式 skill 候选的稳定序列
    def _discover(self, directory: Path, scope: SkillScope) -> list[_SkillCandidate]:
        if not directory.is_dir():
            return []
        candidates = [
            *(_SkillCandidate(path, scope) for path in sorted(directory.glob("*.md"))),
            *(
                _SkillCandidate(path, scope)
                for path in sorted(directory.glob("*/SKILL.md"))
            ),
        ]
        return candidates

    # 按优先级解析指定 skill，digest mismatch 在正文进入模型前明确失败
    def resolve(self, name: str) -> Skill | None:
        for directory, scope in self._candidate_groups():
            paths = [directory / f"{name}.md", directory / name / "SKILL.md"]
            for path in paths:
                if not path.is_file():
                    continue
                skill = _parse_skill_file(path, scope)
                if skill.integrity == "mismatch":
                    raise SkillIntegrityError(
                        f"skill digest mismatch: {skill.name} "
                        f"expected={skill.expected_digest} actual={skill.digest}"
                    )
                return skill
        return None

    # 列出按最终覆盖关系生效的全部 skill 名称
    def list_all(self) -> list[str]:
        return [skill.name for skill in self.list_all_skills()]

    # 列出含 provenance 的所有最终生效 skill，保留 mismatch 供 audit/show
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for directory, scope in reversed(self._candidate_groups()):
            for candidate in self._discover(directory, scope):
                try:
                    skill = _parse_skill_file(candidate.path, candidate.scope)
                except (OSError, SkillError, ValidationError, ValueError, json.JSONDecodeError):
                    continue
                seen[skill.name] = skill
        return [seen[name] for name in sorted(seen)]

    # 返回指定 skill 的 provenance，即使 digest mismatch 也不加载其正文执行
    def show(self, name: str) -> Skill | None:
        return next((skill for skill in self.list_all_skills() if skill.name == name), None)

    # 返回全部 skill 的完整性和信任审计记录
    def audit(self) -> list[SkillAuditRecord]:
        return [
            SkillAuditRecord(
                name=skill.name,
                scope=skill.scope,
                trust=skill.trust,
                source=skill.source,
                path=skill.path,
                digest=skill.digest,
                expected_digest=skill.expected_digest,
                integrity=skill.integrity,
            )
            for skill in self.list_all_skills()
        ]

    # 将参数替换到已经通过 digest 校验的 skill 正文
    def render_prompt(self, skill: Skill, arguments: str) -> str:
        if skill.integrity == "mismatch":
            raise SkillIntegrityError(f"skill digest mismatch: {skill.name}")
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
