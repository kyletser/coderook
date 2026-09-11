_REVIEW_CONTRACT = """
Read-only review contract:
- Do not modify files, run mutating commands, or change external state.
- Inspect repository evidence before drawing conclusions. Do not invent findings.
- Final response must contain: Summary; Findings ordered by P0-P3 severity; Risks and
  unknowns; Verification performed.
- Every finding must include severity, file and line when available, concrete evidence,
  user impact, and a specific recommendation. If no actionable defect is found, state that
  explicitly and keep residual risks separate from findings.
""".strip()


# 将用户审查目标与稳定的只读结构化输出契约组合
def build_review_goal(goal: str) -> str:
    return f"{goal.strip()}\n\n{_REVIEW_CONTRACT}"
