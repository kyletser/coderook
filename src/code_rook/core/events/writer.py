from __future__ import annotations

import logging
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from code_rook.core.audit import AuditHealth
from code_rook.core.events.bus import EventBus

logger = logging.getLogger(__name__)


class EventWriter:
    # 初始化运行事件账本并接入可选的进程级审计健康状态
    def __init__(self, path: Path, *, audit_health: AuditHealth | None = None) -> None:
        self._path = path
        self._file: IO[str] | None = None
        self._bus: EventBus | None = None
        self._audit_health = audit_health

    # 打开事件文件（追加模式），供 async with 使用
    async def __aenter__(self) -> EventWriter:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, "a", encoding="utf-8")
        except (OSError, ValueError) as exc:
            if self._audit_health is not None:
                await self._audit_health.degrade("event_ledger", exc)
            raise
        return self

    # 注销总线订阅并关闭事件文件
    async def __aexit__(self, *args: object) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self.handle)
            self._bus = None
        if self._file is not None:
            self._file.close()
            self._file = None

    # 将事件序列化为 JSON 行并写入文件，写入失败时降级审计并阻止权威事件继续传播
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        try:
            self._file.write(event.model_dump_json() + "\n")
            self._file.flush()
        except (OSError, ValueError) as e:
            logger.error("EventWriter: failed to write event: %s", e)
            if self._audit_health is not None:
                await self._audit_health.degrade("event_ledger", e)
            raise

    # 将写入器置于总线首位，保证事件持久化先于对外广播
    def subscribe(self, bus: EventBus) -> None:
        if self._bus is not None:
            self._bus.unsubscribe(self.handle)
        self._bus = bus
        bus.subscribe(self.handle, first=True, critical=True)
