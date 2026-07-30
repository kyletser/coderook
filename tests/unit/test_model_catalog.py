from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.core.llm.model_catalog import add_model, add_models, list_models


# 功能：验证模型目录只包含活动模型和已探测或手动新增的模型且不重复
# 设计：使用临时目录隔离用户状态，连续添加同一模型以排除未验证的硬编码模型
def test_model_catalog_merges_active_and_verified_models(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models.json"

    add_model("anthropic", "claude-custom", path=path)
    add_model("anthropic", "claude-custom", path=path)
    models = list_models("anthropic", "claude-active", path=path)

    assert models == ["claude-active", "claude-custom"]


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


# 功能：验证 API 探测结果可以一次性写入模型目录并保持顺序
# 设计：批量传入包含重复项的列表，确保只进行语义去重且不改变服务端推荐顺序
def test_model_catalog_adds_discovered_models_in_batch(tmp_path: Path) -> None:
    path = tmp_path / "models.json"

    add_models(
        "deepseek",
        ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-pro"],
        path=path,
    )

    assert list_models("deepseek", "", path=path) == [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ]
