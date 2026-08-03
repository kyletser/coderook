from __future__ import annotations

import pytest
from pydantic import ValidationError

from code_rook.core.authority import RuntimeMode
from code_rook.core.tools.base import BaseTool, ToolResult
from code_rook.core.tools.registry import ToolRegistry
from code_rook.core.tools.spec import (
    ParallelPolicy,
    ResourceClaim,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolCatalogError,
    ToolSpec,
)


class _FakeTool(BaseTool):
    name = "fake"
    description = "fake tool"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {},
    }

    # 返回固定成功结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="ok")


class _ClaimingTool(_FakeTool):
    name = "claiming"

    # 将 path 声明为独占写资源
    def resource_claims(self, params: dict[str, object]) -> tuple[ResourceClaim, ...]:
        return (
            ResourceClaim(
                resource=str(params["path"]),
                capability=ToolCapability.WRITE,
                exclusive=True,
            ),
        )


# 构建同时含只读和写入 action 的 family ToolSpec
def _family_spec(
    name: str = "File",
    *,
    deferred: bool = False,
    allowed_callers: frozenset[ToolCaller] | None = None,
) -> ToolSpec:
    read = ToolActionSpec(name="read", capabilities=frozenset({ToolCapability.READ}))
    patch = ToolActionSpec(name="patch", capabilities=frozenset({ToolCapability.WRITE}))
    return ToolSpec(
        name=name,
        description="file actions",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        actions=(read, patch),
        capabilities=frozenset({ToolCapability.READ, ToolCapability.WRITE}),
        parallel_policy=ParallelPolicy.RESOURCE_CLAIMS,
        deferred=deferred,
        allowed_callers=(
            allowed_callers
            if allowed_callers is not None
            else frozenset({ToolCaller.MODEL, ToolCaller.INTERNAL})
        ),
    )


# 功能：验证相同工具集合不受注册顺序影响并复用 canonical schema 缓存
# 设计：以相反顺序注册同一批工具，比较字节结果，同时用 is 固定 memoization 行为
def test_catalog_is_byte_stable_and_memoized() -> None:
    first = ToolRegistry()
    first.register(_FakeTool())
    claiming = _ClaimingTool()
    first.register(claiming)

    second = ToolRegistry()
    second.register(claiming)
    second.register(_FakeTool())

    encoded = first.canonical_catalog_json()
    assert encoded == second.canonical_catalog_json()
    assert encoded is first.canonical_catalog_json()
    assert [schema["name"] for schema in first.tool_schemas()] == ["claiming", "fake"]


# 功能：验证 Plan 对 family 工具按 action 裁剪而不是隐藏整个 File 工具
# 设计：给单个工具同时声明 read/patch，断言 Act 暴露二者而 Plan 的 action enum 只剩 read
def test_plan_filters_mutating_actions_inside_family() -> None:
    tool = _FakeTool()
    tool.name = "File"
    active = ToolRegistry(runtime_mode=RuntimeMode.ACT)
    active.register(tool, spec=_family_spec())
    planning = ToolRegistry(runtime_mode=RuntimeMode.PLAN)
    planning.register(tool, spec=_family_spec())

    active_action = active.tool_schemas()[0]["input_schema"]
    plan_action = planning.tool_schemas()[0]["input_schema"]

    assert isinstance(active_action, dict)
    assert isinstance(plan_action, dict)
    assert active_action["properties"]["action"]["enum"] == ["read", "patch"]  # type: ignore[index]
    assert plan_action["properties"]["action"]["enum"] == ["read"]  # type: ignore[index]


# 功能：验证 deferred 激活只追加尾部且不改变 always-active head 指纹
# 设计：先记录 head hash，再按显式顺序激活两个延迟工具并检查头部和尾部顺序
def test_deferred_activation_preserves_active_head() -> None:
    registry = ToolRegistry()
    registry.register(_FakeTool())
    alpha = _FakeTool()
    alpha.name = "alpha"
    registry.register(alpha, spec=_family_spec("alpha", deferred=True))
    zeta = _FakeTool()
    zeta.name = "zeta"
    registry.register(zeta, spec=_family_spec("zeta", deferred=True))
    before = registry.active_head_hash()

    schemas = registry.tool_schemas(activated=("zeta", "alpha"))

    assert [schema["name"] for schema in schemas] == ["fake", "zeta", "alpha"]
    assert registry.active_head_hash() == before


# 功能：验证未知 action、caller 不匹配和未知 capability 都会 fail closed
# 设计：分别触发目录解析错误与 Pydantic 枚举校验，覆盖协议边界的三类未声明输入
def test_unknown_action_caller_and_capability_fail_closed() -> None:
    tool = _FakeTool()
    tool.name = "File"
    registry = ToolRegistry()
    registry.register(
        tool,
        spec=_family_spec(allowed_callers=frozenset({ToolCaller.INTERNAL})),
    )

    with pytest.raises(ToolCatalogError, match="not allowed"):
        registry.resolve_call("File", {"action": "read"}, caller=ToolCaller.MODEL)
    with pytest.raises(ToolCatalogError, match="unknown action"):
        registry.resolve_call("File", {"action": "delete"}, caller=ToolCaller.INTERNAL)
    with pytest.raises(ValidationError):
        ToolActionSpec(name="future", capabilities=frozenset({"future_admin"}))  # type: ignore[arg-type]


# 功能：验证资源声明只能在 caller/action 校验成功后取得
# 设计：自定义工具返回独占文件 claim，同时用未知 caller 证明资源规划入口不会绕过目录权限
def test_resource_claims_are_validated_before_planning() -> None:
    tool = _ClaimingTool()
    registry = ToolRegistry()
    registry.register(tool)

    claims = registry.resource_claims("claiming", {"path": "src/app.py"})

    assert claims == (
        ResourceClaim(
            resource="src/app.py",
            capability=ToolCapability.WRITE,
            exclusive=True,
        ),
    )
    with pytest.raises(ToolCatalogError, match="unknown tool caller"):
        registry.resource_claims("claiming", {"path": "x"}, caller="future")


# 功能：验证仅剩一个 action 的 family 仍在 schema 和调用时显式要求 action
# 设计：用 whitelist 常见的单 action family，防止它退化成普通 invoke 工具而绕过 action 边界
def test_single_action_family_keeps_explicit_action_contract() -> None:
    tool = _FakeTool()
    tool.name = "Git"
    action = ToolActionSpec(
        name="diff",
        input_schema={"type": "object", "properties": {}},
        capabilities=frozenset({ToolCapability.READ, ToolCapability.GIT}),
    )
    registry = ToolRegistry()
    registry.register(
        tool,
        spec=ToolSpec(
            name="Git",
            description="git diff",
            input_schema={"type": "object", "properties": {}},
            actions=(action,),
            capabilities=action.capabilities,
        ),
    )

    schema = registry.tool_schemas()[0]["input_schema"]
    assert isinstance(schema, dict)
    assert schema["oneOf"][0]["properties"]["action"]["enum"] == ["diff"]  # type: ignore[index]
    with pytest.raises(ToolCatalogError, match="action is required"):
        registry.resolve_call("Git", {})
    assert registry.resolve_call("Git", {"action": "diff"}).action.name == "diff"
