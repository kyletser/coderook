# 统计文本中的元音字母数量
def solve(value: str) -> int:
    return sum(character in "aeiou" for character in value)
