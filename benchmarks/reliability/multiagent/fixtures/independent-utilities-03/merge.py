from collections.abc import Mapping


# 递归合并两个映射并让覆盖值优先
def deep_merge(
    base: Mapping[str, object],
    override: Mapping[str, object],
) -> dict[str, object]:
    return {**base, **override}
