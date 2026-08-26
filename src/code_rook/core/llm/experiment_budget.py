from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from code_rook.core.daemon_lock import DaemonLock, DaemonLockBusyError
from code_rook.core.llm.base import LLMProvider
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.llm.types import LlmResponse

_BUDGET_LOCK = threading.Lock()
_CALL_RESERVATION_USD = 0.25


class ExperimentBudgetExceeded(RuntimeError):
    pass


# 获取预算账本的跨进程排他锁，避免 Worker 并发覆盖预留与结算状态
def _acquire_budget_lock(path: Path, *, timeout_s: float = 10.0) -> DaemonLock:
    lock = DaemonLock(path.with_name(path.name + ".lock"))
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock.acquire()
            return lock
        except DaemonLockBusyError:
            if time.monotonic() >= deadline:
                raise ExperimentBudgetExceeded(
                    "timed out waiting for the shared experiment budget ledger"
                ) from None
            time.sleep(0.02)


class ExperimentBudgetProvider:
    # 包装真实模型并用共享文件对每次实验调用先预留再结算成本
    def __init__(
        self,
        provider: LLMProvider,
        *,
        model: str,
        ledger_path: Path,
        limit_usd: float,
    ) -> None:
        if model != "deepseek-v4-flash":
            raise ExperimentBudgetExceeded(
                "reliability experiments are locked to deepseek-v4-flash"
            )
        if not 0 < limit_usd <= 35:
            raise ExperimentBudgetExceeded("experiment cost limit must be in (0, 35]")
        if resolve_pricing_quote(model) is None:
            raise ExperimentBudgetExceeded("model pricing is unavailable")
        self._provider = provider
        self._model = model
        self._ledger_path = ledger_path.resolve()
        self._limit_usd = limit_usd

    # 在调用前原子预留保守金额，调用后按真实 usage 结算且不记录正文
    async def chat(self, *args: Any, **kwargs: Any) -> LlmResponse:
        call_id = uuid.uuid4().hex
        self._reserve(call_id)
        try:
            response = await self._provider.chat(*args, **kwargs)
        except BaseException:
            self._release(call_id)
            raise
        self._settle(call_id, response)
        return response

    # 读取不存在或已存在的共享预算状态并验证版本
    def _read_state(self) -> dict[str, Any]:
        if not self._ledger_path.is_file():
            return {
                "schema_version": 1,
                "limit_usd": self._limit_usd,
                "spent_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reservations": {},
            }
        raw_state = json.loads(self._ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            raise ExperimentBudgetExceeded("invalid experiment budget ledger")
        state: dict[str, Any] = raw_state
        if state.get("schema_version") != 1:
            raise ExperimentBudgetExceeded("unsupported experiment budget ledger")
        if float(state.get("limit_usd", 0.0)) != self._limit_usd:
            raise ExperimentBudgetExceeded("experiment budget limit changed mid-run")
        return state

    # 使用临时文件替换持久化不含凭据和 Prompt 的预算状态
    def _write_state(self, state: dict[str, Any]) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._ledger_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary, self._ledger_path)

    # 在共享锁内预留一次调用的最坏成本并在不足时拒绝发出请求
    def _reserve(self, call_id: str) -> None:
        with _BUDGET_LOCK:
            process_lock = _acquire_budget_lock(self._ledger_path)
            try:
                state = self._read_state()
                reservations = dict(state.get("reservations", {}))
                reserved = sum(float(value) for value in reservations.values())
                spent = float(state.get("spent_usd", 0.0))
                if spent + reserved + _CALL_RESERVATION_USD > self._limit_usd:
                    raise ExperimentBudgetExceeded(
                        "experiment budget cannot reserve another complete model call"
                    )
                reservations[call_id] = _CALL_RESERVATION_USD
                state["reservations"] = reservations
                self._write_state(state)
            finally:
                process_lock.release()

    # 在失败或取消时释放本次预留但保留其他并发调用状态
    def _release(self, call_id: str) -> None:
        with _BUDGET_LOCK:
            process_lock = _acquire_budget_lock(self._ledger_path)
            try:
                state = self._read_state()
                reservations = dict(state.get("reservations", {}))
                reservations.pop(call_id, None)
                state["reservations"] = reservations
                self._write_state(state)
            finally:
                process_lock.release()

    # 根据统一 usage 和固定价格结算真实调用成本并释放预留
    def _settle(self, call_id: str, response: LlmResponse) -> None:
        usage = response.usage
        if usage is None:
            self._release(call_id)
            raise ExperimentBudgetExceeded(
                "model response omitted usage; experiment cost cannot be audited"
            )
        quote = resolve_pricing_quote(self._model)
        assert quote is not None
        cost = estimate_cost(
            quote.pricing,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
        )
        if cost > _CALL_RESERVATION_USD:
            self._release(call_id)
            raise ExperimentBudgetExceeded(
                "single call exceeded the conservative experiment reservation"
            )
        with _BUDGET_LOCK:
            process_lock = _acquire_budget_lock(self._ledger_path)
            try:
                state = self._read_state()
                reservations = dict(state.get("reservations", {}))
                reservations.pop(call_id, None)
                state["reservations"] = reservations
                state["spent_usd"] = float(state.get("spent_usd", 0.0)) + cost
                state["input_tokens"] = (
                    int(state.get("input_tokens", 0)) + usage.input_tokens
                )
                state["output_tokens"] = (
                    int(state.get("output_tokens", 0)) + usage.output_tokens
                )
                self._write_state(state)
            finally:
                process_lock.release()


# 仅在显式实验环境变量齐全时启用共享硬预算包装
def maybe_wrap_experiment_budget(
    provider: LLMProvider,
    *,
    model: str,
) -> LLMProvider:
    raw_path = os.environ.get("CODEROOK_EXPERIMENT_BUDGET_FILE", "").strip()
    raw_limit = os.environ.get("CODEROOK_EXPERIMENT_BUDGET_USD", "").strip()
    if not raw_path and not raw_limit:
        return provider
    if not raw_path or not raw_limit:
        raise ExperimentBudgetExceeded(
            "both CODEROOK_EXPERIMENT_BUDGET_FILE and CODEROOK_EXPERIMENT_BUDGET_USD are required"
        )
    try:
        limit = float(raw_limit)
    except ValueError as exc:
        raise ExperimentBudgetExceeded("invalid experiment budget limit") from exc
    return ExperimentBudgetProvider(
        provider,
        model=model,
        ledger_path=Path(raw_path),
        limit_usd=limit,
    )
