from __future__ import annotations

from pathlib import Path

import pytest

from code_rook.core.llm.pricing import (
    ModelPricing,
    cache_read_savings,
    estimate_cost,
    format_cost,
    get_pricing,
    load_pricing_overrides,
)


# 功能：验证单价查找支持精确、前缀与用户覆盖优先级
# 设计：用内置 claude 系模型验证日期后缀前缀匹配，再用覆盖表证明同名覆盖生效
def test_get_pricing_exact_prefix_and_override() -> None:
    exact = get_pricing("claude-sonnet-4-6")
    assert exact is not None and exact.input_per_m == 3.0

    dated = get_pricing("claude-sonnet-4-6-20260101")
    assert dated is exact

    overrides = {"claude-sonnet-4-6": ModelPricing(1.0, 5.0)}
    overridden = get_pricing("claude-sonnet-4-6", overrides)
    assert overridden is not None and overridden.input_per_m == 1.0

    assert get_pricing("totally-unknown-model") is None
    assert get_pricing("") is None


# 功能：验证 DeepSeek V4 当前模型名能命中独立输入、输出与缓存读取单价
# 设计：直接查询两个 Catalog 模型 ID，并确认未知模型不会误套价格
def test_deepseek_v4_pricing_uses_current_catalog_ids() -> None:
    flash = get_pricing("deepseek-v4-flash")
    pro = get_pricing("deepseek-v4-pro")

    assert flash == ModelPricing(0.14, 0.28, 0.0028)
    assert pro == ModelPricing(0.435, 0.87, 0.003625)
    assert get_pricing("retired-model") is None


# 功能：验证成本估算覆盖输入、输出与缓存读写四类用量
# 设计：用整数 token 数乘单价手工核算期望值，避免浮点意外
def test_estimate_cost_all_components() -> None:
    pricing = ModelPricing(
        input_per_m=3.0,
        output_per_m=15.0,
        cache_read_per_m=0.3,
        cache_write_per_m=3.75,
    )

    cost = estimate_cost(
        pricing,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )

    assert cost == pytest.approx(3.0 + 15.0 + 0.3 + 3.75)
    assert cache_read_savings(pricing, 1_000_000) == pytest.approx(2.7)


# 功能：验证成本金额格式化在零、极小与常规区间的展示
# 设计：覆盖三个数量级断言，保证顶栏与 /cost 输出稳定
def test_format_cost_tiers() -> None:
    assert format_cost(0.0) == "$0"
    assert format_cost(0.00005) == "<$0.0001"
    assert format_cost(0.01234) == "$0.0123"
    assert format_cost(1.5) == "$1.50"


# 功能：验证 pricing.toml 覆盖文件解析与错误提示
# 设计：写合法与缺 output 字段两种文件，断言解析结果与 ValueError
def test_load_pricing_overrides_from_toml(tmp_path: Path) -> None:
    good = tmp_path / "pricing.toml"
    good.write_text(
        """
[models."my-finetune"]
input = 0.5
output = 2.0
cache_read = 0.05
""",
        encoding="utf-8",
    )

    overrides = load_pricing_overrides(good)

    assert overrides["my-finetune"] == ModelPricing(0.5, 2.0, 0.05, 0.0)
    assert load_pricing_overrides(tmp_path / "missing.toml") == {}

    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[models."x"]\ninput = 1.0\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="needs numeric input/output"):
        load_pricing_overrides(bad)


# 功能：验证 TUI 按 llm.usage 事件即时更新当前会话的顶栏成本估算
# 设计：投递已知与未知单价事件，只断言实时顶栏；/cost 的持久证据由 Runtime 测试覆盖
async def test_tui_accumulates_cost_from_usage_events() -> None:

    from code_rook.tui.app import ChatTextArea, CodeRookTuiApp

    class CostHarness(CodeRookTuiApp):
        # 只挂载界面骨架并聚焦输入框
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = CostHarness("127.0.0.1", 9999)
    async with app.run_test(size=(120, 24)) as pilot:
        await pilot.pause()
        app._handle_event(
            {
                "type": "llm.usage",
                "run_id": "run-1",
                "input_tokens": 100_000,
                "output_tokens": 10_000,
                "cache_read_input_tokens": 50_000,
                "cache_creation_input_tokens": 0,
                "context_pct": 0.2,
                "model": "claude-sonnet-4-6",
            }
        )
        app._handle_event(
            {
                "type": "llm.usage",
                "run_id": "run-1",
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "context_pct": 0.2,
                "model": "mystery-model",
            }
        )
        await pilot.pause()

        # 100k*3 + 10k*15 + 50k*0.3 = 300+150+15 毫美元 = $0.465
        assert app._cost_total == pytest.approx(0.465)
        assert "mystery-model" in app._unpriced_models
        header_text = str(app.query_one("#header").render())
        assert "0.4650" in header_text

# 功能：验证会话切换复位成本累计
# 设计：先累计一笔成本，调用 _reset_cost_state 后断言全部分解归零
async def test_tui_resets_cost_on_session_switch() -> None:
    from code_rook.tui.app import ChatTextArea, CodeRookTuiApp

    class ResetHarness(CodeRookTuiApp):
        # 只挂载界面骨架
        def on_mount(self) -> None:
            self.query_one("#prompt", ChatTextArea).focus()

    app = ResetHarness("127.0.0.1", 9999)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        app._cost_total = 1.23
        app._cost_by_model = {"m": 1.23}

        app._reset_cost_state()

        assert app._cost_total == 0.0
        assert app._cost_by_model == {}
