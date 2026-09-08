from __future__ import annotations

import json

from pydantic import BaseModel

from code_rook.core.events.bus import EventBus


class AttemptBus(EventBus):
    # 为单次请求保留流片段，同时继续转发实时 UI 事件
    def __init__(self, inner: EventBus) -> None:
        super().__init__()
        self._inner = inner
        self.fragments: list[dict[str, object]] = []
        self.bytes = 0
        self.truncated = False

    # 正文与推理仍实时可见，但只有完整响应才进入正式模型历史
    async def publish(self, event: BaseModel) -> None:
        if getattr(event, "type", "") in {"llm.token", "llm.reasoning"}:
            self.record_stream_fragment(event.model_dump(mode="json"))
        await self._inner.publish(event)

    # 审计缓冲覆盖 watchdog 响应上限及协议开销，超出时明确记录截断
    def record_stream_fragment(self, fragment: dict[str, object]) -> None:
        size = len(json.dumps(fragment, ensure_ascii=False).encode("utf-8"))
        if self.bytes + size > 16 * 1024 * 1024:
            self.truncated = True
            return
        self.bytes += size
        self.fragments.append(fragment)
