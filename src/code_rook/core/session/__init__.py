from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from code_rook.core.session.manager import SessionManager
    from code_rook.core.session.model import Session, SessionMode, SessionStatus
    from code_rook.core.session.store import (
        IncompleteToolCall,
        MessageContent,
        SessionStore,
        SessionTranscriptSink,
        TranscriptRecovery,
    )

__all__ = [
    "IncompleteToolCall",
    "MessageContent",
    "Session",
    "SessionManager",
    "SessionMode",
    "SessionStatus",
    "SessionStore",
    "SessionTranscriptSink",
    "TranscriptRecovery",
]

_EXPORT_MODULES = {
    "SessionManager": "code_rook.core.session.manager",
    "Session": "code_rook.core.session.model",
    "SessionMode": "code_rook.core.session.model",
    "SessionStatus": "code_rook.core.session.model",
    "IncompleteToolCall": "code_rook.core.session.store",
    "MessageContent": "code_rook.core.session.store",
    "SessionStore": "code_rook.core.session.store",
    "SessionTranscriptSink": "code_rook.core.session.store",
    "TranscriptRecovery": "code_rook.core.session.store",
}


# 按需加载会话公开类型，避免轻量 CLI 因包初始化提前导入完整 Agent Runtime
def __getattr__(name: str) -> object:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
