# 将数值限制在给定的闭区间内
def clamp(value: int, lower: int, upper: int) -> int:
    if value < lower:
        return lower
    if value > upper:
        return lower
    return value
