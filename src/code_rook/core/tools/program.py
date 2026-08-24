from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, TypeAdapter, model_validator

from code_rook.core.llm.types import ToolCallBlock
from code_rook.core.tools.base import BaseTool, ToolResult, ToolSideEffect
from code_rook.core.tools.execution_metadata import (
    ToolExecutionMetadata,
    current_tool_invocation_id,
    tool_execution_metadata,
)
from code_rook.core.tools.invocation import invoke_tool
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    ParallelPolicy,
    ToolActionSpec,
    ToolCaller,
    ToolCapability,
    ToolCatalogError,
    ToolSpec,
)

if TYPE_CHECKING:
    from code_rook.core.artifacts import ArtifactStore
    from code_rook.core.authority import AuthoritySnapshot
    from code_rook.core.events.bus import EventBus
    from code_rook.core.hooks import HookManager
    from code_rook.core.permissions.manager import PermissionManager
    from code_rook.core.tools.registry import ToolRegistry

MAX_PROGRAM_NODES = 16
MAX_PROGRAM_DEPTH = 6
MAX_PROGRAM_CONCURRENCY = 4
MAX_PROGRAM_WALL_SECONDS = 120
_FORBIDDEN_TOOLS = frozenset(
    {
        "run_tool_program",
        "agent",
        "goal",
        "goal_update",
        "memory",
        "permission",
        "tasks",
        "update_plan",
        "worktree",
    }
)


class ResultReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    ref: str = Field(alias="$ref", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}\.result$")
    path: tuple[str | int, ...] = ()


class ProgramCondition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ResultReference
    equals: Any = None
    status: Literal["success", "error"] | None = None


class CallNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["call"] = "call"
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    continue_on_error: bool = False


class SequenceNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["sequence"] = "sequence"
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    nodes: tuple[ProgramNode, ...]


class ParallelNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["parallel"] = "parallel"
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    nodes: tuple[ProgramNode, ...]
    max_concurrency: int = Field(default=MAX_PROGRAM_CONCURRENCY, ge=1, le=MAX_PROGRAM_CONCURRENCY)


class IfNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["if"] = "if"
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    condition: ProgramCondition
    then_nodes: tuple[ProgramNode, ...]
    else_nodes: tuple[ProgramNode, ...] = ()


ProgramNode = Annotated[
    CallNode | SequenceNode | ParallelNode | IfNode,
    Discriminator("kind"),
]

SequenceNode.model_rebuild()
ParallelNode.model_rebuild()
IfNode.model_rebuild()
_PROGRAM_NODE_ADAPTER: TypeAdapter[ProgramNode] = TypeAdapter(ProgramNode)


class ToolProgram(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: tuple[ProgramNode, ...]

    @model_validator(mode="after")
    # 校验节点总量、深度、ID 唯一性以及静态引用只能指向已完成节点
    def validate_program(self) -> ToolProgram:
        seen: set[str] = set()
        count = 0

        # 按确定执行顺序递归校验节点并记录引用可见范围
        def visit(nodes: tuple[ProgramNode, ...], depth: int, available: set[str]) -> set[str]:
            nonlocal count
            if depth > MAX_PROGRAM_DEPTH:
                raise ValueError("tool program nesting depth exceeded")
            local_available = set(available)
            for node in nodes:
                count += 1
                if count > MAX_PROGRAM_NODES:
                    raise ValueError("tool program node limit exceeded")
                if node.id in seen:
                    raise ValueError(f"duplicate tool program node id: {node.id}")
                seen.add(node.id)
                references = _node_references(node)
                missing = sorted(references - local_available)
                if missing:
                    raise ValueError(
                        "tool program reference must target a prior node: "
                        + ", ".join(missing)
                    )
                if isinstance(node, SequenceNode):
                    local_available |= visit(node.nodes, depth + 1, local_available)
                elif isinstance(node, ParallelNode):
                    before_parallel = set(local_available)
                    produced: set[str] = set()
                    for child in node.nodes:
                        child_refs = _node_references(child)
                        if not child_refs <= before_parallel:
                            raise ValueError("parallel siblings cannot reference each other")
                        produced |= visit((child,), depth + 1, before_parallel)
                    local_available |= produced
                elif isinstance(node, IfNode):
                    visit(node.then_nodes, depth + 1, local_available)
                    visit(node.else_nodes, depth + 1, local_available)
                local_available.add(node.id)
            return local_available - available

        visit(self.nodes, 1, set())
        return self


class _NodeOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    status: Literal["success", "error", "skipped"]
    result: Any = None
    error_type: str | None = None


class ToolProgramExecutor:
    # 绑定原生工具管线依赖，确保每个 DSL call 都重新经过完整审计边界
    def __init__(
        self,
        registry: ToolRegistry,
        bus: EventBus,
        run_id: str,
        parent_tool_call_id: str,
        *,
        session_id: str = "",
        permission_manager: PermissionManager | None = None,
        hooks: HookManager | None = None,
        artifact_store: ArtifactStore | None = None,
        authority_snapshot: AuthoritySnapshot | None = None,
        step: int = 0,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._run_id = run_id
        self._parent_tool_call_id = parent_tool_call_id
        self._session_id = session_id
        self._permission_manager = permission_manager
        self._hooks = hooks
        self._artifact_store = artifact_store
        self._authority_snapshot = authority_snapshot
        self._step = step
        self._program_id = f"program-{uuid.uuid4().hex[:12]}"
        self._outcomes: dict[str, _NodeOutcome] = {}
        self._commit_order = 0
        self._commit_lock = asyncio.Lock()
        self._declared_orders: dict[str, int] = {}

    # 在统一 120 秒墙钟预算内执行程序并返回按声明顺序稳定排列的结果
    async def execute(self, program: ToolProgram) -> dict[str, Any]:
        self._declared_orders = {
            node_id: index
            for index, node_id in enumerate(_flatten_node_ids(program.nodes), start=1)
        }
        await asyncio.wait_for(
            self._run_nodes(program.nodes),
            timeout=MAX_PROGRAM_WALL_SECONDS,
        )
        ordered = [
            self._outcomes[node_id].model_dump(mode="json")
            for node_id in _flatten_node_ids(program.nodes)
            if node_id in self._outcomes
        ]
        return {"program_id": self._program_id, "nodes": ordered}

    # 按序执行一组节点，默认遇到失败立即停止后续节点
    async def _run_nodes(self, nodes: tuple[ProgramNode, ...]) -> None:
        for node in nodes:
            outcome = await self._run_node(node)
            if outcome.status == "error" and not (
                isinstance(node, CallNode) and node.continue_on_error
            ):
                break

    # 分派单个节点并把结构节点自身也记录为可引用结果
    async def _run_node(self, node: ProgramNode) -> _NodeOutcome:
        if isinstance(node, CallNode):
            return await self._run_call(node)
        if isinstance(node, SequenceNode):
            await self._run_nodes(node.nodes)
            return await self._commit(
                node.id,
                "success",
                {"children": [item.id for item in node.nodes]},
            )
        if isinstance(node, ParallelNode):
            semaphore = asyncio.Semaphore(node.max_concurrency)

            # 在节点自身并发限制内执行一个兄弟节点
            async def run_child(child: ProgramNode) -> _NodeOutcome:
                async with semaphore:
                    return await self._run_node(child)

            children = await asyncio.gather(*(run_child(child) for child in node.nodes))
            status: Literal["success", "error"] = (
                "error" if any(child.status == "error" for child in children) else "success"
            )
            return await self._commit(
                node.id,
                status,
                {"children": [child.id for child in children]},
            )
        condition = _evaluate_condition(node.condition, self._outcomes)
        selected = node.then_nodes if condition else node.else_nodes
        await self._run_nodes(selected)
        return await self._commit(node.id, "success", {"branch": "then" if condition else "else"})

    # 解析引用、验证 Manifest 可编排性并通过 invoke_tool 执行一次原生调用
    async def _run_call(self, node: CallNode) -> _NodeOutcome:
        if node.tool in _FORBIDDEN_TOOLS:
            return await self._commit(
                node.id,
                "error",
                None,
                error_type="tool_not_programmable",
            )
        try:
            arguments = _resolve_value(node.arguments, self._outcomes)
            if not isinstance(arguments, dict):
                raise ValueError("resolved tool arguments must be an object")
            resolved = self._registry.resolve_call(
                node.tool,
                arguments,
                caller=ToolCaller.MODEL,
            )
            if not resolved.action.programmable:
                raise ToolCatalogError(f"tool action is not programmable: {node.tool}")
        except (KeyError, TypeError, ValueError, ToolCatalogError) as exc:
            return await self._commit(
                node.id,
                "error",
                None,
                error_type=f"program_validation:{exc}",
            )
        metadata = ToolExecutionMetadata(
            program_id=self._program_id,
            parent_tool_call_id=self._parent_tool_call_id,
            node_id=node.id,
            commit_order=self._declared_orders[node.id],
        )
        with tool_execution_metadata(metadata):
            result = await invoke_tool(
                self._registry,
                ToolCallBlock(
                    id=f"{self._program_id}:{node.id}",
                    name=node.tool,
                    input=arguments,
                ),
                self._bus,
                self._run_id,
                permission_manager=self._permission_manager,
                session_id=self._session_id,
                hooks=self._hooks,
                caller=ToolCaller.MODEL,
                artifact_store=self._artifact_store,
                authority_snapshot=self._authority_snapshot,
                step=self._step,
            )
        value: Any
        try:
            value = json.loads(result.content)
        except json.JSONDecodeError:
            value = result.content
        return await self._commit(
            node.id,
            "error" if result.is_error else "success",
            value,
            error_type=result.error_type,
        )

    # 原子分配稳定提交序号并保存一个节点终态
    async def _commit(
        self,
        node_id: str,
        status: Literal["success", "error", "skipped"],
        result: Any,
        *,
        error_type: str | None = None,
    ) -> _NodeOutcome:
        async with self._commit_lock:
            self._commit_order += 1
            outcome = _NodeOutcome(
                id=node_id,
                status=status,
                result=result,
                error_type=error_type,
            )
            self._outcomes[node_id] = outcome
            return outcome


class RunToolProgram(BaseTool):
    name = "run_tool_program"
    description = (
        "Run a bounded declarative tool program. It supports call, sequence, parallel, "
        "and if nodes; it never evaluates Python, JavaScript, or shell expressions."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "maxItems": MAX_PROGRAM_NODES,
                "items": {"type": "object"},
            }
        },
        "required": ["nodes"],
    }
    side_effect = ToolSideEffect.EXTERNAL_WRITE
    timeout_s = float(MAX_PROGRAM_WALL_SECONDS)
    spec_override = ToolSpec(
        name=name,
        description=description,
        input_schema=input_schema,
        actions=(
            ToolActionSpec(
                name="invoke",
                description=description,
                capabilities=frozenset({ToolCapability.EXTERNAL}),
                approval_requirement=ApprovalRequirement.NEVER,
                programmable=False,
            ),
        ),
        capabilities=frozenset({ToolCapability.EXTERNAL}),
        approval_requirement=ApprovalRequirement.NEVER,
        parallel_policy=ParallelPolicy.SERIAL,
    )

    # 绑定父循环的原生工具执行依赖
    def __init__(
        self,
        registry: ToolRegistry,
        bus: EventBus,
        run_id: str,
        *,
        session_id: str = "",
        permission_manager: PermissionManager | None = None,
        hooks: HookManager | None = None,
        artifact_store: ArtifactStore | None = None,
        authority_snapshot: AuthoritySnapshot | None = None,
        step_provider: Any = None,
    ) -> None:
        self._registry = registry
        self._bus = bus
        self._run_id = run_id
        self._session_id = session_id
        self._permission_manager = permission_manager
        self._hooks = hooks
        self._artifact_store = artifact_store
        self._authority_snapshot = authority_snapshot
        self._step_provider = step_provider

    # 验证 DSL 后执行程序并返回结构化的有序节点结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            program = ToolProgram.model_validate(params)
        except ValueError as exc:
            return ToolResult(str(exc), is_error=True, error_type="schema_error")
        step = int(self._step_provider()) if callable(self._step_provider) else 0
        executor = ToolProgramExecutor(
            self._registry,
            self._bus,
            self._run_id,
            parent_tool_call_id=current_tool_invocation_id() or "run_tool_program",
            session_id=self._session_id,
            permission_manager=self._permission_manager,
            hooks=self._hooks,
            artifact_store=self._artifact_store,
            authority_snapshot=self._authority_snapshot,
            step=step,
        )
        try:
            result = await executor.execute(program)
        except TimeoutError:
            return ToolResult(
                "tool program exceeded 120 second wall-time budget",
                is_error=True,
                error_type="timeout",
                failure_category="timed_out",
            )
        return ToolResult(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


# 收集节点参数和条件中的静态结果引用 ID
def _node_references(node: ProgramNode) -> set[str]:
    raw: object
    if isinstance(node, CallNode):
        raw = node.arguments
    elif isinstance(node, IfNode):
        raw = node.condition.model_dump(by_alias=True)
    else:
        raw = {}
    references: set[str] = set()

    # 递归扫描 JSON 对象中的严格 $ref 结构
    def scan(value: Any) -> None:
        if isinstance(value, dict):
            raw_ref = value.get("$ref")
            if isinstance(raw_ref, str) and raw_ref.endswith(".result"):
                references.add(raw_ref.removesuffix(".result"))
            for child in value.values():
                scan(child)
        elif isinstance(value, list):
            for child in value:
                scan(child)

    scan(raw)
    return references


# 递归解析参数中的受限结果引用并拒绝缺失路径
def _resolve_value(value: Any, outcomes: dict[str, _NodeOutcome]) -> Any:
    if isinstance(value, dict) and "$ref" in value:
        reference = ResultReference.model_validate(value)
        node_id = reference.ref.removesuffix(".result")
        current: Any = outcomes[node_id].result
        for segment in reference.path:
            if isinstance(segment, int) and isinstance(current, list):
                current = current[segment]
            elif isinstance(segment, str) and isinstance(current, dict):
                current = current[segment]
            else:
                raise KeyError(f"invalid result path segment: {segment}")
        return current
    if isinstance(value, dict):
        return {key: _resolve_value(child, outcomes) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve_value(child, outcomes) for child in value]
    return value


# 依据引用值、状态和 equals 条件计算 if 分支
def _evaluate_condition(
    condition: ProgramCondition,
    outcomes: dict[str, _NodeOutcome],
) -> bool:
    node_id = condition.reference.ref.removesuffix(".result")
    outcome = outcomes[node_id]
    if condition.status is not None and outcome.status != condition.status:
        return False
    if condition.status is not None and condition.equals is None and not condition.reference.path:
        return True
    value = _resolve_value(condition.reference.model_dump(by_alias=True), outcomes)
    return bool(value == condition.equals)


# 按声明树前序返回稳定节点 ID 顺序
def _flatten_node_ids(nodes: tuple[ProgramNode, ...]) -> list[str]:
    result: list[str] = []
    for node in nodes:
        result.append(node.id)
        if isinstance(node, (SequenceNode, ParallelNode)):
            result.extend(_flatten_node_ids(node.nodes))
        elif isinstance(node, IfNode):
            result.extend(_flatten_node_ids(node.then_nodes))
            result.extend(_flatten_node_ids(node.else_nodes))
    return result
