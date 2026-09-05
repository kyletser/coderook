from batching import chunked
from duration import parse_duration
from records import index_by_id


# 功能：验证三个数据转换器分别处理组合输入、重复键和尾部分块
# 设计：每组断言只依赖一个源码文件，保持任务可独立委派且由同一最终门禁统一验收
def test_independent_transformers() -> None:
    assert parse_duration("1h 2m 3s") == 3723
    assert parse_duration("45m") == 2700
    try:
        parse_duration("1x")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid duration must fail")

    records = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    assert index_by_id(records) == {"a": records[0], "b": records[1]}
    try:
        index_by_id([{"id": "a"}, {"id": "a"}])
    except ValueError:
        pass
    else:
        raise AssertionError("duplicate ids must fail")

    assert chunked(range(5), 2) == [[0, 1], [2, 3], [4]]
    try:
        chunked([1], 0)
    except ValueError:
        pass
    else:
        raise AssertionError("non-positive chunk size must fail")


if __name__ == "__main__":
    test_independent_transformers()
