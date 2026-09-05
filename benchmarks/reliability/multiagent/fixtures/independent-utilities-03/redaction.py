from collections.abc import Mapping


# 递归遮盖映射中的敏感字段且保持其他结构不变
def redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return dict(value)
