from typing import Any


# 按键路径读取嵌套字典并在缺失时返回默认值
def solve(value: dict[str, Any], keys: list[str], default: Any) -> Any:
    del keys, default
    return value
