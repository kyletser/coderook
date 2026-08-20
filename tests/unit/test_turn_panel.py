from code_rook.tui.panels import render_turn_inspector


# 功能：验证 Turn Inspector 同屏展示 durable route、权限、用量、工具、审批和验证证据
# 设计：使用包含单个工具与诊断的完整 payload，断言主要字段且确认工具参数保持紧凑单行
def test_turn_inspector_renders_durable_facts() -> None:
    rendered = render_turn_inspector(
        {
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "route": {
                    "route_id": "openai-main",
                    "model": "gpt-test",
                    "wire_format": "openai_responses",
                },
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
            "items": [
                {
                    "kind": "message",
                    "payload": {"role": "user", "content": "修复跨文件缓存缺陷"},
                },
                {
                    "kind": "tool_call",
                    "payload": {
                        "tool_name": "read_file",
                        "params": {"path": "README.md"},
                    },
                }
            ],
            "events": [
                {
                    "type": "verification.completed",
                    "payload": {"action": "run_tests", "passed": 2, "failed": 0},
                }
            ],
            "receipt": {
                "authority": {
                    "mode": "act",
                    "profile": "ask",
                    "workspace_trust": "trusted",
                    "sandbox": {"kind": "windows_none"},
                    "sandbox_plan": {"backend": "degraded"},
                },
                "cost": "unknown",
                "tool_call_count": 1,
                "approvals": {"requested": 1, "granted": 1, "denied": 0},
                "files_changed": [],
                "checkpoints": [],
                "artifacts": [],
                "workers": [{"worker_id": "w1", "status": "running"}],
                "context_selection": {
                    "paths": ["src/auth.py", "tests/test_auth.py"],
                    "used_chars": 4200,
                },
                "unavailable": ["cost"],
            },
        }
    )

    assert "Turn Inspector" in rendered
    assert "openai-main" in rendered
    assert "gpt-test" in rendered
    assert "read_file" in rendered
    assert "README.md" in rendered
    assert "run_tests" in rendered
    assert "context_selection" in rendered
    assert "src/auth.py" in rendered
    assert "1/1/0" in rendered
    assert "修复跨文件缓存缺陷" in rendered
    assert "workers=1/1" in rendered
    assert "pending_approvals=0" in rendered
    assert "failure=none" in rendered
    assert "sandbox=degraded" in rendered
