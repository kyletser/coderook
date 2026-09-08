# Python session path projection adapted from Pi session-manager.ts (MIT).
# Upstream b2602be77cb7b0de45dd616407fd210daa48aa75; vendor/pi/LICENSE.
from __future__ import annotations

from typing import Any

type LedgerRow = tuple[int, dict[str, Any]]


# 从顺序日志及分支选择事件建立父指针，旧日志自然形成单链。
def entry_parents(rows: list[LedgerRow]) -> dict[int, int | None]:
    parents: dict[int, int | None] = {}
    previous: int | None = None
    for line, row in rows:
        sequence = int(row.get("ledger_seq", line))
        parent = previous
        if row.get("type") == "session.branch_selected":
            target = row.get("payload", {}).get("leaf_seq")
            if type(target) is not int or (target != 0 and target not in parents):
                raise ValueError("session branch references an unknown historical entry")
            parent = target or None
        parents[sequence] = parent
        previous = sequence
    return parents


# 沿指定叶节点回溯根节点，只让所选分支参与压缩和模型上下文投影。
def build_session_path(rows: list[LedgerRow], leaf_seq: int | None = None) -> list[LedgerRow]:
    if leaf_seq == 0:
        return []
    if not rows:
        if leaf_seq is not None:
            raise ValueError("session entry does not exist")
        return []
    parents = entry_parents(rows)
    leaf = next(reversed(parents)) if leaf_seq is None else leaf_seq
    if leaf not in parents:
        raise ValueError("session entry does not exist")
    selected: set[int] = set()
    current: int | None = leaf
    while current is not None:
        selected.add(current)
        current = parents[current]
    return [(line, row) for line, row in rows if int(row.get("ledger_seq", line)) in selected]
