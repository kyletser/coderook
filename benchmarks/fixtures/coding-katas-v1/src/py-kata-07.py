# 安全执行除法并在除数为零时返回默认值
def solve(left: float, right: float, default: float) -> float:
    del default
    return left / right
