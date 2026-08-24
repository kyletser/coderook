from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from code_rook.core.authority import RuntimeMode
from code_rook.core.tools.spec import (
    ResolvedToolCall,
    ToolCaller,
    ToolCatalogError,
    ToolSpec,
)


# 将 JSON 兼容对象编码为稳定、紧凑的 UTF-8 字节
def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class ToolCatalog:
    # 初始化空目录及按 Mode/激活尾部划分的 schema 缓存
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._schema_cache: dict[tuple[RuntimeMode, tuple[str, ...]], bytes] = {}

    # 注册或覆盖 ToolSpec，并仅在目录变化时使缓存失效
    def register(self, spec: ToolSpec) -> None:
        if self._specs.get(spec.name) == spec:
            return
        self._specs[spec.name] = spec
        self._schema_cache.clear()

    # 按名称返回 ToolSpec，不存在时返回 None
    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    # 返回按名称稳定排序的全部 ToolSpec
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs[name] for name in sorted(self._specs))

    # 生成给模型使用的 action-aware schema，Plan 仅保留只读 action
    def _model_schema(self, spec: ToolSpec, mode: RuntimeMode) -> dict[str, object] | None:
        actions = spec.visible_actions(mode)
        if not actions or not spec.model_visible:
            return None
        is_family = spec.is_action_family
        action_schemas = [action for action in actions if action.input_schema is not None]
        if is_family and len(action_schemas) == len(actions):
            variants: list[dict[str, object]] = []
            for action in actions:
                assert action.input_schema is not None
                variant = deepcopy(action.input_schema)
                properties = variant.setdefault("properties", {})
                if not isinstance(properties, dict):
                    raise ToolCatalogError(
                        f"invalid properties schema for {spec.name}.{action.name}"
                    )
                properties["action"] = {
                    "type": "string",
                    "enum": [action.name],
                }
                required = variant.setdefault("required", [])
                if not isinstance(required, list):
                    raise ToolCatalogError(
                        f"invalid required schema for {spec.name}.{action.name}"
                    )
                if "action" not in required:
                    required.insert(0, "action")
                variants.append(variant)
            input_schema: dict[str, object] = {
                "type": "object",
                "oneOf": variants,
            }
        else:
            input_schema = deepcopy(spec.input_schema)
        if is_family and not action_schemas:
            properties = input_schema.setdefault("properties", {})
            if not isinstance(properties, dict):
                raise ToolCatalogError(f"invalid properties schema for tool: {spec.name}")
            action_schema = properties.setdefault("action", {})
            if not isinstance(action_schema, dict):
                raise ToolCatalogError(f"invalid action schema for tool: {spec.name}")
            action_schema["type"] = "string"
            action_schema["enum"] = [action.name for action in actions]
            required = input_schema.setdefault("required", [])
            if not isinstance(required, list):
                raise ToolCatalogError(f"invalid required schema for tool: {spec.name}")
            if "action" not in required:
                required.append("action")
        return {
            "name": spec.name,
            "description": spec.description,
            "input_schema": input_schema,
        }

    # 按稳定 active head 和显式 deferred tail 构建模型目录
    def _build_schemas(
        self,
        mode: RuntimeMode,
        activated: tuple[str, ...],
    ) -> list[dict[str, object]]:
        active = [spec for spec in self.specs() if not spec.deferred]
        schemas = [
            schema
            for spec in active
            if (schema := self._model_schema(spec, mode)) is not None
        ]
        seen = {spec.name for spec in active}
        for name in activated:
            spec = self._specs.get(name)
            if spec is None or not spec.deferred or name in seen:
                continue
            schema = self._model_schema(spec, mode)
            if schema is not None:
                schemas.append(schema)
                seen.add(name)
        return schemas

    # 返回目录的 canonical JSON；相同目录直接复用缓存字节对象
    def canonical_json(
        self,
        mode: RuntimeMode = RuntimeMode.ACT,
        *,
        activated: tuple[str, ...] = (),
    ) -> bytes:
        key = (mode, activated)
        cached = self._schema_cache.get(key)
        if cached is None:
            cached = _canonical_json(self._build_schemas(mode, activated))
            self._schema_cache[key] = cached
        return cached

    # 返回模型 schema 的可变副本，避免调用方污染缓存
    def tool_schemas(
        self,
        mode: RuntimeMode = RuntimeMode.ACT,
        *,
        activated: tuple[str, ...] = (),
    ) -> list[dict[str, object]]:
        value: Any = json.loads(self.canonical_json(mode, activated=activated))
        if not isinstance(value, list):
            raise ToolCatalogError("canonical tool catalog is not a list")
        return value

    # 返回 always-active 目录头部的稳定 SHA-256 指纹
    def active_head_hash(self, mode: RuntimeMode = RuntimeMode.ACT) -> str:
        return hashlib.sha256(self.canonical_json(mode)).hexdigest()

    # 校验 caller 与 action 并返回解析结果，任何未知项均 fail closed
    def resolve_call(
        self,
        name: str,
        params: dict[str, object],
        *,
        caller: ToolCaller | str = ToolCaller.MODEL,
    ) -> ResolvedToolCall:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolCatalogError(f"unknown tool: {name}")
        try:
            known_caller = ToolCaller(caller)
        except ValueError:
            raise ToolCatalogError(f"unknown tool caller: {caller}") from None
        if known_caller not in spec.allowed_callers:
            raise ToolCatalogError(
                f"caller {known_caller.value} is not allowed for tool: {name}"
            )
        if known_caller == ToolCaller.MODEL and not spec.model_visible:
            raise ToolCatalogError(f"tool is not model-visible: {name}")
        if len(spec.actions) == 1 and spec.actions[0].name == "invoke":
            action_name = spec.actions[0].name
        else:
            raw_action = params.get("action")
            if not isinstance(raw_action, str) or not raw_action:
                raise ToolCatalogError(f"action is required for tool: {name}")
            action_name = raw_action
        action = spec.action(action_name)
        if action is None:
            raise ToolCatalogError(f"unknown action for {name}: {action_name}")
        return ResolvedToolCall(spec=spec, action=action, caller=known_caller)
