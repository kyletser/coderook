#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from textual.widgets import Static

from code_rook.core.authority import RuntimeMode, WorkspaceTrust
from code_rook.tui.app import CodeRookTuiApp
from code_rook.tui.widgets.input import ChatTextArea


class DemoTuiApp(CodeRookTuiApp):
    # 挂载真实 TUI 控件并注入确定性事件，避免截图依赖 daemon、模型或个人状态
    def on_mount(self) -> None:
        self._slash_items = self._build_slash_items()
        self._session_id = "demo-auth-review"
        self._route = "openai-compatible"
        self._model = "coding-model"
        self._authority_preset = "accept_edits"
        self._input_runtime_mode = RuntimeMode.ACT
        self._workspace_trust = WorkspaceTrust.TRUSTED
        self._last_context_pct = 0.37
        self._cost_total = 0.0187
        self._sandbox = {
            "available": True,
            "kind": "linux_bwrap",
            "reason": "bubblewrap executable is available",
        }
        prompt = self.query_one("#prompt", ChatTextArea)
        prompt.disabled = False
        prompt.border_title = "消息"
        self._append(
            Static(
                "[dim]deterministic product demo · rendered by the real Textual UI[/dim]",
                classes="log-line",
            )
        )
        self._append(
            Static(
                "审查身份认证模块，修复 token 比较中的时序泄漏并运行相关测试。",
                classes="user-turn",
            )
        )
        self._inject_demo_events()
        self._update_header("ready")
        prompt.focus()

    # 用正式事件契约展示计划、仓库上下文、工具时间线和验证闭环
    def _inject_demo_events(self) -> None:
        run_id = "demo-run"
        events: list[dict[str, object]] = [
            {
                "type": "plan.updated",
                "explanation": "先定位认证边界，再做最小修改并验证。",
                "plan": [
                    {"status": "completed", "step": "建立 repository map 与 working set"},
                    {"status": "completed", "step": "修复恒定时间 token 比较"},
                    {"status": "completed", "step": "运行认证单测与静态检查"},
                ],
            },
            {
                "type": "context.repository",
                "run_id": run_id,
                "used_chars": 6240,
                "budget_chars": 12000,
                "cache_hits": 41,
                "parsed_files": 3,
                "paths": [
                    "src/code_rook/core/transport/socket_server.py",
                    "src/code_rook/core/transport/auth.py",
                    "tests/unit/test_socket_server.py",
                ],
            },
            {
                "type": "context.working_set",
                "run_id": run_id,
                "step": 1,
                "paths": [
                    "src/code_rook/core/transport/socket_server.py",
                    "tests/unit/test_socket_server.py",
                ],
            },
            {"type": "run.started", "run_id": run_id},
            {"type": "step.started", "run_id": run_id, "step": 1},
            {
                "type": "tool.call_started",
                "run_id": run_id,
                "tool_use_id": "read-auth",
                "tool_name": "read_file",
                "params": {"path": "src/code_rook/core/transport/socket_server.py"},
            },
            {
                "type": "tool.call_finished",
                "run_id": run_id,
                "tool_use_id": "read-auth",
                "elapsed_ms": 18,
                "output": "authentication handler loaded",
            },
            {
                "type": "tool.call_started",
                "run_id": run_id,
                "tool_use_id": "edit-auth",
                "tool_name": "edit_file",
                "params": {
                    "path": "src/code_rook/core/transport/socket_server.py",
                    "old": "token == expected",
                    "new": "hmac.compare_digest(token, expected)",
                },
            },
            {
                "type": "tool.call_finished",
                "run_id": run_id,
                "tool_use_id": "edit-auth",
                "elapsed_ms": 31,
                "output": "updated 1 occurrence",
            },
            {
                "type": "tool.call_started",
                "run_id": run_id,
                "tool_use_id": "test-auth",
                "tool_name": "bash",
                "params": {"command": "pytest tests/unit/test_socket_server.py -q"},
            },
            {
                "type": "tool.call_finished",
                "run_id": run_id,
                "tool_use_id": "test-auth",
                "elapsed_ms": 842,
                "output": "12 passed",
            },
            {
                "type": "verification.completed",
                "run_id": run_id,
                "step": 1,
                "action": "run_tests",
                "passed": 2,
                "failed": 0,
                "paths": [
                    "src/code_rook/core/transport/socket_server.py",
                    "tests/unit/test_socket_server.py",
                ],
                "gates": [
                    {"name": "pytest", "status": "passed"},
                    {"name": "ruff", "status": "passed"},
                ],
            },
            {
                "type": "llm.token",
                "run_id": run_id,
                "token": (
                    "已改用恒定时间比较，并补充了错误 token 的回归覆盖；"
                    "认证单测与 Ruff 均通过。"
                ),
            },
            {
                "type": "agent.decision",
                "run_id": run_id,
                "intent": "respond",
                "has_visible_text": True,
            },
            {"type": "run.finished", "run_id": run_id, "status": "success", "steps": 1},
            {"type": "session.waiting_for_input", "session_id": self._session_id},
        ]
        for event in events:
            self._handle_event(event)


# 运行无网络的 Textual test driver 并导出确定性 SVG 产品截图
async def capture_tui_demo(output: Path) -> None:
    app = DemoTuiApp(
        "127.0.0.1",
        7437,
        provider="openai-compatible",
        model="coding-model",
        route="openai-compatible",
    )
    async with app.run_test(size=(118, 34)) as pilot:
        await pilot.pause()
        await pilot.pause()
        app.action_scroll_log_end()
        await pilot.pause()
        svg = app.export_screenshot(title="CodeRook TUI · verified coding task")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(svg, encoding="utf-8", newline="\n")


# 解析输出路径并生成 README 使用的实际 TUI 截图
def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the deterministic CodeRook TUI demo.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/images/coderook-tui.svg"),
    )
    args = parser.parse_args()
    asyncio.run(capture_tui_demo(args.output.resolve()))
    print(f"TUI demo written: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
