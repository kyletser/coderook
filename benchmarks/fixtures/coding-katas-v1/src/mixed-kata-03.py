import json


# 以 UTF-8 语义编码 Unicode 文本
def solve(value: str) -> str:
    return json.dumps(value)
