from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class AuditIncident(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str
    diagnostic_id: str
    error_type: str
    ts: str


type AuditIncidentSink = Callable[[AuditIncident], Awaitable[None]]


class AuditHealth:
    # 初始化进程级审计健康状态和可选的可见事件出口
    def __init__(self, sink: AuditIncidentSink | None = None) -> None:
        self._sink = sink
        self._incident: AuditIncident | None = None
        self._lock = asyncio.Lock()

    # 返回审计持久化是否已进入失败关闭状态
    @property
    def degraded(self) -> bool:
        return self._incident is not None

    # 返回首次触发失败关闭的脱敏诊断记录
    @property
    def incident(self) -> AuditIncident | None:
        return self._incident

    # 更新可见事件出口，供 daemon 完成总线初始化后接线
    def set_sink(self, sink: AuditIncidentSink | None) -> None:
        self._sink = sink

    # 首次写入失败时原子切换为降级状态并广播脱敏诊断
    async def degrade(self, source: str, error: BaseException) -> AuditIncident:
        should_emit = False
        async with self._lock:
            if self._incident is None:
                ts = datetime.now(UTC).isoformat()
                error_type = type(error).__name__
                fingerprint = hashlib.sha256(
                    f"{source}:{error_type}:{ts}".encode()
                ).hexdigest()[:12]
                self._incident = AuditIncident(
                    source=source,
                    diagnostic_id=f"AUD-{fingerprint}",
                    error_type=error_type,
                    ts=ts,
                )
                should_emit = True
            incident = self._incident
        assert incident is not None
        if should_emit and self._sink is not None:
            await self._sink(incident)
        return incident

    # 仅由显式修复流程恢复写工具，禁止普通运行静默清除降级状态
    async def mark_repaired(self) -> None:
        async with self._lock:
            self._incident = None
