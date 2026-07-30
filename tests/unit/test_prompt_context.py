from __future__ import annotations

from pathlib import Path

from code_rook.core.agents.loader import AgentProfile
from code_rook.core.prompt_context import build_capability_context, build_runtime_context
from code_rook.core.skills.loader import Skill


# 功能：验证运行环境摘要公开工作目录与本机 shell 能力但不展开环境变量
# 设计：传入临时路径并检查稳定字段，避免绑定具体 Windows 版本或用户机器信息
def test_runtime_context_describes_host_and_workspace(tmp_path: Path) -> None:
    context = build_runtime_context(tmp_path)

    assert str(tmp_path.resolve()) in context
    assert "bash tool runs host shell commands" in context
    assert "operating-system utilities" in context
    assert "approval" in context


# 功能：验证能力目录只注入 Skill 与 Agent 的名称和描述而不提前加载正文
# 设计：构造带独特正文标记的对象，断言描述可见而系统提示正文不可见
def test_capability_context_uses_progressive_disclosure() -> None:
    skill = Skill(
        name="review",
        description="Review changed code",
        system_prompt_template="SECRET_FULL_SKILL_BODY",
    )
    agent = AgentProfile(
        name="planner",
        description="Plan multi-step work",
        system_prompt="SECRET_AGENT_SYSTEM_PROMPT",
    )

    context = build_capability_context([skill], [agent])

    assert "review: Review changed code" in context
    assert "planner: Plan multi-step work" in context
    assert "SECRET_FULL_SKILL_BODY" not in context
    assert "SECRET_AGENT_SYSTEM_PROMPT" not in context
