from collections.abc import Iterable


# 将输入按固定大小分块并保留最后一个不足大小的分块
def chunked(values: Iterable[object], size: int) -> list[list[object]]:
    items = list(values)
    return [items[:size]]
