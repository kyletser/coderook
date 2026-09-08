from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from code_rook.core.authority import AuthorityProfile, AuthoritySnapshot, WorkspaceTrust
from code_rook.core.bus.events import LlmUsageEvent, ToolCallFinishedEvent, ToolCallStartedEvent
from code_rook.core.config import CodeRookConfig
from code_rook.core.events.bus import EventBus
from code_rook.core.llm.route_registry import ResolvedRoute
from code_rook.core.llm.routes import ProviderRoute, RouteReceipt
from code_rook.core.llm.types import LlmResponse, ToolCallBlock
from code_rook.core.permissions.manager import PermissionManager
from code_rook.core.runner import AgentRunner
from code_rook.core.session.model import Session
from code_rook.core.session.store import SessionStore
from code_rook.core.strategy import TaskStrategy

BENCHMARK_TOOLS = ('File', 'Git', 'Bash', 'Run', 'artifact_read', 'Repository')


class SmokeProvider:
    # 初始化不联网的两步假模型，仅检查真实 Shell 工具和运行时能完成一次任务
    def __init__(self) -> None:
        self.calls = 0

    # 先要求执行无修改命令再结束，不读取 benchmark 标准答案或调用远端模型
    async def chat(self, **kwargs: object) -> LlmResponse:
        self.calls += 1
        if self.calls == 1:
            return LlmResponse(stop_reason='tool_use', completion_status='tool_use',
                tool_calls=[ToolCallBlock('smoke-shell', 'Bash', {
                    'command': 'python --version && git rev-parse HEAD && printf CODEROOK_SMOKE_OK',
                })])
        return LlmResponse(stop_reason='end_turn', completion_status='completed',
                           text='Runtime smoke finished.')


# 在独立实例容器内运行原生 Agent，凭据仅经 stdin 进入进程内存而不写入配置或日志
async def run(payload: dict) -> None:
    workspace = Path('/testbed')
    evidence = Path('/coderook-evidence')
    evidence.mkdir(exist_ok=True)
    route = ProviderRoute.model_validate(payload['route'])
    resolved = ResolvedRoute(
        route=route,
        receipt=RouteReceipt.model_validate(payload['receipt']),
        credential=payload.pop('credential'),
    )
    config = CodeRookConfig()
    config.agent.max_steps = payload['max_steps']
    config.agent.max_step_continues = 0
    config.agent.task_router = 'rules_only'
    config.llm.default_model = route.model
    config.llm.provider = 'openai-compatible'
    config.llm.base_url = str(route.base_url)
    sid = 'sess-swebench'
    stamp = datetime.now(UTC).isoformat()
    session = Session(sid, 'one_shot', 'active', payload['instance_id'], stamp, stamp,
                      workspace=str(workspace))
    store = SessionStore(evidence / 'sessions')
    store.write_meta(session)
    goal = (
        'Fix the following repository issue. Inspect the implementation, reproduce the '
        'problem, make a focused source-code fix, and run relevant existing tests plus '
        'a regression check. Work only in /testbed. Do not commit or change existing tests '
        'to conceal failures. Do not retrieve upstream solutions or benchmark answers. '
        'The repository test environment is the conda testbed environment, already on PATH. '
        'Finish once the fix and focused verification are complete.\n\n'
        + payload['problem_statement']
    )
    store.append_message(sid, 'user', goal)
    tools = list(BENCHMARK_TOOLS)
    permissions = PermissionManager(timeout_s=0)
    permissions.set_authority_snapshot(sid, AuthoritySnapshot(
        profile=AuthorityProfile.FULL_ACCESS, workspace_trust=WorkspaceTrust.TRUSTED,
    ))
    permissions.set_session_mode(sid, 'allow_list', allow_tools=tools)
    bus = EventBus()
    usage: list[dict] = []
    calls = 0
    event_file = evidence / 'events.jsonl'

    # 保存真实执行事件与 usage；任何意外包含凭据的字段在落盘前精确脱敏
    async def capture(event: BaseModel) -> None:
        nonlocal calls
        raw = event.model_dump(mode='json')
        encoded = json.dumps(raw, ensure_ascii=False).replace(resolved.credential, '[REDACTED]')
        with event_file.open('a', encoding='utf-8') as stream:
            stream.write(encoded + '\n')
        if isinstance(event, LlmUsageEvent):
            usage.append(raw)
        if isinstance(event, ToolCallStartedEvent):
            calls += 1
            print(json.dumps({'tool': event.tool_name, 'call': calls}), flush=True)
        elif isinstance(event, ToolCallFinishedEvent):
            print(json.dumps({'tool_finished': event.tool_name}), flush=True)

    bus.subscribe(capture)
    runner = AgentRunner(config, bus=bus, permission_manager=permissions,
                         provider=SmokeProvider() if payload.get('smoke') else None,
                         workspace_root=workspace, runs_dir=evidence / 'runs')
    started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(runner.run_and_capture(
            goal, session=session, store=store, resolved_route=resolved,
            resolved_route_is_explicit=True, tool_whitelist=tools,
            strategy_override=TaskStrategy.DIRECT,
        ), timeout=payload['wall_time'])
        result = {'status': outcome.status, 'reason': outcome.reason,
                  'result': outcome.result}
    except TimeoutError:
        result = {'status': 'failed', 'reason': 'wall_time_exceeded'}
    except Exception as exc:
        result = {'status': 'failed', 'reason': type(exc).__name__,
                  'error': str(exc).replace(resolved.credential, '[REDACTED]')}
    result.update(instance_id=payload['instance_id'], elapsed_s=time.monotonic() - started,
                  tool_calls=calls, usage=usage, model=route.model)
    if payload.get('smoke'):
        events = [json.loads(line) for line in event_file.read_text(encoding='utf-8').splitlines()]
        result['smoke_passed'] = any(
            event.get('type') == 'tool.call_finished'
            and 'CODEROOK_SMOKE_OK' in event.get('output', '') for event in events
        ) and not usage
    text = json.dumps(result, ensure_ascii=False, indent=2).replace(
        resolved.credential, '[REDACTED]')
    (evidence / 'execution.json').write_text(text, encoding='utf-8')
    print(json.dumps({'finished': result['status'], 'tool_calls': calls}), flush=True)


# 只在基准专用容器入口读取 stdin 请求，不从仓库读取模型凭据
def main() -> None:
    payload = json.load(sys.stdin)
    os.chdir('/testbed')
    asyncio.run(run(payload))


if __name__ == '__main__':
    main()
