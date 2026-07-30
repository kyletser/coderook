from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from code_rook.core.llm.credentials import normalize_provider

_DEFAULT_CATALOG_PATH = "~/.coderook/models.json"
# 返回模型目录文件路径，并允许测试或高级用户通过环境变量覆盖
def model_catalog_path() -> Path:
    value = os.environ.get("CODEROOK_MODEL_CATALOG", _DEFAULT_CATALOG_PATH)
    return Path(value).expanduser()


# 读取模型目录 JSON，文件不存在时返回空目录
def _read_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "providers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid model catalog ({path}): {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("providers", {}), dict):
        raise ValueError(f"Invalid model catalog ({path}): providers must be an object")
    return data


# 返回当前 provider 可选择的模型，并确保活动模型位于列表首位
def list_models(
    provider: str,
    active_model: str,
    *,
    path: Path | None = None,
) -> list[str]:
    provider_name = normalize_provider(provider)
    catalog = _read_catalog(path or model_catalog_path())
    providers = catalog.get("providers", {})
    saved = providers.get(provider_name, [])
    if not isinstance(saved, list) or any(not isinstance(item, str) for item in saved):
        raise ValueError(
            f"Invalid model catalog ({path or model_catalog_path()}): "
            f"{provider_name} must be a string array"
        )
    candidates = (active_model, *saved)
    return list(dict.fromkeys(item.strip() for item in candidates if item.strip()))


# 将模型添加到指定 provider 的目录并原子保存
def add_model(
    provider: str,
    model: str,
    *,
    path: Path | None = None,
) -> Path:
    return add_models(provider, [model], path=path)


# 将一组已探测模型批量添加到指定 provider 的目录并原子保存
def add_models(
    provider: str,
    models: list[str] | tuple[str, ...],
    *,
    path: Path | None = None,
) -> Path:
    selected_models = list(dict.fromkeys(model.strip() for model in models if model.strip()))
    if not selected_models:
        raise ValueError("model list cannot be empty")
    target = path or model_catalog_path()
    catalog = _read_catalog(target)
    providers = catalog.setdefault("providers", {})
    provider_name = normalize_provider(provider)
    saved = providers.setdefault(provider_name, [])
    if not isinstance(saved, list) or any(not isinstance(item, str) for item in saved):
        raise ValueError(
            f"Invalid model catalog ({target}): {provider_name} must be a string array"
        )
    for selected in selected_models:
        if selected not in saved:
            saved.append(selected)
    catalog["version"] = 1
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(target)
    return target
