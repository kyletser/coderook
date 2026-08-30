from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
from collections.abc import Coroutine
from typing import Any, cast

import code_rook
from code_rook.core.artifacts.image import ImageArtifactInput, inspect_image
from code_rook.core.artifacts.store import ArtifactStore
from code_rook.core.authority import RuntimeMode, WorkspaceTrust
from code_rook.core.bus.events import RuntimeEventAppendedEvent
from code_rook.core.change_center import ChangeCenterService
from code_rook.core.compatibility import build_runtime_capabilities
from code_rook.core.configuration import ConfigurationService
from code_rook.core.interaction import InteractionManager
from code_rook.core.llm.provider_presets import PROVIDER_PRESETS
from code_rook.core.llm.routes import ProviderRoute, get_route_preset
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.processes import ProcessSupervisor
from code_rook.core.receipts.models import TurnReceipt
from code_rook.core.runs import new_run_id
from code_rook.core.runtime.models import (
    RuntimeEventRecord,
    ThreadRecord,
    TurnItemRecord,
    TurnRecord,
)
from code_rook.core.runtime.service import RuntimeService
from code_rook.core.runtime.store import RecordNotFoundError
from code_rook.core.session.exporter import SessionExportFormat
from code_rook.core.session.manager import SessionManager
from code_rook.core.session.model import SessionMode
from code_rook.core.skills import SkillConfirmationRequired, SkillManager
from code_rook.core.skills.manager import InstallScope, InstallTrust
from code_rook.core.tools.builtin.git_diff import GitDiffTool
from code_rook.core.workspace import WorkspaceBoundary

logger = logging.getLogger(__name__)
_TURN_DURABILITY_TIMEOUT_S = 10.0


class RuntimeApiService:
    # 初始化统一 runtime API facade，所有写操作复用 SessionManager 状态机
    def __init__(
        self,
        runtime: RuntimeService,
        sessions: SessionManager,
        *,
        permission_manager: PermissionManager | None = None,
        workspace_boundary: WorkspaceBoundary | None = None,
        interaction_manager: InteractionManager | None = None,
        configuration: ConfigurationService | None = None,
        process_supervisor: ProcessSupervisor | None = None,
        artifact_store: ArtifactStore | None = None,
        labs_enabled: bool | None = None,
    ) -> None:
        self._runtime = runtime
        self._sessions = sessions
        self._tasks: set[asyncio.Task[Any]] = set()
        self._event_changed = asyncio.Condition()
        self._permission_manager = permission_manager
        self._workspace_boundary = workspace_boundary
        self._interaction_manager = interaction_manager
        self._configuration = configuration
        self._process_supervisor = process_supervisor
        self._artifact_store = artifact_store
        self._git_diff = GitDiffTool(workspace_boundary) if workspace_boundary else None
        self._change_center = (
            ChangeCenterService(workspace_boundary, process_supervisor)
            if workspace_boundary is not None
            else None
        )
        self._labs_enabled = labs_enabled

    @property
    # 返回 Web 与 IDE 客户端被允许展示的唯一工作区根
    def workspace_root(self) -> str:
        boundary = self._workspace_boundary
        return str(boundary.root) if boundary is not None else ""

    # 返回浏览器可展示的内置 Provider Catalog 与能力标签，不包含任何凭据正文
    async def provider_catalog(self) -> dict[str, object]:
        presets = [
            {
                "id": preset.id,
                "name": preset.name,
                "description": preset.description,
                "provider": preset.provider_kind,
                "wire_format": preset.wire_format,
                "base_url": preset.chat_url,
                "models": list(preset.preferred_models),
                "credential_required": preset.credential_required,
                "local": preset.local_probe,
                "capabilities": {
                    "tools": preset.supports_tools,
                    "parallel_tools": preset.supports_parallel_tools,
                    "images": preset.supports_images,
                    "prompt_cache": preset.supports_prompt_cache,
                },
            }
            for preset in PROVIDER_PRESETS
        ]
        snapshot = self._configuration_snapshot()
        return {"presets": presets, **snapshot}

    # 返回当前路由的脱敏配置投影，凭据只暴露来源和是否就绪
    def _configuration_snapshot(self) -> dict[str, object]:
        if self._configuration is None:
            return {
                "active_route_id": None,
                "routes": [],
                "readiness": {
                    "status": "unconfigured",
                    "local_ready": False,
                    "reason": "provider configuration is unavailable",
                },
            }
        snapshot = self._configuration.snapshot()
        routes = []
        for route in snapshot.routes:
            route_payload = route.model_dump(
                mode="json",
                exclude={"credential_ref", "doctor_receipt"},
            )
            route_payload["credential_source"] = snapshot.credential_sources.get(route.id)
            route_payload["doctor_verified"] = route.has_current_doctor_receipt()
            routes.append(route_payload)
        return {
            "active_route_id": snapshot.active_route_id,
            "routes": routes,
            "readiness": snapshot.readiness.model_dump(mode="json"),
            "route_issues": [issue.model_dump(mode="json") for issue in snapshot.route_issues],
        }

    # 诊断并保存一个 Provider 路由，API Key 只在本次 Core 请求内出现
    async def save_provider(self, payload: dict[str, Any]) -> dict[str, object]:
        configuration = self._require_configuration()
        route_id = str(payload.get("route_id", "")).strip()
        preset_id = str(payload.get("preset_id", "")).strip()
        model = str(payload.get("model", "")).strip()
        if not route_id or not preset_id or not model:
            raise ValueError("route_id, preset_id and model are required")
        update = bool(payload.get("update", False))
        base = (
            configuration.routes.get(route_id)
            if update
            else get_route_preset(preset_id)
        )
        changes: dict[str, object] = {
            "id": route_id,
            "model": model,
            "catalog_id": preset_id,
            "doctor_receipt": None,
        }
        if payload.get("base_url") is not None:
            changes["base_url"] = str(payload["base_url"]).strip()
        route = ProviderRoute.model_validate(
            {**base.model_dump(mode="python"), **changes}
        )
        raw_secret = payload.get("api_key")
        if raw_secret is not None and not isinstance(raw_secret, str):
            raise ValueError("api_key must be a string")
        saved = await configuration.save_route_checked(
            route,
            secret=raw_secret.strip() if isinstance(raw_secret, str) else None,
            activate=bool(payload.get("activate", True)),
            update=update,
        )
        active = configuration.routes.active()
        return {
            "route_id": saved.id,
            "active": active is not None and active.id == saved.id,
            **self._configuration_snapshot(),
        }

    # 对已保存 Provider 执行真实 Doctor 后激活，失败时不改变活动路由
    async def activate_provider(self, route_id: str) -> dict[str, object]:
        configuration = self._require_configuration()
        route = await configuration.set_active_checked(route_id)
        return {"route_id": route.id, **self._configuration_snapshot()}

    # 删除指定 Provider 路由，并按显式开关决定是否同步删除凭据
    async def delete_provider(
        self,
        route_id: str,
        *,
        delete_credential: bool,
    ) -> dict[str, object]:
        configuration = self._require_configuration()
        configuration.remove_route(route_id, delete_credential=delete_credential)
        return self._configuration_snapshot()

    # 要求 Core 已装配统一 Provider 配置事务服务
    def _require_configuration(self) -> ConfigurationService:
        if self._configuration is None:
            raise ValueError("provider configuration is unavailable")
        return self._configuration

    # 在工作区边界内列出一层目录和可模糊搜索的文件，拒绝越界符号链接
    async def list_workspace_files(
        self,
        *,
        path: str = ".",
        query: str = "",
        limit: int = 300,
    ) -> dict[str, object]:
        boundary = self._require_workspace()
        selected = boundary.resolve(path)
        if not selected.is_dir():
            raise ValueError("workspace path is not a directory")
        normalized_query = query.strip().casefold()
        roots = selected.rglob("*") if normalized_query else selected.iterdir()
        entries: list[dict[str, object]] = []
        skipped = {
            ".git",
            ".coderook",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "node_modules",
            "dist",
            "build",
        }
        for candidate in roots:
            if any(part in skipped for part in candidate.relative_to(boundary.root).parts):
                continue
            try:
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(boundary.root).as_posix()
            except (OSError, RuntimeError, ValueError):
                continue
            if normalized_query and normalized_query not in relative.casefold():
                continue
            entries.append(
                {
                    "path": relative,
                    "name": candidate.name,
                    "kind": "directory" if candidate.is_dir() else "file",
                    "size": candidate.stat().st_size if candidate.is_file() else None,
                }
            )
            if len(entries) >= max(1, min(limit, 500)):
                break
        entries.sort(key=lambda item: (item["kind"] != "directory", str(item["path"])))
        return {
            "root": str(boundary.root),
            "path": selected.relative_to(boundary.root).as_posix() or ".",
            "entries": entries,
            "truncated": len(entries) >= max(1, min(limit, 500)),
        }

    # 在工作区边界内读取有界文本文件，二进制与超大文件只返回安全元数据
    async def read_workspace_file(self, path: str) -> dict[str, object]:
        boundary = self._require_workspace()
        selected = boundary.resolve(path)
        if not selected.is_file():
            raise ValueError("workspace path is not a file")
        size = selected.stat().st_size
        media_type = mimetypes.guess_type(selected.name)[0] or "text/plain"
        if size > 2 * 1024 * 1024:
            return {
                "path": selected.relative_to(boundary.root).as_posix(),
                "size": size,
                "media_type": media_type,
                "content": "",
                "truncated": True,
                "binary": False,
            }
        raw = selected.read_bytes()
        if b"\x00" in raw[:8192]:
            return {
                "path": selected.relative_to(boundary.root).as_posix(),
                "size": size,
                "media_type": media_type,
                "content": "",
                "truncated": False,
                "binary": True,
            }
        return {
            "path": selected.relative_to(boundary.root).as_posix(),
            "size": size,
            "media_type": media_type,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": False,
            "binary": False,
        }

    # 要求 HTTP 服务已绑定受限工作区
    def _require_workspace(self) -> WorkspaceBoundary:
        if self._workspace_boundary is None:
            raise ValueError("workspace access is unavailable")
        return self._workspace_boundary

    # 列出当前优先级下的 Skill 元数据，系统提示正文不通过 Web 暴露
    async def list_skills(self) -> dict[str, object]:
        manager = SkillManager(self._require_workspace().root)
        return {
            "skills": [
                skill.model_dump(
                    mode="json",
                    exclude={"system_prompt_template"},
                )
                for skill in manager.list_all()
            ]
        }

    # 预览或显式确认安装工作区内 Skill，禁止浏览器引用工作区外任意源
    async def install_skill(self, payload: dict[str, Any]) -> dict[str, object]:
        boundary = self._require_workspace()
        source = boundary.resolve(str(payload.get("source", "")))
        raw_scope = str(payload.get("scope", "project"))
        raw_trust = str(payload.get("trust", "untrusted"))
        if raw_scope not in {"project", "user"}:
            raise ValueError("scope must be project or user")
        if raw_trust not in {"trusted", "untrusted"}:
            raise ValueError("trust must be trusted or untrusted")
        manager = SkillManager(boundary.root)
        try:
            skill = manager.install(
                str(source),
                scope=cast(InstallScope, raw_scope),
                trust=cast(InstallTrust, raw_trust),
                confirmed=payload.get("confirmed") is True,
                overwrite=payload.get("overwrite") is True,
            )
        except SkillConfirmationRequired as exc:
            return {
                "installed": False,
                "confirmation_required": True,
                "preview": exc.preview.model_dump(mode="json"),
            }
        return {
            "installed": True,
            "confirmation_required": False,
            "skill": skill.model_dump(
                mode="json",
                exclude={"system_prompt_template"},
            ),
        }

    # 经显式确认从受管 project 或 user 目录删除 Skill
    async def remove_skill(
        self,
        name: str,
        *,
        scope: str,
        confirmed: bool,
    ) -> dict[str, object]:
        if scope not in {"project", "user"}:
            raise ValueError("scope must be project or user")
        SkillManager(self._require_workspace().root).remove(
            name,
            scope=cast(InstallScope, scope),
            confirmed=confirmed,
        )
        return {"name": name, "scope": scope, "removed": True}

    # 响应活动工具审批并返回是否命中待处理请求
    async def respond_permission(
        self,
        tool_use_id: str,
        decision: str,
        *,
        selected_hunks: list[str] | None = None,
        patch_plan_id: str | None = None,
    ) -> dict[str, object]:
        if self._permission_manager is None:
            raise ValueError("permission control is unavailable")
        accepted = self._permission_manager.respond(
            tool_use_id,
            decision,
            selected_hunks=selected_hunks,
            patch_plan_id=patch_plan_id,
        )
        return {"tool_use_id": tool_use_id, "accepted": accepted}

    # 读取工作区结构化 diff，供 IDE 等 HTTP 客户端打开变更视图
    async def workspace_diff(
        self,
        *,
        scope: str = "all",
        path: str = ".",
    ) -> dict[str, object]:
        if (
            self._git_diff is None
            or self._workspace_boundary is None
            or self._change_center is None
        ):
            raise ValueError("workspace diff is unavailable")
        if path == ".":
            return await self._change_center.diff(scope)
        result = await self._git_diff.invoke({"scope": scope, "path": path})
        payload = json.loads(result.content)
        if not isinstance(payload, dict):
            raise ValueError("workspace diff returned an invalid payload")
        return cast(dict[str, object], payload)

    # 将用户已完整审查的选定文件加入 index，并返回新的 staged 审查摘要
    async def workspace_stage(
        self,
        thread_id: str,
        paths: list[str],
        *,
        expected_digest: str,
        confirmed: bool,
    ) -> dict[str, object]:
        self._require_change_mutation(thread_id, confirmed=confirmed)
        self._require_workspace()
        if self._change_center is None:
            raise ValueError("workspace change center is unavailable")
        async with self._sessions.workspace_mutation():
            return await self._change_center.stage(
                paths,
                expected_digest=expected_digest,
            )

    # 从已审查 staged tree 创建本地 commit，永不自动 push
    async def workspace_commit(
        self,
        thread_id: str,
        message: str,
        *,
        expected_digest: str,
        confirmed: bool,
    ) -> dict[str, object]:
        self._require_change_mutation(thread_id, confirmed=confirmed)
        self._require_workspace()
        if self._change_center is None:
            raise ValueError("workspace change center is unavailable")
        async with self._sessions.workspace_mutation():
            result = await self._change_center.commit(
                message,
                expected_digest=expected_digest,
            )
        return {
            "commit": result.commit,
            "subject": result.subject,
            "files": list(result.files),
            "hooks_skipped": result.hooks_skipped,
        }

    # 校验会话空闲、工作区受信且用户显式确认后才允许 Git mutation
    def _require_change_mutation(self, thread_id: str, *, confirmed: bool) -> None:
        if not confirmed:
            raise ValueError("change action requires explicit confirmation")
        self._sessions.get_session(thread_id)
        if self._sessions.is_busy(thread_id) or self._sessions.active_run_count() != 0:
            raise ValueError("change action is blocked while a workspace turn is active")
        if self._permission_manager is None:
            raise ValueError("permission control is unavailable")
        authority = self._permission_manager.get_effective_authority_snapshot(thread_id)
        if authority.workspace_trust != WorkspaceTrust.TRUSTED:
            raise ValueError("change action requires a trusted workspace")

    # 作为 EventBus 订阅者：新的 durable 事件落盘后唤醒全部等待者
    async def notify_runtime_event(self, event: Any) -> None:
        if not isinstance(event, RuntimeEventAppendedEvent):
            return
        async with self._event_changed:
            self._event_changed.notify_all()

    # 挂起等待新的 runtime 事件通知，超时自动返回（供 SSE 与 create_turn 使用）
    async def wait_for_change(self, timeout: float) -> None:
        async with self._event_changed:
            try:
                await asyncio.wait_for(self._event_changed.wait(), timeout=timeout)
            except TimeoutError:
                return

    # 跟踪 API 启动的后台 turn 并记录未被读取的异常
    def _track(self, coroutine: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)

        # 移除完成任务并消费异常，避免事件循环输出未读取警告
        def finished(done: asyncio.Task[Any]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                logger.error("API background turn failed: %s", error)

        task.add_done_callback(finished)
        return task

    # 列出所有 durable threads
    async def list_threads(self) -> list[ThreadRecord]:
        sessions = await self._sessions.list_sessions(include_closed=False, limit=200)
        records: list[ThreadRecord] = []
        for session in sessions:
            try:
                records.append(await self._runtime.get_thread(session.id))
            except RecordNotFoundError:
                logger.warning(
                    "session runtime projection missing after bootstrap sid=%s",
                    session.id,
                )
        return records

    # 创建 chat thread 并返回其 durable 投影
    async def create_thread(self, title: str, mode: str) -> ThreadRecord:
        session = await self._sessions.create(mode=cast(SessionMode, mode), title=title)
        return await self._runtime.get_thread(session.id)

    # 将浏览器提交的后续消息交给 Core 持久队列
    async def queue_message(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode,
        attachments: list[ImageArtifactInput] | None = None,
        *,
        display_content: str | None = None,
    ) -> dict[str, object]:
        record = await self._sessions.queue_message(
            thread_id,
            content,
            runtime_mode=mode,
            attachments=attachments,
            display_content=display_content,
        )
        return record.model_dump(mode="json")

    # 返回当前 thread 在所有前端之间共享的持久消息队列
    async def list_queued_messages(self, thread_id: str) -> list[dict[str, object]]:
        records = await self._sessions.list_queued_messages(thread_id)
        return [record.model_dump(mode="json") for record in records]

    # 删除浏览器明确取消的排队消息
    async def remove_queued_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> dict[str, object]:
        await self._sessions.remove_queued_message(thread_id, message_id)
        return {"thread_id": thread_id, "message_id": message_id, "removed": True}

    # 重试处于 blocked 状态的排队消息
    async def retry_queued_message(
        self,
        thread_id: str,
        message_id: str,
    ) -> dict[str, object]:
        await self._sessions.retry_queued_message(thread_id, message_id)
        return {"thread_id": thread_id, "message_id": message_id, "retried": True}

    # 读取单个 durable thread
    async def get_thread(self, thread_id: str) -> ThreadRecord:
        return await self._runtime.get_thread(thread_id)

    # 通过 SessionManager 更新标题或归档状态并返回 durable 投影
    async def update_thread(
        self,
        thread_id: str,
        *,
        title: str | None = None,
        archived: bool | None = None,
    ) -> ThreadRecord:
        if title is not None:
            await self._sessions.rename(thread_id, title)
        if archived is True:
            await self._sessions.close(thread_id)
        elif archived is False:
            raise ValueError("unarchiving is not supported by the current session ledger")
        return await self._runtime.get_thread(thread_id)

    # 从指定会话创建可独立继续的历史 Fork，并返回新的 durable thread
    async def fork_thread(self, thread_id: str, *, title: str = "") -> ThreadRecord:
        session = await self._sessions.fork(thread_id, title)
        return await self._runtime.get_thread(session.id)

    # 导出会话为 Markdown 或 JSON 正文，不在服务端写入用户任意路径
    async def export_thread(
        self,
        thread_id: str,
        export_format: SessionExportFormat,
    ) -> dict[str, str]:
        filename, media_type, content = await self._sessions.export(
            thread_id,
            export_format,
        )
        return {"filename": filename, "media_type": media_type, "content": content}

    # 删除空闲会话及其 runtime 投影，活动会话由 SessionManager 拒绝
    async def delete_thread(self, thread_id: str) -> dict[str, object]:
        await self._sessions.delete(thread_id)
        return {"thread_id": thread_id, "deleted": True}

    # 返回会话的上下文摘要与最近 checkpoint 元数据
    async def thread_context(self, thread_id: str) -> dict[str, object]:
        run_id, checkpoints = self._sessions.list_checkpoints(thread_id)
        return {
            **self._sessions.context_info(thread_id),
            "checkpoint_run_id": run_id,
            "checkpoints": checkpoints,
        }

    # 返回 checkpoint 恢复预览，浏览器确认前不修改工作区
    async def preview_rewind(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._sessions.preview_rewind(thread_id, checkpoint_id, run_id),
        )

    # 按用户确认的状态摘要恢复 checkpoint，避免基于过期预览覆盖文件
    async def rewind_thread(
        self,
        thread_id: str,
        checkpoint_id: str,
        *,
        run_id: str | None = None,
        expected_digest: str | None = None,
    ) -> dict[str, object]:
        async with self._sessions.workspace_mutation():
            return cast(
                dict[str, object],
                self._sessions.rewind(
                    thread_id,
                    checkpoint_id,
                    run_id,
                    expected_digest=expected_digest,
                ),
            )

    # 响应指定 Turn 的 Plan 卡片并复用 SessionManager 的持久审批不变量
    async def respond_plan(
        self,
        thread_id: str,
        turn_id: str,
        decision: str,
        revision: str = "",
    ) -> dict[str, object]:
        if decision not in {"approve", "revise", "cancel"}:
            raise ValueError("invalid plan decision")
        event = await self._sessions.respond_plan(
            thread_id,
            turn_id,
            cast(Any, decision),
            revision,
        )
        return event.model_dump(mode="json")

    # 回答当前 Agent 的结构化问题，未知或已过期问题明确返回未接受
    async def answer_question(self, question_id: str, answer: str) -> dict[str, object]:
        if self._interaction_manager is None:
            raise ValueError("question control is unavailable")
        normalized = answer.strip()
        if not normalized:
            raise ValueError("answer must not be blank")
        accepted = self._interaction_manager.answer(question_id, normalized)
        return {"question_id": question_id, "accepted": accepted}

    # 验证 thread 存在，供长连接在发送 200 header 前失败
    async def ensure_thread(self, thread_id: str) -> None:
        await self._runtime.get_thread(thread_id)

    # 启动后台 turn，并等待其 durable running 记录对读接口可见
    async def create_turn(
        self,
        thread_id: str,
        content: str,
        mode: RuntimeMode,
        attachments: list[ImageArtifactInput] | None = None,
        *,
        display_content: str | None = None,
    ) -> TurnRecord:
        run_id = new_run_id()
        task = self._track(
            self._sessions.send_message(
                thread_id,
                content,
                run_id=run_id,
                runtime_mode=mode,
                attachments=attachments,
                display_content=display_content,
            ),
            name=f"api-turn:{run_id}",
        )
        deadline = time.monotonic() + _TURN_DURABILITY_TIMEOUT_S
        while True:
            try:
                return await self._runtime.get_turn(run_id)
            except RecordNotFoundError:
                if task.done():
                    await task
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await self.wait_for_change(min(remaining, 0.1))
        raise TimeoutError(
            "turn did not enter durable runtime within "
            f"{_TURN_DURABILITY_TIMEOUT_S:g} seconds"
        )

    # 校验浏览器图片并存入内容寻址 Artifact，返回发送 Turn 所需的脱敏元数据
    async def upload_image(self, encoded: str) -> dict[str, object]:
        if self._artifact_store is None:
            raise ValueError("artifact storage is unavailable")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("image data must be valid base64") from exc
        if not data or len(data) > 2 * 1024 * 1024:
            raise ValueError("image must contain between 1 byte and 2 MiB")
        metadata = inspect_image(data)
        reference = await self._artifact_store.put(data, media_type=metadata.media_type)
        return ImageArtifactInput(
            sha256=reference.sha256,
            media_type=metadata.media_type,
            size=reference.size,
            width=metadata.width,
            height=metadata.height,
        ).model_dump(mode="json")

    # 分页读取持久 Artifact 文本，完整大输出不需要重新进入时间线
    async def read_artifact(
        self,
        sha256: str,
        *,
        offset: int = 0,
        limit: int = 20_000,
    ) -> dict[str, object]:
        if self._artifact_store is None:
            raise ValueError("artifact storage is unavailable")
        result = await self._artifact_store.read(sha256, offset=offset, limit=limit)
        return result.model_dump(mode="json")

    # 列出指定 thread 的 durable turns
    async def list_turns(self, thread_id: str) -> list[TurnRecord]:
        await self._runtime.get_thread(thread_id)
        return await self._runtime.list_turns(thread_id)

    # 读取单个 durable turn
    async def get_turn(self, turn_id: str) -> TurnRecord:
        return await self._runtime.get_turn(turn_id)

    # 中断当前活动 turn
    async def interrupt_turn(self, turn_id: str) -> TurnRecord:
        await self._sessions.cancel_run(turn_id)
        return await self._runtime.get_turn(turn_id)

    # 向当前活动 turn 注入用户 steering 指令
    async def steer_turn(self, turn_id: str, content: str) -> TurnRecord:
        await self._sessions.steer_run(turn_id, content)
        return await self._runtime.get_turn(turn_id)

    # 读取 thread 的 durable 事件游标窗口
    async def list_events(
        self,
        thread_id: str,
        after_seq: int,
        limit: int = 1000,
    ) -> list[RuntimeEventRecord]:
        return await self._runtime.list_events(thread_id, after_seq=after_seq, limit=limit)

    # 读取 turn 的 durable items
    async def list_items(self, turn_id: str) -> list[TurnItemRecord]:
        await self._runtime.get_turn(turn_id)
        return await self._runtime.list_items(turn_id)

    # 读取 turn 的 durable receipt
    async def get_receipt(self, turn_id: str) -> TurnReceipt:
        return await self._runtime.get_receipt(turn_id)

    # 返回服务端可协商的 API 和运行能力
    async def capabilities(self) -> dict[str, Any]:
        permission_manager = getattr(self, "_permission_manager", None)
        sandbox = (
            permission_manager.get_authority_snapshot(
                "__runtime_capabilities__"
            ).sandbox
            if permission_manager is not None
            else None
        )
        return build_runtime_capabilities(
            code_rook.__version__,
            sandbox=sandbox,
            labs_enabled=getattr(self, "_labs_enabled", None),
        ).model_dump(mode="json")

    # 汇总全部 durable turns 的 token usage 与状态计数
    async def usage(self) -> dict[str, Any]:
        totals: dict[str, int] = {}
        status_counts: dict[str, int] = {}
        turn_count = 0
        estimated_cost_usd = 0.0
        cost_known = True
        threads = await self._runtime.list_threads()
        for thread in threads:
            for turn in await self._runtime.list_turns(thread.id):
                turn_count += 1
                status_counts[turn.status.value] = status_counts.get(turn.status.value, 0) + 1
                for key, value in turn.usage.items():
                    if key.endswith("tokens") and isinstance(value, (int, float)):
                        totals[key] = totals.get(key, 0) + int(value)
                turn_cost = turn.usage.get("estimated_cost_usd")
                if isinstance(turn_cost, (int, float)):
                    estimated_cost_usd += float(turn_cost)
                elif turn.usage:
                    cost_known = False
        return {
            "threads": len(threads),
            "turns": turn_count,
            "status_counts": status_counts,
            "tokens": totals,
            "cost": estimated_cost_usd if cost_known else "unknown",
        }

    # 等待 API 启动的后台 turn 结束，仅用于受控关闭与测试
    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
