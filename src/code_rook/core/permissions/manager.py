from __future__ import annotations

import asyncio
import datetime
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

from code_rook.core.audit import AuditHealth
from code_rook.core.authority import (
    AuthorityDecision,
    AuthorityProfile,
    AuthoritySnapshot,
    ToolAction,
    detect_sandbox_capability,
    evaluate_action,
)
from code_rook.core.permissions.command_pattern import (
    command_pattern_key,
    matches_command_pattern,
)
from code_rook.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    matches_outside_cwd,
    param_preview,
)
from code_rook.core.permissions.storage import (
    load_authority_profile,
    load_policy_file,
    save_policy_file,
)
from code_rook.core.sandbox.planner import (
    SandboxPlan,
    SandboxTier,
    plan_sandbox,
    tier_for_auto_review,
)
from code_rook.core.tools.spec import (
    ApprovalRequirement,
    PermissionScope,
    ResolvedToolCall,
)

logger = logging.getLogger(__name__)

PermissionRunMode = Literal["interactive", "deny", "fail_fast", "allow_list"]

def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


# 从已解析 manifest 返回权限范围；旧直调仅生成精确键且不猜测兼容别名
def _permission_scope(
    tool_name: str,
    params: dict[str, Any],
    resolved_call: ResolvedToolCall | None = None,
) -> PermissionScope | None:
    if resolved_call is not None:
        if resolved_call.spec.name != tool_name:
            return None
        if resolved_call.spec.is_action_family:
            action = params.get("action")
            if action != resolved_call.action.name:
                return None
        return resolved_call.permission_scope
    action = params.get("action")
    if not isinstance(action, str) or not action:
        return PermissionScope(key=tool_name)
    return PermissionScope(key=f"{tool_name}.{action}")


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str


@dataclass(frozen=True)
class _SessionPermissionMode:
    mode: PermissionRunMode
    allow_tools: frozenset[str] = frozenset()


# 管理工具调用权限：策略评估、用户审批挂起、session 级和持久化 always 缓存、超时
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: Path | None = None,
        timeout_s: float = 60.0,
        audit_health: AuditHealth | None = None,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        self._response_metadata: dict[str, dict[str, Any]] = {}
        # (session_id, tool_name) → "allow" | "deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow" | "deny"（持久化，从 policy_file 加载）
        self._policy_file = policy_file
        self._persistent_always: dict[str, str] = (
            load_policy_file(policy_file) if policy_file is not None else {}
        )
        persistent_profile = (
            load_authority_profile(policy_file) if policy_file is not None else None
        )
        # 0 表示不超时
        self._timeout_s = timeout_s
        self._session_modes: dict[str, _SessionPermissionMode] = {}
        self._default_authority = AuthoritySnapshot(
            profile=persistent_profile or AuthorityProfile.ASK,
            sandbox=detect_sandbox_capability(),
        )
        self._session_authorities: dict[str, AuthoritySnapshot] = {}
        # active turn 使用独立不可变快照；会话设置只能影响后续 turn
        self._active_turn_authorities: dict[str, AuthoritySnapshot] = {}
        self._audit_health = audit_health

    # 持久化新会话的默认权限姿态，不修改已有会话或正在运行的 turn
    def set_default_profile(self, profile: AuthorityProfile) -> None:
        self._default_authority = self._default_authority.model_copy(
            update={"profile": profile}
        )
        if self._policy_file is not None:
            save_policy_file(
                self._persistent_always,
                self._policy_file,
                authority_profile=profile,
            )

    # 设置从下一 turn 开始使用的 session authority 快照
    def set_authority_snapshot(
        self,
        session_id: str,
        snapshot: AuthoritySnapshot,
    ) -> None:
        self._session_authorities[session_id] = snapshot

    # 返回 session 当前配置的 authority 快照
    def get_authority_snapshot(self, session_id: str) -> AuthoritySnapshot:
        return self._session_authorities.get(session_id, self._default_authority)

    # 冻结当前 turn 的执行权限，后续会话设置变更不得影响该快照
    def begin_turn(self, session_id: str, snapshot: AuthoritySnapshot) -> None:
        if session_id in self._active_turn_authorities:
            raise RuntimeError(f"turn authority already active for session {session_id}")
        self._active_turn_authorities[session_id] = snapshot

    # 清除已结束 turn 的冻结权限快照
    def end_turn(self, session_id: str) -> None:
        self._active_turn_authorities.pop(session_id, None)

    # 返回工具审批和沙箱启动必须共同消费的有效权限快照
    def get_effective_authority_snapshot(self, session_id: str) -> AuthoritySnapshot:
        return self._active_turn_authorities.get(
            session_id,
            self.get_authority_snapshot(session_id),
        )

    # 清除 session 的 authority 配置并恢复默认值
    def clear_authority_snapshot(self, session_id: str) -> None:
        self._session_authorities.pop(session_id, None)

    # 依据 authority 快照生成 shell 沙箱；Windows partial 始终包裹但仍由审批层逐次确认
    def shell_sandbox_plan(self, session_id: str, workspace: str) -> SandboxPlan | None:
        snap = self.get_effective_authority_snapshot(session_id)
        if snap.sandbox.available and snap.sandbox.kind == "windows_acl":
            return plan_sandbox(snap.sandbox, SandboxTier.WORKSPACE_WRITE, workspace)
        if snap.profile != AuthorityProfile.AUTO_REVIEW:
            return None
        if tier_for_auto_review(snap.sandbox) == SandboxTier.NONE:
            return None
        return plan_sandbox(snap.sandbox, SandboxTier.WORKSPACE_WRITE, workspace)

    # 设置 headless session 的兼容权限模式
    def set_session_mode(
        self,
        session_id: str,
        mode: PermissionRunMode,
        *,
        allow_tools: list[str] | None = None,
    ) -> None:
        if mode == "interactive":
            self._session_modes.pop(session_id, None)
            return
        self._session_modes[session_id] = _SessionPermissionMode(
            mode=mode,
            allow_tools=frozenset(allow_tools or []),
        )

    def clear_session_mode(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)

    # 按 manifest 新键优先、旧别名回退选择静态策略
    def _policy_for_scope(
        self,
        scope: PermissionScope,
    ) -> tuple[str, ToolPolicy | None]:
        for key in scope.lookup_keys:
            policy = self._policies.get(key)
            if policy is not None:
                return key, policy
        return scope.key, None

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        resolved_call: ResolvedToolCall | None = None,
    ) -> PermissionDecision:
        from code_rook.core.permissions.policy import evaluate

        scope = _permission_scope(tool_name, params, resolved_call)
        if scope is None:
            return PermissionDecision.DENY
        policy_name, policy = self._policy_for_scope(scope)
        return evaluate(policy_name, params, policy)

    # 检查权限；如需 ask 则向客户端发事件并等待响应；返回 (allowed, decision_str)
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        resolved_call: ResolvedToolCall | None = None,
        action: ToolAction | str | None = None,
        approval_requirement: ApprovalRequirement = ApprovalRequirement.POLICY,
        authority_override: AuthoritySnapshot | None = None,
    ) -> tuple[bool, str]:
        effective_action = (
            resolved_call.authority_action if resolved_call is not None else action
        )
        effective_approval = (
            resolved_call.effective_approval_requirement
            if resolved_call is not None
            else approval_requirement
        )
        scope = _permission_scope(tool_name, params, resolved_call)
        if scope is None:
            logger.error("permission manifest mismatch: tool=%s", tool_name)
            return False, "manifest_mismatch"
        if (
            self._audit_health is not None
            and self._audit_health.degraded
            and effective_action not in {ToolAction.READ, ToolAction.READ.value}
        ):
            logger.error(
                "audit degraded: denied mutating tool=%s action=%s",
                tool_name,
                effective_action,
            )
            return False, "audit_degraded"
        permission_key = scope.key
        policy_name, policy = self._policy_for_scope(scope)
        is_declared_shell = effective_action in {
            ToolAction.SHELL,
            ToolAction.SHELL.value,
        }
        is_shell = is_declared_shell or (
            resolved_call is None and tool_name == "bash"
        )
        command = str(params.get("command", "")) if is_shell else ""
        session_mode = self._session_modes.get(
            session_id,
            _SessionPermissionMode("interactive"),
        )
        authority_snapshot = (
            authority_override
            if authority_override is not None
            else self.get_effective_authority_snapshot(session_id)
        )
        authority_decision = AuthorityDecision.ASK
        if effective_action is not None:
            authority = evaluate_action(
                authority_snapshot,
                effective_action,
            )
            authority_decision = authority.decision
            if authority.decision == AuthorityDecision.DENY:
                logger.info(
                    "authority: denied tool=%s action=%s reason=%s",
                    tool_name,
                    effective_action,
                    authority.reason,
                )
                return False, "authority_denied"

        # Tier 1: deny_patterns（bash only，不可被缓存绕过）
        if command and policy:
            for pat in policy.deny_patterns:
                if re.search(pat, command):
                    logger.debug("permission: deny_pattern hit tool=%s", tool_name)
                    return False, "auto_deny"

        # Tier 2: 标记工作区外 bash，未显式 always 或 Full Access 时仍要求确认
        outside_cwd = bool(command and matches_outside_cwd(command))

        windows_shell_requires_approval = (
            is_declared_shell
            and authority_snapshot.sandbox.kind in {"windows_none", "windows_acl"}
        )

        if session_mode.mode == "interactive" and not windows_shell_requires_approval:
            # Tier 3: session always 缓存（用户显式选择后也适用于工作区外命令）
            session_key = (session_id, permission_key)
            if session_key in self._session_always:
                cached = self._session_always[session_key]
                logger.debug(
                    "permission: session cache hit tool=%s decision=%s",
                    permission_key,
                    cached,
                )
                return cached == "allow", f"auto_{cached}"

            # Tier 4: persistent always（跨 session，同样尊重用户对工作区外命令的选择）
            cached_key = next(
                (
                    key
                    for key in scope.lookup_keys
                    if key in self._persistent_always
                ),
                None,
            )
            if cached_key is not None:
                cached = self._persistent_always[cached_key]
                logger.debug(
                    "permission: persistent cache hit tool=%s decision=%s",
                    cached_key,
                    cached,
                )
                return cached == "allow", f"auto_{cached}"

            # Tier 4b: 命令前缀级 always（首 token 解析，见 W3.2）——bash 专属；
            # 前缀模式只落在 _persistent_always（串键），session 缓存仅存整键故不参与前缀匹配
            prefix_hit = self._match_prefix(self._persistent_always, policy_name, command)
            if prefix_hit is not None:
                logger.debug(
                    "permission: command-prefix cache hit command=%s decision=%s",
                    command,
                    prefix_hit,
                )
                return prefix_hit == "allow", "auto_always_prefix"

        if (
            effective_approval == ApprovalRequirement.NEVER
            and not windows_shell_requires_approval
        ):
            return True, "auto_allow"

        force_approval = effective_approval == ApprovalRequirement.ALWAYS
        if force_approval and (
            authority_snapshot.profile == AuthorityProfile.FULL_ACCESS
            and not windows_shell_requires_approval
        ):
            return True, "authority_allow"

        # Full Access 可自动执行工作区外命令，但前面的 deny_patterns 仍不可绕过
        if (
            not force_approval
            and not windows_shell_requires_approval
            and outside_cwd
            and authority_decision == AuthorityDecision.ALLOW
        ):
            return True, "authority_allow"

        # 权限闭环（W3.1）：AUTO_REVIEW + OS 沙箱可用时，bash 在沙箱内自动放行；
        # 沙箱不可用（降级）则回落后续 ASK 路径，deny_patterns 在 Tier 1 已不可绕过
        if (
            not force_approval
            and not windows_shell_requires_approval
            and policy_name == "bash"
            and command
            and authority_snapshot.profile == AuthorityProfile.AUTO_REVIEW
            and tier_for_auto_review(authority_snapshot.sandbox)
            != SandboxTier.NONE
        ):
            return True, "authority_sandbox_allow"

        if not outside_cwd and not force_approval and not windows_shell_requires_approval:
            # Tier 5: allow_patterns（bash only）
            if command and policy:
                for pat in policy.allow_patterns:
                    if re.search(pat, command):
                        return True, "auto_allow"

            # Tier 6: tool default
            if policy is not None:
                if policy.default == PermissionDecision.ALLOW:
                    return True, "auto_allow"
                if policy.default == PermissionDecision.DENY:
                    return False, "auto_deny"
                if authority_decision == AuthorityDecision.ALLOW:
                    return True, "authority_allow"
            elif authority_decision == AuthorityDecision.ALLOW:
                return True, "authority_allow"
            # default == ASK（bash、unknown tool）→ fall through to Future

        if session_mode.mode != "interactive":
            if (
                not outside_cwd
                and session_mode.mode == "allow_list"
                and bool(
                    {tool_name, *scope.lookup_keys}
                    & session_mode.allow_tools
                )
            ):
                return True, "headless_allow_list"
            if session_mode.mode == "fail_fast":
                return False, "headless_fail_fast"
            return False, "headless_deny"

        # ASK 路径（来自 OUTSIDE_CWD 强制 ASK，或 default=ASK）
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name,
        )

        preview = param_preview(policy_name, params)
        event_params = params
        if windows_shell_requires_approval:
            partial = authority_snapshot.sandbox.kind == "windows_acl"
            safety_notice = (
                "Windows partial sandbox: workspace write restrictions are enforced; "
                "reads and network are not isolated. Explicit approval is required."
                if partial
                else "No OS sandbox is available on Windows."
            )
            marker = "PARTIAL WINDOWS SANDBOX" if partial else "NO OS SANDBOX"
            preview = f"{preview} [{marker}: explicit approval required]"
            event_params = {
                **params,
                "_safety_notice": safety_notice,
            }
        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": event_params,
                "param_preview": preview,
                "session_id": session_id,
                "ts": _now(),
            }
        )

        try:
            if self._timeout_s > 0:
                raw = await asyncio.wait_for(future, timeout=self._timeout_s)
            else:
                raw = await future
        except TimeoutError:
            self._pending.pop(tool_use_id, None)
            logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
            return False, "timeout"
        except asyncio.CancelledError:
            self._pending.pop(tool_use_id, None)
            if not future.done():
                future.cancel()
            raise

        allowed = self._apply_response(
            raw,
            session_id,
            permission_key,
            prefix_key=(
                f"{policy_name}:{command_pattern_key(command)}"
                if policy_name == "bash" and command
                else ""
            ),
        )
        return allowed, raw

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(
        self,
        tool_use_id: str,
        decision: str,
        *,
        selected_hunks: list[str] | None = None,
        patch_plan_id: str | None = None,
    ) -> bool:
        req = self._pending.pop(tool_use_id, None)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return False
        if selected_hunks is not None or patch_plan_id is not None:
            self._response_metadata[tool_use_id] = {
                "selected_hunks": selected_hunks,
                "patch_plan_id": patch_plan_id,
            }
        if not req.future.done():
            req.future.set_result(decision)
        return True

    # 取出并清除一次性审批元数据，防止跨工具调用复用 hunk 选择
    def take_response_metadata(self, tool_use_id: str) -> dict[str, Any]:
        return self._response_metadata.pop(tool_use_id, {})

    # 在给定缓存中查找命中 command 的命令前缀规则，返回缓存值或 None
    @staticmethod
    def _match_prefix(
        cache: dict[str, str], policy_name: str, command: str
    ) -> str | None:
        if not command or policy_name != "bash":
            return None
        prefix = f"{policy_name}:"
        for key, value in cache.items():
            if key.startswith(prefix) and matches_command_pattern(
                command, key[len(prefix):]
            ):
                return value
        return None

    # 应用审批决策，更新 session + persistent 缓存，返回是否放行
    def _apply_response(
        self,
        decision: str,
        session_id: str,
        permission_key: str,
        *,
        prefix_key: str = "",
    ) -> bool:
        allow = decision in ("allow_once", "always_allow", "always_allow_pattern")
        if decision == "always_allow_pattern":
            if not prefix_key:
                return False
            self._store_always(prefix_key, "allow", session_id)
            return True
        if decision == "always_allow":
            self._store_always(permission_key, "allow", session_id)
        elif decision == "always_deny":
            self._store_always(permission_key, "deny", session_id)
        return allow

    # 写入持久 always 缓存并回写 policy 文件（session 键按 permission_key 缓存）
    def _store_always(
        self, key: str, value: str, session_id: str, permission_key: str | None = None
    ) -> None:
        self._persistent_always[key] = value
        if permission_key is not None:
            self._session_always[(session_id, permission_key)] = value
        logger.info(
            "permission: always %s key=%s policy_file=%s",
            value, key, self._policy_file,
        )
        if self._policy_file is not None:
            try:
                save_policy_file(
                    self._persistent_always,
                    self._policy_file,
                    authority_profile=self._default_authority.profile,
                )
                logger.info("permission: policy.toml written path=%s", self._policy_file)
            except Exception:
                logger.exception(
                    "permission: failed to write policy.toml path=%s", self._policy_file
                )
        else:
            logger.warning("permission: policy_file is None, skipping persistence")

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [
            uid for uid, req in self._pending.items()
            if req.session_id == session_id
        ]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug(
                    "permission: cancel pending tool_use_id=%s reason=%s", uid, reason
                )
                req.future.set_result("deny_once")
