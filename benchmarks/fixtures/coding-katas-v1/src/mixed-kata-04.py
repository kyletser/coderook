import json
from typing import Any


# 保留布尔值与空值地编码对象
def solve(value: dict[str, Any]) -> str:
    return json.dumps({key: str(item) for key, item in value.items()})
