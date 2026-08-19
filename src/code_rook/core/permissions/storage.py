from __future__ import annotations

from pathlib import Path

from code_rook.core.authority import AuthorityProfile

_DEFAULT_POLICY_PATH = Path("~/.coderook/policy.toml")
_CURRENT_POLICY_SCHEMA_VERSION = 1


class PolicyStoreError(ValueError):
    pass


# 读取 policy meta 版本并阻断旧版 daemon 覆盖未来格式，缺失版本按兼容 v0 处理
def _validate_policy_version(path: Path) -> None:
    in_meta = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[meta]":
            in_meta = True
            continue
        if stripped.startswith("["):
            in_meta = False
            continue
        if not in_meta or "=" not in stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != "schema_version":
            continue
        try:
            version = int(value.strip())
        except ValueError as exc:
            raise PolicyStoreError("policy schema_version must be an integer") from exc
        if version > _CURRENT_POLICY_SCHEMA_VERSION:
            raise PolicyStoreError(
                "policy schema "
                f"{version} is newer than supported {_CURRENT_POLICY_SCHEMA_VERSION}"
            )
        if version < 0:
            raise PolicyStoreError(f"invalid policy schema version: {version}")
        return


# 加载 policy.toml 中 [always] 节，返回 {tool_name: "allow"/"deny"}；文件不存在时返回空字典
def load_policy_file(path: Path | None = None) -> dict[str, str]:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    if not p.exists():
        return {}
    _validate_policy_version(p)
    result: dict[str, str] = {}
    in_always = False
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[always]":
            in_always = True
            continue
        if stripped.startswith("["):
            in_always = False
            continue
        if in_always and "=" in stripped and not stripped.startswith("#"):
            k, _, v = stripped.partition("=")
            k = k.strip()
            v = v.strip().strip('"')
            if v in ("allow", "deny"):
                result[k] = v
    return result


# 加载 policy.toml 中持久化的默认权限姿态，旧文件缺失该节时返回空值
def load_authority_profile(path: Path | None = None) -> AuthorityProfile | None:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    if not p.exists():
        return None
    _validate_policy_version(p)
    in_authority = False
    for line in p.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[authority]":
            in_authority = True
            continue
        if stripped.startswith("["):
            in_authority = False
            continue
        if in_authority and "=" in stripped and not stripped.startswith("#"):
            key, _, value = stripped.partition("=")
            if key.strip() != "profile":
                continue
            try:
                return AuthorityProfile(value.strip().strip('"'))
            except ValueError:
                return None
    return None


# 将持久权限姿态与工具决策一并写入 policy.toml
def save_policy_file(
    always: dict[str, str],
    path: Path | None = None,
    *,
    authority_profile: AuthorityProfile | None = None,
) -> None:
    p = (path or _DEFAULT_POLICY_PATH).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    profile = authority_profile or load_authority_profile(p)
    lines = [
        "# ~/.coderook/policy.toml",
        "# 由 coderook-core 自动管理，手动编辑生效但格式须正确",
        "",
        "[meta]",
        f"schema_version = {_CURRENT_POLICY_SCHEMA_VERSION}",
        "",
    ]
    if profile is not None:
        lines.extend(("[authority]", f'profile = "{profile.value}"', ""))
    lines.append("[always]")
    for tool, decision in sorted(always.items()):
        lines.append(f'{tool} = "{decision}"')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
