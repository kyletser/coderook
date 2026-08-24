from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from code_rook.core.goal.models import GoalRecord, UnsupportedGoalSchemaError
from code_rook.core.quarantine import quarantine_invalid_file

_GOAL_ID_PATTERN = re.compile(r"^goal-[a-f0-9]{12}$")
logger = logging.getLogger(__name__)


class GoalStoreError(ValueError):
    pass


class GoalStoreUnsupportedVersion(GoalStoreError):
    pass


class GoalStore:
    # 初始化用户级 goal 目录和原子写锁
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().absolute()
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_dir()):
            raise GoalStoreError("goal state root must be a real directory")
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    # 返回指定 goal 的稳定 JSON 路径
    def goal_path(self, goal_id: str) -> Path:
        if _GOAL_ID_PATTERN.fullmatch(goal_id) is None:
            raise GoalStoreError(f"invalid goal id: {goal_id}")
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
        if target.is_symlink() or not target.is_file():
            raise GoalStoreError(f"goal not found: {goal_id}")
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("goal document must be an object")
            record = GoalRecord.from_dict(raw)
            if record.id != goal_id:
                raise ValueError("goal record id does not match its filename")
            return record
        except UnsupportedGoalSchemaError as exc:
            raise GoalStoreUnsupportedVersion(str(exc)) from exc
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise GoalStoreError(f"invalid goal {goal_id}: {exc}") from exc

    # 按创建时间和 ID 稳定列出合法 Goal，并隔离单条损坏记录
    def list_all(self) -> list[GoalRecord]:
        records: list[GoalRecord] = []
        for path in sorted(self.path.glob("goal-*.json")):
            try:
                records.append(self.get(path.stem))
            except GoalStoreUnsupportedVersion:
                logger.warning("skip unsupported future goal record: %s", path)
            except GoalStoreError:
                quarantined = quarantine_invalid_file(
                    path,
                    category="goal",
                    reason="record failed strict GoalRecord validation",
                    state_root=self.path,
                )
                logger.warning(
                    "isolated invalid goal record: %s",
                    quarantined or path,
                )
        return sorted(records, key=lambda item: (item.created_at, item.id))

    # 返回指定 session 的全部 Goal，并保持创建顺序稳定
    def list_for_session(self, session_id: str) -> list[GoalRecord]:
        return [goal for goal in self.list_all() if goal.session_id == session_id]

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
