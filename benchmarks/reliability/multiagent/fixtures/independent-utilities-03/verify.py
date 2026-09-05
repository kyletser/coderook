from merge import deep_merge
from redaction import redact_mapping
from retry import exponential_delay


# 功能：验证退避、递归脱敏和深合并三个运行时辅助函数的关键边界
# 设计：用嵌套映射和零基重试约束排除表面实现，同时保持三个修复文件完全不重叠
def test_independent_runtime_helpers() -> None:
    assert exponential_delay(0, 0.5, 4.0) == 0.5
    assert exponential_delay(3, 0.5, 3.0) == 3.0
    try:
        exponential_delay(-1, 0.5, 4.0)
    except ValueError:
        pass
    else:
        raise AssertionError("negative attempts must fail")

    source = {
        "name": "rook",
        "api_key": "secret",
        "nested": {"session_token": "token", "keep": 7},
    }
    assert redact_mapping(source) == {
        "name": "rook",
        "api_key": "[REDACTED]",
        "nested": {"session_token": "[REDACTED]", "keep": 7},
    }
    assert source["api_key"] == "secret"

    assert deep_merge(
        {"model": {"name": "a", "timeout": 30}, "enabled": True},
        {"model": {"timeout": 60}, "extra": 1},
    ) == {
        "model": {"name": "a", "timeout": 60},
        "enabled": True,
        "extra": 1,
    }


if __name__ == "__main__":
    test_independent_runtime_helpers()
