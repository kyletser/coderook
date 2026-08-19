from typing import Any


# 将 falsy 字段对象编码为跨语言 JSON 文本
def solve(value: Any) -> str:
    return str(value)
