from typing import Any


# 将对象编码为跨语言 JSON 文本
def solve(value: Any) -> str:
    return repr(value)
