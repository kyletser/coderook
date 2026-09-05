from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from code_rook.benchmark import experiment
from code_rook.core.config import CodeRookConfig


class _Route:
    id = "active-route"
    model = "deepseek-v4-flash"
    wire_format = "openai_chat"
    temperature = 0.0

    # 返回固定路由摘要供候选清单断言
    def validation_digest(self) -> str:
        return "a" * 64


class _Registry:
    readiness_status = "provider_verified"

    # 接受生产构造参数但只保存测试所需状态
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.route = _Route()

    # 返回不含真实凭据的最小解析结果
    def resolve(self) -> SimpleNamespace:
        return SimpleNamespace(route=self.route, credential="test")

    # 返回绑定当前路由的同步 readiness stub
    def configuration_service(self) -> SimpleNamespace:
        readiness = SimpleNamespace(
            status=self.readiness_status,
            local_ready=self.readiness_status == "provider_verified",
            provider_validation="verified_passed",
        )
        return SimpleNamespace(readiness=lambda _route: readiness)


# 功能：验证实验候选跟随当前活动 Route 而不是依赖历史 route id
# 设计：注入非 legacy 的已验证 Route，断言候选保留 id、wire format 与摘要
def test_resolve_experiment_candidate_uses_verified_active_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "RouteRegistry", _Registry)
    monkeypatch.setattr(
        experiment,
        "resolve_pricing_quote",
        lambda _model: SimpleNamespace(source="builtin", effective_date="2026-01-01"),
    )

    _resolved, candidate = experiment.resolve_experiment_candidate(CodeRookConfig())

    assert candidate["route_id"] == "active-route"
    assert candidate["wire_format"] == "openai_chat"
    assert candidate["doctor_status"] == "verified_passed"
    assert candidate["route_digest"] == "a" * 64


# 功能：验证实验候选默认接受任意已验证活动模型且可显式锁定模型
# 设计：复用固定 Route stub，先断言无锁定成功，再用不匹配名称确认在调用模型前失败
def test_resolve_experiment_candidate_supports_explicit_model_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "RouteRegistry", _Registry)
    monkeypatch.setattr(
        experiment,
        "resolve_pricing_quote",
        lambda _model: SimpleNamespace(source="builtin", effective_date="2026-01-01"),
    )

    _resolved, candidate = experiment.resolve_experiment_candidate(CodeRookConfig())

    assert candidate["model"] == "deepseek-v4-flash"
    with pytest.raises(RuntimeError, match="required-model"):
        experiment.resolve_experiment_candidate(
            CodeRookConfig(),
            expected_model="required-model",
        )


# 功能：验证显式允许未知价格时仍可冻结模型候选并如实标记成本不可估算
# 设计：让定价解析返回空值，分别覆盖默认拒绝和显式放行，防止实验偷偷采用虚构价格
def test_resolve_experiment_candidate_can_disclose_unknown_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "RouteRegistry", _Registry)
    monkeypatch.setattr(experiment, "resolve_pricing_quote", lambda _model: None)

    with pytest.raises(RuntimeError, match="pricing is unavailable"):
        experiment.resolve_experiment_candidate(CodeRookConfig())

    _resolved, candidate = experiment.resolve_experiment_candidate(
        CodeRookConfig(),
        require_pricing=False,
    )

    assert candidate["pricing_known"] is False
    assert candidate["pricing_source"] == "unavailable"
    assert candidate["pricing_effective_date"] == ""


# 功能：验证未通过当前 Doctor 的 Route 在任何付费调用前被预检拒绝
# 设计：仅切换 stub readiness 状态，证明失败来自候选门禁而非网络或 Provider
def test_resolve_experiment_candidate_rejects_unverified_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnverifiedRegistry(_Registry):
        readiness_status = "provider_unverified"

    monkeypatch.setattr(experiment, "RouteRegistry", _UnverifiedRegistry)

    with pytest.raises(RuntimeError, match="Doctor"):
        experiment.resolve_experiment_candidate(CodeRookConfig())


# 功能：验证直接运行实验时自动配置与 CLI 上限一致的共享硬预算
# 设计：隔离环境并使用临时输出目录，断言路径和金额在 Provider 创建前已冻结
def test_configure_experiment_budget_sets_matching_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CODEROOK_EXPERIMENT_BUDGET_FILE", raising=False)
    monkeypatch.delenv("CODEROOK_EXPERIMENT_BUDGET_USD", raising=False)

    try:
        ledger = experiment.configure_experiment_budget(
            tmp_path,
            limit_usd=2.0,
            expected_model="deepseek-v4-flash-0731",
        )

        assert ledger == (tmp_path / "budget.json").resolve()
        assert experiment.os.environ["CODEROOK_EXPERIMENT_BUDGET_FILE"] == str(ledger)
        assert experiment.os.environ["CODEROOK_EXPERIMENT_BUDGET_USD"] == "2.0"
        assert (
            experiment.os.environ["CODEROOK_EXPERIMENT_EXPECTED_MODEL"]
            == "deepseek-v4-flash-0731"
        )
    finally:
        experiment.os.environ.pop("CODEROOK_EXPERIMENT_BUDGET_FILE", None)
        experiment.os.environ.pop("CODEROOK_EXPERIMENT_BUDGET_USD", None)
        experiment.os.environ.pop("CODEROOK_EXPERIMENT_EXPECTED_MODEL", None)
