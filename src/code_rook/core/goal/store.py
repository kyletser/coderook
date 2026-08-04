from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from code_rook.core.goal.models import GoalRecord


class GoalStoreError(ValueError):
    pass


class GoalStore:
    # 初始化用户级 goal 目录和原子写锁
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # 返回指定 goal 的稳定 JSON 路径
    def goal_path(self, goal_id: str) -> Path:
        return self.path / f"{goal_id}.json"

    # 原子保存完整 goal 记录
    def save(self, goal: GoalRecord) -> None:
        target = self.goal_path(goal.id)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        with self._lock:
            temporary.write_text(
                goal.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(target)

    # 读取并严格校验指定 goal
    def get(self, goal_id: str) -> GoalRecord:
        target = self.goal_path(goal_id)
        if not target.exists():
            raise GoalStoreError(f"goal not found: {goal_id}")
        try:
            return GoalRecord.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise GoalStoreError(f"invalid goal {goal_id}: {exc}") from exc

    # 按创建时间和 ID 稳定列出所有 goal
    def list(self) -> list[GoalRecord]:
        records = [self.get(path.stem) for path in self.path.glob("goal-*.json")]
        return sorted(records, key=lambda item: (item.created_at, item.id))

    # 在同一写锁内执行 goal 读改写事务
    def mutate(
        self,
        goal_id: str,
        mutation: Callable[[GoalRecord], GoalRecord],
    ) -> GoalRecord:
        with self._lock:
            goal = self.get(goal_id)
            updated = mutation(goal)
            if not isinstance(updated, GoalRecord):
                raise GoalStoreError("goal mutation returned an invalid record")
            self.save(updated)
            return updated
