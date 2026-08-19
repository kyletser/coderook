from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from code_rook.core.runtime.migrations import connect_database
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RuntimeStore
from code_rook.core.session.store import SessionStore


class RuntimeConsistencyIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: Literal["warning", "error"]
    thread_id: str = ""
    turn_id: str = ""
    detail: str
    repairable: bool = False


class RuntimeReconcileReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    checked_at: datetime
    runtime_schema_version: int
    session_count: int = Field(ge=0)
    thread_count: int = Field(ge=0)
    turn_count: int = Field(ge=0)
    issues: list[RuntimeConsistencyIssue] = Field(default_factory=list)
    repaired: list[str] = Field(default_factory=list)

    @property
    # 返回报告是否不存在会阻止可靠恢复的一致性错误
    def healthy(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


class RuntimeReconciler:
    # 保存 runtime 投影、文件账本和 repair journal 的明确边界
    def __init__(
        self,
        runtime: RuntimeStore,
        sessions: SessionStore,
        *,
        workspace: Path,
        journal_path: Path,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._workspace = workspace
        self._journal_path = journal_path

    # 对比 session/run 索引、runtime 投影和事件序号，不修改任何状态
    def inspect(self) -> RuntimeReconcileReport:
        sessions = self._sessions.list_sessions()
        session_by_id = {session.id: session for session in sessions}
        threads = self._runtime.list_threads()
        thread_by_id = {thread.id: thread for thread in threads}
        issues: list[RuntimeConsistencyIssue] = []
        turn_count = 0

        for session in sessions:
            for detail in self._sessions.verify_ledger(session.id):
                issues.append(
                    RuntimeConsistencyIssue(
                        code="transcript_checksum_mismatch",
                        severity="error",
                        thread_id=session.id,
                        detail=detail,
                    )
                )
            thread = thread_by_id.get(session.id)
            if thread is None:
                issues.append(
                    RuntimeConsistencyIssue(
                        code="missing_thread_projection",
                        severity="error",
                        thread_id=session.id,
                        detail="session ledger exists but runtime thread is missing",
                        repairable=True,
                    )
                )
                continue
            runtime_turns = {turn.id for turn in self._runtime.list_turns(session.id)}
            turn_count += len(runtime_turns)
            for run_id in session.run_ids:
                if run_id not in runtime_turns:
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="missing_turn_projection",
                            severity="error",
                            thread_id=session.id,
                            turn_id=run_id,
                            detail="session run index exists but runtime turn is missing",
                            repairable=True,
                        )
                    )

        for thread in threads:
            if thread.id not in session_by_id:
                issues.append(
                    RuntimeConsistencyIssue(
                        code="orphan_runtime_thread",
                        severity="warning",
                        thread_id=thread.id,
                        detail="runtime thread has no compatible session ledger",
                    )
                )
            if thread.id not in session_by_id:
                turn_count += len(self._runtime.list_turns(thread.id))

        issues.extend(self._inspect_event_sequences())
        return RuntimeReconcileReport(
            checked_at=datetime.now(UTC),
            runtime_schema_version=self._runtime.schema_version(),
            session_count=len(sessions),
            thread_count=len(threads),
            turn_count=turn_count,
            issues=issues,
        )

    # 检查每个 thread 的事件序号是否连续且 counter 指向下一个序号
    def _inspect_event_sequences(self) -> list[RuntimeConsistencyIssue]:
        issues: list[RuntimeConsistencyIssue] = []
        with connect_database(self._runtime.path) as connection:
            counters = connection.execute(
                "SELECT thread_id, next_seq FROM runtime_event_counters ORDER BY thread_id"
            ).fetchall()
            for counter in counters:
                thread_id = str(counter["thread_id"])
                rows = connection.execute(
                    "SELECT seq FROM runtime_events WHERE thread_id = ? ORDER BY seq",
                    (thread_id,),
                ).fetchall()
                sequences = [int(row["seq"]) for row in rows]
                expected = list(range(1, len(sequences) + 1))
                if sequences != expected:
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="event_sequence_gap",
                            severity="error",
                            thread_id=thread_id,
                            detail=f"event sequences are {sequences}, expected {expected}",
                        )
                    )
                expected_next = (sequences[-1] + 1) if sequences else 1
                if int(counter["next_seq"]) != expected_next:
                    issues.append(
                        RuntimeConsistencyIssue(
                            code="event_counter_mismatch",
                            severity="error",
                            thread_id=thread_id,
                            detail=(
                                f"next_seq={counter['next_seq']}, expected {expected_next}"
                            ),
                            repairable=True,
                        )
                    )
        return issues

    # 幂等修复缺失投影与错误 counter，修复前后均写入 journal
    async def repair(self) -> RuntimeReconcileReport:
        before = self.inspect()
        repaired: list[str] = []
        if any(
            issue.code in {"missing_thread_projection", "missing_turn_projection"}
            for issue in before.issues
        ):
            service = RuntimeService(self._runtime, workspace=self._workspace)
            sessions = self._sessions.list_sessions()
            ranges = {
                run_id: timestamps
                for session in sessions
                for run_id, timestamps in self._sessions.run_time_ranges(session.id).items()
            }
            await service.bootstrap_sessions(sessions, turn_times=ranges)
            repaired.append("projection_bootstrap")
        counter_threads = {
            issue.thread_id
            for issue in before.issues
            if issue.code == "event_counter_mismatch" and issue.repairable
        }
        if counter_threads:
            await asyncio.to_thread(self._repair_event_counters, counter_threads)
            repaired.append("event_counters")
        after = self.inspect().model_copy(update={"repaired": repaired})
        self._append_journal(before, after)
        return after

    # 将指定 thread 的 counter 原子重置为当前最大事件序号加一
    def _repair_event_counters(self, thread_ids: set[str]) -> None:
        with connect_database(self._runtime.path) as connection:
            for thread_id in sorted(thread_ids):
                row = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM runtime_events "
                    "WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                next_seq = int(row["max_seq"]) + 1
                connection.execute(
                    "UPDATE runtime_event_counters SET next_seq = ? WHERE thread_id = ?",
                    (next_seq, thread_id),
                )

    # 追加不覆盖历史的修复日志，保留修复前后问题与动作
    def _append_journal(
        self,
        before: RuntimeReconcileReport,
        after: RuntimeReconcileReport,
    ) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "ts": datetime.now(UTC).isoformat(),
            "before": before.model_dump(mode="json"),
            "after": after.model_dump(mode="json"),
            "repaired": after.repaired,
        }
        with self._journal_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
