from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


# 返回文本或字节内容的 SHA-256，不保留原始敏感正文
def _digest(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class PrefixFingerprintReceipt:
    digest: str
    source_hashes: dict[str, str]
    changed_sources: tuple[str, ...]


class PrefixFingerprintTracker:
    # 初始化空的前缀来源 hash 状态
    def __init__(self) -> None:
        self._previous: dict[str, str] = {}

    # 计算 system/catalog/memory 来源 hash 与总指纹，并指出具体变化来源
    def observe(
        self,
        *,
        system_prompt: str,
        tool_catalog: bytes,
        stable_memory: str,
    ) -> PrefixFingerprintReceipt:
        source_hashes = {
            "system_prompt": _digest(system_prompt),
            "tool_catalog": _digest(tool_catalog),
            "stable_memory": _digest(stable_memory),
        }
        changed = tuple(
            source
            for source in sorted(source_hashes)
            if self._previous.get(source) != source_hashes[source]
        )
        combined = json.dumps(
            source_hashes,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = PrefixFingerprintReceipt(
            digest=_digest(combined),
            source_hashes=source_hashes,
            changed_sources=changed,
        )
        self._previous = dict(source_hashes)
        return receipt
