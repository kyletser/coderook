from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

from code_rook.core.hooks.models import HookConfig, HookEvent, HookPayload
from code_rook.core.trace.redaction import redact_trace_data

_MAX_PAYLOAD_BYTES = 32 * 1024
_MAX_STRING_CHARS = 4096
_MAX_COLLECTION_ITEMS = 128
_MAX_DEPTH = 8


# 返回当前 UTC 时间的稳定 ISO 文本
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 递归限制 hook payload 的深度、集合数量和单个字符串长度
def _bound_value(value: Any, *, depth: int = 0) -> JsonValue:
    if depth >= _MAX_DEPTH:
        return "[depth truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_STRING_CHARS:
            return value
        return value[:_MAX_STRING_CHARS] + "\n[truncated]"
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {
            str(key): _bound_value(item, depth=depth + 1)
            for key, item in items[:_MAX_COLLECTION_ITEMS]
        }
        if len(items) > _MAX_COLLECTION_ITEMS:
            bounded["_truncated_items"] = len(items) - _MAX_COLLECTION_ITEMS
        return bounded
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        bounded_list = [
            _bound_value(item, depth=depth + 1)
            for item in items[:_MAX_COLLECTION_ITEMS]
        ]
        if len(items) > _MAX_COLLECTION_ITEMS:
            bounded_list.append(f"[{len(items) - _MAX_COLLECTION_ITEMS} items truncated]")
        return bounded_list
    return str(value)[:_MAX_STRING_CHARS]


# 构造版本化、脱敏且总大小有界的 hook stdin payload
def build_hook_payload(
    config: HookConfig,
    event: HookEvent,
    context: dict[str, Any],
) -> HookPayload:
    redacted = redact_trace_data(context)
    raw_encoded = json.dumps(
        redacted,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    bounded = _bound_value(redacted)
    if not isinstance(bounded, dict):
        bounded = {"value": bounded}
    original_bytes = len(raw_encoded)
    truncated = original_bytes > _MAX_PAYLOAD_BYTES or bounded != redacted
    if original_bytes > _MAX_PAYLOAD_BYTES:
        bounded = {
            "truncated": True,
            "original_bytes": original_bytes,
            "sha256": hashlib.sha256(raw_encoded).hexdigest(),
            "keys": [str(key) for key in list(bounded)[:_MAX_COLLECTION_ITEMS]],
        }
    return HookPayload(
        event=event,
        hook_id=config.id,
        emitted_at=_now(),
        context=bounded,
        truncated=truncated,
        original_bytes=original_bytes,
    )
