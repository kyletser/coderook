from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 7437
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE = "~/.coderook/logs/core.log"
_DEFAULT_LOG_FORMAT = "text"
_DEFAULT_CONFIG_PATH = "~/.coderook/config.toml"
_DEFAULT_MAX_STEPS = 20
_DEFAULT_MODEL = "claude-sonnet-4-6"
_DEFAULT_TRACE_FILE = "~/.coderook/traces/daemon.jsonl"
_DEFAULT_LLM_PROVIDER = "anthropic"
_DEFAULT_IPC_TOKEN_FILE = "~/.coderook/ipc-token"
_DEFAULT_API_HOST = "127.0.0.1"
_DEFAULT_API_PORT = 7438


@dataclass
class LoggingConfig:
    level: str = _DEFAULT_LOG_LEVEL
    file: str = _DEFAULT_LOG_FILE
    format: str = _DEFAULT_LOG_FORMAT  # "text" | "json"


@dataclass
class AgentConfig:
    max_steps: int = _DEFAULT_MAX_STEPS
    # 步数耗尽时的自动续段数（每段追加 max_steps 步）；交互模式另有 ask 续跑
    max_step_continues: int = 0
    task_router: str = "rules_only"
    delegation_policy: str = "routed"


@dataclass
class LlmConfig:
    provider: str = _DEFAULT_LLM_PROVIDER  # "anthropic" | "openai_compatible"
    default_model: str = _DEFAULT_MODEL
    router: str = "static"  # "static" | "rule_based" | "cost_budget"
    # rule_based：PLAN 模式选用的高推理路由 id（空则沿用活动路由）
    router_plan_route: str = ""
    # rule_based：ACT/默认模式选用的路由 id（空则沿用活动路由）
    router_act_route: str = ""
    # cost_budget：单 run 累计成本超限阈值（USD）；<=0 表示不启用
    router_cost_budget: float = 0.0
    # cost_budget：超限后降档到的廉价路由 id（空则沿用活动路由）
    router_cost_fallback: str = ""
    base_url: str = ""
    api_key_env: str = "ANTHROPIC_API_KEY"
    # 仅保存用户显式 --env-file 的进程内凭据覆盖，不写回全局环境或日志
    credential_overlay: dict[str, str] = field(default_factory=dict, repr=False)


@dataclass
class TraceConfig:
    enabled: bool = True
    file: str = _DEFAULT_TRACE_FILE
    include_payload: bool = False
    include_llm_payload: bool = False  # 显式启用时仍会经过 secret redaction
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class PermissionConfig:
    timeout_s: float = 60.0  # 审批超时秒数；0 表示不超时


@dataclass
class ApiConfig:
    host: str = _DEFAULT_API_HOST
    port: int = _DEFAULT_API_PORT
    token: str = field(default="", repr=False)


@dataclass
class CompactionConfig:
    strategy: str = "adaptive_evidence"
    # context_pct 触发自动压缩的阈值（0 表示禁用）
    auto_threshold: float = 0.80
    retain_ratio: float = 0.25  # 压缩后保留的最近原文 token 比例
    tool_result_limit: int = 8_000  # tool_result 截断触发字符数
    tool_result_keep: int = 4_000   # 截断后保留的前缀字符数
    tool_result_summarize_threshold: int = 20_000  # 超过后优先使用 LLM 蒸馏


@dataclass
class McpServerConfig:
    name: str
    transport: str = "stdio"       # "stdio" | "tcp"
    command: str = ""              # stdio 专用：可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    host: str = "localhost"        # tcp 专用
    port: int = 3000               # tcp 专用
    url: str = ""                  # streamable_http 专用：单一 HTTP endpoint
    auth_token_env: str = ""       # 可选 Bearer token 环境变量名


@dataclass
class McpConfig:
    servers: list[McpServerConfig] = field(default_factory=list)


@dataclass
class CodeRookConfig:
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    ipc_token_file: str = _DEFAULT_IPC_TOKEN_FILE
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    permission: PermissionConfig = field(default_factory=PermissionConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    mcp: McpConfig = field(default_factory=McpConfig)


# 读取用户显式指定的 env 文件，但禁止其间接选择 TOML 配置路径
def _load_explicit_env_file(env_file: str | Path | None) -> dict[str, str]:
    if env_file is None:
        return {}
    path = Path(env_file).expanduser()
    if not path.is_file():
        raise SystemExit(f"Config error: env file does not exist: {path}")
    values = dotenv_values(path, interpolate=False)
    if "CODEROOK_CONFIG" in values:
        raise SystemExit(
            "Config error: CODEROOK_CONFIG is only accepted from the process environment"
        )
    return {
        key: value
        for key, value in values.items()
        if isinstance(key, str) and isinstance(value, str)
    }


# 判断候选路径是否为当前工作区的标准项目配置，显式路径也不能绕过项目安全约束
def _is_project_config_path(path: Path, project_path: Path) -> bool:
    try:
        return path.resolve(strict=False) == project_path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False


# 判断显式路径是否具有标准项目配置目录形态
def _looks_like_project_config(path: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return (
        resolved.name.lower() == "config.toml"
        and resolved.parent.name.lower() == ".coderook"
    )


# 构建运行时配置：默认值 → 全局 TOML → 项目 TOML → 显式 env 文件 → 用户进程环境
def get_config(
    *,
    env_file: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> CodeRookConfig:
    config = CodeRookConfig()

    process_env = os.environ if environ is None else environ
    file_env = _load_explicit_env_file(env_file)

    # 若显式指定 CODEROOK_CONFIG，只读该文件；否则按优先级叠加：全局 → 项目本地
    explicit = process_env.get("CODEROOK_CONFIG")
    project_path = Path(".coderook/config.toml")
    if explicit:
        config_paths = [Path(explicit).expanduser()]
    else:
        config_paths = [
            Path(_DEFAULT_CONFIG_PATH).expanduser(),
            project_path,
        ]

    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, "rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Config parse error ({config_path}): {e}") from e
            is_user_config = _is_project_config_path(
                config_path,
                Path(_DEFAULT_CONFIG_PATH).expanduser(),
            )
            is_project_config = _is_project_config_path(config_path, project_path) or (
                bool(explicit) and _looks_like_project_config(config_path)
            )
            if not is_user_config and is_project_config:
                _reject_project_sensitive_settings(data, config_path)
            _apply_toml(config, data)

    merged_env = dict(file_env)
    merged_env.update(process_env)
    _apply_env(config, merged_env)
    config.llm.credential_overlay = dict(file_env)
    return config


# 仅允许项目配置修改无外部副作用的行为参数，拒绝端点、进程、路径和凭据来源
def _reject_project_sensitive_settings(data: dict[str, Any], path: Path) -> None:
    allowed_sections = {"agent", "compaction", "logging"}
    forbidden_sections = set(data) - allowed_sections
    if forbidden_sections:
        names = ", ".join(sorted(forbidden_sections))
        raise SystemExit(
            f"Config error ({path}): project config cannot set security-sensitive "
            f"sections: {names}"
        )
    logging = data.get("logging")
    if isinstance(logging, dict) and "file" in logging:
        raise SystemExit(
            f"Config error ({path}): project [logging] cannot set output file paths"
        )


# 将已解析的 TOML 根表写入 config；未知小节或类型错误时退出进程
def _apply_toml(config: CodeRookConfig, data: dict[str, Any]) -> None:
    known_sections = {
        "core",
        "logging",
        "agent",
        "llm",
        "trace",
        "permission",
        "api",
        "compaction",
        "mcp",
    }
    unknown = set(data.keys()) - known_sections
    if unknown:
        raise SystemExit(f"Unknown top-level config keys: {', '.join(sorted(unknown))}")

    if "core" in data:
        core = data["core"]
        if not isinstance(core, dict):
            raise SystemExit("Config error: [core] must be a table")
        unknown_core: set[str] = set(core.keys()) - {"host", "port", "ipc_token_file"}
        if unknown_core:
            raise SystemExit(f"Unknown [core] keys: {', '.join(sorted(unknown_core))}")
        if "host" in core:
            val = core["host"]
            if not isinstance(val, str):
                raise SystemExit("Config error: core.host must be a string")
            config.host = val
        if "port" in core:
            val = core["port"]
            if not isinstance(val, int):
                raise SystemExit("Config error: core.port must be an integer")
            config.port = val
        if "ipc_token_file" in core:
            val = core["ipc_token_file"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: core.ipc_token_file must be a non-empty string")
            config.ipc_token_file = val

    if "logging" in data:
        log = data["logging"]
        if not isinstance(log, dict):
            raise SystemExit("Config error: [logging] must be a table")
        unknown_log: set[str] = set(log.keys()) - {"level", "file", "format"}
        if unknown_log:
            raise SystemExit(f"Unknown [logging] keys: {', '.join(sorted(unknown_log))}")
        for key in ("level", "file", "format"):
            if key in log:
                val = log[key]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: logging.{key} must be a string")
                setattr(config.logging, key, val)

    if "agent" in data:
        agent = data["agent"]
        if not isinstance(agent, dict):
            raise SystemExit("Config error: [agent] must be a table")
        unknown_agent: set[str] = set(agent.keys()) - {
            "max_steps",
            "max_step_continues",
            "task_router",
            "delegation_policy",
        }
        if unknown_agent:
            raise SystemExit(f"Unknown [agent] keys: {', '.join(sorted(unknown_agent))}")
        if "max_steps" in agent:
            val = agent["max_steps"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit("Config error: agent.max_steps must be a positive integer")
            config.agent.max_steps = val
        if "max_step_continues" in agent:
            continues = agent["max_step_continues"]
            if not isinstance(continues, int) or continues < 0:
                raise SystemExit(
                    "Config error: agent.max_step_continues must be a non-negative integer"
                )
            config.agent.max_step_continues = continues
        if "task_router" in agent:
            task_router = agent["task_router"]
            if task_router not in {"rules_only", "llm_only", "hybrid"}:
                raise SystemExit(
                    "Config error: agent.task_router must be rules_only, llm_only, or hybrid"
                )
            config.agent.task_router = str(task_router)
        if "delegation_policy" in agent:
            delegation_policy = agent["delegation_policy"]
            if delegation_policy not in {"single", "always_delegate", "routed"}:
                raise SystemExit(
                    "Config error: agent.delegation_policy must be single, "
                    "always_delegate, or routed"
                )
            config.agent.delegation_policy = str(delegation_policy)

    if "llm" in data:
        llm = data["llm"]
        if not isinstance(llm, dict):
            raise SystemExit("Config error: [llm] must be a table")
        unknown_llm: set[str] = set(llm.keys()) - {
            "provider",
            "default_model",
            "router",
            "router_plan_route",
            "router_act_route",
            "router_cost_budget",
            "router_cost_fallback",
            "base_url",
            "api_key_env",
        }
        if unknown_llm:
            raise SystemExit(f"Unknown [llm] keys: {', '.join(sorted(unknown_llm))}")
        if "provider" in llm:
            val = llm["provider"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.provider must be a string")
            config.llm.provider = val
        if "default_model" in llm:
            val = llm["default_model"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.default_model must be a string")
            config.llm.default_model = val
        if "router" in llm:
            val = llm["router"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router must be a string")
            config.llm.router = val
        if "router_plan_route" in llm:
            val = llm["router_plan_route"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router_plan_route must be a string")
            config.llm.router_plan_route = val
        if "router_act_route" in llm:
            val = llm["router_act_route"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router_act_route must be a string")
            config.llm.router_act_route = val
        if "router_cost_budget" in llm:
            val = llm["router_cost_budget"]
            if not isinstance(val, bool) and not isinstance(val, (int, float)):
                raise SystemExit("Config error: llm.router_cost_budget must be a number")
            config.llm.router_cost_budget = float(val)
        if "router_cost_fallback" in llm:
            val = llm["router_cost_fallback"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.router_cost_fallback must be a string")
            config.llm.router_cost_fallback = val
        if "base_url" in llm:
            val = llm["base_url"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.base_url must be a string")
            config.llm.base_url = val
        if "api_key_env" in llm:
            val = llm["api_key_env"]
            if not isinstance(val, str):
                raise SystemExit("Config error: llm.api_key_env must be a string")
            config.llm.api_key_env = val

    if "trace" in data:
        trace = data["trace"]
        if not isinstance(trace, dict):
            raise SystemExit("Config error: [trace] must be a table")
        known_trace = {
            "enabled",
            "file",
            "include_payload",
            "include_llm_payload",
            "max_bytes",
            "backup_count",
        }
        unknown_trace: set[str] = set(trace.keys()) - known_trace
        if unknown_trace:
            raise SystemExit(f"Unknown [trace] keys: {', '.join(sorted(unknown_trace))}")
        if "enabled" in trace:
            val = trace["enabled"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.enabled must be a boolean")
            config.trace.enabled = val
        if "file" in trace:
            val = trace["file"]
            if not isinstance(val, str):
                raise SystemExit("Config error: trace.file must be a string")
            config.trace.file = val
        if "include_llm_payload" in trace:
            val = trace["include_llm_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_llm_payload must be a boolean")
            config.trace.include_llm_payload = val
        if "include_payload" in trace:
            val = trace["include_payload"]
            if not isinstance(val, bool):
                raise SystemExit("Config error: trace.include_payload must be a boolean")
            config.trace.include_payload = val
        if "max_bytes" in trace:
            val = trace["max_bytes"]
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise SystemExit("Config error: trace.max_bytes must be a non-negative integer")
            config.trace.max_bytes = val
        if "backup_count" in trace:
            val = trace["backup_count"]
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise SystemExit("Config error: trace.backup_count must be a non-negative integer")
            config.trace.backup_count = val

    if "permission" in data:
        perm = data["permission"]
        if not isinstance(perm, dict):
            raise SystemExit("Config error: [permission] must be a table")
        unknown_perm: set[str] = set(perm.keys()) - {"timeout_s"}
        if unknown_perm:
            raise SystemExit(f"Unknown [permission] keys: {', '.join(sorted(unknown_perm))}")
        if "timeout_s" in perm:
            val = perm["timeout_s"]
            if not isinstance(val, (int, float)) or val < 0:
                raise SystemExit("Config error: permission.timeout_s must be a non-negative number")
            config.permission.timeout_s = float(val)

    if "api" in data:
        api = data["api"]
        if not isinstance(api, dict):
            raise SystemExit("Config error: [api] must be a table")
        unknown_api: set[str] = set(api.keys()) - {"host", "port"}
        if unknown_api:
            raise SystemExit(f"Unknown [api] keys: {', '.join(sorted(unknown_api))}")
        if "host" in api:
            val = api["host"]
            if not isinstance(val, str) or not val.strip():
                raise SystemExit("Config error: api.host must be a non-empty string")
            config.api.host = val
        if "port" in api:
            val = api["port"]
            if not isinstance(val, int) or isinstance(val, bool) or not (0 <= val <= 65535):
                raise SystemExit("Config error: api.port must be between 0 and 65535")
            config.api.port = val

    if "compaction" in data:
        comp = data["compaction"]
        if not isinstance(comp, dict):
            raise SystemExit("Config error: [compaction] must be a table")
        known_compaction = {
            "strategy",
            "auto_threshold",
            "retain_ratio",
            "tool_result_limit",
            "tool_result_keep",
            "tool_result_summarize_threshold",
        }
        unknown_comp: set[str] = set(comp.keys()) - known_compaction
        if unknown_comp:
            raise SystemExit(f"Unknown [compaction] keys: {', '.join(sorted(unknown_comp))}")
        if "auto_threshold" in comp:
            val = comp["auto_threshold"]
            if not isinstance(val, (int, float)) or not (0.0 <= val <= 1.0):
                raise SystemExit("Config error: compaction.auto_threshold must be between 0 and 1")
            config.compaction.auto_threshold = float(val)
        if "strategy" in comp:
            strategy = comp["strategy"]
            if strategy not in {"truncate", "structured", "adaptive_evidence"}:
                raise SystemExit(
                    "Config error: compaction.strategy must be truncate, structured, "
                    "or adaptive_evidence"
                )
            config.compaction.strategy = str(strategy)
        if "retain_ratio" in comp:
            val = comp["retain_ratio"]
            if not isinstance(val, (int, float)) or not (0.0 < val < 1.0):
                raise SystemExit("Config error: compaction.retain_ratio must be between 0 and 1")
            config.compaction.retain_ratio = float(val)
        if "tool_result_limit" in comp:
            val = comp["tool_result_limit"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_limit must be a positive integer"
                )
            config.compaction.tool_result_limit = val
        if "tool_result_keep" in comp:
            val = comp["tool_result_keep"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_keep must be a positive integer"
                )
            config.compaction.tool_result_keep = val
        if "tool_result_summarize_threshold" in comp:
            val = comp["tool_result_summarize_threshold"]
            if not isinstance(val, int) or val <= 0:
                raise SystemExit(
                    "Config error: compaction.tool_result_summarize_threshold must be a "
                    "positive integer"
                )
            config.compaction.tool_result_summarize_threshold = val

    if "mcp" in data:
        mcp = data["mcp"]
        if not isinstance(mcp, dict):
            raise SystemExit("Config error: [mcp] must be a table")
        unknown_mcp: set[str] = set(mcp.keys()) - {"servers"}
        if unknown_mcp:
            raise SystemExit(f"Unknown [mcp] keys: {', '.join(sorted(unknown_mcp))}")
        servers_raw = mcp.get("servers", [])
        if not isinstance(servers_raw, list):
            raise SystemExit("Config error: mcp.servers must be an array of tables")
        for i, srv in enumerate(servers_raw):
            if not isinstance(srv, dict):
                raise SystemExit(f"Config error: mcp.servers[{i}] must be a table")
            name = srv.get("name")
            if not isinstance(name, str) or not name:
                raise SystemExit(f"Config error: mcp.servers[{i}].name must be a non-empty string")
            transport = srv.get("transport", "stdio")
            if transport not in ("stdio", "tcp", "streamable_http"):
                raise SystemExit(
                    f"Config error: mcp.servers[{i}].transport must be "
                    "'stdio', 'tcp', or 'streamable_http'"
                )
            s = McpServerConfig(name=name, transport=transport)
            if "command" in srv:
                val = srv["command"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].command must be a string")
                s.command = val
            if "args" in srv:
                val = srv["args"]
                if not isinstance(val, list):
                    raise SystemExit(f"Config error: mcp.servers[{i}].args must be an array")
                s.args = [str(a) for a in val]
            if "env" in srv:
                val = srv["env"]
                if not isinstance(val, dict):
                    raise SystemExit(f"Config error: mcp.servers[{i}].env must be a table")
                s.env = {str(k): str(v) for k, v in val.items()}
            if "host" in srv:
                val = srv["host"]
                if not isinstance(val, str):
                    raise SystemExit(f"Config error: mcp.servers[{i}].host must be a string")
                s.host = val
            if "port" in srv:
                val = srv["port"]
                if not isinstance(val, int):
                    raise SystemExit(f"Config error: mcp.servers[{i}].port must be an integer")
                s.port = val
            if "url" in srv:
                val = srv["url"]
                if not isinstance(val, str):
                    raise SystemExit(
                        f"Config error: mcp.servers[{i}].url must be a string"
                    )
                s.url = val
            if "auth_token_env" in srv:
                val = srv["auth_token_env"]
                if not isinstance(val, str):
                    raise SystemExit(
                        f"Config error: mcp.servers[{i}].auth_token_env must be a string"
                    )
                s.auth_token_env = val
            config.mcp.servers.append(s)


# 用可信来源合并后的 CODEROOK_* 环境映射覆盖 config 中对应字段
def _apply_env(config: CodeRookConfig, environ: Mapping[str, str]) -> None:
    host = environ.get("CODEROOK_HOST")
    if host is not None:
        config.host = host

    port_str = environ.get("CODEROOK_PORT")
    if port_str is not None:
        try:
            config.port = int(port_str)
        except ValueError:
            raise SystemExit(f"Config error: CODEROOK_PORT must be an integer, got: {port_str!r}")

    ipc_token_file = environ.get("CODEROOK_IPC_TOKEN_FILE")
    if ipc_token_file is not None:
        if not ipc_token_file.strip():
            raise SystemExit("Config error: CODEROOK_IPC_TOKEN_FILE must not be empty")
        config.ipc_token_file = ipc_token_file

    api_host = environ.get("CODEROOK_API_HOST")
    if api_host is not None:
        if not api_host.strip():
            raise SystemExit("Config error: CODEROOK_API_HOST must not be empty")
        config.api.host = api_host

    api_port = environ.get("CODEROOK_API_PORT")
    if api_port is not None:
        try:
            config.api.port = int(api_port)
            if not (0 <= config.api.port <= 65535):
                raise ValueError
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_API_PORT must be between 0 and 65535, "
                f"got: {api_port!r}"
            ) from None

    api_token = environ.get("CODEROOK_API_TOKEN")
    if api_token is not None:
        if not api_token.strip():
            config.api.token = ""
        elif api_token != api_token.strip() or any(
            character.isspace() for character in api_token
        ):
            raise SystemExit(
                "Config error: CODEROOK_API_TOKEN must not contain whitespace"
            )
        else:
            config.api.token = api_token

    log_level = environ.get("CODEROOK_LOG_LEVEL")
    if log_level is not None:
        config.logging.level = log_level

    log_file = environ.get("CODEROOK_LOG_FILE")
    if log_file is not None:
        config.logging.file = log_file

    log_format = environ.get("CODEROOK_LOG_FORMAT")
    if log_format is not None:
        config.logging.format = log_format

    max_steps_str = environ.get("CODEROOK_MAX_STEPS")
    if max_steps_str is not None:
        try:
            val = int(max_steps_str)
            if val <= 0:
                raise SystemExit(
                    "Config error: CODEROOK_MAX_STEPS must be a positive integer,"
                    f" got: {max_steps_str!r}"
                )
            config.agent.max_steps = val
        except ValueError:
            raise SystemExit(
                f"Config error: CODEROOK_MAX_STEPS must be an integer, got: {max_steps_str!r}"
            )

    task_router = environ.get("CODEROOK_TASK_ROUTER")
    if task_router is not None:
        if task_router not in {"rules_only", "llm_only", "hybrid"}:
            raise SystemExit(
                "Config error: CODEROOK_TASK_ROUTER must be rules_only, llm_only, or hybrid"
            )
        config.agent.task_router = task_router

    delegation_policy = environ.get("CODEROOK_DELEGATION_POLICY")
    if delegation_policy is not None:
        if delegation_policy not in {"single", "always_delegate", "routed"}:
            raise SystemExit(
                "Config error: CODEROOK_DELEGATION_POLICY must be single, "
                "always_delegate, or routed"
            )
        config.agent.delegation_policy = delegation_policy

    default_model = environ.get("CODEROOK_LLM_DEFAULT_MODEL")
    if default_model is not None:
        config.llm.default_model = default_model

    llm_provider = environ.get("CODEROOK_LLM_PROVIDER")
    if llm_provider is not None:
        config.llm.provider = llm_provider

    llm_base_url = environ.get("CODEROOK_LLM_BASE_URL")
    if llm_base_url is not None:
        config.llm.base_url = llm_base_url

    llm_api_key_env = environ.get("CODEROOK_LLM_API_KEY_ENV")
    if llm_api_key_env is not None:
        config.llm.api_key_env = llm_api_key_env

    trace_enabled = environ.get("CODEROOK_TRACE_ENABLED")
    if trace_enabled is not None:
        config.trace.enabled = trace_enabled.lower() not in ("0", "false", "no")

    trace_file = environ.get("CODEROOK_TRACE_FILE")
    if trace_file is not None:
        config.trace.file = trace_file

    trace_payload = environ.get("CODEROOK_TRACE_INCLUDE_LLM_PAYLOAD")
    if trace_payload is not None:
        config.trace.include_llm_payload = trace_payload.lower() not in ("0", "false", "no")

    trace_event_payload = environ.get("CODEROOK_TRACE_INCLUDE_PAYLOAD")
    if trace_event_payload is not None:
        config.trace.include_payload = trace_event_payload.lower() not in ("0", "false", "no")

    trace_max_bytes = environ.get("CODEROOK_TRACE_MAX_BYTES")
    if trace_max_bytes is not None:
        try:
            config.trace.max_bytes = int(trace_max_bytes)
            if config.trace.max_bytes < 0:
                raise ValueError
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_TRACE_MAX_BYTES must be a non-negative integer, "
                f"got: {trace_max_bytes!r}"
            ) from None

    trace_backup_count = environ.get("CODEROOK_TRACE_BACKUP_COUNT")
    if trace_backup_count is not None:
        try:
            config.trace.backup_count = int(trace_backup_count)
            if config.trace.backup_count < 0:
                raise ValueError
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_TRACE_BACKUP_COUNT must be a non-negative integer, "
                f"got: {trace_backup_count!r}"
            ) from None

    perm_timeout = environ.get("CODEROOK_PERMISSION_TIMEOUT_S")
    if perm_timeout is not None:
        try:
            perm_timeout_val = float(perm_timeout)
            if perm_timeout_val < 0:
                raise SystemExit(
                    "Config error: CODEROOK_PERMISSION_TIMEOUT_S must be >= 0, "
                    f"got: {perm_timeout!r}"
                )
            config.permission.timeout_s = perm_timeout_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_PERMISSION_TIMEOUT_S must be a number, "
                f"got: {perm_timeout!r}"
            )

    compact_threshold = environ.get("CODEROOK_COMPACT_THRESHOLD")
    if compact_threshold is not None:
        try:
            compact_threshold_val = float(compact_threshold)
            if not (0.0 <= compact_threshold_val <= 1.0):
                raise SystemExit(
                    "Config error: CODEROOK_COMPACT_THRESHOLD must be between 0 and 1, "
                    f"got: {compact_threshold!r}"
                )
            config.compaction.auto_threshold = compact_threshold_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_THRESHOLD must be a number, "
                f"got: {compact_threshold!r}"
            )

    compact_strategy = environ.get("CODEROOK_COMPACT_STRATEGY")
    if compact_strategy is not None:
        if compact_strategy not in {"truncate", "structured", "adaptive_evidence"}:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_STRATEGY must be truncate, structured, "
                "or adaptive_evidence"
            )
        config.compaction.strategy = compact_strategy

    compact_retain_ratio = environ.get("CODEROOK_COMPACT_RETAIN_RATIO")
    if compact_retain_ratio is not None:
        try:
            compact_retain_ratio_val = float(compact_retain_ratio)
            if not (0.0 < compact_retain_ratio_val < 1.0):
                raise SystemExit(
                    "Config error: CODEROOK_COMPACT_RETAIN_RATIO must be between 0 and 1, "
                    f"got: {compact_retain_ratio!r}"
                )
            config.compaction.retain_ratio = compact_retain_ratio_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_RETAIN_RATIO must be a number, "
                f"got: {compact_retain_ratio!r}"
            )

    compact_tool_limit = environ.get("CODEROOK_COMPACT_TOOL_LIMIT")
    if compact_tool_limit is not None:
        try:
            compact_tool_limit_val = int(compact_tool_limit)
            if compact_tool_limit_val <= 0:
                raise SystemExit(
                    "Config error: CODEROOK_COMPACT_TOOL_LIMIT must be a positive integer, "
                    f"got: {compact_tool_limit!r}"
                )
            config.compaction.tool_result_limit = compact_tool_limit_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_TOOL_LIMIT must be an integer, "
                f"got: {compact_tool_limit!r}"
            )

    compact_tool_keep = environ.get("CODEROOK_COMPACT_TOOL_KEEP")
    if compact_tool_keep is not None:
        try:
            compact_tool_keep_val = int(compact_tool_keep)
            if compact_tool_keep_val <= 0:
                raise SystemExit(
                    "Config error: CODEROOK_COMPACT_TOOL_KEEP must be a positive integer, "
                    f"got: {compact_tool_keep!r}"
                )
            config.compaction.tool_result_keep = compact_tool_keep_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_TOOL_KEEP must be an integer, "
                f"got: {compact_tool_keep!r}"
            )

    compact_tool_summary = environ.get("CODEROOK_COMPACT_TOOL_SUMMARY_THRESHOLD")
    if compact_tool_summary is not None:
        try:
            compact_tool_summary_val = int(compact_tool_summary)
            if compact_tool_summary_val <= 0:
                raise SystemExit(
                    "Config error: CODEROOK_COMPACT_TOOL_SUMMARY_THRESHOLD must be positive, "
                    f"got: {compact_tool_summary!r}"
                )
            config.compaction.tool_result_summarize_threshold = compact_tool_summary_val
        except ValueError:
            raise SystemExit(
                "Config error: CODEROOK_COMPACT_TOOL_SUMMARY_THRESHOLD must be an integer, "
                f"got: {compact_tool_summary!r}"
            )
