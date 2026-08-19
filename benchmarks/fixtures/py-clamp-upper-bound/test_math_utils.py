from math_utils import clamp


# 功能：验证 clamp 对下界、区间内部和上界外输入都返回正确结果
# 设计：用三个代表值覆盖全部条件分支，使错误的上界返回值稳定暴露
def test_clamp_respects_both_bounds() -> None:
    assert clamp(-3, 0, 10) == 0
    assert clamp(4, 0, 10) == 4
    assert clamp(13, 0, 10) == 10
