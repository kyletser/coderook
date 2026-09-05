# 计算带上限的指数退避时间，首次重试使用 base 秒
def exponential_delay(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2**attempt))
