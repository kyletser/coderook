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
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_FRONTMATTER_KEYS = frozenset(
    {"schema_version", "name", "description", "allowed_tools"}
)
_MAX_SKILL_ENTRY_BYTES = 512 * 1_024
_MAX_SKILL_BUNDLE_BYTES = 16 * 1_024 * 1_024


class SkillError(ValueError):
    pass


class SkillIntegrityError(SkillError):
    pass


class SkillTrustError(SkillError):
    pass


@dataclass(frozen=True)
class _SkillCandidate:
    path: Path
    scope: SkillScope


# 判断 skill 来源是否可在当前 workspace trust 快照下进入模型上下文
def _is_execution_trusted(skill: Skill, *, workspace_trusted: bool) -> bool:
    if skill.scope == "builtin":
        return True
    if skill.scope in {"project", "legacy"}:
        return workspace_trusted
    return skill.trust == "trusted"


# 返回文件修改时间的 UTC ISO 文本
def _installed_time(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()


# 返回 skill 内容根，目录式 skill 包含除安装元数据外的全部文件
def _content_root(path: Path) -> Path:
    return path.parent if path.name == "SKILL.md" else path


# 以有界内存分块把单个文件内容加入 digest
def _update_digest_from_file(digest: hashlib._Hash, path: Path) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1_024):
            digest.update(chunk)


# 计算单文件或目录 skill 的确定性 SHA-256
def digest_skill_path(path: Path) -> str:
    if path.is_symlink():
        raise SkillError(f"skill entry must not be a symbolic link: {path}")
    digest = hashlib.sha256()
    root = _content_root(path)
    if root.is_file():
        if root.stat().st_size > _MAX_SKILL_BUNDLE_BYTES:
            raise SkillError(f"skill file exceeds size limit: {root}")
        digest.update(root.name.encode("utf-8"))
        digest.update(b"\0")
        _update_digest_from_file(digest, root)
        return f"sha256:{digest.hexdigest()}"
    items = list(root.rglob("*"))
    for item in items:
        if item.is_symlink():
            raise SkillError(f"skill contains a symbolic link: {item}")
    files = sorted(
        item
        for item in items
        if item.is_file() and item.name != _METADATA_FILE
    )
    total_bytes = sum(item.stat().st_size for item in files)
    if total_bytes > _MAX_SKILL_BUNDLE_BYTES:
        raise SkillError(f"skill bundle exceeds size limit: {root}")
    for item in files:
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        _update_digest_from_file(digest, item)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


# 读取受管 skill 的安装元数据；缺失表示只读兼容或手工维护 skill
def _read_metadata(path: Path) -> SkillInstallMetadata | None:
    root = _content_root(path)
    metadata_path = root / _METADATA_FILE if root.is_dir() else None
    if metadata_path is None or not metadata_path.is_file():
        return None
    if metadata_path.is_symlink():
        raise SkillError(f"skill metadata must not be a symbolic link: {metadata_path}")
    try:
        return SkillInstallMetadata.model_validate_json(
            metadata_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise SkillError(f"invalid skill metadata {metadata_path}: {exc}") from exc


# 解析受限 YAML 标量，拒绝不配对引号而不尝试执行通用 YAML 语义
def _parse_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise SkillError("skill frontmatter contains an unterminated quoted scalar")
        return value[1:-1]
    if value[-1] in {'"', "'"}:
        raise SkillError("skill frontmatter contains an unmatched quote")
    return value


# 解析严格受限的 Markdown frontmatter manifest 和正文
def _parse_manifest(path: Path) -> tuple[SkillManifest, str]:
    if path.stat().st_size > _MAX_SKILL_ENTRY_BYTES:
        raise SkillError(f"skill entry exceeds size limit: {path}")
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
        seen: set[str] = set()
        while index < len(lines):
            raw_line = lines[index]
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if raw_line.startswith((" ", "\t")) or ":" not in stripped:
                raise SkillError(
                    f"invalid skill frontmatter line {index + 1}: {stripped!r}"
                )
            key, raw_value = stripped.split(":", 1)
            if key not in _FRONTMATTER_KEYS:
                raise SkillError(f"unknown skill manifest field: {key}")
            if key in seen:
                raise SkillError(f"duplicate skill manifest field: {key}")
            seen.add(key)
            value = raw_value.strip()
            if key == "name":
                name = _parse_scalar(value)
            elif key == "schema_version":
                if value != "2":
                    raise SkillError("skill schema_version must be the integer 2")
            elif key == "description":
                if value in (">", "|"):
                    fold = value == ">"
                    parts: list[str] = []
                    index += 1
                    while index < len(lines) and lines[index].startswith((" ", "\t")):
                        parts.append(lines[index].strip())
                        index += 1
                    description = (" ".join(parts) if fold else "\n".join(parts)).strip()
                    continue
                description = _parse_scalar(value)
            elif key == "allowed_tools":
                if value:
                    raise SkillError("skill allowed_tools must be an indented list")
                index += 1
                while index < len(lines) and lines[index].startswith((" ", "\t")):
                    item = lines[index].strip()
                    if not item.startswith("- ") or not item[2:].strip():
                        raise SkillError("skill allowed_tools contains an invalid list item")
                    allowed_tools.append(_parse_scalar(item[2:]))
                    index += 1
                continue
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
    if path.is_symlink():
        raise SkillError(f"skill entry must not be a symbolic link: {path}")
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


# 验证候选来源仍位于声明目录内且整个受管相对路径不经过符号链接
def _validate_candidate_source(
    path: Path,
    directory: Path,
    *,
    containment_root: Path | None = None,
) -> None:
    if directory.is_symlink() or path.is_symlink():
        raise SkillError(f"skill source must not be a symbolic link: {path}")
    if containment_root is not None:
        try:
            directory_relative = directory.relative_to(containment_root)
        except ValueError as exc:
            raise SkillError(f"skill directory escapes declared root: {directory}") from exc
        cursor = containment_root
        for part in directory_relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise SkillError(f"skill directory traverses a symbolic link: {cursor}")
        if not directory.resolve().is_relative_to(containment_root.resolve()):
            raise SkillError(f"skill directory escapes declared root: {directory}")
    try:
        relative = path.relative_to(directory)
    except ValueError as exc:
        raise SkillError(f"skill source escapes declared directory: {path}") from exc
    cursor = directory
    for part in relative.parts[:-1]:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SkillError(f"skill source traverses a symbolic link: {cursor}")
    resolved_directory = directory.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_directory):
        raise SkillError(f"skill source escapes declared directory: {path}")


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

    # 按优先级解析指定 skill，并可要求只有可信来源的正文进入模型
    def resolve(
        self,
        name: str,
        *,
        require_trusted: bool = False,
        workspace_trusted: bool = False,
        expected_digest: str | None = None,
    ) -> Skill | None:
        if _SKILL_NAME_RE.fullmatch(name) is None:
            raise SkillError(f"invalid skill name: {name!r}")
        trust_error: SkillTrustError | None = None
        for directory, scope in self._candidate_groups():
            paths = [directory / f"{name}.md", directory / name / "SKILL.md"]
            for path in paths:
                if not path.is_file():
                    continue
                if (
                    require_trusted
                    and scope in {"project", "legacy"}
                    and not workspace_trusted
                ):
                    trust_error = SkillTrustError(
                        f"skill is not trusted for execution: {name} scope={scope}"
                    )
                    continue
                _validate_candidate_source(
                    path,
                    directory,
                    containment_root=(
                        self._project_root
                        if scope in {"project", "legacy"}
                        else None
                    ),
                )
                skill = _parse_skill_file(path, scope)
                if skill.integrity == "mismatch":
                    raise SkillIntegrityError(
                        f"skill digest mismatch: {skill.name} "
                        f"expected={skill.expected_digest} actual={skill.digest}"
                    )
                if skill.name != name:
                    raise SkillError(
                        f"skill manifest name mismatch: requested={name} actual={skill.name}"
                    )
                if require_trusted and not _is_execution_trusted(
                    skill,
                    workspace_trusted=workspace_trusted,
                ):
                    trust_error = SkillTrustError(
                        f"skill is not trusted for execution: {skill.name} "
                        f"scope={skill.scope} integrity={skill.integrity}"
                    )
                    continue
                if expected_digest is not None and skill.digest != expected_digest:
                    raise SkillIntegrityError(
                        f"skill digest changed after discovery: {skill.name} "
                        f"expected={expected_digest} actual={skill.digest}"
                    )
                return skill
        if require_trusted and trust_error is not None:
            raise trust_error
        return None

    # 列出按最终覆盖关系生效的全部 skill 名称
    def list_all(self) -> list[str]:
        return [skill.name for skill in self.list_all_skills()]

    # 列出可进入当前 Turn 模型上下文的 skill，并冻结每项当前 digest
    def list_for_execution(self, *, workspace_trusted: bool) -> list[Skill]:
        names: set[str] = set()
        for directory, scope in self._candidate_groups():
            if scope in {"project", "legacy"} and not workspace_trusted:
                continue
            for candidate in self._discover(directory, scope):
                name = (
                    candidate.path.parent.name
                    if candidate.path.name == "SKILL.md"
                    else candidate.path.stem
                )
                if _SKILL_NAME_RE.fullmatch(name) is not None:
                    names.add(name)
        ready: list[Skill] = []
        for name in sorted(names):
            try:
                skill = self.resolve(
                    name,
                    require_trusted=True,
                    workspace_trusted=workspace_trusted,
                )
            except (OSError, SkillError, ValidationError, ValueError, json.JSONDecodeError):
                continue
            if skill is not None:
                ready.append(skill)
        return ready

    # 列出含 provenance 的所有最终生效 skill，保留 mismatch 供 audit/show
    def list_all_skills(self) -> list[Skill]:
        seen: dict[str, Skill] = {}
        for directory, scope in reversed(self._candidate_groups()):
            for candidate in self._discover(directory, scope):
                try:
                    _validate_candidate_source(
                        candidate.path,
                        directory,
                        containment_root=(
                            self._project_root
                            if scope in {"project", "legacy"}
                            else None
                        ),
                    )
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

    # 将参数替换到通过完整性校验的正文，并可对直接调用再次执行信任检查
    def render_prompt(
        self,
        skill: Skill,
        arguments: str,
        *,
        require_trusted: bool = False,
        workspace_trusted: bool = False,
        expected_digest: str | None = None,
    ) -> str:
        if skill.integrity == "mismatch":
            raise SkillIntegrityError(f"skill digest mismatch: {skill.name}")
        if expected_digest is not None and skill.digest != expected_digest:
            raise SkillIntegrityError(
                f"skill digest changed after discovery: {skill.name}"
            )
        if require_trusted and not _is_execution_trusted(
            skill,
            workspace_trusted=workspace_trusted,
        ):
            raise SkillTrustError(f"skill is not trusted for execution: {skill.name}")
        return skill.system_prompt_template.replace("$ARGUMENTS", arguments)
