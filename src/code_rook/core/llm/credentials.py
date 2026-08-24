from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

import keyring
from pydantic import BaseModel, ConfigDict

from code_rook.core.config import LlmConfig
from code_rook.core.llm.kinds import SUPPORTED_LEGACY_PROVIDERS
from code_rook.core.llm.provider_presets import get_provider_preset
from code_rook.core.llm.routes import CredentialSource

logger = logging.getLogger(__name__)

_DEFAULT_CREDENTIALS_PATH = "~/.coderook/credentials.json"
_KEYRING_SERVICE = "coderook"
_CREDENTIALS_VERSION = 2
_CREDENTIAL_FIELDS = frozenset({"version", "api_keys", "route_credentials"})

CredentialStoreStatus = Literal["missing", "ready", "invalid"]
CredentialStoreErrorCode = Literal[
    "unsafe_path",
    "read_failed",
    "invalid_json",
    "invalid_format",
    "unsupported_version",
    "write_failed",
]


class CredentialStoreError(RuntimeError):
    # 保存可供进程边界分类的脱敏凭据存储错误
    def __init__(
        self,
        code: CredentialStoreErrorCode,
        path: Path,
        message: str,
    ) -> None:
        self.code = code
        self.path = path
        self.safe_message = message
        super().__init__(f"{message} ({path})")


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


# 按用户进程环境高于显式文件 overlay 的顺序解析单个凭据变量
def resolve_env_credential(
    name: str,
    overlay: Mapping[str, str] | None = None,
) -> str | None:
    if name in os.environ:
        value = os.environ.get(name)
    else:
        value = (overlay or {}).get(name)
    return value if isinstance(value, str) and value else None


# 将 provider 别名归一化为凭据文件使用的稳定键名
def normalize_provider(provider: str) -> str:
    return provider.lower().replace("-", "_")


# 返回凭据文件路径，允许环境变量覆盖以便测试和高级部署
def credentials_path() -> Path:
    return Path(os.environ.get("CODEROOK_CREDENTIALS_FILE", _DEFAULT_CREDENTIALS_PATH)).expanduser()


# 验证凭据文档及其直接父目录不会通过符号链接越出调用者选择的位置
def _validated_credentials_path(path: Path) -> Path:
    target = path.expanduser().absolute()
    parent = target.parent
    if os.path.lexists(parent) and (parent.is_symlink() or not parent.is_dir()):
        raise CredentialStoreError(
            "unsafe_path",
            target,
            "credential store parent directory is unsafe",
        )
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise CredentialStoreError(
            "unsafe_path",
            target,
            "credential store must be a regular file",
        )
    return target


# 严格读取凭据文件，兼容旧版但拒绝静默降级未来版本或丢弃未知字段
def _load_credentials(path: Path | None = None) -> dict[str, Any]:
    target = _validated_credentials_path(path or credentials_path())
    if not target.exists():
        return {
            "version": _CREDENTIALS_VERSION,
            "api_keys": {},
            "route_credentials": {},
        }
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CredentialStoreError(
            "invalid_json",
            target,
            "credential store contains invalid JSON",
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise CredentialStoreError(
            "read_failed",
            target,
            "credential store could not be read",
        ) from exc
    if not isinstance(data, dict):
        raise CredentialStoreError(
            "invalid_format",
            target,
            "credential store document must be an object",
        )
    version = data.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise CredentialStoreError(
            "invalid_format",
            target,
            "credential store version is invalid",
        )
    if version > _CREDENTIALS_VERSION:
        raise CredentialStoreError(
            "unsupported_version",
            target,
            "credential store version is newer than supported",
        )
    if version < 1:
        raise CredentialStoreError(
            "invalid_format",
            target,
            "credential store version is invalid",
        )
    unknown = sorted(set(data) - _CREDENTIAL_FIELDS)
    if unknown:
        raise CredentialStoreError(
            "invalid_format",
            target,
            "credential store contains unknown fields",
        )
    api_keys = data.get("api_keys", {})
    route_credentials = data.get("route_credentials", {})
    if not _valid_credential_mapping(api_keys) or not _valid_credential_mapping(
        route_credentials
    ):
        raise CredentialStoreError(
            "invalid_format",
            target,
            "credential store mappings are invalid",
        )
    return {
        "version": _CREDENTIALS_VERSION,
        "api_keys": dict(api_keys),
        "route_credentials": dict(route_credentials),
    }


# 判断凭据映射只包含字符串账户名与字符串密钥正文
def _valid_credential_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(secret, str)
        for key, secret in value.items()
    )


# 使用原子替换保存权限收紧的凭据文档
def _save_credentials(data: dict[str, Any], target: Path) -> None:
    target = _validated_credentials_path(target)
    temporary: Path | None = None
    descriptor = -1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target = _validated_credentials_path(target)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            descriptor = -1
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        _validated_credentials_path(target)
        os.replace(temporary, target)
        temporary = None
    except CredentialStoreError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise CredentialStoreError(
            "write_failed",
            target,
            "credential store could not be written",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


# 只读返回脱敏凭据存储健康状态且绝不移动、迁移或重写原文档
def inspect_credential_store(path: Path | None = None) -> CredentialStoreStatus:
    target = path or credentials_path()
    try:
        validated = _validated_credentials_path(target)
        if not validated.exists():
            return "missing"
        _load_credentials(validated)
    except CredentialStoreError:
        return "invalid"
    return "ready"


# 按 provider 保存 API key，并使用仅当前用户可读写的文件权限
def save_api_key(provider: str, api_key: str, path: Path | None = None) -> Path:
    target = path or credentials_path()
    data = _load_credentials(target)
    keys = dict(data.get("api_keys", {}))
    keys[normalize_provider(provider)] = api_key
    payload = {
        "version": _CREDENTIALS_VERSION,
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
        env_overlay: Mapping[str, str] | None = None,
    ) -> None:
        self.path = path or credentials_path()
        self._backend = backend or _SystemCredentialBackend()
        self._env_overlay = dict(env_overlay or {})

    # 解析 env/keyring/file 引用，只返回密钥来源而不在错误中包含正文
    def resolve(self, credential_ref: str) -> CredentialResolution:
        kind, separator, account = credential_ref.partition(":")
        if not separator or not account:
            return CredentialResolution(source="missing")
        if kind == "env":
            value = resolve_env_credential(account, self._env_overlay)
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
                    legacy.get(normalize_provider(account)) if isinstance(legacy, dict) else None
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
            except Exception as exc:
                logger.warning(
                    "OS keyring unavailable for route %s (%s); "
                    "falling back to the credentials file",
                    route_id,
                    type(exc).__name__,
                )
        data = _load_credentials(self.path)
        route_values = dict(data.get("route_credentials", {}))
        route_values[route_id] = secret
        payload = {
            "version": _CREDENTIALS_VERSION,
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
                "version": _CREDENTIALS_VERSION,
                "api_keys": dict(data.get("api_keys", {})),
                "route_credentials": route_values,
            }
            _save_credentials(payload, self.path)


# 按环境变量优先、凭据文件兜底的顺序解析当前 provider 的 API key
def resolve_api_key(config: LlmConfig, path: Path | None = None) -> str | None:
    if config.api_key_env:
        env_value = resolve_env_credential(
            config.api_key_env,
            config.credential_overlay,
        )
        if env_value:
            return env_value
    keys = _load_credentials(path).get("api_keys", {})
    value = keys.get(normalize_provider(config.provider))
    return value if isinstance(value, str) and value else None


# 判断当前 LLM 配置是否具备启动所需的模型、端点和密钥
def llm_is_configured(config: LlmConfig, path: Path | None = None) -> bool:
    provider = normalize_provider(config.provider)
    if provider not in SUPPORTED_LEGACY_PROVIDERS:
        return False
    if not config.default_model.strip():
        return False
    try:
        credential_required = get_provider_preset(provider).credential_required
    except ValueError:
        credential_required = True
    if credential_required and resolve_api_key(config, path) is None:
        return False
    return provider == "anthropic" or bool(config.base_url.strip())
