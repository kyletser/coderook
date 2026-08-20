from __future__ import annotations

from code_rook.cli.commands import review as review_module
from code_rook.core.config import CodeRookConfig


# 功能：验证审查目标固定要求分级 finding、证据、风险和验证信息
# 设计：直接检查纯函数输出，避免依赖 daemon 或模型对提示文本的二次解释
def test_build_review_goal_contains_structured_contract() -> None:
    goal = review_module.build_review_goal("Review auth changes")

    assert goal.startswith("Review auth changes")
    assert "Findings ordered by P0-P3" in goal
    assert "file and line" in goal
    assert "concrete evidence" in goal
    assert "Verification performed" in goal


# 功能：验证 review preset 只以 allow-list 方式委托 headless run
# 设计：替换 cmd_run 捕获全部参数，证明写工具未获授权且结构化契约实际传入执行入口
def test_cmd_review_uses_read_only_allow_list(monkeypatch) -> None:
    captured: dict[str, object] = {}

    # 捕获 review 对通用 run 命令的委托参数
    def _capture(goal: str, config: CodeRookConfig, **kwargs: object) -> None:
        captured.update({"goal": goal, "config": config, **kwargs})

    monkeypatch.setattr(review_module, "cmd_run", _capture)
    config = CodeRookConfig()

    review_module.cmd_review("Review parser", config, output_format="json")

    assert captured["permission_mode"] == "allow_list"
    assert captured["output_format"] == "json"
    assert captured["question_mode"] == "preset"
    assert captured["allow_tools"] == [
        "read_file",
        "list_dir",
        "glob",
        "grep",
        "git_diff",
        "Repository",
    ]
    assert "write_file" not in captured["allow_tools"]
    assert "Findings ordered by P0-P3" in captured["goal"]
