from code_rook.core.agent_runtime.driver import (
    _STREAM_UPDATE_MAX_INTERVAL_S,
    _STREAM_UPDATE_MIN_CHARS,
    _should_emit_stream_update,
)


# 功能：验证快速到达的短 token 不会为每个字符生成一次完整消息刷新。
# 设计：固定单调时钟并比较阈值前后结果，排除真实 sleep 带来的不稳定性。
def test_stream_updates_are_batched_by_character_count() -> None:
    assert not _should_emit_stream_update(
        text="x" * (_STREAM_UPDATE_MIN_CHARS - 1),
        thinking="",
        previous_chars=0,
        previous_at=10.0,
        now=10.01,
    )
    assert _should_emit_stream_update(
        text="x" * _STREAM_UPDATE_MIN_CHARS,
        thinking="",
        previous_chars=0,
        previous_at=10.0,
        now=10.01,
    )


# 功能：验证模型输出较慢时即使字符不足阈值也会按最大等待时间刷新。
# 设计：同时覆盖正文与思考字符，证明刷新依据总可见内容且不会让短回答长期无反馈。
def test_stream_updates_flush_after_visible_interval() -> None:
    assert _should_emit_stream_update(
        text="回答",
        thinking="思考",
        previous_chars=0,
        previous_at=10.0,
        now=10.0 + _STREAM_UPDATE_MAX_INTERVAL_S,
    )
