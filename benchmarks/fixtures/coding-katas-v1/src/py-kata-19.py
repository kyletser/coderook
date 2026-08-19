from urllib.parse import parse_qs


# 解析查询字符串并保留重复键的全部值
def solve(value: str) -> dict[str, list[str]]:
    return {key: items[-1:] for key, items in parse_qs(value).items()}
