# 将数值限制在包含上下界的闭区间内
def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, value)
