from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    TurnItemKind,
    TurnItemRecord,
)


# 功能：验证工具调用和工具结果必须携带稳定的 tool_call_id
# 设计：直接构造缺失标识的最小模型，确保不合法记录在进入 store 前被拒绝
def test_tool_items_require_tool_call_id() -> None:
    with pytest.raises(ValidationError, match="requires tool_call_id"):
        TurnItemRecord(
            id="item-1",
            turn_id="turn-1",
            kind=TurnItemKind.TOOL_CALL,
            payload={},
            created_at=datetime.now(UTC),
        )


# 功能：验证非工具 item 不接受无意义的 tool_call_id
# 设计：使用 message item 覆盖反向约束，避免普通记录被错误关联到工具调用
def test_non_tool_items_reject_tool_call_id() -> None:
    with pytest.raises(ValidationError, match="does not accept tool_call_id"):
        TurnItemRecord(
            id="item-1",
            turn_id="turn-1",
            kind=TurnItemKind.MESSAGE,
            payload={},
            tool_call_id="call-1",
            created_at=datetime.now(UTC),
        )


# 功能：验证 runtime event 的 seq 必须从一开始
# 设计：在模型边界传入零值，确保非法游标不能进入持久层
def test_runtime_event_rejects_non_positive_seq() -> None:
    with pytest.raises(ValidationError):
        RuntimeEventRecord(
            thread_id="thread-1",
            seq=0,
            type="turn.started",
            ts=datetime.now(UTC),
        )
