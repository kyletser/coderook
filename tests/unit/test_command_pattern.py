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
# 设计：用合法参数和串接攻击对照，验证首 token 解析优于纯 fnmatch
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

# 功能：换行串接的命令不得命中前缀模式，换行与 CR 都是命令分隔符
# 设计：使用 shlex 曾误判为普通空白的换行攻击样本验证结构性拦截
def test_pattern_rejects_newline_chained_commands() -> None:
    assert not matches_command_pattern("echo hello\nrm -rf ~", "echo hello*")
    assert not matches_command_pattern("echo hello\r\ncat ~/.ssh/id", "echo hello*")
    assert matches_command_pattern("echo hello", "echo hello*")


# 功能：单引号外的命令替换（$()、反引号、${}）不得命中前缀模式
# 设计：对比可执行替换与单引号字面量，兼顾阻断攻击和正常 commit 消息
def test_pattern_rejects_unquoted_substitution() -> None:
    assert not matches_command_pattern("git status $(rm -rf ~)", "git status*")
    assert not matches_command_pattern("git status `rm -rf ~`", "git status*")
    assert not matches_command_pattern("git status ${VAR}", "git status*")
    assert matches_command_pattern("git commit -m '$(not expanded)'", "git commit*")


# 功能：含换行或命令替换的命令不生成 always-allow 前缀模式键
# 设计：从源头阻断"存储可被利用的键"，用空键返回值验证调用方不会落盘危险模式
def test_command_pattern_key_refuses_structural_risk() -> None:
    assert command_pattern_key("echo hi\nrm -rf ~") == ""
    assert command_pattern_key("echo $(x)") == ""
    assert command_pattern_key("uv run pytest -q") == "uv run pytest -q*"
