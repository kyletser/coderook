# Python port of Pi prompt-templates.ts argument expansion (MIT, vendor/pi/LICENSE).
import logging
import re
from collections.abc import Iterator
from pathlib import Path

import yaml

_PLACEHOLDER = re.compile(
    r"\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)


# 读取模板的 YAML 元数据和原始正文，不在发现命令时替换参数。
def read_prompt_template(path: Path) -> tuple[dict[str, str], str]:
    raw = path.read_text(encoding="utf-8-sig")
    match = re.match(r"\A---\n([\s\S]*?)^---(?:\n|$)", raw, re.MULTILINE)
    if match is None:
        return {}, raw
    loaded = yaml.safe_load(match[1]) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Template metadata must be a mapping: {path.name}")
    metadata = {key: value for key, value in loaded.items()
                if isinstance(key, str) and isinstance(value, str)}
    return metadata, raw[match.end():].strip()


# 按资源顺序加载目录或显式 Markdown 文件，补全与执行共享相同候选。
def _templates(paths: list[Path]) -> Iterator[tuple[Path, dict[str, str], str]]:
    seen: set[str] = set()
    for source in paths:
        source = source.expanduser()
        candidates = [source] if source.is_file() else sorted(source.glob("*.md"))
        for path in candidates:
            if path.suffix != ".md" or path.stem in seen or not path.is_file():
                continue
            try:
                metadata, body = read_prompt_template(path)
            except (OSError, ValueError, yaml.YAMLError):
                logging.getLogger(__name__).warning("Cannot read prompt template %s", path)
                continue
            seen.add(path.stem)
            yield path, metadata, body


# 发现可用模板名称和摘要，重复名称按与执行相同的资源顺序去重。
def list_prompt_templates(directories: list[Path]) -> list[dict[str, str]]:
    templates: dict[str, dict[str, str]] = {}
    for path, metadata, body in _templates(directories):
        description = metadata.get("description", "")
        if not description:
            first = next((line for line in body.splitlines() if line.strip()), "")
            description = first[:60] + ("..." if len(first) > 60 else "")
        templates[path.stem] = {
            "name": path.stem, "description": description, "kind": "template",
        }
        if metadata.get("argument-hint"):
            templates[path.stem]["argument_hint"] = metadata["argument-hint"]
    return list(templates.values())


# 按 Pi 的引号规则解析参数，反斜杠保持原样以兼容 Windows 路径。
def parse_arguments(text: str) -> list[str]:
    arguments: list[str] = []
    current = ""
    quote = ""
    for character in text:
        if quote:
            if character == quote:
                quote = ""
            else:
                current += character
        elif character in "\"'":
            quote = character
        elif character.isspace():
            if current:
                arguments.append(current)
                current = ""
        else:
            current += character
    if current:
        arguments.append(current)
    return arguments


# 单次替换位置、全量、默认值和切片参数，不递归解释参数内的占位符。
def substitute_arguments(content: str, arguments: list[str]) -> str:
    # 按一处模板占位符计算替换值。
    def replace(match: re.Match[str]) -> str:
        target, default, start, length, simple = match.groups()
        if target:
            index = int(target) - 1 if target.isdigit() else -1
            value = " ".join(arguments) if target in {"@", "ARGUMENTS"} else (
                arguments[index] if 0 <= index < len(arguments) else ""
            )
            return value or default
        if start is not None:
            index = max(0, int(start) - 1)
            return " ".join(arguments[index:index + int(length)] if length else arguments[index:])
        if simple in {"@", "ARGUMENTS"}:
            return " ".join(arguments)
        index = int(simple) - 1
        return arguments[index] if 0 <= index < len(arguments) else ""

    return _PLACEHOLDER.sub(replace, content)


# 按用户目录优先顺序读取同名 Markdown 模板并展开显式命令。
def expand_prompt_template(text: str, directories: list[Path]) -> str:
    match = re.fullmatch(r"/([^\s/\\]+)(?:\s+([\s\S]*))?", text)
    if not match or match[1] in {".", ".."}:
        return text
    for path, _, body in _templates(directories):
        if path.stem == match[1]:
            return substitute_arguments(body, parse_arguments(match[2] or ""))
    return text
