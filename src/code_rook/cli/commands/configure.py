from __future__ import annotations

import getpass
import json
import os
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, set_key, unset_key

from code_rook.core.config import CodeRookConfig, LlmConfig, get_config
from code_rook.core.llm.credentials import (
    credentials_path,
    llm_is_configured,
    normalize_provider,
    resolve_api_key,
    save_api_key,
)

_DEFAULT_CONFIG_PATH = Path("~/.coderook/config.toml").expanduser()
_SECTION_PATTERN = re.compile(r"(?m)^\s*\[\[?[^\]\r\n]+\]\]?\s*(?:#.*)?$")


# 返回交互式配置应写入的最高优先级 TOML 路径
def config_write_path() -> Path:
    explicit = os.environ.get("CODEROOK_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    local = Path(".coderook/config.toml")
    return local if local.exists() else _DEFAULT_CONFIG_PATH


# 使用 TOML 基本字符串语法安全编码配置值
def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# 将新的 llm 表原子写入配置文件，同时保留其他 TOML 小节
def write_llm_config(config: LlmConfig, path: Path | None = None) -> Path:
    target = path or config_write_path()
    original = target.read_text(encoding="utf-8") if target.exists() else ""
    if original:
        try:
            tomllib.loads(original)
        except tomllib.TOMLDecodeError as exc:
            raise SystemExit(f"Config parse error ({target}): {exc}") from exc

    block = (
        "[llm]\n"
        f"provider = {_toml_string(normalize_provider(config.provider))}\n"
        f"default_model = {_toml_string(config.default_model)}\n"
        f"router = {_toml_string(config.router)}\n"
        f"base_url = {_toml_string(config.base_url)}\n"
        f"api_key_env = {_toml_string(config.api_key_env)}\n"
    )
    match = re.search(r"(?m)^\s*\[llm\]\s*(?:#.*)?$", original)
    if match is None:
        separator = "" if not original else ("\n" if original.endswith("\n") else "\n\n")
        updated = f"{original}{separator}{block}"
    else:
        next_section = _SECTION_PATTERN.search(original, match.end())
        end = next_section.start() if next_section else len(original)
        prefix = original[:match.start()]
        suffix = original[end:]
        updated = f"{prefix}{block}"
        if suffix and not updated.endswith("\n\n"):
            updated += "\n"
        updated += suffix

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp")
    temporary.write_text(updated, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


# 仅更新默认模型，保留 provider、endpoint、router 和凭据引用
def switch_llm_model(
    current: CodeRookConfig,
    model: str,
    *,
    config_path: Path | None = None,
) -> CodeRookConfig:
    selected = model.strip()
    if not selected:
        raise ValueError("model name cannot be empty")
    updated = replace(current.llm, default_model=selected)
    write_llm_config(updated, config_path)
    _sync_project_dotenv(updated, current.llm.api_key_env)
    os.environ["CODEROOK_LLM_DEFAULT_MODEL"] = selected
    return get_config()


# 若项目使用 .env，则同步非敏感 LLM 设置并把旧明文 key 迁出该文件
def _sync_project_dotenv(config: LlmConfig, previous_key_env: str) -> None:
    dotenv_path = Path(".env")
    if not dotenv_path.exists():
        return
    values = dotenv_values(dotenv_path)
    updates = {
        "CODEROOK_LLM_PROVIDER": normalize_provider(config.provider),
        "CODEROOK_LLM_DEFAULT_MODEL": config.default_model,
        "CODEROOK_LLM_BASE_URL": config.base_url,
        "CODEROOK_LLM_API_KEY_ENV": config.api_key_env,
    }
    for name, value in updates.items():
        set_key(dotenv_path, name, value, quote_mode="always")
        os.environ[name] = value
    secret_names = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CODEROOK_LLM_API_KEY",
        previous_key_env,
        config.api_key_env,
    }
    for name in secret_names:
        if name and name in values:
            unset_key(dotenv_path, name)
            os.environ.pop(name, None)


# 循环读取选项，直到用户输入受支持的 provider 编号或名称
def _prompt_provider(input_fn: Callable[[str], str], current: str) -> str:
    current_name = normalize_provider(current)
    default = "1" if current_name == "anthropic" else "2"
    while True:
        raw = input_fn(
            "API 格式 [1=Anthropic-compatible, 2=OpenAI-compatible] "
            f"(默认 {default}): "
        ).strip()
        choice = raw or default
        if choice in {"1", "anthropic", "Anthropic"}:
            return "anthropic"
        if choice.lower().replace("-", "_") in {"2", "openai", "openai_compatible"}:
            return "openai_compatible"
        print("请输入 1 或 2。")


# 读取文本字段并支持通过默认值直接回车确认
def _prompt_value(
    input_fn: Callable[[str], str],
    label: str,
    default: str,
    *,
    required: bool = True,
) -> str:
    suffix = f" (默认 {default})" if default else ""
    while True:
        value = input_fn(f"{label}{suffix}: ").strip() or default
        if value or not required:
            return value
        print(f"{label}不能为空。")


# 校验自定义 API 地址必须是完整的 HTTP(S) URL
def _validate_base_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# 交互式收集并持久化 LLM 配置，API key 使用隐藏输入
def configure_llm(
    current: CodeRookConfig,
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    config_path: Path | None = None,
    credential_file: Path | None = None,
) -> CodeRookConfig:
    provider = _prompt_provider(input_fn, current.llm.provider)
    same_provider = normalize_provider(current.llm.provider) == provider
    if provider == "anthropic":
        model_default = current.llm.default_model if same_provider else "claude-sonnet-4-6"
        base_default = current.llm.base_url if same_provider else ""
        base_label = "Anthropic Base URL，官方接口可留空"
        api_key_env = (
            current.llm.api_key_env if same_provider and current.llm.api_key_env
            else "ANTHROPIC_API_KEY"
        )
    else:
        model_default = current.llm.default_model if same_provider else ""
        base_default = current.llm.base_url if same_provider else ""
        base_label = "Chat Completions 完整地址"
        api_key_env = (
            current.llm.api_key_env if same_provider and current.llm.api_key_env
            else "OPENAI_API_KEY"
        )

    model = _prompt_value(input_fn, "模型名称", model_default)
    while True:
        base_url = _prompt_value(
            input_fn,
            base_label,
            base_default,
            required=provider == "openai_compatible",
        )
        if not base_url or _validate_base_url(base_url):
            break
        print("请输入完整的 http:// 或 https:// 地址。")

    probe = LlmConfig(
        provider=provider,
        default_model=model,
        router=current.llm.router,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
    )
    existing_key = resolve_api_key(probe, credential_file)
    hint = "（回车保留现有密钥）" if existing_key else ""
    api_key = secret_fn(f"API key{hint}: ").strip()
    if not api_key and existing_key is None:
        raise SystemExit("API key 不能为空。")
    save_api_key(provider, api_key or existing_key or "", credential_file)

    written_path = write_llm_config(probe, config_path)
    _sync_project_dotenv(probe, current.llm.api_key_env)
    print(f"LLM 配置已保存：{written_path}")
    print(f"API key 已安全保存：{credential_file or credentials_path()}")
    return get_config()


# 打印当前配置状态，隐藏密钥正文
def print_llm_status(config: CodeRookConfig) -> None:
    provider = normalize_provider(config.llm.provider)
    endpoint = config.llm.base_url or "(Anthropic official)"
    key_source = (
        f"environment:{config.llm.api_key_env}"
        if config.llm.api_key_env and os.environ.get(config.llm.api_key_env)
        else f"credentials:{credentials_path()}"
    )
    state = "configured" if llm_is_configured(config.llm) else "incomplete"
    print(f"status:   {state}")
    print(f"provider: {provider}")
    print(f"model:    {config.llm.default_model}")
    print(f"endpoint: {endpoint}")
    print(f"api key:  {key_source if resolve_api_key(config.llm) else '(missing)'}")


# 执行手动配置命令并提示 Core 重启要求
def cmd_configure(config: CodeRookConfig) -> None:
    if not sys.stdin.isatty():
        raise SystemExit("Interactive terminal required for `coderook configure`.")
    updated = configure_llm(config)
    from code_rook.cli.commands.core import ensure_core_running, stop_core

    if stop_core():
        ensure_core_running(updated)
        print("Core 已使用新配置重新启动。")
