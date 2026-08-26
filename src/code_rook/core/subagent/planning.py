from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, model_validator

from code_rook.core.subagent.models import WriteClaim


class DelegationTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    role: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=1, max_length=8_000)
    dependencies: tuple[str, ...] = ()
    write_claim: WriteClaim
    acceptance: tuple[str, ...] = Field(min_length=1)
    token_budget: int = Field(ge=256)
    wall_time_s: int = Field(default=900, ge=1, le=3_600)


class DelegationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tasks: tuple[DelegationTask, ...] = Field(min_length=1, max_length=3)
    total_token_budget: int = Field(ge=256)
    max_workers: int = Field(default=3, ge=1, le=3)
    allow_nested_delegation: bool = False

    @model_validator(mode="after")
    # 校验任务 DAG、总预算、非嵌套约束和所有写入声明互不重叠
    def validate_plan(self) -> DelegationPlan:
        if self.allow_nested_delegation:
            raise ValueError("nested delegation is not allowed")
        if len(self.tasks) > self.max_workers:
            raise ValueError("delegation task count exceeds max_workers")
        ids = [task.id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("delegation task ids must be unique")
        known = set(ids)
        for task in self.tasks:
            unknown = set(task.dependencies) - known
            if unknown:
                raise ValueError(f"task {task.id} has unknown dependencies: {sorted(unknown)}")
            if task.id in task.dependencies:
                raise ValueError(f"task {task.id} cannot depend on itself")
            _validate_claim_paths(task.write_claim)
        if sum(task.token_budget for task in self.tasks) > self.total_token_budget:
            raise ValueError("worker token budgets exceed total_token_budget")
        _validate_acyclic(self.tasks)
        for index, left in enumerate(self.tasks):
            if left.write_claim.read_only:
                continue
            for right in self.tasks[index + 1 :]:
                if right.write_claim.read_only:
                    continue
                overlap = _claim_overlap(left.write_claim, right.write_claim)
                if overlap:
                    raise ValueError(f"write claim conflict: {left.id} and {right.id}: {overlap}")
        return self

    # 按依赖拓扑返回可并行启动的稳定任务波次
    def execution_waves(self) -> tuple[tuple[str, ...], ...]:
        remaining = {task.id: set(task.dependencies) for task in self.tasks}
        waves: list[tuple[str, ...]] = []
        completed: set[str] = set()
        while remaining:
            ready = tuple(
                sorted(
                    task_id
                    for task_id, dependencies in remaining.items()
                    if dependencies <= completed
                )
            )
            if not ready:
                raise ValueError("delegation plan contains a dependency cycle")
            waves.append(ready)
            completed.update(ready)
            for task_id in ready:
                remaining.pop(task_id)
        return tuple(waves)


# 将声明路径转换为禁止绝对路径和父级逃逸的稳定工作区相对路径
def _normalize_claim_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip().strip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid write claim path: {raw}")
    return str(path).casefold()


# 验证所有精确文件和目录根都位于工作区相对命名空间
def _validate_claim_paths(claim: WriteClaim) -> None:
    for raw in (*claim.exact_files, *claim.write_roots):
        _normalize_claim_path(raw)


# 判断两个写入声明是否存在精确文件或父子目录交集
def _claim_overlap(left: WriteClaim, right: WriteClaim) -> str:
    left_files = {_normalize_claim_path(value) for value in left.exact_files}
    right_files = {_normalize_claim_path(value) for value in right.exact_files}
    exact = sorted(left_files & right_files)
    if exact:
        return exact[0]
    left_roots = {_normalize_claim_path(value) for value in left.write_roots}
    right_roots = {_normalize_claim_path(value) for value in right.write_roots}
    for left_root in sorted(left_roots):
        for right_root in sorted(right_roots):
            if (
                left_root == right_root
                or left_root.startswith(right_root + "/")
                or right_root.startswith(left_root + "/")
            ):
                return f"{left_root} <-> {right_root}"
    for file_path in sorted(left_files):
        for root in sorted(right_roots):
            if file_path == root or file_path.startswith(root + "/"):
                return f"{file_path} <-> {root}"
    for file_path in sorted(right_files):
        for root in sorted(left_roots):
            if file_path == root or file_path.startswith(root + "/"):
                return f"{root} <-> {file_path}"
    return ""


# 使用深度优先遍历拒绝所有直接或间接任务依赖环
def _validate_acyclic(tasks: tuple[DelegationTask, ...]) -> None:
    graph = {task.id: set(task.dependencies) for task in tasks}
    visiting: set[str] = set()
    visited: set[str] = set()

    # 递归检查单个任务的祖先链并定位依赖环
    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ValueError("delegation plan contains a dependency cycle")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in graph[task_id]:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in graph:
        visit(task_id)
