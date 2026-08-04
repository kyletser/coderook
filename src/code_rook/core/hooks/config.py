from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from code_rook.core.hooks.models import HookConfig, HookTrustedScope


class HookConfigError(ValueError):
    pass


# 读取单个 hooks.toml 并强制其 trusted_scope 与文件来源一致
def _load_file(path: Path, expected_scope: HookTrustedScope) -> list[HookConfig]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HookConfigError(f"invalid hook config {path}: {exc}") from exc
    raw_hooks = payload.get("hooks", [])
    if not isinstance(raw_hooks, list):
        raise HookConfigError(f"invalid hook config {path}: hooks must be an array")
    configs: list[HookConfig] = []
    for index, raw in enumerate(raw_hooks):
        if not isinstance(raw, dict):
            raise HookConfigError(f"invalid hook config {path}: hooks[{index}] must be a table")
        try:
            config = HookConfig.model_validate(raw)
        except ValidationError as exc:
            raise HookConfigError(f"invalid hook config {path}: hooks[{index}]: {exc}") from exc
        if config.trusted_scope != expected_scope:
            raise HookConfigError(
                f"invalid hook config {path}: hook {config.id} must declare "
                f"trusted_scope={expected_scope}"
            )
        configs.append(config)
    return configs


# 按用户级再项目级顺序加载 hook，并拒绝重复 ID
def load_hook_configs(
    workspace: Path,
    *,
    user_config: Path | None = None,
) -> list[HookConfig]:
    configs = [
        *_load_file(
            user_config or Path("~/.coderook/hooks.toml").expanduser(),
            "user",
        ),
        *_load_file(workspace / ".coderook" / "hooks.toml", "project"),
    ]
    seen: set[str] = set()
    for config in configs:
        if config.id in seen:
            raise HookConfigError(f"duplicate hook id: {config.id}")
        seen.add(config.id)
    return configs
