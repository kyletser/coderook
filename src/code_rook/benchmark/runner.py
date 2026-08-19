from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import pathspec

from code_rook.benchmark.loader import LoadedBenchmarkTask
from code_rook.benchmark.models import (
    AgentExecution,
    BenchmarkCategorySummary,
    BenchmarkReport,
    BenchmarkRunConfig,
    BenchmarkSummary,
    BenchmarkTask,
    BenchmarkTaskResult,
    FileChange,
    VerifierResult,
    VerifierSpec,
)

_IGNORED_WORKSPACE_PATHS = (".coderook/**", ".pytest_cache/**", "**/__pycache__/**")
_MAX_CAPTURE_CHARS = 20_000


class BenchmarkExecutor(Protocol):
    # 执行一个任务并返回与具体模型实现无关的运行结果
    def execute(
        self,
        task: BenchmarkTask,
        workspace: Path,
        runs_dir: Path,
    ) -> Awaitable[AgentExecution]: ...


class BenchmarkRunner:
    # 保存执行器和临时目录父路径，便于生产运行与测试共享评分逻辑
    def __init__(
        self,
        executor: BenchmarkExecutor,
        *,
        temp_root: Path | None = None,
        evidence_root: Path | None = None,
    ) -> None:
        self._executor = executor
        self._temp_root = temp_root
        self._evidence_root = evidence_root

    # 顺序运行选定任务并生成稳定排序的汇总报告
    async def run(
        self,
        tasks: list[LoadedBenchmarkTask],
        *,
        repository_commit: str,
        suite: str | None = None,
        run_config: BenchmarkRunConfig | None = None,
    ) -> BenchmarkReport:
        results = [await self.run_task(task) for task in tasks]
        return BenchmarkReport(
            generated_at=datetime.now(UTC).isoformat(),
            repository_commit=repository_commit,
            suite=suite,
            results=results,
            summary=_summarize_results(results),
            run_config=run_config or BenchmarkRunConfig(),
        )

    # 复制 fixture、运行 Agent、审计改动并执行全部 verifier
    async def run_task(self, loaded: LoadedBenchmarkTask) -> BenchmarkTaskResult:
        with tempfile.TemporaryDirectory(
            prefix=f"coderook-benchmark-{loaded.task.id}-",
            dir=self._temp_root,
        ) as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            runs_dir = root / "runs"
            shutil.copytree(loaded.fixture_path, workspace)
            before = _snapshot_workspace(workspace)
            execution = await self._executor.execute(
                loaded.task,
                workspace,
                runs_dir,
            )
            after = _snapshot_workspace(workspace)
            changes = _diff_snapshots(before, after)
            forbidden, unexpected = _audit_changes(loaded.task, changes)
            verifiers = await run_benchmark_verifiers(loaded.task, workspace)
            failure_class = _classify_failure(
                loaded.task,
                execution,
                changes,
                forbidden,
                unexpected,
                verifiers,
            )
            result = BenchmarkTaskResult(
                task_id=loaded.task.id,
                title=loaded.task.title,
                category=loaded.task.category,
                passed=failure_class is None,
                failure_class=failure_class,
                execution=execution,
                changes=changes,
                forbidden_changes=forbidden,
                unexpected_changes=unexpected,
                verifiers=verifiers,
                evidence_path=(
                    f"evidence/{loaded.task.id}"
                    if self._evidence_root is not None
                    else ""
                ),
            )
            self._write_task_evidence(result, runs_dir)
            return result

    # 保存每个任务的结构化 receipt 与原始事件账本，供 CI artifact 离线审计
    def _write_task_evidence(
        self,
        result: BenchmarkTaskResult,
        runs_dir: Path,
    ) -> None:
        if self._evidence_root is None:
            return
        target = self._evidence_root / result.task_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "receipt.json").write_text(
            result.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        event_path = runs_dir / result.execution.run_id / "events.jsonl"
        if event_path.is_file():
            shutil.copy2(event_path, target / "events.jsonl")


# 在临时副本上运行全部 verifier，确认未修复基线不会意外通过
async def verify_benchmark_baseline(
    loaded: LoadedBenchmarkTask,
    *,
    temp_root: Path | None = None,
) -> list[VerifierResult]:
    with tempfile.TemporaryDirectory(
        prefix=f"coderook-benchmark-baseline-{loaded.task.id}-",
        dir=temp_root,
    ) as temp_dir:
        workspace = Path(temp_dir) / "workspace"
        shutil.copytree(loaded.fixture_path, workspace)
        return await run_benchmark_verifiers(loaded.task, workspace)


# 在给定工作区执行任务声明的全部 verifier，供最终评分和首次编辑探针共用
async def run_benchmark_verifiers(
    task: BenchmarkTask,
    workspace: Path,
) -> list[VerifierResult]:
    return [await _run_verifier(spec, workspace) for spec in task.verifiers]


# 计算离散样本的 nearest-rank 百分位，空样本返回未知
def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile + 0.5)))
    return ordered[index]


# 汇总类别成功率、验证率、首改正确率、成本与运行时行为指标
def _summarize_results(results: list[BenchmarkTaskResult]) -> BenchmarkSummary:
    total = len(results)
    passed = sum(result.passed for result in results)
    verifier_total = sum(len(result.verifiers) for result in results)
    verifier_passed = sum(
        verifier.passed for result in results for verifier in result.verifiers
    )
    first_edit_values = [
        result.execution.first_edit_correct
        for result in results
        if result.execution.first_edit_correct is not None
    ]
    category_names = sorted({result.category for result in results})
    categories: dict[str, BenchmarkCategorySummary] = {}
    for category in category_names:
        category_results = [result for result in results if result.category == category]
        category_passed = sum(result.passed for result in category_results)
        categories[category] = BenchmarkCategorySummary(
            total=len(category_results),
            passed=category_passed,
            pass_rate=category_passed / len(category_results),
        )
    costs = [
        result.execution.estimated_cost_usd
        for result in results
        if result.execution.estimated_cost_usd is not None
    ]
    diagnostic_durations = [
        float(duration)
        for result in results
        for duration in result.execution.diagnostic_durations_ms
    ]
    return BenchmarkSummary(
        total=total,
        passed=passed,
        pass_rate=passed / total if total else 0.0,
        verifier_pass_rate=(verifier_passed / verifier_total if verifier_total else 0.0),
        first_edit_correct_rate=(
            sum(first_edit_values) / len(first_edit_values)
            if first_edit_values
            else None
        ),
        first_edit_known=len(first_edit_values),
        non_target_file_changes=sum(
            len(set(result.forbidden_changes + result.unexpected_changes))
            for result in results
        ),
        approval_requests=sum(result.execution.approval_requests for result in results),
        rollbacks=sum(result.execution.rollback_count for result in results),
        retries=sum(result.execution.retry_count for result in results),
        compactions=sum(result.execution.compaction_count for result in results),
        daemon_restarts=sum(
            result.execution.daemon_restart_count for result in results
        ),
        total_tokens=sum(
            result.execution.input_tokens + result.execution.output_tokens
            for result in results
        ),
        cost_p50_usd=_percentile(costs, 0.50),
        cost_p95_usd=_percentile(costs, 0.95),
        diagnostics_p95_ms=_percentile(diagnostic_durations, 0.95),
        categories=categories,
    )


# 计算工作区内受审计文件的 SHA-256 指纹
def _snapshot_workspace(workspace: Path) -> dict[str, str]:
    ignored = pathspec.GitIgnoreSpec.from_lines(_IGNORED_WORKSPACE_PATHS)
    snapshot: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if ignored.match_file(relative):
            continue
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


# 对比两次文件指纹并返回新增、修改和删除列表
def _diff_snapshots(before: dict[str, str], after: dict[str, str]) -> list[FileChange]:
    changes: list[FileChange] = []
    for path in sorted(before.keys() | after.keys()):
        if path not in before:
            kind: Literal["added", "modified", "deleted"] = "added"
        elif path not in after:
            kind = "deleted"
        elif before[path] != after[path]:
            kind = "modified"
        else:
            continue
        changes.append(FileChange(path=path, kind=kind))
    return changes


# 按禁止规则和允许范围审计 Agent 产生的文件改动
def _audit_changes(
    task: BenchmarkTask,
    changes: list[FileChange],
) -> tuple[list[str], list[str]]:
    allowed = pathspec.GitIgnoreSpec.from_lines(task.allowed_change_paths)
    forbidden_spec = pathspec.GitIgnoreSpec.from_lines(task.forbidden_paths)
    forbidden = [change.path for change in changes if forbidden_spec.match_file(change.path)]
    unexpected = [change.path for change in changes if not allowed.match_file(change.path)]
    return forbidden, unexpected


# 在 fixture 根目录内解析 verifier 的工作目录
def _resolve_verifier_cwd(workspace: Path, relative: str) -> Path:
    cwd = (workspace / relative).resolve()
    if not cwd.is_relative_to(workspace.resolve()) or not cwd.is_dir():
        raise ValueError(f"verifier cwd is missing or outside workspace: {relative}")
    return cwd


# 替换 verifier 命令中与机器相关的稳定占位符
def _expand_argv(argv: list[str]) -> list[str]:
    return [sys.executable if part == "{python}" else part for part in argv]


# 运行单个无 shell verifier，限制时间并有界捕获输出
async def _run_verifier(spec: VerifierSpec, workspace: Path) -> VerifierResult:
    argv = _expand_argv(spec.argv)
    started = time.monotonic()
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=_resolve_verifier_cwd(workspace, spec.cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_raw, stderr_raw = await asyncio.wait_for(
                process.communicate(),
                timeout=spec.timeout_s,
            )
            exit_code: int | None = process.returncode
            timed_out = False
        except TimeoutError:
            process.kill()
            stdout_raw, stderr_raw = await process.communicate()
            exit_code = None
            timed_out = True
    except OSError as exc:
        stdout_raw = b""
        stderr_raw = str(exc).encode("utf-8", errors="replace")
        exit_code = None
        timed_out = False
    return VerifierResult(
        name=spec.name,
        argv=argv,
        exit_code=exit_code,
        elapsed_s=time.monotonic() - started,
        stdout=stdout_raw.decode("utf-8", errors="replace")[-_MAX_CAPTURE_CHARS:],
        stderr=stderr_raw.decode("utf-8", errors="replace")[-_MAX_CAPTURE_CHARS:],
        timed_out=timed_out,
    )


# 按安全、预算、Agent 终态和验证结果的优先级归类失败原因
def _classify_failure(
    task: BenchmarkTask,
    execution: AgentExecution,
    changes: list[FileChange],
    forbidden: list[str],
    unexpected: list[str],
    verifiers: list[VerifierResult],
) -> str | None:
    if forbidden:
        return "forbidden_change"
    if unexpected:
        return "unexpected_change"
    if execution.timed_out:
        return "budget_exhausted"
    total_tokens = execution.input_tokens + execution.output_tokens
    if task.budgets.max_total_tokens is not None:
        if total_tokens > task.budgets.max_total_tokens:
            return "token_budget_exceeded"
    if task.budgets.max_cost_usd is not None and execution.estimated_cost_usd is not None:
        if execution.estimated_cost_usd > task.budgets.max_cost_usd:
            return "cost_budget_exceeded"
    if execution.status != "success":
        reason = execution.reason or ""
        if "permission" in reason:
            return "permission_blocked"
        if reason in {"exceeded_max_steps", "benchmark_wall_time_exceeded"}:
            return "budget_exhausted"
        if reason == "llm_error" or reason.startswith("model_error"):
            return "model_error"
        if reason.startswith("runtime_error"):
            return "runtime_error"
        return "understanding_error"
    if any(not verifier.passed for verifier in verifiers):
        if task.category == "explain":
            return "understanding_error"
        if not changes:
            return "retrieval_failure"
        return "incorrect_edit"
    return None
