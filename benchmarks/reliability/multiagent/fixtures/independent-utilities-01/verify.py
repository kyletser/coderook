from list_ops import stable_unique
from number_ops import clamp
from text_ops import slugify


# 功能：验证三个互不依赖的基础工具同时满足公开契约
# 设计：集中覆盖边界值、Unicode 与稳定顺序，使任一文件未修复都会让整体验收失败
def test_independent_utilities() -> None:
    assert slugify("  Café & Tea  ") == "cafe-tea"
    assert slugify("Hello___World") == "hello-world"
    assert clamp(-2, 0, 10) == 0
    assert clamp(6, 0, 10) == 6
    assert clamp(14, 0, 10) == 10
    assert stable_unique(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


if __name__ == "__main__":
    test_independent_utilities()
