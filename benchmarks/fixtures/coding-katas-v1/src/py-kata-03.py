# 保留首次出现顺序地去重列表
def solve(values: list[int]) -> list[int]:
    return sorted(set(values))
