from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.llm.model_catalog import add_model, list_models


# 功能：验证 Anthropic 模型目录包含活动模型、内置选项和新增模型且不重复
# 设计：使用临时目录隔离用户状态，连续添加同一模型以覆盖持久化去重边界
def test_anthropic_model_catalog_merges_active_defaults_and_custom(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.json"

    add_model("anthropic", "claude-custom", path=path)
    add_model("anthropic", "claude-custom", path=path)
    models = list_models("anthropic", "claude-active", path=path)

    assert models[0] == "claude-active"
    assert "claude-sonnet-4-6" in models
    assert models.count("claude-custom") == 1


# 功能：验证不同 Provider 的自定义模型目录相互隔离
# 设计：向同一 JSON 文件写入两类 Provider，再分别读取以排除跨端点模型污染
def test_model_catalog_isolated_by_provider(tmp_path: Path) -> None:
    path = tmp_path / "models.json"

    add_model("anthropic", "claude-custom", path=path)
    add_model("openai_compatible", "deepseek-custom", path=path)

    assert "deepseek-custom" not in list_models("anthropic", "", path=path)
    assert list_models("openai_compatible", "active", path=path) == [
        "active",
        "deepseek-custom",
    ]


# 功能：验证损坏的模型目录不会被静默覆盖
# 设计：预写非法 JSON 后尝试读取，断言错误包含文件路径以便用户定位修复
def test_model_catalog_reports_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="models.json"):
        list_models("anthropic", "claude-active", path=path)


# 功能：验证模型目录使用稳定的版本化 JSON 结构
# 设计：写入单个模型后直接解析磁盘内容，确保后续迁移有明确 schema 入口
def test_model_catalog_writes_versioned_json(tmp_path: Path) -> None:
    path = tmp_path / "models.json"

    add_model("openai-compatible", "qwen-test", path=path)
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data == {
        "version": 1,
        "providers": {"openai_compatible": ["qwen-test"]},
    }
