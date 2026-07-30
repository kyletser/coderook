from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_EVENT_METADATA_KEYS = {
    "type",
    "run_id",
    "session_id",
    "last_run_id",
    "tool_use_id",
    "tool_name",
    "skill_name",
    "status",
    "reason",
    "step",
    "steps",
    "elapsed_ms",
    "attempt",
    "error_class",
    "decision",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "context_pct",
    "original_tokens",
    "summary_tokens",
}
_RESPONSE_METADATA_KEYS = {
    "run_id",
    "session_id",
    "subscription_id",
    "status",
    "ok",
    "replayed_count",
}

_SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "password",
    "passwd",
    "privatekey",
    "secret",
    "setcookie",
    "cookie",
    "token",
}
_SENSITIVE_KEY_SUFFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "authtoken",
    "clientsecret",
    "password",
    "privatekey",
)
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{12,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
]
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|"
    r"client[_-]?secret|secret)\b\s*[=:]\s*[\"']?)([^\"'\s,;&]+)"
)


def redact_trace_data(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_trace_data(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_trace_data(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def minimize_trace_data(layer: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if layer == "llm":
        return data
    if layer == "event":
        return {key: value for key, value in data.items() if key in _EVENT_METADATA_KEYS}
    if layer == "ipc" and kind == "command":
        params = data.get("params")
        return {
            "method": data.get("method"),
            "id": data.get("id"),
            "param_keys": sorted(str(key) for key in params) if isinstance(params, dict) else [],
        }
    if layer == "ipc" and kind == "response":
        result = data.get("result")
        minimized: dict[str, Any] = {"id": data.get("id")}
        if isinstance(result, dict):
            minimized["result"] = {
                key: value
                for key, value in result.items()
                if key in _RESPONSE_METADATA_KEYS
            }
            minimized["result_keys"] = sorted(str(key) for key in result)
        return minimized
    if layer == "ipc" and kind == "error":
        error = data.get("error")
        return {
            "id": data.get("id"),
            "error": {"code": error.get("code")} if isinstance(error, dict) else {},
        }
    return {"keys": sorted(str(key) for key in data)}


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_string(value: str) -> str:
    if "PRIVATE KEY-----" in value:
        return REDACTED
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: match.group(1) + REDACTED, value)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted
