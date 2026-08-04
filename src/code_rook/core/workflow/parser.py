from __future__ import annotations

import json
import tomllib
from itertools import combinations
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from code_rook.core.workflow.models import (
    BranchStep,
    FanInStep,
    ParallelStep,
    RetryStep,
    ReviewGateStep,
    SequenceStep,
    WorkerStep,
    WorkflowSpec,
    WorkflowStep,
)


class WorkflowParseError(ValueError):
    pass


# 把 reduce 别名递归规范化为 V1 fan_in，不执行配置中的任何代码
def _normalize_aliases(value: object) -> object:
    if isinstance(value, list):
        return [_normalize_aliases(item) for item in value]
    if not isinstance(value, dict):
        return value
    normalized = {str(key): _normalize_aliases(item) for key, item in value.items()}
    if normalized.get("type") == "reduce":
        normalized["type"] = "fan_in"
    return normalized


# 深度优先遍历全部 IR 节点并携带是否处于 review gate 内的上下文
def _walk(
    step: WorkflowStep,
    *,
    depth: int = 1,
    under_review_gate: bool = False,
) -> list[tuple[WorkflowStep, int, bool]]:
    rows = [(step, depth, under_review_gate)]
    if isinstance(step, (SequenceStep, ParallelStep, FanInStep)):
        for child in step.steps:
            rows.extend(
                _walk(
                    child,
                    depth=depth + 1,
                    under_review_gate=under_review_gate,
                )
            )
    elif isinstance(step, BranchStep):
        rows.extend(
            _walk(
                step.then_step,
                depth=depth + 1,
                under_review_gate=under_review_gate,
            )
        )
        rows.extend(
            _walk(
                step.else_step,
                depth=depth + 1,
                under_review_gate=under_review_gate,
            )
        )
    elif isinstance(step, RetryStep):
        rows.extend(
            _walk(
                step.step,
                depth=depth + 1,
                under_review_gate=under_review_gate,
            )
        )
    elif isinstance(step, ReviewGateStep):
        rows.extend(
            _walk(step.step, depth=depth + 1, under_review_gate=True)
        )
        rows.extend(
            _walk(step.reviewer, depth=depth + 1, under_review_gate=False)
        )
    return rows


# 将 claim 路径转换为跨平台稳定的声明式相对路径
def _claim_path(value: str) -> PurePosixPath:
    return PurePosixPath(value.replace("\\", "/"))


# 判断一个声明路径是否等于或位于另一个声明目录下
def _claim_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


# 判断两个 WorkerStep 的写声明是否会在同一 parallel 域相交
def _claims_overlap(left: WorkerStep, right: WorkerStep) -> bool:
    a = left.write_claim
    b = right.write_claim
    if a.read_only or b.read_only:
        return False
    if (
        a.coordination_contract.strip()
        and a.coordination_contract.strip() == b.coordination_contract.strip()
    ):
        return False
    a_files = {_claim_path(item) for item in a.exact_files}
    b_files = {_claim_path(item) for item in b.exact_files}
    a_roots = [_claim_path(item) for item in a.write_roots]
    b_roots = [_claim_path(item) for item in b.write_roots]
    if a_files & b_files:
        return True
    if any(_claim_within(path, root) for path in a_files for root in b_roots):
        return True
    if any(_claim_within(path, root) for path in b_files for root in a_roots):
        return True
    return any(
        _claim_within(left_root, right_root)
        or _claim_within(right_root, left_root)
        for left_root in a_roots
        for right_root in b_roots
    )


# 校验节点唯一性、全局边界、parallel 并发和高风险 review gate
def validate_workflow(spec: WorkflowSpec) -> WorkflowSpec:
    rows = _walk(spec.root)
    if len(rows) > spec.limits.max_nodes:
        raise WorkflowParseError(
            f"workflow node limit exceeded: {len(rows)} > {spec.limits.max_nodes}"
        )
    deepest = max(depth for _step, depth, _gate in rows)
    if deepest > spec.limits.max_depth:
        raise WorkflowParseError(
            f"workflow depth limit exceeded: {deepest} > {spec.limits.max_depth}"
        )
    ids = [step.id for step, _depth, _gate in rows]
    if len(ids) != len(set(ids)):
        raise WorkflowParseError("workflow node ids must be unique")
    for step, _depth, under_gate in rows:
        if (
            isinstance(step, ParallelStep)
            and step.max_concurrency is not None
            and step.max_concurrency > spec.limits.max_concurrency
        ):
            raise WorkflowParseError(
                f"parallel concurrency exceeds workflow limit: {step.id}"
            )
        if isinstance(step, ParallelStep):
            workers = [
                child
                for child, _child_depth, _child_gate in _walk(step)
                if isinstance(child, WorkerStep)
            ]
            for left, right in combinations(workers, 2):
                if _claims_overlap(left, right):
                    raise WorkflowParseError(
                        f"parallel write claims overlap: {left.id} and {right.id}"
                    )
        if isinstance(step, WorkerStep) and step.high_risk_write and not under_gate:
            raise WorkflowParseError(
                f"high-risk worker requires reviewer gate: {step.id}"
            )
        if isinstance(step, BranchStep) and step.condition.source not in set(ids):
            raise WorkflowParseError(
                f"branch condition source is unknown: {step.condition.source}"
            )
    return spec


# 从 JSON 或 TOML 声明文本解析并完整校验 WorkflowSpec
def parse_workflow_text(text: str, *, format: str) -> WorkflowSpec:
    try:
        if format == "json":
            raw = json.loads(text)
        elif format == "toml":
            raw = tomllib.loads(text)
        else:
            raise WorkflowParseError(f"unsupported workflow format: {format}")
        normalized = _normalize_aliases(raw)
        return validate_workflow(WorkflowSpec.model_validate(normalized))
    except WorkflowParseError:
        raise
    except (json.JSONDecodeError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise WorkflowParseError(f"invalid workflow: {exc}") from exc


# 根据文件扩展名读取 UTF-8 声明式 workflow 配置
def load_workflow(path: Path) -> WorkflowSpec:
    suffix = path.suffix.lower()
    format_name = {".json": "json", ".toml": "toml"}.get(suffix)
    if format_name is None:
        raise WorkflowParseError("workflow file must use .json or .toml")
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorkflowParseError(f"cannot read workflow: {exc}") from exc
    return parse_workflow_text(content, format=format_name)
