from code_rook.core.permissions.command_pattern import (
    command_pattern_key,
    first_command_tokens,
    matches_command_pattern,
)


# 功能：首命令解析只保留第一条可执行命令的 argv，遇串接/重定向运算符即停
# 设计：用 shlex 的 punctuation_chars 切分运算符，验证注入型的 ;、&&、|、> 被排除在 argv 之外
def test_first_command_tokens_stops_at_operator() -> None:
    assert first_command_tokens("uv run pytest -k unit") == ["uv", "run", "pytest", "-k", "unit"]
    assert first_command_tokens("uv run pytest; rm -rf /") == ["uv", "run", "pytest"]
    assert first_command_tokens("cd /tmp && ls") == ["cd", "/tmp"]
    assert first_command_tokens("echo hi > out.txt") == ["echo", "hi"]


# 功能：构造的始终允许前缀模式为"首命令 argv + 通配符 *"
# 设计：验证 key 仅取自首条命令，后续注入命令体不会污染允许模式
def test_command_pattern_key_builds_from_first_command() -> None:
    assert command_pattern_key("uv run pytest; rm -rf /") == "uv run pytest*"
    assert command_pattern_key("git status --short") == "git status --short*"
    assert command_pattern_key("") == ""


# 功能：通配符前缀命中合法尾参命令，但拒绝命令串接/重定向注入
# 设计：这是 W3.2 的核心安全契约——用首 token 解析而非纯 fnmatch，
#      `uv run pytest; rm -rf /` 不得命中 `uv run pytest*` 前缀
def test_pattern_matches_legal_args_but_rejects_injection() -> None:
    assert matches_command_pattern("uv run pytest -k x", "uv run pytest*")
    assert matches_command_pattern("git status --short", "git status*")
    assert not matches_command_pattern("uv run pytest; rm -rf /", "uv run pytest*")
    assert not matches_command_pattern("cd /tmp && ls", "cd /tmp*")
    assert not matches_command_pattern("echo hi > f", "echo*")


# 功能：前缀必须在命令起始对齐，中段重合不算命中
# 设计：验证 zip 定位在整个 argv 前缀处，避免子串误匹配
def test_pattern_requires_leading_alignment() -> None:
    assert matches_command_pattern("pytest -q", "pytest*")
    assert not matches_command_pattern("uv run pytest", "pytest*")
    assert matches_command_pattern("rm -rf /", "rm -rf /*")


# 功能：无通配符的模式要求与命令 argv 完全一致
# 设计：精确匹配时禁止多出任何尾参，保持契约严格、可预期
def test_pattern_without_wildcard_requires_exact() -> None:
    assert matches_command_pattern("git status", "git status")
    assert not matches_command_pattern("git status --short", "git status")