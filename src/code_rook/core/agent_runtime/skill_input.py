# Python port of Pi AgentSession._expandSkillCommand (MIT, vendor/pi/LICENSE).
import re
from html import escape
from pathlib import Path

from code_rook.core.skills.loader import SkillLoader


# 将显式 Skill 命令展开为带来源的用户上下文，不替换系统提示或重建工具集。
def expand_skill_input(text: str, loader: SkillLoader, *, workspace_trusted: bool) -> str:
    if not text.startswith("/skill:"):
        return text
    parts = text[7:].split(maxsplit=1)
    if not parts or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", parts[0]) is None:
        return text
    skill = loader.resolve(parts[0], require_trusted=True, workspace_trusted=workspace_trusted)
    if skill is None:
        return text
    path = Path(skill.path)
    body = skill.system_prompt_template.strip()
    block = (
        f'<skill name="{escape(skill.name, quote=True)}" '
        f'location="{escape(str(path), quote=True)}">\n'
        f"References are relative to {path.parent}.\n\n{body}\n</skill>"
    )
    return f"{block}\n\n{parts[1].strip()}" if len(parts) > 1 else block
