# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Install / sync dependencies
uv sync

# Lint
uv run ruff check src tests scripts
uv run mypy src

# Tests
uv run pytest tests/unit -v           # unit only (fast, no daemon)
uv run pytest tests/integration -v    # needs no running daemon; fixture spawns one
uv run pytest tests/ -v               # all

# Single test
uv run pytest tests/unit/test_envelope.py::test_request_roundtrip -v

# Regenerate WIRE_PROTOCOL.md after changing bus models
uv run python scripts/gen_protocol_doc.py

# Verify WIRE_PROTOCOL.md is in sync (used in CI equivalent)
uv run python scripts/gen_protocol_doc.py --check

# Reproduce the complete CI gate before every push
uv run ruff check .
uv run python scripts/check_brand.py
uv run mypy src
uv run mypy --platform linux src
uv run pytest -q
uv run python scripts/gen_protocol_doc.py --check
uv build
uv run python scripts/smoke_wheel.py dist

# Run daemon manually
uv run coderook-core                        # foreground; Ctrl+C to stop
CODEROOK_PORT=8000 uv run coderook-core        # override port

# Send a ping
uv run coderook ping
uv run coderook --version
```

## Architecture

This is a **dual-process** local AI coding agent runtime. `coderook-core` is a persistent daemon that owns all state (sessions, runs, background workers, permissions, durable persistence); `coderook` and `coderook-tui` are thin clients that connect to it over loopback TCP.

```
coderook-core (daemon)
  ├─ 127.0.0.1:7437  TCP JSON-RPC 2.0 / NDJSON  (IPC for CLI & TUI, token auth)
  ├─ 127.0.0.1:7438  hand-written HTTP/1.1 + SSE (durable runtime API, Bearer auth)
  └─ spawns: fleet worker subprocesses, hooks, MCP servers, git/rg/pyright
       ↑
coderook (CLI)   coderook-tui (TUI, primary frontend)
```

**`coderook-tui` is the primary frontend.** All user-facing work on task management, observability, and interaction should be designed for and validated in the TUI first. The `coderook` CLI exists only for quick scripted testing and debugging — it is not a product surface. Note that `coderook` with **no arguments launches the TUI**; subcommands are the CLI surface.

**`docs/FUNCTIONAL_ARCHITECTURE.md` is the authoritative architecture reference** (generated from a full code read; regenerate its claims against code when in doubt). The summary below is the orientation map.

### Protocol layer (`src/code_rook/core/bus/`)

All IPC messages are typed pydantic v2 models with a **discriminated union on the `type` field**. This is the contract boundary — adding a new command or event means adding a new model class to `commands.py` or `events.py` and extending the `Command`/`Event` union.

- `envelope.py` — `JsonRpcRequest`, `JsonRpcSuccess`, `JsonRpcError`, `EventPushEnvelope`, error code constants (`AUTH_REQUIRED=-32001`, `AUTH_FAILED=-32002`), `HandlerError`, `make_error()`
- `commands.py` — `Command` union of 45 command models across domains: core/auth, headless run (`agent.run`), event subscribe/replay, durable `thread.*`/`turn.*`, `session.*` (create/send/authority/history/fork/export/compact/checkpoints/rewind/context), `permission.respond`, `user_question.respond`, `worker.list`, `workflow.start/list/get`, `workspace.diff`, `turn.inspect`
- `events.py` — `Event` union of 45 event types: run/step lifecycle, `agent.decision`, `tool.call_*`, `llm.*`, `context.*`, `permission.*`, `plan.*`, `subagent.*`, `background.*`, `skill.invoked`, `hook.executed`, `lsp.diagnostics`, `runtime.event`

`WIRE_PROTOCOL.md` is **generated** from these models by `scripts/gen_protocol_doc.py`. Always regenerate and commit it after changing bus models.

### Transport layer (`src/code_rook/core/transport/`)

- `socket_server.py` — TCP server (`asyncio.start_server`); reads NDJSON lines, dispatches each line as an independent task (so long handlers don't block concurrent commands like `permission.respond`), handles JSON-RPC error cases. Enforces loopback peer, first-frame `core.authenticate` with `hmac.compare_digest`. On `start()`, probes `host:port` first — errors if another daemon is already listening. Handlers registered via `server.register("method.name", handler_fn)` (44 business handlers in `app.py`).
- `socket_client.py` — shared client for CLI/TUI: token read, auth handshake, command/response futures, event dispatch.
- `ipc_broadcaster.py` — topic (fnmatch) + scope (`global`/`run:`/`thread:`) subscriptions; durable runtime replay with high-water handoff so reconnects lose no events.
- `auth.py` — IPC token lifecycle (`~/.coderook/ipc-token`, 0600, exclusive create, strict validation).

### HTTP runtime API (`src/code_rook/core/api/`)

Second interface on port 7438 for external integrations: hand-written HTTP/1.1 (no framework), Bearer auth, SSE event stream with `after_seq`/`Last-Event-ID` cursor replay. Endpoints under `/v1/` (threads/turns/events/interrupt/steer/items/receipt/capabilities/usage).

### Config (`src/code_rook/core/config.py`)

Five-tier priority: **built-in defaults → `~/.coderook/config.toml` → `.coderook/config.toml` → `.env` → `CODEROOK_*` env vars**. `CODEROOK_CONFIG` forces a single TOML path. Config file is silently skipped if absent; unknown keys cause a hard exit. **Project-level TOML must not set route security keys** (`provider`, `base_url`, `api_key_env`, `active_route_id`) — the loader exits hard if it does.

Sections: `[core]` (host/port/ipc_token_file), `[logging]`, `[agent]` (max_steps), `[llm]` (legacy provider settings), `[trace]`, `[permission]` (timeout_s), `[api]` (host/port), `[compaction]`, `[[mcp.servers]]`.

### Daemon entry (`src/code_rook/core/app.py`)

`CoreApp.run()` is the single async entry point: loads config → logging → trace → PermissionManager + HookManager → IpcEventBroadcaster → SessionStore + RuntimeService (SQLite `~/.coderook/runtime.db`) → RouteRegistry → MCP → subagent/fleet registries → LocalFleet (workflow ledger) → SessionManager → HTTP API → SocketServer with all handlers → waits for shutdown signal → ordered teardown. Adding new handlers: implement a handler method on `CoreApp` and call `server.register()`.

### Core subsystems (`src/code_rook/core/`)

- `loop.py` / `runner.py` / `context.py` / `interaction.py` — async Plan-Act-Observe agent loop, run assembly, system-prompt layering, interactive question/steer futures
- `tools/` — capability model (`ToolSpec`), registry/catalog/discovery, invocation pipeline (validation → hooks → permission → execute → output policy), action families (`File`/`Git`/`Bash`/`Run` + control), builtin tools
- `permissions/` + `authority/` — six-tier permission decision flow, authority matrix (mode × profile × trust × allowed actions), sandbox capability detection (advisory metadata only — no OS isolation is enforced; the real defense is the approval chain)
- `llm/` — explicit wire-format routes, credential store (keyring → file), Anthropic/OpenAI-compatible/OpenAI Responses providers, doctor
- `session/` + `runtime/` — dual source of truth: file ledger (`~/.coderook/sessions/`) is the operational truth; SQLite runtime is the queryable/auditable projection
- `compact/` — context budget, distillation, structured compaction with quality gate
- `task/` / `goal/` / `subagent/` / `fleet/` / `workflow/` — multi-agent: run-level task board, goal control plane, in-process subagents with write claims and budgets, cross-process fleet workers, declarative event-sourced workflows
- `skills/` / `hooks/` / `mcp/` / `agents/` — extension mechanisms
- `trace/` / `receipts/` / `events/` — observability, redacted trace, offline turn receipts

### Persistence layout

User-level state lives in `~/.coderook/` (sessions/, runtime.db, fleet.db, workflow.db, routes.json, credentials.json, policy.toml, ipc-token, traces/). Workspace-level state lives in `<workspace>/.coderook/` (context.md, memory/, artifacts/, worktrees/, skills/, agents/, hooks.toml).

### Testing

Integration tests in `tests/conftest.py` spawn a real daemon subprocess using a random free port (via `free_port` fixture) with an isolated `HOME`/`USERPROFILE` and placeholder LLM config — they never touch developer state or real API keys. Unit tests cover protocol, loop, tools, permissions, compaction, workflow IR/executor, runtime store, and TUI/CLI components.

### Pre-push CI discipline

Never push changes until the complete CI gate listed in **Commands** passes locally.

- On Windows, run both normal Mypy and `mypy --platform linux`; Windows-only `ctypes` attributes can pass locally but fail on Ubuntu.
- Integration fixtures must be self-contained. They must not depend on a developer `.env`, a real API key, or a GitHub Secret unless the test is explicitly marked and skipped when the secret is absent.
- After any change under `src/code_rook/core/bus/` or `scripts/gen_protocol_doc.py`, regenerate `WIRE_PROTOCOL.md`, commit the generated file, and run `--check`.
- Generated text must use explicit UTF-8 and deterministic LF comparison. Prefer ASCII punctuation in generated protocol text when typography has no semantic value.
- Before pushing, inspect `git status --short` and the staged diff so generated files and test fixes are included in the same commit.
- A failed command blocks the push. Fix the failure and rerun the complete gate from the beginning.

### Code style

All functions must have a **single-line Chinese comment** immediately above the `def` line explaining what the function does. Example:

```python
# 发送 JSON-RPC 响应并刷新写缓冲区
async def _send(self, writer: asyncio.StreamWriter, msg: BaseModel) -> None:
    ...
```

Do not write multi-line docstrings; one concise Chinese line is enough.

**Test functions** require **two Chinese comment lines** immediately above the `def` line:

```python
# 功能：验证 publish 后订阅者能收到事件对象
# 设计：用内联 handler 收集事件引用，断言 is 而非 ==，排除序列化中间步骤的干扰
async def test_publish_reaches_subscriber() -> None:
    ...
```

- `# 功能：` — 该测试验证的具体行为或不变式，一句话说清楚"测什么"
- `# 设计：` — 为什么选择这种测试方式：覆盖了什么边界条件、为什么用这个 stub/fixture、这种断言方式相比其他方式的优势

两行注释缺一不可。功能行让读者 5 秒内判断测试意图；设计行让读者理解测试背后的决策，而非只看到操作步骤。

### Design docs (outside the repo)

The planning documents live in `../docs/` (sibling of this repo, not committed here):
- `agent_development_plan.md` — staged development roadmap S0–S8
- `s0_implementation_plan.md` — detailed S0 decisions and rationale
- `agent_functional_outline.md` — full feature catalogue

In-repo documentation lives in `docs/`:
- `docs/FUNCTIONAL_ARCHITECTURE.md` — authoritative functional architecture (component deep-dives, data flows, known issues)
- `docs/USER_GUIDE.md` / `docs/USAGE_GUIDE.md` — end-user guides
- `docs/ADR_RUNTIME_CONTRACT.md` — durable runtime contract decisions
- `RUNBOOK.md`, `WIRE_PROTOCOL.md` — operations and protocol references
