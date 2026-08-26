from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from code_rook.core.events.bus import EventBus
from code_rook.core.llm.experiment_budget import (
    ExperimentBudgetExceeded,
    ExperimentBudgetProvider,
)
from code_rook.core.llm.types import LlmResponse, UsageStats


class _Provider:
    # 返回带可审计 usage 的固定响应供预算结算测试
    async def chat(self, *_args: Any, **_kwargs: Any) -> LlmResponse:
        return LlmResponse(
            stop_reason="end_turn",
            text="secret response",
            usage=UsageStats(input_tokens=1_000, output_tokens=100),
        )


# 功能：验证实验预算账本只保存用量成本且不会落盘 Prompt 或响应正文
# 设计：调用真实包装器后读取 JSON 文本，检查 token 结算与敏感正文缺席
async def test_experiment_budget_records_usage_without_content(tmp_path: Path) -> None:
    ledger = tmp_path / "budget.json"
    provider = ExperimentBudgetProvider(
        _Provider(),
        model="deepseek-v4-flash",
        ledger_path=ledger,
        limit_usd=1.0,
    )

    await provider.chat(
        messages=[{"role": "user", "content": "secret prompt"}],
        tool_schemas=[],
        bus=EventBus(),
        run_id="budget-test",
    )

    text = ledger.read_text(encoding="utf-8")
    state = json.loads(text)
    assert state["input_tokens"] == 1_000
    assert state["output_tokens"] == 100
    assert state["reservations"] == {}
    assert "secret prompt" not in text
    assert "secret response" not in text


# 功能：验证不足一次保守预留额度时 Provider 在网络调用前硬拒绝
# 设计：用低于 0.25 USD 的总预算构造边界，断言异常且底层 Provider 未被调用
async def test_experiment_budget_refuses_call_without_reservation(tmp_path: Path) -> None:
    calls = 0

    class CountingProvider:
        # 记录底层调用次数以证明拒绝发生在请求之前
        async def chat(self, *_args: Any, **_kwargs: Any) -> LlmResponse:
            nonlocal calls
            calls += 1
            return LlmResponse(stop_reason="end_turn", text="unexpected")

    provider = ExperimentBudgetProvider(
        CountingProvider(),
        model="deepseek-v4-flash",
        ledger_path=tmp_path / "budget.json",
        limit_usd=0.2,
    )

    with pytest.raises(ExperimentBudgetExceeded, match="reserve"):
        await provider.chat(
            messages=[],
            tool_schemas=[],
            bus=EventBus(),
            run_id="budget-test",
        )

    assert calls == 0
