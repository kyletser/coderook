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

from code_rook.core.authority import (
    AuthorityDecision,
    AuthorityProfile,
    AuthoritySnapshot,
    ToolAction,
    detect_sandbox_capability,
    evaluate_action,
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
from code_rook.core.tools.spec import ApprovalRequirement

logger = logging.getLogger(__name__)

PermissionRunMode = Literal["interactive", "deny", "fail_fast", "allow_list"]

_FAMILY_POLICY_ALIASES: dict[tuple[str, str], str] = {
    ("Bash", "run"): "bash",
    ("Bash", "wait"): "background_result",
    ("Bash", "interact"): "background_interact",
    ("Bash", "cancel"): "background_cancel",
    ("File", "read"): "read_file",
    ("File", "list"): "list_dir",
    ("File", "search_name"): "glob",
    ("File", "search_content"): "grep",
    ("File", "write"): "write_file",
    ("File", "edit"): "edit_file",
    ("File", "patch"): "apply_patch",
    ("Git", "diff"): "git_diff",
    ("Run", "tests"): "run_tests",
    ("Run", "verifiers"): "run_verifiers",
    ("agent", "start"): "spawn_agent",
    ("agent", "status"): "agent_result",
    ("agent", "peek"): "agent_result",
    ("agent", "wait"): "agent_result",
    ("agent", "cancel"): "spawn_agent",
    ("agent", "followup"): "spawn_agent",
}


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


# 返回 action-family 调用的精确缓存键和兼容旧策略名
def _permission_scope(
    tool_name: str,
    params: dict[str, Any],
) -> tuple[str, str]:
    action = params.get("action")
    if not isinstance(action, str) or not action:
        return tool_name, tool_name
    scope = f"{tool_name}.{action}"
    return scope, _FAMILY_POLICY_ALIASES.get((tool_name, action), scope)


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
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
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

    # 持久化全局权限姿态并同步已有会话，避免每次启动后重新批准
    def set_default_profile(self, profile: AuthorityProfile) -> None:
        self._default_authority = self._default_authority.model_copy(
            update={"profile": profile}
        )
        self._session_authorities = {
            session_id: snapshot.model_copy(update={"profile": profile})
            for session_id, snapshot in self._session_authorities.items()
        }
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

    # 清除 session 的 authority 配置并恢复默认值
    def clear_authority_snapshot(self, session_id: str) -> None:
        self._session_authorities.pop(session_id, None)

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

    # 对工具名 + 参数执行 4 层静态评估，不挂起
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        from code_rook.core.permissions.policy import evaluate
        _permission_key, policy_name = _permission_scope(tool_name, params)
        policy = self._policies.get(policy_name)
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
        action: ToolAction | str | None = None,
        approval_requirement: ApprovalRequirement = ApprovalRequirement.POLICY,
    ) -> tuple[bool, str]:
        permission_key, policy_name = _permission_scope(tool_name, params)
        command = str(params.get("command", "")) if policy_name == "bash" else ""
        policy = self._policies.get(policy_name)
        session_mode = self._session_modes.get(
            session_id,
            _SessionPermissionMode("interactive"),
        )
        authority_decision = AuthorityDecision.ASK
        if action is not None:
            authority = evaluate_action(
                self.get_authority_snapshot(session_id),
                action,
            )
            authority_decision = authority.decision
            if authority.decision == AuthorityDecision.DENY:
                logger.info(
                    "authority: denied tool=%s action=%s reason=%s",
                    tool_name,
                    action,
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

        if session_mode.mode == "interactive":
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
            cached_key = (
                permission_key
                if permission_key in self._persistent_always
                else policy_name
            )
            if cached_key in self._persistent_always:
                cached = self._persistent_always[cached_key]
                logger.debug(
                    "permission: persistent cache hit tool=%s decision=%s",
                    cached_key,
                    cached,
                )
                return cached == "allow", f"auto_{cached}"

        if approval_requirement == ApprovalRequirement.NEVER:
            return True, "auto_allow"

        force_approval = approval_requirement == ApprovalRequirement.ALWAYS
        if force_approval and (
            self.get_authority_snapshot(session_id).profile
            == AuthorityProfile.FULL_ACCESS
        ):
            return True, "authority_allow"

        # Full Access 可自动执行工作区外命令，但前面的 deny_patterns 仍不可绕过
        if (
            not force_approval
            and outside_cwd
            and authority_decision == AuthorityDecision.ALLOW
        ):
            return True, "authority_allow"

        if not outside_cwd and not force_approval:
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
                    {tool_name, permission_key, policy_name}
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

        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": params,
                "param_preview": param_preview(tool_name, params),
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

        allowed = self._apply_response(raw, session_id, permission_key)
        return allowed, raw

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(self, tool_use_id: str, decision: str) -> None:
        req = self._pending.pop(tool_use_id, None)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return
        if not req.future.done():
            req.future.set_result(decision)

    # 应用审批决策，更新 session + persistent 缓存，返回是否放行
    def _apply_response(self, decision: str, session_id: str, permission_key: str) -> bool:
        allow = decision in ("allow_once", "always_allow")
        if decision == "always_allow":
            self._session_always[(session_id, permission_key)] = "allow"
            self._persistent_always[permission_key] = "allow"
            logger.info(
                "permission: always allow tool=%s policy_file=%s persistent=%s",
                permission_key, self._policy_file, self._persistent_always,
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
        elif decision == "always_deny":
            self._session_always[(session_id, permission_key)] = "deny"
            self._persistent_always[permission_key] = "deny"
            logger.info(
                "permission: always deny tool=%s policy_file=%s persistent=%s",
                permission_key, self._policy_file, self._persistent_always,
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
        return allow

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
