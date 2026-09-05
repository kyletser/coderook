from collections.abc import Iterable


# 按首次出现顺序返回去重后的元素
def stable_unique(values: Iterable[object]) -> list[object]:
    return list(set(values))
