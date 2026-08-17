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
    tokens = first_command_tokens(command)
    if not tokens:
        return ""
    return " ".join(tokens) + "*"


# 判断完整命令是否命中前缀模式（首 token 语义）：
# 命中后若命令仍有串接/重定向运算符则拒绝，防止 `uv run pytest; rm -rf /` 借前缀放行注入
def matches_command_pattern(command: str, pattern: str) -> bool:
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


# 构造带运算符切分的 POSIX shlex；意外引用不平衡时返回空流由调用方兜底
def _make_lexer(command: str) -> shlex.shlex:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.commenters = ""
    lexer.whitespace_split = True
    # 保留常见文件路径与命令行选项字符，使其不被切碎
    lexer.wordchars += "=.:/-@%^+"
    return lexer