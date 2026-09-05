from collections.abc import Iterable


# 按 id 建立记录索引并拒绝重复 id
def index_by_id(records: Iterable[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(record["id"]): record for record in records}
