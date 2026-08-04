from __future__ import annotations

import json
import sys
from pathlib import Path

from code_rook.core.skills import (
    SkillConfirmationRequired,
    SkillManager,
    SkillManagerError,
)
from code_rook.core.skills.manager import InstallScope, InstallTrust


# 构造绑定当前项目和默认用户目录的 skill manager
def _manager() -> SkillManager:
    return SkillManager(Path.cwd())


# 列出最终优先级下的所有 skill manifest 和 provenance 摘要
def cmd_skills_list() -> None:
    skills = _manager().list_all()
    if not skills:
        print("No skills found.")
        return
    for skill in skills:
        print(
            f"{skill.name}\t{skill.scope}\t{skill.trust}\t{skill.integrity}\t"
            f"{skill.digest[:19]}"
        )


# 显示指定 skill 的完整 manifest 和 provenance，但不输出正文
def cmd_skills_show(name: str) -> None:
    skill = _manager().show(name)
    if skill is None:
        print(f"error: skill not found: {name}", file=sys.stderr)
        raise SystemExit(1)
    payload = skill.model_dump(mode="json", exclude={"system_prompt_template"})
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# preview 或经明确确认后安装本地 skill
def cmd_skills_install(
    source: str,
    *,
    scope: InstallScope,
    trust: bool,
    confirmed: bool,
    overwrite: bool,
) -> None:
    manager = _manager()
    trust_value: InstallTrust = "trusted" if trust else "untrusted"
    try:
        installed = manager.install(
            source,
            scope=scope,
            trust=trust_value,
            confirmed=confirmed,
            overwrite=overwrite,
        )
    except SkillConfirmationRequired as exc:
        print(json.dumps(exc.preview.model_dump(mode="json"), ensure_ascii=False, indent=2))
        print("Preview only. Re-run with --yes to install.")
        return
    except SkillManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"installed {installed.name} -> {Path(installed.path).parent}")


# 经明确确认后从受管 project/user 目录删除 skill
def cmd_skills_remove(name: str, *, scope: InstallScope, confirmed: bool) -> None:
    try:
        _manager().remove(
            name,
            scope=scope,
            confirmed=confirmed,
        )
    except SkillManagerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"removed {scope} skill {name}")


# 审计全部 skill digest，存在 mismatch 时返回非零退出码
def cmd_skills_audit() -> None:
    records = _manager().audit()
    mismatches = 0
    for record in records:
        print(
            f"{record.name}\t{record.scope}\t{record.trust}\t{record.integrity}\t"
            f"{record.digest}"
        )
        if record.integrity == "mismatch":
            mismatches += 1
    if mismatches:
        print(f"error: {mismatches} skill digest mismatch(es)", file=sys.stderr)
        raise SystemExit(1)
