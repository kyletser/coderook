from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from code_rook.core.trace.redaction import redact_trace_data

MemoryType = Literal["user", "feedback", "project", "reference"]
AutoMemoryMode = Literal["prompt", "off"]
_WORD_RE = re.compile(r"[a-zA-Z0-9_./-]{2,}|[\u4e00-\u9fff]")
_EXPLICIT_MEMORY_RE = re.compile(
    r"(?i)(记住|以后|始终|总是|不要再|偏好|remember|from now on|always|never|prefer)"
)


# 返回当前 UTC ISO 时间字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 将文本拆成适合中英文项目记忆召回的规范化词元
def _tokens(text: str) -> set[str]:
    raw = [item.lower() for item in _WORD_RE.findall(text)]
    chinese = "".join(item for item in raw if len(item) == 1 and "\u4e00" <= item <= "\u9fff")
    bigrams = {chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))}
    return set(raw) | bigrams


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    name: str
    description: str
    type: MemoryType
    body: str
    source_session_id: str
    source_run_id: str
    created_at: str
    updated_at: str
    pinned: bool = False
    expires_at: str | None = None

    # 从持久化字典恢复并校验一条记忆记录
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MemoryRecord:
        mem_type = str(data.get("type", "project"))
        if mem_type not in {"user", "feedback", "project", "reference"}:
            mem_type = "project"
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            description=str(data.get("description", "")),
            type=mem_type,  # type: ignore[arg-type]
            body=str(data["body"]),
            source_session_id=str(data.get("source_session_id", "")),
            source_run_id=str(data.get("source_run_id", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            pinned=bool(data.get("pinned", False)),
            expires_at=(
                str(data["expires_at"])
                if data.get("expires_at") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class MemorySettings:
    auto_save: AutoMemoryMode = "prompt"

    # 从持久化字典恢复设置，非法值按关闭处理以避免静默写入
    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MemorySettings:
        value = data.get("auto_save", "prompt")
        if value == "prompt":
            return cls(auto_save="prompt")
        return cls(auto_save="off")


class MemoryStore:
    # 初始化项目记忆路径并延迟到首次写入时创建目录
    def __init__(self, root: Path) -> None:
        self.root = root
        self.records_dir = root / "records"
        self.settings_path = root / "settings.json"

    # 读取 Agent 自动保存策略，缺省为每次提示且损坏配置按关闭处理
    def load_settings(self) -> MemorySettings:
        if not self.settings_path.is_file():
            return MemorySettings()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return MemorySettings(auto_save="off")
        if not isinstance(raw, dict):
            return MemorySettings(auto_save="off")
        return MemorySettings.from_dict(raw)

    # 更新 Agent 自动保存策略并原子持久化
    def set_auto_save(self, mode: AutoMemoryMode) -> MemorySettings:
        if mode not in {"prompt", "off"}:
            raise ValueError("auto_save must be 'prompt' or 'off'")
        settings = MemorySettings(auto_save=mode)
        self._atomic_write(
            self.settings_path,
            (json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n").encode(),
        )
        return settings

    # 创建或按名称覆盖记忆，并原子更新可读索引
    def save(
        self,
        *,
        name: str,
        description: str,
        mem_type: MemoryType,
        body: str,
        source_session_id: str = "",
        source_run_id: str = "",
        pinned: bool = False,
        expires_at: str | None = None,
    ) -> MemoryRecord:
        clean_name = name.strip()
        clean_body = body.strip()
        if not clean_name or not clean_body:
            raise ValueError("memory name and body must not be empty")
        clean_expiry = self._normalize_expiry(expires_at)
        existing = next(
            (
                item
                for item in self.list_all(include_expired=True)
                if item.name == clean_name
            ),
            None,
        )
        now = _now()
        record = MemoryRecord(
            id=existing.id if existing else f"mem-{uuid.uuid4().hex[:12]}",
            name=clean_name,
            description=description.strip(),
            type=mem_type,
            body=clean_body,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            pinned=pinned,
            expires_at=clean_expiry,
        )
        self._atomic_write(
            self.records_dir / f"{record.id}.json",
            (json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n").encode(),
        )
        self._rebuild_index()
        return record

    # 从用户明确的长期规则中自动提取一条脱敏记忆，普通请求返回 None
    def remember_explicit_prompt(
        self,
        prompt: str,
        *,
        source_session_id: str = "",
        source_run_id: str = "",
        confirmed: bool = False,
    ) -> MemoryRecord | None:
        clean = prompt.strip()
        if (
            not confirmed
            or self.load_settings().auto_save == "off"
            or not clean
            or _EXPLICIT_MEMORY_RE.search(clean) is None
        ):
            return None
        redacted = str(redact_trace_data(clean))
        digest = hashlib.sha256(redacted.encode()).hexdigest()[:12]
        is_feedback = re.search(r"(?i)(不要再|never|prefer|偏好)", redacted) is not None
        return self.save(
            name=f"explicit-rule-{digest}",
            description=redacted.replace("\n", " ")[:120],
            mem_type="feedback" if is_feedback else "user",
            body=redacted,
            source_session_id=source_session_id,
            source_run_id=source_run_id,
        )

    # 返回全部有效记忆，默认排除过期项并把 pinned 项稳定置顶
    def list_all(self, *, include_expired: bool = False) -> list[MemoryRecord]:
        records: list[MemoryRecord] = []
        for path in self.records_dir.glob("mem-*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    record = MemoryRecord.from_dict(raw)
                    if include_expired or not self.is_expired(record):
                        records.append(record)
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return sorted(
            records,
            key=lambda item: (item.pinned, item.updated_at),
            reverse=True,
        )

    # 按 ID 返回包含过期项的单条记忆，不存在时返回 None
    def get(self, memory_id: str) -> MemoryRecord | None:
        return next(
            (
                item
                for item in self.list_all(include_expired=True)
                if item.id == memory_id
            ),
            None,
        )

    # 修改记忆正文和元数据，未提供的字段保持原值
    def edit(
        self,
        memory_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        mem_type: MemoryType | None = None,
        body: str | None = None,
    ) -> MemoryRecord:
        current = self.get(memory_id)
        if current is None:
            raise ValueError(f"memory not found: {memory_id}")
        clean_name = current.name if name is None else name.strip()
        clean_body = current.body if body is None else body.strip()
        if not clean_name or not clean_body:
            raise ValueError("memory name and body must not be empty")
        updated = replace(
            current,
            name=clean_name,
            description=(
                current.description if description is None else description.strip()
            ),
            type=current.type if mem_type is None else mem_type,
            body=clean_body,
            updated_at=_now(),
        )
        self._write_record(updated)
        return updated

    # 固定或取消固定一条记忆并刷新索引排序
    def pin(self, memory_id: str, *, pinned: bool = True) -> MemoryRecord:
        current = self.get(memory_id)
        if current is None:
            raise ValueError(f"memory not found: {memory_id}")
        updated = replace(current, pinned=pinned, updated_at=_now())
        self._write_record(updated)
        return updated

    # 设置或清除记忆过期时间，过期项保留在磁盘供审计和恢复
    def expire(self, memory_id: str, expires_at: str | None) -> MemoryRecord:
        current = self.get(memory_id)
        if current is None:
            raise ValueError(f"memory not found: {memory_id}")
        updated = replace(
            current,
            expires_at=self._normalize_expiry(expires_at),
            updated_at=_now(),
        )
        self._write_record(updated)
        return updated

    # 使用确定性词法评分检索相关记忆，避免引入陈旧向量索引
    def search(self, query: str, limit: int = 5) -> list[MemoryRecord]:
        query_tokens = _tokens(query)
        query_lower = query.lower().strip()
        scored: list[tuple[int, MemoryRecord]] = []
        for record in self.list_all():
            title = f"{record.name} {record.description}".lower()
            body = record.body.lower()
            score = 4 * len(query_tokens & _tokens(title))
            score += len(query_tokens & _tokens(body))
            if query_lower and query_lower in f"{title} {body}":
                score += 8
            if score > 0:
                scored.append((score, record))
        scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
        return [record for _, record in scored[: max(0, limit)]]

    # 删除指定记忆并重建索引；不存在时返回 False
    def forget(self, memory_id: str) -> bool:
        path = self.records_dir / f"{memory_id}.json"
        if not path.exists() or path.parent != self.records_dir:
            return False
        path.unlink()
        self._rebuild_index()
        return True

    # 校验可选 ISO 8601 过期时间并规范为 UTC 文本
    def _normalize_expiry(self, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()

    # 判断记忆是否已达到过期时间，非法旧记录按过期处理以避免污染上下文
    def is_expired(self, record: MemoryRecord) -> bool:
        if record.expires_at is None:
            return False
        try:
            expires = datetime.fromisoformat(record.expires_at.replace("Z", "+00:00"))
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires.astimezone(UTC) <= datetime.now(UTC)

    # 原子写入单条记录并重建人类可读索引
    def _write_record(self, record: MemoryRecord) -> None:
        self._atomic_write(
            self.records_dir / f"{record.id}.json",
            (json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n").encode(),
        )
        self._rebuild_index()

    # 将召回记录格式化为带来源的上下文片段
    def format_context(self, records: list[MemoryRecord]) -> str:
        parts: list[str] = []
        for record in records:
            source = record.source_run_id or record.source_session_id or "manual"
            parts.append(
                f"### {record.name} [{record.type}]\n"
                f"{record.body}\n"
                f"Source: {source}; memory_id={record.id}"
            )
        return "\n\n".join(parts)

    # 根据全部记录重建人类可读的 MEMORY.md 索引
    def _rebuild_index(self) -> None:
        lines = ["# CodeRook Project Memory", ""]
        for record in self.list_all():
            lines.append(
                f"- `{record.id}` [{record.type}] **{record.name}**: {record.description}"
            )
        self._atomic_write(self.root / "MEMORY.md", ("\n".join(lines) + "\n").encode())

    # 在同目录写临时文件后原子替换目标文件
    def _atomic_write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temp_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)
