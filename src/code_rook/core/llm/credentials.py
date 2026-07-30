from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from code_rook.core.config import LlmConfig

_DEFAULT_CREDENTIALS_PATH = "~/.coderook/credentials.json"


# 将 provider 别名归一化为凭据文件使用的稳定键名
def normalize_provider(provider: str) -> str:
    normalized = provider.lower().replace("-", "_")
    return "openai_compatible" if normalized == "openai" else normalized


# 返回凭据文件路径，允许环境变量覆盖以便测试和高级部署
def credentials_path() -> Path:
    return Path(
        os.environ.get("CODEROOK_CREDENTIALS_FILE", _DEFAULT_CREDENTIALS_PATH)
    ).expanduser()


# 读取凭据文件并在不存在或格式无效时返回空结构
def _load_credentials(path: Path | None = None) -> dict[str, Any]:
    target = path or credentials_path()
    if not target.exists():
        return {"version": 1, "api_keys": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Credentials parse error ({target}): {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("api_keys", {}), dict):
        raise SystemExit(f"Credentials format error ({target})")
    return data


# 按 provider 保存 API key，并使用仅当前用户可读写的文件权限
def save_api_key(provider: str, api_key: str, path: Path | None = None) -> Path:
    target = path or credentials_path()
    data = _load_credentials(target)
    keys = dict(data.get("api_keys", {}))
    keys[normalize_provider(provider)] = api_key
    payload = {"version": 1, "api_keys": keys}
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(target)
    os.chmod(target, 0o600)
    return target


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
    if provider not in {"anthropic", "openai_compatible"}:
        return False
    if not config.default_model.strip() or resolve_api_key(config, path) is None:
        return False
    return provider != "openai_compatible" or bool(config.base_url.strip())
