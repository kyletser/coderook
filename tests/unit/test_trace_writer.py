import asyncio
import json
from pathlib import Path

import pytest

from code_rook.core.trace.record import TraceRecord
from code_rook.core.trace.redaction import REDACTED, redact_trace_data
from code_rook.core.trace.writer import TraceWriter


def _record(direction: str = "CORE", kind: str = "event") -> TraceRecord:
    return TraceRecord(
        ts="2026-01-01T00:00:00.000Z",
        direction=direction,  # type: ignore[arg-type]
        layer="event",
        kind=kind,
        data={"type": "run.started", "run_id": "r1"},
    )


# 功能：验证 emit 后 stop 能将 record 写入文件
# 设计：用临时目录避免污染；await stop() 保证 drain 完成后再读文件
@pytest.mark.asyncio
async def test_emit_writes_record_to_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    writer.emit(_record())
    await writer.stop()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["direction"] == "CORE"
    assert parsed["kind"] == "event"


# 功能：验证多条 record 按 emit 顺序写入文件
# 设计：emit 三条方向各异的 record，断言顺序与方向均保持一致
@pytest.mark.asyncio
async def test_emit_multiple_records_in_order(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    writer.emit(_record("CLIENT→CORE", "command"))
    writer.emit(_record("CORE", "event"))
    writer.emit(_record("LLM→CORE", "api_response"))
    await writer.stop()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["direction"] == "CLIENT→CORE"
    assert json.loads(lines[1])["direction"] == "CORE"
    assert json.loads(lines[2])["direction"] == "LLM→CORE"


# 功能：验证 emit 是同步非阻塞的（不需要 await）
# 设计：在 start() 之前调用 emit 会放入队列而不抛异常，start 后正常 drain
@pytest.mark.asyncio
async def test_emit_is_nonblocking(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()

    # emit 是同步调用，不应阻塞事件循环
    for _ in range(10):
        writer.emit(_record())
    await writer.stop()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 10


# 功能：验证 TraceWriter 自动创建不存在的父目录
# 设计：指定一个深层嵌套路径，start() 后 emit 能正常写入
@pytest.mark.asyncio
async def test_start_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c" / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()
    writer.emit(_record())
    await writer.stop()

    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


# 功能：验证 stop 后再次 start 可以追加写入（文件已存在时）
# 设计：两次 start/stop 循环，断言文件行数累加而非覆盖
@pytest.mark.asyncio
async def test_append_mode_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"

    writer = TraceWriter(path)
    await writer.start()
    writer.emit(_record())
    await writer.stop()

    writer2 = TraceWriter(path)
    await writer2.start()
    writer2.emit(_record())
    await writer2.stop()

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_recursive_redaction_covers_keys_and_embedded_secrets() -> None:
    data = {
        "authorization": "Bearer abcdefghijklmnop",
        "nested": {
            "api_key": "sk-exampleSecret123",
            "OPENAI_API_KEY": "sk-environmentSecret",
            "x-api-key": "header-secret",
            "token": "opaque-token",
            "message": "use token=secret-value and sk-anotherSecret456",
            "input_tokens": 42,
        },
        "items": [{"password": "hunter2"}],
    }

    redacted = redact_trace_data(data)

    assert redacted["authorization"] == REDACTED
    assert redacted["nested"]["api_key"] == REDACTED
    assert redacted["nested"]["OPENAI_API_KEY"] == REDACTED
    assert redacted["nested"]["x-api-key"] == REDACTED
    assert redacted["nested"]["token"] == REDACTED
    assert "secret-value" not in redacted["nested"]["message"]
    assert "sk-anotherSecret456" not in redacted["nested"]["message"]
    assert redacted["nested"]["input_tokens"] == 42
    assert redacted["items"][0]["password"] == REDACTED


@pytest.mark.asyncio
async def test_writer_redacts_all_trace_layers_before_disk(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, include_payload=True)
    await writer.start()
    writer.emit(
        TraceRecord(
            ts="t",
            direction="CLIENT→CORE",
            layer="ipc",
            kind="command",
            data={
                "params": {
                    "api_key": "sk-do-not-store-this",
                    "goal": "Authorization: Bearer abcdefghijklmnop",
                }
            },
        )
    )
    await writer.stop()

    raw = path.read_text(encoding="utf-8")
    assert "sk-do-not-store-this" not in raw
    assert "abcdefghijklmnop" not in raw
    assert raw.count(REDACTED) >= 2


@pytest.mark.asyncio
async def test_default_metadata_mode_omits_ipc_and_event_payloads(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    await writer.start()
    writer.emit(
        TraceRecord(
            ts="t",
            direction="CLIENT→CORE",
            layer="ipc",
            kind="command",
            data={
                "method": "agent.run",
                "id": "1",
                "params": {"goal": "private prompt", "api_key": "sk-private-value"},
            },
        )
    )
    writer.emit(
        TraceRecord(
            ts="t",
            direction="CORE",
            layer="event",
            kind="event",
            data={
                "type": "tool.call_finished",
                "run_id": "run-1",
                "tool_name": "read_file",
                "output": "private source code",
                "elapsed_ms": 5,
            },
        )
    )
    await writer.stop()

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["data"] == {
        "method": "agent.run",
        "id": "1",
        "param_keys": ["api_key", "goal"],
    }
    assert rows[1]["data"] == {
        "type": "tool.call_finished",
        "run_id": "run-1",
        "tool_name": "read_file",
        "elapsed_ms": 5,
    }
    raw = path.read_text(encoding="utf-8")
    assert "private prompt" not in raw
    assert "private source code" not in raw
    assert "sk-private-value" not in raw


@pytest.mark.asyncio
async def test_rotation_enforces_size_and_backup_retention(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, max_bytes=280, backup_count=2, include_payload=True)
    await writer.start()
    for index in range(8):
        writer.emit(
            TraceRecord(
                ts="t",
                direction="CORE",
                layer="event",
                kind="event",
                data={"index": index, "payload": "x" * 100},
            )
        )
    await writer.stop()

    assert path.exists()
    assert path.with_name("trace.jsonl.1").exists()
    assert path.with_name("trace.jsonl.2").exists()
    assert not path.with_name("trace.jsonl.3").exists()
    retained = "".join(
        candidate.read_text(encoding="utf-8")
        for candidate in (path.with_name("trace.jsonl.2"), path.with_name("trace.jsonl.1"), path)
    )
    assert '"index":7' in retained


@pytest.mark.asyncio
async def test_restart_rotates_existing_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    path.write_text("old\n" * 100, encoding="utf-8")
    writer = TraceWriter(path, max_bytes=100, backup_count=1)
    await writer.start()
    writer.emit(_record())
    await writer.stop()

    assert path.with_name("trace.jsonl.1").exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_rotation_failure_propagates_without_stop_deadlock(tmp_path: Path) -> None:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path, max_bytes=1, include_payload=True)

    def fail_rotation() -> None:
        raise OSError("disk failure")

    writer._rotate = fail_rotation  # type: ignore[method-assign]
    await writer.start()
    writer.emit(_record())
    writer.emit(_record())

    with pytest.raises(OSError, match="disk failure"):
        await asyncio.wait_for(writer.stop(), timeout=1)
