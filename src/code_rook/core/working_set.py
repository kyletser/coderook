from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Literal

WorkingSetSource = Literal["read", "edit", "diagnostic"]


@dataclass(frozen=True)
class WorkingSetEntry:
    path: str
    sources: frozenset[WorkingSetSource]
    last_step: int
    content_hash: str = ""


class WorkingSet:
    # 初始化有界路径工作集，按最近触达顺序维护
    def __init__(self, *, max_entries: int = 32) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, WorkingSetEntry] = OrderedDict()

    # 记录路径的读取、编辑或诊断来源并更新可选内容 hash
    def touch(
        self,
        path: str,
        source: WorkingSetSource,
        *,
        step: int,
        content_hash: str = "",
    ) -> None:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.strip("/")
        if not normalized:
            return
        current = self._entries.get(normalized)
        if current is None:
            entry = WorkingSetEntry(
                path=normalized,
                sources=frozenset({source}),
                last_step=step,
                content_hash=content_hash,
            )
        else:
            entry = replace(
                current,
                sources=current.sources | {source},
                last_step=step,
                content_hash=content_hash or current.content_hash,
            )
        self._entries[normalized] = entry
        self._entries.move_to_end(normalized)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    # 返回按最近触达优先且不含文件正文的不可变快照
    def snapshot(self) -> tuple[WorkingSetEntry, ...]:
        return tuple(reversed(self._entries.values()))

    # 返回供模型使用的路径、来源和 hash 摘要
    def render_context(self) -> str:
        entries = self.snapshot()
        if not entries:
            return ""
        lines = ["## Working Set"]
        for entry in entries:
            sources = ",".join(sorted(entry.sources))
            content_hash = f" hash={entry.content_hash}" if entry.content_hash else ""
            lines.append(
                f"- {entry.path} ({sources}, step={entry.last_step}{content_hash})"
            )
        return "\n".join(lines)
