from __future__ import annotations

import ipaddress
import secrets


# 判断监听主机是否明确限制在本机回环地址
def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


# 验证非回环监听必须配置 bearer token
def validate_api_binding(host: str, token: str) -> None:
    if not is_loopback_host(host) and not token:
        raise ValueError("non-loopback API binding requires CODEROOK_API_TOKEN")


# 使用常量时间比较验证 Authorization bearer header
def bearer_authorized(header: str | None, token: str) -> bool:
    if not token:
        return True
    prefix = "Bearer "
    if header is None or not header.startswith(prefix):
        return False
    return secrets.compare_digest(header[len(prefix) :], token)
