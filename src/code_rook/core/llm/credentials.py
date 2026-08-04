from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

import keyring
from pydantic import BaseModel, ConfigDict

from code_rook.core.config import LlmConfig
from code_rook.core.llm.routes import CredentialSource

_DEFAULT_CREDENTIALS_PATH = "~/.coderook/credentials.json"
_KEYRING_SERVICE = "coderook"


class CredentialBackend(Protocol):
    # 从 OS 密钥环读取指定账户的密钥
    def get_password(self, service: str, account: str) -> str | None: ...

    # 将指定账户的密钥写入 OS 密钥环
    def set_password(self, service: str, account: str, password: str) -> None: ...

    # 从 OS 密钥环删除指定账户的密钥
    def delete_password(self, service: str, account: str) -> None: ...


class _SystemCredentialBackend:
    # 从当前操作系统 keyring 读取密钥
    def get_password(self, service: str, account: str) -> str | None:
        return keyring.get_password(service, account)

    # 向当前操作系统 keyring 保存密钥
    def set_password(self, service: str, account: str, password: str) -> None:
        keyring.set_password(service, account, password)

    # 从当前操作系统 keyring 删除密钥
    def delete_password(self, service: str, account: str) -> None:
        keyring.delete_password(service, account)


class CredentialResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str | None = None
    source: CredentialSource


# 将 provider 别名归一化为凭据文件使用的稳定键名
def normalize_provider(provider: str) -> str:
    return provider.lower().replace("-", "_")


# 返回凭据文件路径，允许环境变量覆盖以便测试和高级部署
def credentials_path() -> Path:
    return Path(
        os.environ.get("CODEROOK_CREDENTIALS_FILE", _DEFAULT_CREDENTIALS_PATH)
    ).expanduser()


# 读取凭据文件并在不存在或格式无效时返回空结构
def _load_credentials(path: Path | None = None) -> dict[str, Any]:
    target = path or credentials_path()
    if not target.exists():
        return {"version": 2, "api_keys": {}, "route_credentials": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Credentials parse error ({target}): {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("api_keys", {}), dict)
        or not isinstance(data.get("route_credentials", {}), dict)
    ):
        raise SystemExit(f"Credentials format error ({target})")
    return data


# 使用原子替换保存权限收紧的凭据文档
def _save_credentials(data: dict[str, Any], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    os.chmod(target, 0o600)


# 按 provider 保存 API key，并使用仅当前用户可读写的文件权限
def save_api_key(provider: str, api_key: str, path: Path | None = None) -> Path:
    target = path or credentials_path()
    data = _load_credentials(target)
    keys = dict(data.get("api_keys", {}))
    keys[normalize_provider(provider)] = api_key
    payload = {
        "version": 2,
        "api_keys": keys,
        "route_credentials": dict(data.get("route_credentials", {})),
    }
    _save_credentials(payload, target)
    return target


class CredentialStore:
    # 初始化 route 凭据解析器，并允许测试注入 keyring 后端
    def __init__(
        self,
        path: Path | None = None,
        *,
        backend: CredentialBackend | None = None,
    ) -> None:
        self.path = path or credentials_path()
        self._backend = backend or _SystemCredentialBackend()

    # 解析 env/keyring/file 引用，只返回密钥来源而不在错误中包含正文
    def resolve(self, credential_ref: str) -> CredentialResolution:
        kind, separator, account = credential_ref.partition(":")
        if not separator or not account:
            return CredentialResolution(source="missing")
        if kind == "env":
            value = os.environ.get(account)
            return CredentialResolution(
                value=value or None,
                source="env" if value else "missing",
            )
        if kind == "keyring":
            try:
                value = self._backend.get_password(_KEYRING_SERVICE, account)
            except Exception:
                value = None
            return CredentialResolution(
                value=value or None,
                source="keyring" if value else "missing",
            )
        if kind == "file":
            data = _load_credentials(self.path)
            route_values = data.get("route_credentials", {})
            value = route_values.get(account) if isinstance(route_values, dict) else None
            if not isinstance(value, str) or not value:
                legacy = data.get("api_keys", {})
                value = (
                    legacy.get(normalize_provider(account))
                    if isinstance(legacy, dict)
                    else None
                )
            return CredentialResolution(
                value=value if isinstance(value, str) and value else None,
                source="file" if isinstance(value, str) and value else "missing",
            )
        return CredentialResolution(source="missing")

    # 默认优先写 OS keyring，后端不可用时回退到用户级权限收紧文件
    def save(
        self,
        route_id: str,
        api_key: str,
        *,
        prefer_keyring: bool = True,
    ) -> str:
        secret = api_key.strip()
        if not route_id.strip() or not secret:
            raise ValueError("route id and API key must not be empty")
        if prefer_keyring:
            try:
                self._backend.set_password(_KEYRING_SERVICE, route_id, secret)
                return f"keyring:{route_id}"
            except Exception:
                pass
        data = _load_credentials(self.path)
        route_values = dict(data.get("route_credentials", {}))
        route_values[route_id] = secret
        payload = {
            "version": 2,
            "api_keys": dict(data.get("api_keys", {})),
            "route_credentials": route_values,
        }
        _save_credentials(payload, self.path)
        return f"file:{route_id}"

    # 删除 route 引用的单个凭据，不影响任何其他 route
    def delete(self, credential_ref: str) -> None:
        kind, separator, account = credential_ref.partition(":")
        if not separator or not account:
            return
        if kind == "keyring":
            try:
                self._backend.delete_password(_KEYRING_SERVICE, account)
            except Exception:
                return
        elif kind == "file":
            data = _load_credentials(self.path)
            route_values = dict(data.get("route_credentials", {}))
            if account not in route_values:
                return
            route_values.pop(account)
            payload = {
                "version": 2,
                "api_keys": dict(data.get("api_keys", {})),
                "route_credentials": route_values,
            }
            _save_credentials(payload, self.path)


# 按环境变量优先、凭据文件兜底的顺序解析当前 provider 的 API key
def resolve_api_key(config: LlmConfig, path: Path | None = None) -> str | None:
    if config.api_key_env:
        env_value = os.environ.get(config.api_key_env)
        if env_value:
            return env_value
    keys = _load_credentials(path).get("api_keys", {})
    value = keys.get(normalize_provider(config.provider))
    return value if isinstance(value, str) and value else None


# 判断当前 LLM 配置是否具备启动所需的模型、端点和密钥
def llm_is_configured(config: LlmConfig, path: Path | None = None) -> bool:
    provider = normalize_provider(config.provider)
    supported = {
        "anthropic",
        "deepseek",
        "openai",
        "openai_compatible",
        "siliconflow",
    }
    if provider not in supported:
        return False
    if not config.default_model.strip() or resolve_api_key(config, path) is None:
        return False
    return provider == "anthropic" or bool(config.base_url.strip())
