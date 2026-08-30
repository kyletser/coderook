from __future__ import annotations

import shlex

# 命令串接与重定向保留字，命中即视为存在构成性运算符（首 token 前缀不得覆盖后续子命令）
_OPERATOR_START = ("&", "|", ";", ">", "<", "\n")


# 将 bash 命令完整切分为 token 流（含运算符），供前缀匹配判断命令是否存在串接/重定向
def command_tokens(command: str) -> list[str]:
    lexer = _make_lexer(command)
    try:
        return list(lexer)
    except ValueError:
        return []


# 仅保留第一条可执行命令的 argv（遇运算符即停），用于构造干净的 always-allow 前缀模式
def first_command_tokens(command: str) -> list[str]:
    argv: list[str] = []
    for token in command_tokens(command):
        if _is_operator(token):
            break
        argv.append(token)
    return argv


# 构造 always-allow 前缀模式字符串：取首条命令的八字令牌并附其后任意通配符 *
def command_pattern_key(command: str) -> str:
    # 含换行或命令替换的命令不得生成前缀模式，避免存储可被后续利用的自动放行键
    if _has_structural_risk(command):
        return ""
    tokens = first_command_tokens(command)
    if not tokens:
        return ""
    return " ".join(tokens) + "*"


# 按首 token 语义匹配命令前缀，并拒绝串接或重定向运算符注入
def matches_command_pattern(command: str, pattern: str) -> bool:
    # 换行会被 shlex 当空白吞掉而不产生运算符 token，含换行/CR 的多行命令必须整体排除
    # 未引用的 $(、`、${ 会在执行期展开，前缀匹配看不到其内部运算符，同样必须整体排除
    if _has_structural_risk(command):
        return False
    full = command_tokens(command)
    if not full:
        return False
    pat_tokens = pattern.split()
    if not pat_tokens:
        return False
    # 通配符 * 拼接在末令牌上（如 `uv run pytest*`），也可能是独立 `*`
    wildcard = pat_tokens[-1].endswith("*")
    last = pat_tokens[-1]
    if wildcard:
        base = last[:-1]
        literal = pat_tokens[:-1] + ([base] if base else [])
    else:
        literal = pat_tokens
    if not literal:
        return False
    if len(full) < len(literal):
        return False
    if not wildcard and len(full) != len(literal):
        return False
    if any(c != p for c, p in zip(full, literal)):
        return False
    if wildcard and any(_is_operator(t) for t in full[len(literal):]):
        return False
    return True


# 判定 token 是否为命令构成性运算符（& | ; > < 及其组合、换行）
def _is_operator(token: str) -> bool:
    return token.startswith(_OPERATOR_START)


# 判定命令是否含前缀匹配无法看见的结构性风险：换行/CR 或单引号外的 $（、`、${ 命令替换
def _has_structural_risk(command: str) -> bool:
    if "\n" in command or "\r" in command:
        return True
    in_single = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if in_single:
            if char == "'":
                in_single = False
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'":
            in_single = True
            index += 1
            continue
        if char == "`":
            return True
        if char == "$" and index + 1 < len(command) and command[index + 1] in ("(", "{"):
            return True
        index += 1
    return False


# 构造带运算符切分的 POSIX shlex；意外引用不平衡时返回空流由调用方兜底
def _make_lexer(command: str) -> shlex.shlex:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.commenters = ""
    lexer.whitespace_split = True
    # 保留常见文件路径与命令行选项字符，使其不被切碎
    lexer.wordchars += "=.:/-@%^+"
    return lexer
