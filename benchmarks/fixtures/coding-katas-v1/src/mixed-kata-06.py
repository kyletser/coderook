from typing import Any


# 将数字数组编码为跨语言 JSON 文本
def solve(value: list[Any]) -> str:
    return ",".join(map(str, value))
