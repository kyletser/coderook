from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import AnyHttpUrl

from code_rook.core.llm.route_registry import ResolvedRoute, RouteResolutionError
from code_rook.core.llm.routes import CredentialSource, ProviderRoute

_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_API_WIRES = {
    "anthropic-messages": ("anthropic-compatible", "anthropic_messages"),
    "anthropic_messages": ("anthropic-compatible", "anthropic_messages"),
    "openai-completions": ("openai-compatible", "openai_chat"),
    "openai-chat": ("openai-compatible", "openai_chat"),
    "openai_chat": ("openai-compatible", "openai_chat"),
    "openai-responses": ("openai", "openai_responses"),
    "openai_responses": ("openai", "openai_responses"),
}


@dataclass(frozen=True)
class ExtensionProviderModel:
    id: str
    name: str
    base_url: str | None
    context_window: int | None
    supports_images: bool
    supports_tools: bool
    supports_parallel_tools: bool
    headers: dict[str, str]


@dataclass(frozen=True)
class ExtensionProvider:
    id: str
    name: str
    base_url: str | None
    provider_kind: str | None
    wire_format: str | None
    api_key: str | None
    headers: dict[str, str]
    models: tuple[ExtensionProviderModel, ...]

    # 从 Python 扩展的 Pi 风格配置创建会话级 Provider。
    @classmethod
    def from_config(cls, name: str, config: dict[str, Any]) -> ExtensionProvider:
        if _PROVIDER_ID.fullmatch(name) is None:
            raise ValueError("Provider name must contain letters, digits, ., _ or -")
        if not isinstance(config, dict):
            raise TypeError("Provider config must be an object")
        base_url = config.get("baseUrl", config.get("base_url"))
        api = config.get("api")
        display_name = config.get("name", name)
        if base_url is not None and (
            not isinstance(base_url, str) or not base_url.strip()
        ):
            raise ValueError("Provider baseUrl must be non-empty text")
        if api is not None and (not isinstance(api, str) or api not in _API_WIRES):
            raise ValueError(f"Unsupported Provider API: {api}")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ValueError("Provider display name must be text")
        headers = cls._parse_headers(config.get("headers"), owner="Provider")
        raw_api_key = config.get("apiKey", config.get("api_key"))
        if raw_api_key is not None and not isinstance(raw_api_key, str):
            raise TypeError("Provider apiKey must be text")
        raw_models = config.get("models")
        if raw_models is not None and not isinstance(raw_models, list):
            raise TypeError("Provider models must be a list")
        models = tuple(cls._parse_model(item) for item in (raw_models or []))
        if models and base_url is None:
            raise ValueError("Provider baseUrl is required when registering models")
        if models and api is None:
            api = "openai-completions"
        if not models and base_url is None and not headers and raw_api_key is None:
            raise ValueError("Provider override requires baseUrl, apiKey, or headers")
        if len({item.id for item in models}) != len(models):
            raise ValueError("Provider model IDs must be unique")
        provider_kind, wire_format = _API_WIRES[api] if api is not None else (None, None)
        return cls(
            id=name,
            name=display_name.strip(),
            base_url=base_url.strip() if isinstance(base_url, str) else None,
            provider_kind=provider_kind,
            wire_format=wire_format,
            api_key=raw_api_key,
            headers=headers,
            models=models,
        )

    # 校验扩展声明的 HTTP 请求头，具体值在每次解析路由时才展开环境变量。
    @staticmethod
    def _parse_headers(value: object, *, owner: str) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError(f"{owner} headers must be an object")
        headers: dict[str, str] = {}
        for raw_name, raw_value in value.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"{owner} header names must be non-empty text")
            if not isinstance(raw_value, str):
                raise TypeError(f"{owner} header values must be text")
            if any(character in raw_name for character in "\r\n:"):
                raise ValueError(f"Invalid {owner} header name: {raw_name!r}")
            if "\r" in raw_value or "\n" in raw_value:
                raise ValueError(f"Invalid {owner} header value for {raw_name!r}")
            headers[raw_name.strip()] = raw_value
        return headers

    # 校验并规范化扩展声明的单个模型元数据。
    @staticmethod
    def _parse_model(value: object) -> ExtensionProviderModel:
        if not isinstance(value, dict):
            raise TypeError("Provider model must be an object")
        model_id = value.get("id")
        name = value.get("name", model_id)
        base_url = value.get("baseUrl", value.get("base_url"))
        context_window = value.get("contextWindow", value.get("context_window"))
        inputs = value.get("input", ["text"])
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("Provider model id is required")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Provider model name must be text")
        if base_url is not None and not isinstance(base_url, str):
            raise TypeError("Provider model baseUrl must be text")
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window < 1
        ):
            raise ValueError("Provider model contextWindow must be a positive integer")
        if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
            raise TypeError("Provider model input must be a list of strings")
        supports_tools = value.get("supportsTools", value.get("supports_tools", True))
        supports_parallel = value.get(
            "supportsParallelTools", value.get("supports_parallel_tools", supports_tools)
        )
        if not isinstance(supports_tools, bool) or not isinstance(supports_parallel, bool):
            raise TypeError("Provider model tool capabilities must be booleans")
        return ExtensionProviderModel(
            id=model_id.strip(),
            name=name.strip(),
            base_url=base_url.strip() if isinstance(base_url, str) else None,
            context_window=context_window,
            supports_images="image" in inputs,
            supports_tools=supports_tools,
            supports_parallel_tools=supports_parallel,
            headers=ExtensionProvider._parse_headers(value.get("headers"), owner="Model"),
        )

    # 返回扩展 Provider 声明的模型 ID 列表。
    def model_ids(self) -> list[str]:
        return [model.id for model in self.models]

    # 将扩展 Provider 的模型选择解析为现有 Python Provider 可执行的冻结路由。
    def resolve(
        self,
        model_id: str = "",
        *,
        base: ResolvedRoute | None = None,
    ) -> ResolvedRoute:
        if not self.models:
            if base is None:
                raise RouteResolutionError(
                    f"provider {self.id!r} overrides an existing Provider and has no models"
                )
            return self._apply_override(base, model_id)
        selected_id = model_id.strip() or self.models[0].id
        selected = next((model for model in self.models if model.id == selected_id), None)
        if selected is None:
            raise RouteResolutionError(
                f"model {selected_id!r} is not registered by provider {self.id!r}"
            )
        credential, source = self._resolve_credential()
        assert self.base_url is not None
        assert self.provider_kind is not None
        assert self.wire_format is not None
        base_url = self._endpoint(selected.base_url or self.base_url)
        route = ProviderRoute(
            id=self.id,
            provider=self.provider_kind,  # type: ignore[arg-type]
            wire_format=self.wire_format,  # type: ignore[arg-type]
            base_url=AnyHttpUrl(base_url),
            model=selected.id,
            credential_ref=f"extension:{self.id}",
            catalog_id=self.id,
            credential_required=source != "missing",
            context_window=selected.context_window,
            supports_tools=selected.supports_tools,
            supports_parallel_tools=selected.supports_parallel_tools,
            supports_images=selected.supports_images,
        )
        headers = {
            name: self._resolve_config_value(value, label=f"header {name!r}")
            for name, value in {**self.headers, **selected.headers}.items()
        }
        return ResolvedRoute(
            route=route,
            receipt=route.receipt(source),
            credential=credential,
            request_headers=headers,
        )

    # 将扩展覆盖项叠加到已有冻结路由，不复制或改写共享 Provider Catalog。
    def _apply_override(self, base: ResolvedRoute, model_id: str) -> ResolvedRoute:
        selected_model = model_id.strip() or base.route.model
        credential = base.credential
        credential_source = base.receipt.credential_source
        credential_ref = base.route.credential_ref
        credential_required = base.route.credential_required
        if self.api_key is not None:
            credential, credential_source = self._resolve_credential()
            credential_ref = f"extension:{self.id}"
            credential_required = credential_source != "missing"
        provider_kind = self.provider_kind or base.route.provider
        wire_format = self.wire_format or base.route.wire_format
        raw_url = self.base_url or str(base.route.base_url)
        route_payload = base.route.model_dump(mode="json")
        route_payload.update({
            "provider": provider_kind,
            "wire_format": wire_format,
            "base_url": raw_url,
            "model": selected_model,
            "credential_ref": credential_ref,
            "credential_required": credential_required,
            "doctor_receipt": None,
        })
        if wire_format == "openai_responses":
            route_payload["base_url"] = self._endpoint(raw_url, wire_format=wire_format)
        route = ProviderRoute.model_validate(route_payload)
        headers = dict(base.request_headers)
        headers.update({
            name: self._resolve_config_value(value, label=f"header {name!r}")
            for name, value in self.headers.items()
        })
        return ResolvedRoute(
            route=route,
            receipt=route.receipt(credential_source),
            credential=credential,
            request_headers=headers,
        )

    # 解析扩展声明的字面量或环境变量凭据，不将正文写入 Session。
    def _resolve_credential(self) -> tuple[str, CredentialSource]:
        raw = self.api_key
        if raw is None or not raw.strip():
            return "", "missing"
        value = raw.strip()
        environment_expression = value.replace("$$", "").replace("$!", "")
        uses_environment = re.search(
            r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*",
            environment_expression,
        ) is not None
        resolved = self._resolve_config_value(value, label="Provider apiKey")
        source: CredentialSource = "env" if uses_environment else "extension"
        return resolved, source

    # 按 Pi 配置值语法展开环境变量和转义符，但不执行命令型凭据。
    @staticmethod
    def _resolve_config_value(value: str, *, label: str) -> str:
        if value.startswith("!") and not value.startswith("!!"):
            command = value[1:].strip()
            if not command:
                raise RouteResolutionError(f"{label} command is empty")
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RouteResolutionError(f"{label} command failed: {type(exc).__name__}") from exc
            if completed.returncode != 0:
                raise RouteResolutionError(
                    f"{label} command exited with status {completed.returncode}"
                )
            resolved_command = completed.stdout.strip()
            if not resolved_command:
                raise RouteResolutionError(f"{label} command returned an empty value")
            return resolved_command
        if value.startswith("!!"):
            value = value[1:]
        sentinel_dollar = "\x00CODEROOK_DOLLAR\x00"
        sentinel_bang = "\x00CODEROOK_BANG\x00"
        escaped = value.replace("$$", sentinel_dollar).replace("$!", sentinel_bang)

        # 仅从当前用户进程解析扩展明确引用的环境变量。
        def replace(match: re.Match[str]) -> str:
            variable = match.group(1) or match.group(2)
            resolved = os.environ.get(variable, "")
            if not resolved:
                raise RouteResolutionError(
                    f"environment value {variable!r} required by {label} is not configured"
                )
            return resolved

        resolved = re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
            replace,
            escaped,
        )
        return resolved.replace(sentinel_dollar, "$").replace(sentinel_bang, "!")

    # 按既有 Provider 构造器需要的形式补全 Responses 端点。
    def _endpoint(self, value: str, *, wire_format: str | None = None) -> str:
        if (wire_format or self.wire_format) != "openai_responses":
            return value
        parsed = urlsplit(value.strip())
        path = parsed.path.rstrip("/")
        if path.endswith("/v1"):
            path += "/responses"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
