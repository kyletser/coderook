from __future__ import annotations

from code_rook.core.features import labs_enabled


# 功能：验证 Labs 在没有显式进程开关时保持关闭
# 设计：注入空映射而不是依赖开发机环境，保证测试结果可重复
def test_labs_are_disabled_by_default() -> None:
    assert labs_enabled({}) is False


# 功能：验证维护者可使用文档化的真值形式显式开启 Labs
# 设计：参数化常用大小写和空白形式，同时排除任意非空字符串被误当成授权
def test_labs_require_an_explicit_true_value() -> None:
    for value in ("1", "true", "TRUE", " yes ", "On"):
        assert labs_enabled({"CODEROOK_LABS": value}) is True
    for value in ("", "0", "false", "enabled", "no"):
        assert labs_enabled({"CODEROOK_LABS": value}) is False
