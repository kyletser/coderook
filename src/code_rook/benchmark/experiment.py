from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from code_rook.core.config import CodeRookConfig
from code_rook.core.llm.pricing import resolve_pricing_quote
from code_rook.core.llm.route_registry import ResolvedRoute, RouteRegistry


# 解析已通过 Doctor 的当前活动路由并返回不含凭据的实验候选信息
def resolve_experiment_candidate(
    config: CodeRookConfig,
    *,
    temperature: float = 0.0,
    expected_model: str | None = None,
    require_pricing: bool = True,
) -> tuple[ResolvedRoute, dict[str, Any]]:
    configured_registry = RouteRegistry(config.llm)
    configured = configured_registry.resolve()
    readiness = configured_registry.configuration_service().readiness(configured.route)
    if readiness.status != "provider_verified" or not readiness.local_ready:
        raise RuntimeError(
            "active route has not passed a current Provider Doctor verification"
        )
    registry = RouteRegistry(config.llm, temperature_override=temperature)
    resolved = registry.resolve()
    if expected_model is not None and resolved.route.model != expected_model:
        raise RuntimeError(
            f"reliability experiments require model {expected_model}; "
            f"active model is {resolved.route.model}"
        )
    quote = resolve_pricing_quote(resolved.route.model)
    if quote is None and require_pricing:
        raise RuntimeError(f"pricing is unavailable for model {resolved.route.model}")
    return resolved, {
        "route_id": resolved.route.id,
        "model": resolved.route.model,
        "wire_format": resolved.route.wire_format,
        "temperature": resolved.route.temperature,
        "route_digest": resolved.route.validation_digest(),
        "doctor_status": readiness.provider_validation,
        "pricing_known": quote is not None,
        "pricing_source": quote.source if quote is not None else "unavailable",
        "pricing_effective_date": quote.effective_date if quote is not None else "",
    }


# 为直接运行的付费实验补齐共享硬预算环境且拒绝不一致的外部预算
def configure_experiment_budget(
    output: Path,
    *,
    limit_usd: float,
    expected_model: str,
    ledger_name: str = "budget.json",
) -> Path:
    raw_path = os.environ.get("CODEROOK_EXPERIMENT_BUDGET_FILE", "").strip()
    raw_limit = os.environ.get("CODEROOK_EXPERIMENT_BUDGET_USD", "").strip()
    raw_model = os.environ.get("CODEROOK_EXPERIMENT_EXPECTED_MODEL", "").strip()
    if len({bool(raw_path), bool(raw_limit), bool(raw_model)}) != 1:
        raise RuntimeError(
            "experiment budget path, limit and expected model must be configured together"
        )
    if raw_path:
        if float(raw_limit) != limit_usd:
            raise RuntimeError(
                "experiment stage budget does not match the script --max-cost-usd value"
            )
        if raw_model != expected_model:
            raise RuntimeError(
                "experiment stage model does not match the frozen candidate model"
            )
        return Path(raw_path).resolve()
    ledger = (output / ledger_name).resolve()
    os.environ["CODEROOK_EXPERIMENT_BUDGET_FILE"] = str(ledger)
    os.environ["CODEROOK_EXPERIMENT_BUDGET_USD"] = str(limit_usd)
    os.environ["CODEROOK_EXPERIMENT_EXPECTED_MODEL"] = expected_model
    return ledger


# 返回完整提交、工作树状态和是否适合生成可引用实验报告
def candidate_git_state() -> dict[str, Any]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    dirty_paths = [line[3:] for line in status.stdout.splitlines() if line.strip()]
    return {
        "commit": commit or "unknown",
        "working_tree_clean": status.returncode == 0 and not dirty_paths,
        "dirty_paths": dirty_paths,
    }
