from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sys
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from pydantic import BaseModel

from code_rook.core.agent_runtime.messages import from_provider, to_provider
from code_rook.core.agent_runtime.providers import ExtensionProvider
from code_rook.core.events.bus import EventBus
from code_rook.core.interaction import ExtensionMessageSender, UserMessageContent
from code_rook.core.llm.routes import ThinkingLevel
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.processes import terminate_process_tree
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class StartupResult:
    system_prompt: str
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ExtensionCommand:
    name: str
    description: str
    handler: Callable[[str], Any]


class ExtensionAPI:
    # 给用户显式加载的 Python 扩展提供本轮工具注册和清理入口
    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.tools: list[BaseTool] = []
        self.cleanup: list[Callable[[], Any]] = []
        self.handlers: list[tuple[str, Callable[[dict[str, Any]], Any]]] = []
        self.closed = False
        self.registry: ToolRegistry | None = None
        self.message_sender: ExtensionMessageSender | None = None
        self.custom_message_sender: Callable[
            [dict[str, Any], bool | None, Literal["steer", "follow_up", "next_turn"] | None],
            Any,
        ] | None = None
        self.notification_sender: Callable[
            [str, Literal["info", "warning", "error"]], Any
        ] | None = None
        self.active_selection: list[str] | None = None
        self.commands: list[ExtensionCommand] = []
        self.providers: dict[str, ExtensionProvider] = {}
        self.model_setter: Callable[[str, str], Any] | None = None
        self.model_getter: Callable[[], dict[str, Any] | None] | None = None
        self.thinking_getter: Callable[[], ThinkingLevel] | None = None
        self.thinking_setter: Callable[[ThinkingLevel], Any] | None = None
        self.entry_appender: Callable[[str, Any], Any] | None = None
        self.session_name_getter: Callable[[], str | None] | None = None
        self.session_name_setter: Callable[[str], Any] | None = None
        self.entry_label_setter: Callable[[str, str | None], Any] | None = None
        self.idle_getter: Callable[[], bool] | None = None
        self.idle_waiter: Callable[[], Any] | None = None
        self.run_aborter: Callable[[], Any] | None = None
        self.compaction_requester: Callable[[str], Any] | None = None
        self.resource_reloader: Callable[[], Any] | None = None
        self.question_asker: Callable[[str, str, list[str], bool], Any] | None = None
        self._system_prompt = ""
        self._context_usage: dict[str, int | float | None] | None = None
        self._current_extension_path: Path | None = None
        self._handler_origins: dict[int, Path] = {}
        self._tool_registrar: Callable[[BaseTool], None] | None = None
        self._command_registrar: Callable[[ExtensionCommand], None] | None = None

    # 注册由用户显式执行的斜杠命令，处理器通过当前 API 访问会话能力
    def register_command(
        self, name: str, handler: Callable[[str], Any], *, description: str = "",
    ) -> None:
        self._check_active()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
            raise ValueError("Extension command name must contain letters, digits, _ or -")
        command = ExtensionCommand(name, description, handler)
        self.commands.append(command)
        if self._command_registrar is not None:
            self._command_registrar(command)

    # 返回当前扩展注册的斜杠命令目录，供命令面板和扩展自省复用。
    def get_commands(self) -> list[dict[str, str]]:
        self._check_active()
        return [
            {"name": command.name, "description": command.description}
            for command in self.commands
        ]

    # 向所属会话发送文本或图片，空闲时启动任务，运行中按纠偏或后续方式交付
    async def send_user_message(
        self, content: UserMessageContent, *,
        deliver_as: Literal["steer", "follow_up"] = "follow_up",
        expand_prompt_templates: bool = False,
    ) -> None:
        self._check_active()
        if not isinstance(content, (str, list)) or not content:
            raise ValueError("Extension message must contain text or image blocks")
        if deliver_as not in {"steer", "follow_up"}:
            raise ValueError("deliver_as must be steer or follow_up")
        if self.message_sender is None:
            raise RuntimeError("Extension messaging requires a session-owned host")
        await self.message_sender(deepcopy(content), deliver_as, expand_prompt_templates)

    # 向会话发送带扩展来源的模型消息，支持当前轮纠偏、后续轮和仅追加语义。
    async def send_message(
        self,
        message: dict[str, Any],
        *,
        trigger_turn: bool | None = None,
        deliver_as: Literal["steer", "follow_up", "next_turn"] | None = None,
    ) -> None:
        self._check_active()
        custom_type = message.get("custom_type", message.get("customType"))
        if not isinstance(custom_type, str) or not custom_type.strip():
            raise ValueError("Custom extension messages require a custom_type")
        content = message.get("content", [])
        if content is None:
            content = []
        if not isinstance(content, (str, list)):
            raise ValueError("Custom extension message content must be text or content blocks")
        if isinstance(content, list) and any(not isinstance(block, dict) for block in content):
            raise ValueError("Custom extension message blocks must be objects")
        display = message.get("display", False)
        if not isinstance(display, bool):
            raise ValueError("Custom extension message display must be a boolean")
        if trigger_turn is not None and not isinstance(trigger_turn, bool):
            raise ValueError("trigger_turn must be a boolean or omitted")
        if deliver_as not in {None, "steer", "follow_up", "next_turn"}:
            raise ValueError("deliver_as must be steer, follow_up, next_turn, or omitted")
        if self.custom_message_sender is None:
            raise RuntimeError("Custom extension messaging requires a session-owned host")
        payload = {
            "role": "custom",
            "customType": custom_type.strip(),
            "content": deepcopy(content),
            "display": display,
            "details": deepcopy(message.get("details")),
        }
        result = self.custom_message_sender(payload, trigger_turn, deliver_as)
        if inspect.isawaitable(result):
            await result

    # 将扩展通知作为可见的持久自定义消息追加到当前会话。
    async def notify(
        self,
        message: str,
        *,
        notification_type: Literal["info", "warning", "error"] = "info",
    ) -> None:
        self._check_active()
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Extension notification must contain text")
        if notification_type not in {"info", "warning", "error"}:
            raise ValueError("notification_type must be info, warning, or error")
        if self.notification_sender is None:
            raise RuntimeError("Extension notifications require a session-owned host")
        result = self.notification_sender(message, notification_type)
        if inspect.isawaitable(result):
            await result

    # 获取运行时工具目录，装载阶段尚未绑定时给出明确提示
    def _runtime_registry(self) -> ToolRegistry:
        self._check_active()
        if self.registry is None:
            raise RuntimeError("Tools are available after extension setup")
        return self.registry

    # 查询工具名称和说明，供启动钩子决定实际工具集
    def get_all_tools(self) -> list[dict[str, str]]:
        return self._runtime_registry().all_tools()

    # 返回经过当前模式与权限裁剪后的活动工具名称
    def get_active_tools(self) -> list[str]:
        return [str(schema["name"]) for schema in self._runtime_registry().tool_schemas()]

    # 设置后续请求的活动工具集合，空列表明确禁用全部工具
    def set_active_tools(self, names: list[str]) -> None:
        self._runtime_registry().set_active_tools(frozenset(names))
        self.active_selection = list(names)

    # 注册遵守现有 schema、权限和执行协议的工具
    def register_tool(self, tool: BaseTool) -> None:
        self._check_active()
        self.tools.append(tool)
        if self._tool_registrar is not None:
            self._tool_registrar(tool)

    # 注册或覆盖当前会话可用的 Pi 风格 Provider 配置。
    def register_provider(self, name: str, config: dict[str, Any]) -> None:
        self._check_active()
        self.providers[name] = ExtensionProvider.from_config(name, config)

    # 移除当前会话内由 Python 扩展注册的 Provider。
    def unregister_provider(self, name: str) -> None:
        self._check_active()
        self.providers.pop(name, None)

    # 返回扩展 Provider 的模型目录，供命令或前端构建选择器。
    def get_registered_providers(self) -> list[dict[str, Any]]:
        self._check_active()
        return [
            {"id": provider.id, "name": provider.name, "models": provider.model_ids()}
            for provider in self.providers.values()
        ]

    # 将扩展 Provider/模型设为当前会话后续 Turn 的选择。
    async def set_model(self, provider: str, model: str = "") -> bool:
        self._check_active()
        registration = self.providers.get(provider)
        selected_model = model
        if registration is not None:
            selected_model = registration.resolve(model).route.model
        if self.model_setter is None:
            raise RuntimeError("Model selection requires a session-owned host")
        result = self.model_setter(provider, selected_model)
        if inspect.isawaitable(result):
            await result
        return True

    # 返回当前会话选择的 Provider 与模型摘要，不暴露凭据或请求头。
    def get_model(self) -> dict[str, Any] | None:
        self._check_active()
        if self.model_getter is None:
            raise RuntimeError("Model inspection requires a session-owned host")
        return deepcopy(self.model_getter())

    # 返回当前会话思考强度，未绑定会话时给出明确错误。
    def get_thinking_level(self) -> ThinkingLevel:
        self._check_active()
        if self.thinking_getter is None:
            raise RuntimeError("Thinking selection requires a session-owned host")
        return self.thinking_getter()

    # 持久切换当前会话思考强度，后续请求沿用而不修改其他会话。
    async def set_thinking_level(self, level: ThinkingLevel) -> None:
        self._check_active()
        if level not in {"off", "low", "medium", "high"}:
            raise ValueError("thinking level must be off, low, medium, or high")
        if self.thinking_setter is None:
            raise RuntimeError("Thinking selection requires a session-owned host")
        result = self.thinking_setter(level)
        if inspect.isawaitable(result):
            await result

    # 追加只供扩展恢复状态的会话条目，不把内容送进模型上下文。
    def append_entry(self, custom_type: str, data: Any = None) -> int:
        self._check_active()
        normalized = custom_type.strip()
        if not normalized:
            raise ValueError("custom_type must not be blank")
        if self.entry_appender is None:
            raise RuntimeError("Session entries require a session-owned host")
        result = self.entry_appender(normalized, deepcopy(data))
        if not isinstance(result, int):
            raise TypeError("Session entry appender must return a ledger sequence")
        return result

    # 返回当前会话显示名称；尚未设置标题时返回空值。
    def get_session_name(self) -> str | None:
        self._check_active()
        if self.session_name_getter is None:
            raise RuntimeError("Session metadata requires a session-owned host")
        return self.session_name_getter()

    # 修改当前会话显示名称并复用现有持久化和前端通知链路。
    async def set_session_name(self, name: str) -> None:
        self._check_active()
        normalized = name.strip()
        if not normalized:
            raise ValueError("Session name must not be blank")
        if self.session_name_setter is None:
            raise RuntimeError("Session metadata requires a session-owned host")
        result = self.session_name_setter(normalized)
        if inspect.isawaitable(result):
            await result

    # 为账本条目设置或清除用户书签标签，目标使用稳定 ledger seq 字符串。
    def set_label(self, entry_id: str, label: str | None) -> None:
        self._check_active()
        normalized_id = entry_id.strip()
        if not normalized_id:
            raise ValueError("entry_id must not be blank")
        normalized_label = label.strip() if isinstance(label, str) else None
        if self.entry_label_setter is None:
            raise RuntimeError("Session labels require a session-owned host")
        self.entry_label_setter(normalized_id, normalized_label or None)

    # 返回最近一次可解释的上下文占用快照，尚未绑定模型时返回空值。
    def get_context_usage(self) -> dict[str, int | float | None] | None:
        self._check_active()
        return deepcopy(self._context_usage)

    # 返回当前运行实际组装后的 System Prompt，运行前尚不可用时返回空字符串。
    def get_system_prompt(self) -> str:
        self._check_active()
        return self._system_prompt

    # 返回所属会话当前是否没有运行中的 Agent 任务。
    def is_idle(self) -> bool:
        self._check_active()
        if self.idle_getter is None:
            raise RuntimeError("Idle state requires a session-owned host")
        return self.idle_getter()

    # 等待所属会话的当前任务完整安定，空闲时立即返回。
    async def wait_for_idle(self) -> None:
        self._check_active()
        if self.idle_waiter is None:
            raise RuntimeError("Idle waiting requires a session-owned host")
        result = self.idle_waiter()
        if inspect.isawaitable(result):
            await result

    # 取消所属会话当前任务；已经空闲时返回 false。
    async def abort(self) -> bool:
        self._check_active()
        if self.run_aborter is None:
            raise RuntimeError("Run cancellation requires a session-owned host")
        result = self.run_aborter()
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    # 使用当前会话 Provider 执行一次正式上下文压缩。
    async def compact(self, focus: str = "") -> Any:
        self._check_active()
        if self.compaction_requester is None:
            raise RuntimeError("Compaction requires a session-owned host")
        result = self.compaction_requester(focus)
        return await result if inspect.isawaitable(result) else result

    # 重新加载当前会话的 Python 扩展、Skill 与 Prompt 资源。
    async def reload(self) -> Any:
        self._check_active()
        if self.resource_reloader is None:
            raise RuntimeError("Resource reload requires a session-owned host")
        result = self.resource_reloader()
        return await result if inspect.isawaitable(result) else result

    # 显示单选问题并返回用户选择，复用 TUI/Web 的统一问题卡。
    async def select(self, title: str, options: list[str]) -> str | None:
        self._check_active()
        normalized = [option.strip() for option in options if option.strip()]
        if not title.strip() or not normalized:
            raise ValueError("select requires a title and at least one option")
        return await self._ask_question(title.strip(), "Select", normalized, False)

    # 显示确认问题并把 Yes/No 答案转换为布尔值。
    async def confirm(self, title: str, message: str) -> bool:
        self._check_active()
        question = message.strip() or title.strip()
        answer = await self._ask_question(
            question, title.strip() or "Confirm", ["Yes", "No"], False,
        )
        return answer is not None and answer.strip().casefold() == "yes"

    # 显示自由输入问题，placeholder 作为非强制提示附在问题后。
    async def input(self, title: str, placeholder: str = "") -> str | None:
        self._check_active()
        question = title.strip()
        if not question:
            raise ValueError("input requires a title")
        if placeholder.strip():
            question += f"\n{placeholder.strip()}"
        return await self._ask_question(question, "Input", [], False)

    # 调用会话绑定的问题服务并保留取消信号。
    async def _ask_question(
        self, question: str, header: str, options: list[str], multi_select: bool,
    ) -> str | None:
        if self.question_asker is None:
            raise RuntimeError("Interactive prompts require a session-owned host")
        result = self.question_asker(question, header[:40], options, multi_select)
        answer = await result if inspect.isawaitable(result) else result
        return str(answer) if answer is not None else None

    # 更新扩展自省所需的有效 Prompt 和上下文占用，不暴露可变运行对象。
    def update_runtime_context(
        self, *, system_prompt: str | None = None, tokens: int | None = None,
        context_window: int | None = None,
    ) -> None:
        self._check_active()
        if system_prompt is not None:
            self._system_prompt = system_prompt
        if type(context_window) is not int or context_window <= 0:
            return
        self._context_usage = {
            "tokens": tokens,
            "contextWindow": context_window,
            "percent": None if tokens is None else tokens / context_window * 100,
        }

    # 以参数数组执行扩展子进程，分别返回输出、退出码与取消状态。
    async def exec(
        self, command: str, args: list[str], *, cwd: str | Path | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, str | int | bool]:
        self._check_active()
        if not command.strip():
            raise ValueError("command must not be blank")
        if any(not isinstance(argument, str) for argument in args):
            raise TypeError("command arguments must be strings")
        if timeout_ms is not None and timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        working_directory = Path(cwd).expanduser() if cwd is not None else self.workspace
        if not working_directory.is_absolute():
            working_directory = self.workspace / working_directory
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=working_directory,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        killed = False
        try:
            async with asyncio.timeout(None if timeout_ms is None else timeout_ms / 1000):
                stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            killed = True
            await asyncio.shield(terminate_process_tree(process))
            await asyncio.shield(process.communicate())
            raise
        except TimeoutError:
            killed = True
            await asyncio.shield(terminate_process_tree(process))
            stdout, stderr = await asyncio.shield(process.communicate())
        return {
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "code": process.returncode if process.returncode is not None else 1,
            "killed": killed,
        }

    # 注册结束或加载失败时执行的资源清理回调，允许异步函数
    def on_shutdown(self, callback: Callable[[], Any]) -> None:
        self._check_active()
        self.cleanup.append(callback)

    # 注册本轮事件观察者并返回可重复调用的撤销函数
    def on(self, event_type: str, callback: Callable[[dict[str, Any]], Any]) -> Callable[[], None]:
        self._check_active()
        registration = (event_type, callback)
        self.handlers.append(registration)
        if self._current_extension_path is not None:
            self._handler_origins[id(callback)] = self._current_extension_path

        # 撤销当前注册而不影响相同事件的其他监听者
        def unsubscribe() -> None:
            if registration in self.handlers:
                self.handlers.remove(registration)
            self._handler_origins.pop(id(callback), None)

        return unsubscribe

    # 拒绝扩展使用已经结束的本轮 API 注册新资源
    def _check_active(self) -> None:
        if self.closed:
            raise RuntimeError("Extension run has ended")


class ExtensionHost:
    # 只接收用户配置中的明确文件路径，不搜索或自动执行仓库代码
    def __init__(self, paths: list[str], workspace: Path, run_id: str) -> None:
        self.paths = tuple(workspace / Path(path).expanduser() for path in paths)
        self.api = ExtensionAPI(workspace, run_id)
        self.api._tool_registrar = self._register_dynamic_tool
        self.api._command_registrar = self._register_dynamic_command
        self.modules: list[str] = []
        self.bus: EventBus | None = None
        self.registry: ToolRegistry | None = None
        self.tool_disposers: list[Callable[[], None]] = []
        self.loaded = False
        self.selected_tools: dict[str, BaseTool] = {}
        self.commands: dict[str, ExtensionCommand] = {}
        self._turn_index = 0

    # 让扩展在启动后注册的工具立即进入当前及后续请求目录。
    def _register_dynamic_tool(self, tool: BaseTool) -> None:
        if not self.loaded:
            return
        self.selected_tools[tool.name] = tool
        if self.registry is None:
            return
        self.tool_disposers.append(self.registry.register_scoped(tool))
        self.registry.extend_model_surface(frozenset({tool.name}))

    # 让扩展在启动后注册的命令立即进入当前会话命令目录。
    def _register_dynamic_command(self, command: ExtensionCommand) -> None:
        if self.loaded:
            self.commands[command.name] = command

    # 将原生 Agent 循环事件按 Pi 的轮次语义分发给扩展处理器
    async def emit_agent_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.api._check_active()
        kind = str(event.get("type", ""))
        emitted = deepcopy(event)
        emitted["run_id"] = self.api.run_id
        if kind == "agent_start":
            self._turn_index = 0
        elif kind == "turn_start":
            emitted["turnIndex"] = self._turn_index
            emitted["timestamp"] = int(datetime.now(UTC).timestamp() * 1000)
        elif kind == "turn_end":
            emitted["turnIndex"] = self._turn_index
        elif kind == "message_end" and emitted.get("message", {}).get("role") == "assistant":
            message = emitted["message"]
            usage = message.get("usage")
            context_window = message.get("contextWindow")
            if isinstance(usage, dict) and isinstance(context_window, int):
                tokens = sum(
                    int(usage.get(key, 0) or 0)
                    for key in (
                        "input_tokens", "output_tokens", "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                    )
                )
                self.api.update_runtime_context(tokens=tokens, context_window=context_window)
        for event_type, callback in tuple(self.api.handlers):
            if event_type != kind:
                continue
            try:
                result = callback(deepcopy(emitted))
                if inspect.isawaitable(result):
                    result = await result
                if kind == "message_end" and result is not None and "message" in result:
                    replacement = result["message"]
                    if not isinstance(replacement, dict):
                        raise TypeError("message_end replacement must be a message object")
                    if replacement.get("role") != emitted["message"].get("role"):
                        raise ValueError("message_end replacement must keep the original role")
                    emitted["message"] = deepcopy(replacement)
            except Exception:
                logger.exception(
                    "Python extension lifecycle hook failed event=%s run_id=%s",
                    kind, self.api.run_id,
                )
        if kind == "turn_end":
            self._turn_index += 1
        return emitted

    # 分发会话级事件；before 类事件可返回 cancel 阻止对应操作
    async def emit_session_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        self.api._check_active()
        kind = str(event.get("type", ""))
        emitted = {**deepcopy(event), "run_id": self.api.run_id}
        decision: dict[str, Any] | None = None
        for event_type, callback in tuple(self.api.handlers):
            if event_type != kind:
                continue
            try:
                result = callback(deepcopy(emitted))
                if inspect.isawaitable(result):
                    result = await result
                if kind.startswith("session_before_") and isinstance(result, dict):
                    decision = deepcopy(result)
                    if result.get("cancel") is True:
                        return decision
            except Exception:
                logger.exception(
                    "Python extension session hook failed event=%s run_id=%s",
                    kind, self.api.run_id,
                )
        return decision

    # 每个扩展实例只执行一次 setup，保留模块闭包和工具实例供后续运行复用
    async def initialize(self) -> None:
        self.api._check_active()
        if self.loaded:
            return
        selected = {tool.name: tool for tool in self.api.tools}
        commands = {command.name: command for command in self.api.commands}
        for path in dict.fromkeys(path.resolve() for path in self.paths):
            name = f"_coderook_extension_{uuid.uuid4().hex}"
            module = ModuleType(name)
            module.__file__ = str(path)
            sys.modules[name] = module
            self.modules.append(name)
            exec(compile(path.read_text(encoding="utf-8-sig"), str(path), "exec"), module.__dict__)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise ValueError(f"Python extension must define setup(api): {path}")
            tool_start = len(self.api.tools)
            command_start = len(self.api.commands)
            self.api._current_extension_path = path
            try:
                result = setup(self.api)
                if inspect.isawaitable(result):
                    await result
            finally:
                self.api._current_extension_path = None
            local_tools = {tool.name: tool for tool in self.api.tools[tool_start:]}
            for name, tool in local_tools.items():
                selected.setdefault(name, tool)
            for name, command in {
                item.name: item for item in self.api.commands[command_start:]
            }.items():
                commands.setdefault(name, command)
        self.selected_tools = selected
        self.commands = commands
        self.loaded = True

    # 聚合扩展声明的 Skill、Prompt 与主题路径，并相对各扩展源码解析
    async def discover_resources(
        self, reason: Literal["startup", "reload"] = "startup",
    ) -> dict[str, tuple[Path, ...]]:
        self.api._check_active()
        collected: dict[str, list[Path]] = {
            "skill_paths": [], "prompt_paths": [], "theme_paths": [],
        }
        keys = {
            "skillPaths": "skill_paths",
            "promptPaths": "prompt_paths",
            "themePaths": "theme_paths",
        }
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "resources_discover":
                continue
            try:
                result = callback({
                    "type": "resources_discover", "cwd": str(self.api.workspace),
                    "reason": reason,
                })
                if inspect.isawaitable(result):
                    result = await result
                if result is None:
                    continue
                if not isinstance(result, dict):
                    raise TypeError("resources_discover must return an object")
                origin = self.api._handler_origins.get(id(callback))
                base = origin.parent if origin is not None else self.api.workspace
                for public_key, internal_key in keys.items():
                    values = result.get(public_key, [])
                    if not isinstance(values, list) or any(
                        not isinstance(value, str) for value in values
                    ):
                        raise TypeError(f"{public_key} must be a list of paths")
                    for value in values:
                        path = Path(value).expanduser()
                        collected[internal_key].append(
                            (path if path.is_absolute() else base / path).resolve()
                        )
            except Exception:
                logger.exception(
                    "Python extension resource discovery failed run_id=%s", self.api.run_id,
                )
        return {
            key: tuple(dict.fromkeys(paths)) for key, paths in collected.items()
        }

    # 让扩展在用户 Shell 执行前接管命令，并采用首个有效完整结果
    async def emit_user_bash(
        self, command: str, *, exclude_from_context: bool,
    ) -> dict[str, Any] | None:
        self.api._check_active()
        event = {
            "type": "user_bash",
            "command": command,
            "excludeFromContext": exclude_from_context,
            "cwd": str(self.api.workspace),
        }
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "user_bash":
                continue
            try:
                response = callback(deepcopy(event))
                if inspect.isawaitable(response):
                    response = await response
                if response is None:
                    continue
                if not isinstance(response, dict):
                    raise TypeError("user_bash must return an object")
                result = response.get("result")
                if result is None:
                    continue
                if not isinstance(result, dict):
                    raise TypeError("user_bash result must be an object")
                output = result.get("output", "")
                exit_code = result.get("exitCode")
                cancelled = result.get("cancelled", False)
                truncated = result.get("truncated", False)
                full_output_path = result.get("fullOutputPath")
                if not isinstance(output, str):
                    raise TypeError("user_bash result.output must be text")
                if exit_code is not None and (
                    not isinstance(exit_code, int) or isinstance(exit_code, bool)
                ):
                    raise TypeError("user_bash result.exitCode must be an integer or null")
                if not isinstance(cancelled, bool) or not isinstance(truncated, bool):
                    raise TypeError("user_bash result flags must be booleans")
                if full_output_path is not None and not isinstance(full_output_path, str):
                    raise TypeError("user_bash result.fullOutputPath must be text or null")
                return {
                    "output": output,
                    "exit_code": exit_code,
                    "cancelled": cancelled,
                    "truncated": truncated,
                    "full_output_path": full_output_path,
                }
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Python extension user bash hook failed run_id=%s", self.api.run_id,
                )
        return None

    # 执行已注册的用户命令，不经过模型工具调用或意图分类
    async def execute_command(self, name: str, arguments: str) -> str:
        self.api._check_active()
        command = self.commands.get(name)
        if command is None:
            raise ValueError(f"Unknown extension command: {name}")
        result = command.handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if result is not None and not isinstance(result, str):
            raise TypeError("Extension command must return text or None")
        return result or ""

    # 将会话扩展重新绑定到当前运行的工具目录，不重复执行模块 setup
    async def load(self, registry: ToolRegistry, bus: EventBus | None = None) -> None:
        self.api._check_active()
        if self.registry is not None:
            raise RuntimeError("Extension host is already bound to a run")
        if not self.loaded:
            try:
                await self.initialize()
            except BaseException:
                await self.close()
                raise
        for tool in self.selected_tools.values():
            self.tool_disposers.append(registry.register_scoped(tool))
        registry.extend_model_surface(frozenset(self.selected_tools))
        self.registry = registry
        self.api.registry = registry
        if self.api.active_selection is not None:
            registry.set_active_tools(frozenset(self.api.active_selection))
        registry.before_tool_call = self.before_tool_call
        registry.after_tool_call = self.after_tool_call
        self.bus = bus
        if bus is not None:
            bus.subscribe(self._observe)

    # 按注册顺序让启动钩子修改基础系统提示，后一钩子看到前一钩子的结果
    async def before_agent_start(
        self, prompt: str, system_prompt: str, images: list[dict[str, Any]],
    ) -> StartupResult:
        self.api._check_active()
        current = system_prompt
        messages: list[dict[str, Any]] = []
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "before_agent_start":
                continue
            try:
                result = callback({
                    "type": "before_agent_start", "prompt": prompt,
                    "system_prompt": current, "images": deepcopy(images),
                    "run_id": self.api.run_id,
                })
                if inspect.isawaitable(result):
                    result = await result
                if result is not None and "system_prompt" in result:
                    if not isinstance(result["system_prompt"], str):
                        raise TypeError("before_agent_start system_prompt must be a string")
                    current = result["system_prompt"]
                if result is not None and result.get("message") is not None:
                    message = deepcopy(result["message"])
                    if not isinstance(message, dict):
                        raise TypeError("before_agent_start message must be an object")
                    if not isinstance(message.get("custom_type"), str):
                        raise TypeError("extension message requires custom_type")
                    content = message.get("content")
                    if content is None:
                        message["content"] = []
                    elif not isinstance(content, (str, list)):
                        raise TypeError("extension message content must be text or content blocks")
                    messages.append(message)
            except Exception:
                logger.exception("Python extension startup hook failed run_id=%s", self.api.run_id)
        return StartupResult(current, messages)

    # 按注册顺序处理输入，改写结果传给后续处理器，接管则立即停止
    async def emit_input(
        self, text: str, images: list[dict[str, Any]] | None = None, *,
        source: Literal["interactive", "rpc", "extension"] = "interactive",
        streaming_behavior: Literal["steer", "follow_up"] | None = None,
    ) -> dict[str, Any]:
        self.api._check_active()
        current_text, current_images = text, deepcopy(images)
        transformed = False
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "input":
                continue
            try:
                result = callback({
                    "type": "input", "text": current_text, "images": deepcopy(current_images),
                    "source": source, "streaming_behavior": streaming_behavior,
                })
                if inspect.isawaitable(result):
                    result = await result
                if result is None or result.get("action") == "continue":
                    continue
                if result.get("action") == "handled":
                    return {"action": "handled"}
                if result.get("action") != "transform" or not isinstance(result.get("text"), str):
                    raise TypeError("input result must be continue, handled or transform with text")
                replacement = result.get("images")
                if replacement is not None and (
                    not isinstance(replacement, list)
                    or any(not isinstance(image, dict) for image in replacement)
                ):
                    raise TypeError("input images must be a list of image blocks")
                current_text = result["text"]
                if replacement is not None:
                    current_images = deepcopy(replacement)
                transformed = True
            except Exception:
                logger.exception("Python extension input hook failed run_id=%s", self.api.run_id)
        return (
            {"action": "transform", "text": current_text, "images": current_images}
            if transformed else {"action": "continue"}
        )

    # 为每次模型请求链式变换上下文副本，不修改会话中的原始消息
    async def transform_context(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.api._check_active()
        handlers = [callback for event_type, callback in tuple(self.api.handlers)
                    if event_type == "context"]
        if not handlers:
            return deepcopy(messages)
        current = from_provider(messages)
        for callback in handlers:
            try:
                result = callback({
                    "type": "context", "messages": current, "run_id": self.api.run_id,
                })
                if inspect.isawaitable(result):
                    result = await result
                if result is not None and "messages" in result:
                    replacement = result["messages"]
                    if not isinstance(replacement, list) or any(
                        not isinstance(message, dict) or "role" not in message
                        for message in replacement
                    ):
                        raise TypeError("context messages must be a list of message objects")
                    current = deepcopy(replacement)
            except Exception:
                logger.exception("Python extension context hook failed run_id=%s", self.api.run_id)
        return to_provider(current)

    # 在冻结请求快照前链式应用扩展的 Provider 请求变换。
    async def transform_provider_request(
        self, request: dict[str, Any],
    ) -> dict[str, Any]:
        self.api._check_active()
        current = deepcopy(request)
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "before_provider_request":
                continue
            try:
                result = callback({
                    "type": "before_provider_request",
                    "payload": deepcopy(current),
                    "run_id": self.api.run_id,
                })
                if inspect.isawaitable(result):
                    result = await result
                if result is None:
                    continue
                if not isinstance(result, dict):
                    raise TypeError("before_provider_request must return an object")
                replacement = result.get("payload", result)
                if not isinstance(replacement, dict):
                    raise TypeError("before_provider_request payload must be an object")
                current = deepcopy(replacement)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Python extension provider request hook failed run_id=%s",
                    self.api.run_id,
                )
        return current

    # 在 Provider 返回统一响应后向扩展发布只读观察事件。
    async def observe_provider_response(self, response: LlmResponse) -> None:
        self.api._check_active()
        event = {
            "type": "after_provider_response",
            "response": asdict(response),
            "run_id": self.api.run_id,
        }
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "after_provider_response":
                continue
            try:
                result = callback(deepcopy(event))
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Python extension provider response hook failed run_id=%s",
                    self.api.run_id,
                )

    # 工具调用钩子按顺序执行，阻止或异常均不允许底层工具继续运行
    async def before_tool_call(self, call: ToolCallBlock) -> ToolResult | None:
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "tool_call":
                continue
            result = callback({
                "type": "tool_call", "tool_name": call.name, "tool_call_id": call.id,
                "input": deepcopy(call.input), "run_id": self.api.run_id,
            })
            if inspect.isawaitable(result):
                result = await result
            if result and result.get("block"):
                return ToolResult(
                    str(result.get("reason") or "Tool call blocked by extension"),
                    is_error=True, error_type="hook_denied",
                    terminate=result.get("terminate") is True,
                )
        return None

    # 结果钩子链式修改模型内容；错误记录后继续其他钩子，保留原执行证据
    async def after_tool_call(self, call: ToolCallBlock, result: ToolResult) -> ToolResult:
        current = deepcopy(result)
        for event_type, callback in tuple(self.api.handlers):
            if event_type != "tool_result":
                continue
            try:
                change = callback({
                    "type": "tool_result", "tool_name": call.name, "tool_call_id": call.id,
                    "input": deepcopy(call.input), "run_id": self.api.run_id,
                    "content": current.content, "images": deepcopy(current.images),
                    "is_error": current.is_error,
                    "details": deepcopy(current.details),
                })
                if inspect.isawaitable(change):
                    change = await change
                if change is None:
                    continue
                candidate = deepcopy(current)
                if "terminate" in change:
                    if not isinstance(change["terminate"], bool):
                        raise TypeError("tool_result terminate must be a bool")
                    candidate.terminate = change["terminate"]
                if "details" in change:
                    if change["details"] is not None and not isinstance(change["details"], dict):
                        raise TypeError("tool_result details must be an object or null")
                    candidate.details = deepcopy(change["details"])
                if "content" in change:
                    if not isinstance(change["content"], str):
                        raise TypeError("tool_result content must be a string")
                    candidate.content = change["content"]
                if "images" in change:
                    images = change["images"]
                    if images is not None and (
                        not isinstance(images, list)
                        or any(not isinstance(image, dict) or image.get("type") != "image"
                               for image in images)
                    ):
                        raise TypeError("tool_result images must be image blocks or null")
                    candidate.images = deepcopy(images)
                if "is_error" in change:
                    if not isinstance(change["is_error"], bool):
                        raise TypeError("tool_result is_error must be a bool")
                    candidate.is_error = change["is_error"]
                    if not candidate.is_error:
                        candidate.error_type = None
                current = candidate
            except Exception:
                logger.exception("Python extension result hook failed run_id=%s", self.api.run_id)
        return current

    # 将本轮已发布事件复制给每个观察者，不共享可变事件内容
    async def _observe(self, event: BaseModel) -> None:
        if self.api.closed or getattr(event, "run_id", None) != self.api.run_id:
            return
        for pattern, callback in tuple(self.api.handlers):
            if not fnmatchcase(str(getattr(event, "type", "")), pattern):
                continue
            try:
                result = callback(event.model_dump(mode="json"))
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("Python extension event failed run_id=%s", self.api.run_id)

    # 解绑本轮目录与观察者，但保留会话拥有的扩展代码和状态
    async def unbind(self) -> None:
        for dispose in reversed(self.tool_disposers):
            dispose()
        self.tool_disposers.clear()
        if self.registry is not None:
            self.registry.set_active_tools(None)
            self.registry.before_tool_call = None
            self.registry.after_tool_call = None
            self.registry = None
        self.api.registry = None
        if self.bus is not None:
            self.bus.unsubscribe(self._observe)
            self.bus = None

    # 逆序释放扩展资源，单个清理失败不跳过其他扩展的释放
    async def close(self) -> None:
        self.api.closed = True
        self.api.message_sender = None
        self.api.custom_message_sender = None
        self.api.notification_sender = None
        self.api.model_setter = None
        self.api.model_getter = None
        self.api.thinking_getter = None
        self.api.thinking_setter = None
        self.api.entry_appender = None
        self.api.session_name_getter = None
        self.api.session_name_setter = None
        self.api.entry_label_setter = None
        self.api.idle_getter = None
        self.api.idle_waiter = None
        self.api.run_aborter = None
        self.api.compaction_requester = None
        self.api.resource_reloader = None
        self.api.question_asker = None
        self.api._tool_registrar = None
        self.api._command_registrar = None
        await self.unbind()
        self.api.handlers.clear()
        self.api._handler_origins.clear()
        callbacks, self.api.cleanup = self.api.cleanup, []
        cancelled = False
        for callback in reversed(callbacks):
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                logger.exception("Python extension cleanup failed run_id=%s", self.api.run_id)
        for name in self.modules:
            sys.modules.pop(name, None)
        self.modules.clear()
        self.api.tools.clear()
        self.api.active_selection = None
        self.selected_tools.clear()
        self.commands.clear()
        self.api.commands.clear()
        self.api.providers.clear()
        if cancelled:
            raise asyncio.CancelledError()
