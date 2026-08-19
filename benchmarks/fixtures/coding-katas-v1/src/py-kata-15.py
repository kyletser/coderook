# 将列表向右循环移动指定步数
def solve(values: list[int], steps: int) -> list[int]:
    return values[steps:] + values[:steps]
