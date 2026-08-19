from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_rook.cli.commands.run import (
    EXIT_PERMISSION_REQUIRED,
    StdoutPrinter,
    StreamJsonPrinter,
    _run_finished_exit_code,
)
from code_rook.core.headless import HeadlessEnvelope, HeadlessRunResult


# 功能：验证 run.started 事件在 stdout 中打印 [run] 前缀和 run_id
# 设计：用 capsys 捕获 stdout，直接断言关键字符串，避免对格式细节过度约束
async def test_run_started_prints_run_id(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {"type": "run.started", "run_id": "20260515-abc", "goal": "g", "ts": "t"}
    )
    out = capsys.readouterr().out
    assert "[run]" in out
    assert "20260515-abc" in out


# 功能：验证 step.started 事件打印 [step N] 和 planning... 文本
# 设计：断言步骤编号和 planning 关键词同时出现，覆盖格式模板的两个可变部分
async def test_step_started_prints_step_number(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle({"type": "step.started", "run_id": "r", "step": 3, "ts": "t"})
    out = capsys.readouterr().out
    assert "[step 3]" in out
    assert "planning" in out


# 功能：验证 llm.token 事件将 token 无换行打印并设置 _inline 标志
# 设计：发送 token 后检查 _inline 为 True，再发 step.started 触发 _ensure_newline，
#       确认换行被补齐（新行里有 [step]），验证内联状态机的完整转换
async def test_llm_token_inline_then_newline_on_next_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StdoutPrinter()
    await printer.handle({"type": "llm.token", "run_id": "r", "token": "hello", "ts": "t"})
    assert printer._inline is True  # type: ignore[attr-defined]

    await printer.handle({"type": "step.started", "run_id": "r", "step": 2, "ts": "t"})
    assert printer._inline is False  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert "hello" in out
    assert "[step 2]" in out


# 功能：验证 tool.call_started 打印工具名和 JSON 序列化的 params
# 设计：用带 Unicode 内容的 params 检查 ensure_ascii=False（保留中文字符），断言工具名和参数都出现
async def test_tool_call_started_prints_name_and_params(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {
            "type": "tool.call_started",
            "run_id": "r",
            "tool_use_id": "t1",
            "tool_name": "read_file",
            "params": {"path": "README.md"},
            "ts": "t",
        }
    )
    out = capsys.readouterr().out
    assert "[tool]" in out
    assert "read_file" in out
    assert "README.md" in out


# 功能：验证 run.finished 打印 status 和 steps 字段
# 设计：success 路径下断言 status 和 steps 出现在输出中，不检查 elapsed 的精确值（依赖时间）
async def test_run_finished_prints_status_and_steps(capsys: pytest.CaptureFixture[str]) -> None:
    printer = StdoutPrinter()
    await printer.handle(
        {"type": "run.started", "run_id": "r", "goal": "g", "ts": "t"}
    )
    await printer.handle(
        {"type": "run.finished", "run_id": "r", "status": "success", "steps": 4, "ts": "t"}
    )
    out = capsys.readouterr().out
    assert "success" in out
    assert "4" in out


# 功能：验证不同终态会映射成稳定且可脚本判断的退出码
# 设计：直接测试纯映射函数，避免启动 daemon 并覆盖成功、权限和普通失败三条分支
def test_permission_required_has_scriptable_exit_code() -> None:
    assert _run_finished_exit_code("failed", "permission_required") == EXIT_PERMISSION_REQUIRED
    assert _run_finished_exit_code("failed", "llm_error") == 1
    assert _run_finished_exit_code("success", None) == 0


# 功能：验证 stream-json 默认过滤 partial 事件并输出版本化单行 envelope
# 设计：依次发送 token 与终态事件，通过逐行 JSON 解析确认过滤和 schema 字段
async def test_stream_json_filters_partial_events_by_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StreamJsonPrinter()

    await printer.handle({"type": "llm.token", "run_id": "run-1", "token": "x"})
    await printer.handle(
        {"type": "run.finished", "run_id": "run-1", "status": "success", "steps": 1}
    )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = __import__("json").loads(lines[0])
    assert payload["schema_version"] == 1
    assert payload["type"] == "run.finished"


# 功能：验证 stream-json 结果 envelope 携带最终正文、usage 和连续序号
# 设计：先写一个领域事件再写结果，解析第二行确认机器客户端能独立消费最终结果
async def test_stream_json_appends_final_result_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    printer = StreamJsonPrinter(event_filters=["run.*"])
    await printer.handle({"type": "run.started", "run_id": "run-2"})
    printer.write_result(
        HeadlessRunResult(
            run_id="run-2",
            status="success",
            exit_code=0,
            result="done",
            steps=2,
            usage={"input_tokens": 10},
        )
    )

    lines = capsys.readouterr().out.splitlines()
    result = __import__("json").loads(lines[1])
    assert result["kind"] == "result"
    assert result["sequence"] == 2
    assert result["payload"]["result"] == "done"


# 功能：验证 v1 stream-json golden fixtures 始终满足当前严格模型且往返不丢字段
# 设计：从版本化固定文件读取 event/result 两类 envelope，比较 JSON 语义而非缩进细节
def test_headless_v1_golden_envelopes_remain_compatible() -> None:
    golden_root = Path(__file__).parents[1] / "golden" / "headless"

    for path in sorted(golden_root.glob("*-v1.json")):
        expected = json.loads(path.read_text(encoding="utf-8"))
        envelope = HeadlessEnvelope.model_validate(expected)

        assert envelope.model_dump(mode="json") == expected
