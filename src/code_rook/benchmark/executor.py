from __future__ import annotations

import asyncio
import copy
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from code_rook.benchmark.models import AgentExecution, BenchmarkTask
from code_rook.benchmark.runner import run_benchmark_verifiers
from code_rook.core.bus.events import (
    ContextCompactedEvent,
    LlmRetryEvent,
    LlmRouteSelectedEvent,
    LlmUsageEvent,
    LspDiagnosticsEvent,
    PermissionRequestedEvent,
    RunFinishedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.pricing import estimate_cost, resolve_pricing_quote
from code_rook.core.llm.route_registry import RouteRegistry, RouteResolutionError
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.strategy import TaskStrategy
from code_rook.core.subagent import BackgroundTaskRegistry, WorkerStatus
from code_rook.core.worktree import WorktreeBatchApplyItem, WorktreeManager


class CodeRookBenchmarkExecutor:
    # 保存基准使用的配置，单个任务执行时再复制以隔离 step 预算
    def __init__(
        self,
        config: CodeRookConfig,
        *,
        temperature: float = 0.0,
        auto_apply_reviewed_workers: bool = False,
        strategy_override: TaskStrategy | None = None,
    ) -> None:
        self._config = config
        self._temperature = temperature
        self._auto_apply_reviewed_workers = auto_apply_reviewed_workers
        self._strategy_override = strategy_override

    # 在隔离工作区内运行当前 CodeRook Agent，并收集评分需要的最小事件指标
    async def execute(
        self,
        task: BenchmarkTask,
        workspace: Path,
        runs_dir: Path,
    ) -> AgentExecution:
        config = copy.deepcopy(self._config)
        config.agent.max_steps = task.budgets.max_steps
        config.agent.max_step_continues = 0

        run_id = f"benchmark-{task.id}-{uuid.uuid4().hex[:10]}"
        bus = EventBus()
        usage_events: list[LlmUsageEvent] = []
        steps = 0
        tool_calls = 0
        approval_requests = 0
        rollback_count = 0
        retry_count = 0
        compaction_count = 0
        first_edit_correct: bool | None = None
        mutation_calls: set[str] = set()
        route_id = ""
        model = ""
        wire_format = ""
        temperature: float | None = None
        diagnostic_durations_ms: list[int] = []
        process_usage: list[dict[str, object]] = []

        # 收集 token、step 和工具调用数量，不保存可能含敏感内容的事件正文
        async def capture(event: BaseModel) -> None:
            nonlocal approval_requests, compaction_count, first_edit_correct
            nonlocal retry_count, rollback_count, steps, tool_calls
            nonlocal model, route_id, wire_format
            nonlocal temperature
            raw_process_usage = getattr(event, "process_usage", None)
            if isinstance(raw_process_usage, dict) and raw_process_usage:
                process_usage.append(dict(raw_process_usage))
            if isinstance(event, LlmUsageEvent):
                usage_events.append(event)
            elif isinstance(event, RunFinishedEvent):
                steps = event.steps
            elif isinstance(event, ToolCallStartedEvent):
                tool_calls += 1
                if self._is_mutating_call(event.tool_name, event.params):
                    mutation_calls.add(event.tool_use_id)
                if self._is_rollback_call(event.tool_name, event.params):
                    rollback_count += 1
            elif isinstance(event, ToolCallFinishedEvent):
                if event.tool_use_id in mutation_calls and first_edit_correct is None:
                    first_results = await run_benchmark_verifiers(task, workspace)
                    first_edit_correct = bool(first_results) and all(
                        verifier.passed for verifier in first_results
                    )
            elif isinstance(event, PermissionRequestedEvent):
                approval_requests += 1
            elif isinstance(event, LlmRetryEvent):
                retry_count += 1
            elif isinstance(event, ContextCompactedEvent):
                compaction_count += 1
            elif isinstance(event, LspDiagnosticsEvent):
                diagnostic_durations_ms.append(event.duration_ms)
            elif isinstance(event, LlmRouteSelectedEvent):
                route_id = event.route_id
                model = event.model
                wire_format = event.wire_format
                temperature = event.temperature

        bus.subscribe(capture)
        permission_manager = PermissionManager(timeout_s=0)
        permission_manager.set_session_mode(
            "",
            "allow_list",
            allow_tools=task.allowed_tools,
        )
        route_registry = RouteRegistry(
            config.llm,
            temperature_override=self._temperature,
        )
        worker_registry = BackgroundTaskRegistry()
        runner = AgentRunner(
            config,
            bus=bus,
            runs_dir=runs_dir,
            permission_manager=permission_manager,
            workspace_root=workspace,
            route_registry=route_registry,
            subagent_registry=worker_registry,
        )

        started = time.monotonic()
        try:
            resolved_route = route_registry.resolve()
            outcome = await asyncio.wait_for(
                runner.run_and_capture(
                    task.goal,
                    run_id=run_id,
                    tool_whitelist=task.allowed_tools,
                    resolved_route=resolved_route,
                    resolved_route_is_explicit=True,
                    strategy_override=self._strategy_override,
                ),
                timeout=task.budgets.wall_time_s,
            )
            status = outcome.status
            result = outcome.result
            reason = outcome.reason
            timed_out = False
        except TimeoutError:
            status = "failed"
            result = ""
            reason = "benchmark_wall_time_exceeded"
            timed_out = True
        except (RouteResolutionError, SystemExit) as exc:
            status = "failed"
            result = ""
            reason = f"runtime_error:{type(exc).__name__}"
            timed_out = False
        except Exception as exc:
            status = "failed"
            result = ""
            reason = f"runtime_error:{type(exc).__name__}"
            timed_out = False

        worker_apply_count = 0
        worker_conflicts = 0
        unreviewed_workspace_writes = 0
        if self._auto_apply_reviewed_workers:
            try:
                live_tasks = [live_task for live_task, _context in worker_registry.all()]
                if live_tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*live_tasks, return_exceptions=True),
                        timeout=min(60.0, task.budgets.wall_time_s),
                    )
                completed = [
                    worker
                    for worker in worker_registry.list_records()
                    if worker.status == WorkerStatus.COMPLETED
                    and worker.handoff_status == "pending_review"
                    and worker.verification_status == "verified"
                ]
                manager = WorktreeManager(workspace)
                if worker_registry.list_records():
                    unreviewed_workspace_writes = len(
                        [
                            path
                            for path in await manager.workspace_changes()
                            if path != ".coderook" and not path.startswith(".coderook/")
                        ]
                    )
                batch_items: list[WorktreeBatchApplyItem] = []
                for worker in completed:
                    preview = await manager.preview_apply(
                        worker.worktree,
                        base_commit=worker.base_commit,
                    )
                    worker_registry.review_handoff(
                        worker.id,
                        approved=True,
                        review_digest=preview.state_digest,
                        changed_files=list(preview.changed_files),
                        diff_truncated=preview.diff_truncated,
                        diff_preview=preview.diff,
                    )
                    batch_items.append(
                        WorktreeBatchApplyItem(
                            name=worker.worktree,
                            base_commit=worker.base_commit,
                            expected_digest=preview.state_digest,
                            reviewed_files=preview.changed_files,
                        )
                    )
                if batch_items:
                    applied = await manager.apply_many(tuple(batch_items))
                    digests = dict(applied.item_digests)
                    for worker in completed:
                        worker_registry.mark_handoff_applied(
                            worker.id,
                            state_digest=digests[worker.worktree],
                            changed_files=list(
                                next(
                                    item.reviewed_files
                                    for item in batch_items
                                    if item.name == worker.worktree
                                )
                            ),
                        )
                    worker_apply_count = len(batch_items)
            except (TimeoutError, ValueError, RuntimeError):
                worker_conflicts += 1

        input_tokens = sum(event.input_tokens for event in usage_events)
        output_tokens = sum(event.output_tokens for event in usage_events)
        cache_read_tokens = sum(event.cache_read_input_tokens for event in usage_events)
        cache_write_tokens = sum(event.cache_creation_input_tokens for event in usage_events)
        estimated_cost, pricing_evidence = self._estimate_usage_cost(usage_events)
        process_wall_ms = sum(
            self._nonnegative_int(record.get("wall_time_ms")) for record in process_usage
        )
        process_cpu_ms = sum(
            self._nonnegative_int(record.get("user_cpu_ms"))
            + self._nonnegative_int(record.get("system_cpu_ms"))
            for record in process_usage
        )
        return AgentExecution(
            run_id=run_id,
            status=status,
            result=result,
            reason=reason,
            route_id=route_id,
            model=model,
            wire_format=wire_format,
            temperature=temperature,
            elapsed_s=time.monotonic() - started,
            steps=steps,
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            estimated_cost_usd=estimated_cost,
            pricing_evidence=pricing_evidence,
            approval_requests=approval_requests,
            rollback_count=rollback_count,
            retry_count=retry_count,
            compaction_count=compaction_count,
            daemon_restart_count=0,
            diagnostic_durations_ms=diagnostic_durations_ms,
            process_usage_records=len(process_usage),
            complete_process_records=sum(
                record.get("complete") is True for record in process_usage
            ),
            process_wall_ms=process_wall_ms,
            process_cpu_ms=process_cpu_ms,
            peak_memory_bytes=max(
                (
                    self._nonnegative_int(record.get("peak_memory_bytes"))
                    for record in process_usage
                ),
                default=0,
            ),
            process_count=sum(
                self._nonnegative_int(record.get("process_count")) for record in process_usage
            ),
            first_edit_correct=first_edit_correct,
            timed_out=timed_out,
            worker_count=len(worker_registry.list_records()),
            worker_conflicts=worker_conflicts,
            worker_apply_count=worker_apply_count,
            unreviewed_workspace_writes=unreviewed_workspace_writes,
        )

    # 将事件中的任意 JSON 数值安全收敛为非负整数
    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    # 判断工具调用是否可能修改 benchmark 工作区，用于首次编辑正确率探针
    @staticmethod
    def _is_mutating_call(tool_name: str, params: dict[str, object]) -> bool:
        if tool_name in {"write_file", "edit_file", "apply_patch"}:
            return True
        return tool_name == "File" and params.get("action") in {
            "write",
            "edit",
            "apply_patch",
        }

    # 判断调用是否执行 checkpoint rewind，计入显式回滚次数
    @staticmethod
    def _is_rollback_call(tool_name: str, params: dict[str, object]) -> bool:
        return tool_name == "checkpoint_rewind" or (
            tool_name == "File" and params.get("action") == "rewind"
        )

    # 汇总已知模型单价及来源；任一事件缺少单价时成本返回未知
    @staticmethod
    def _estimate_usage_cost(
        events: list[LlmUsageEvent],
    ) -> tuple[float | None, list[str]]:
        if not events:
            return 0.0, []
        total = 0.0
        evidence: set[str] = set()
        for event in events:
            quote = resolve_pricing_quote(event.model)
            if quote is None:
                evidence.add(f"unknown:{event.model or 'unspecified'}")
                return None, sorted(evidence)
            evidence.add(f"{quote.source}@{quote.effective_date}")
            total += estimate_cost(
                quote.pricing,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_input_tokens,
                cache_write_tokens=event.cache_creation_input_tokens,
            )
        return total, sorted(evidence)
