# Python Agent Runtime

The execution loop is a Python source port of Pi's
`packages/agent/src/agent-loop.ts` at commit
`b2602be77cb7b0de45dd616407fd210daa48aa75`.
The reference source and MIT license are under `vendor/pi/`.
The application does not launch Node or a Pi subprocess.
The wheel includes the original copyright and permission notice at
`code_rook/licenses/pi-MIT.txt`.

Agent-invoked Bash commands receive the non-secret runtime variables
`CODEROOK_SESSION_ID`, `CODEROOK_SESSION_FILE`, `CODEROOK_PROVIDER`,
`CODEROOK_MODEL`, and `CODEROOK_REASONING_LEVEL`. This lets the model inspect its
actual frozen session and route identity instead of guessing. User-entered `!shell`
commands intentionally remain ordinary user shell commands and do not receive this
Agent-only metadata.

The same frozen Provider route, model ID, and thinking level are included in the
effective System Prompt. Identity questions therefore use Core-owned runtime facts
and do not need exploratory file or shell calls. Credentials, endpoints, and request
headers are not included.

All installed Python entry points set `AI_AGENT=coderook` and
`CODEROOK_CODING_AGENT=true` before starting a client or daemon. Child processes
can therefore distinguish CodeRook from an outer terminal Agent. These process
markers are separate from the session-specific variables above.

## Execution

User configuration `agent.extension_paths` accepts explicit Python files defining
`setup(api)` (sync or async). Setup can register existing `BaseTool`
implementations with `api.register_tool(tool)` and sync/async cleanup callbacks
with `api.on_shutdown(callback)`. `api.workspace` and `api.run_id` identify the
current run. Contributions use the ordinary tool schemas, permission checks,
results, and presentation pipeline. SessionManager owns one host per opened
session: setup runs once, module/tool state and active tool selection survive
successive runs, and each run binds a fresh permission-filtered tool registry.
Run completion detaches registry contributions and bus listeners without unloading
the session's modules. Session close/delete and Core shutdown release the host and
run cleanup callbacks. Different sessions do not share extension instances.
Direct standalone Runner calls without a session-owned host retain per-run loading
and cleanup. Partial setup failures unload immediately; the next run can create a
fresh host. TUI and Web `/reload` call the same session resource reload operation
(`session.reload` over IPC, `POST /v1/threads/{id}/reload` over HTTP). When the
session is idle it unloads the old host, reads fresh Python source and calls setup,
then refreshes the input command catalog without starting a model run. Active runs
must be stopped before reloading. On a Web welcome page without a session,
`/reload` only refreshes the workspace's templates and Skills.

Registered tools may override a built-in of the same name. Within one setup the
last registration wins; across extension files the first loaded extension wins,
matching Pi's tool selection. Closing the host restores the original implementation
and schema, or removes newly introduced tools, and invalidates cached catalogs.
Tools and commands registered later from a command or lifecycle callback take
effect immediately in that session. During an active run a new tool enters the
next model request without restarting or reloading the extension; when idle it is
attached to the next run. Re-registering a name replaces the current session
implementation, and unload restores the complete registry stack.

`api.register_command(name, handler, description="...")` contributes a user
slash command. The handler receives the unparsed argument string, can be sync or
async, and may return text for immediate display or `None`. It can use
`send_user_message` to request a model task explicitly; a local command does not
create a Run. Commands are available after opening the session context, before
its first model request, and disappear when their extension is unloaded.
TUI dispatches command handlers in a worker so permission events remain usable;
Web uses `POST /v1/threads/{id}/command`, backed by the same session operation as
IPC `session.execute_command`. Built-in TUI commands retain precedence. Extension
names replace conflicting prompt-template names in the session completion list.
Custom command output is a frontend notice, not an assistant message in history.

Tools can set `prompt_snippet` and `prompt_guidelines` on their Python `BaseTool`
class. The short snippet replaces the description in the prompt's tool list;
trimmed guidelines are deduplicated. Only currently model-visible tools contribute
these instructions. They never add private fields to Provider tool schemas, and
the composed prompt still enters the normal Request Snapshot path.

`BaseTool.prepare_arguments(params)` can normalize input before schema validation.
It runs once per call, before scheduling, and receives a copy of the model
arguments. Resource claims, approval, tool hooks, execution and
the tool-start record consume the normalized result. The original assistant
tool call remains unchanged in model history. Invalid preparation produces a
schema error and does not execute the tool.
Repeated read calls still execute the tool pipeline, including extension hooks.
The native loop does not substitute the old read-result cache for an invocation;
changing files and image/display metadata therefore receive fresh results.

After setup, `api.get_all_tools()` returns tool names and descriptions,
`api.get_active_tools()` returns the currently exposed names, and
`api.set_active_tools(names)` selects the tools used by subsequent requests.
An empty list disables all model tools; deferred tools can be explicitly selected.
Selection affects both schema generation and model invocation, while mode and
authority filtering still apply. Closing the host resets its selection.

Tool results and `tool_result` hooks support `terminate: true`; a blocking
`tool_call` hook may return it as well. Like Pi, the loop stops tool-driven
continuation only when every result in the batch requests termination. Mixed
batches continue, and queued follow-up messages remain eligible. Ending without
a final assistant answer is reported as incomplete, not successful completion.

`api.on(event_type, callback)` observes subsequent run-scoped Core events, accepts
wildcards such as `tool.*`, and returns an unsubscribe function. Callbacks may be
sync or async and receive independent JSON payload copies. They remain active
through `run.finished`; run exit removes listeners and session close later runs
shutdown callbacks. This
is an observer API, not Pi's mutable tool-result or input interception hooks.

Exact `tool_call` and `tool_result` registrations are execution hooks, separate
from the dotted observation event names. `tool_call` receives `tool_name`,
`tool_call_id`, and `input`; returning `{"block": true, "reason": "..."}` or raising
prevents execution. `tool_result` receives the same identity plus `content`,
`images`, and `is_error`; returning `content` (text), `images`, or `is_error` replaces those
fields before output policy, presentation, and persistence. Result hooks chain
in registration order and isolate ordinary callback errors. `images` accepts image
blocks, an empty list, or null to clear attachments. Replaced images follow normal
model capability filtering and durable tool-result history. Execution exceptions
and timeouts also enter result hooks as error results before the terminal event;
hooks may provide a replacement without re-running the tool. Task cancellation
still propagates rather than being converted to a recoverable tool result.
`details` accepts an object or null and travels in the persisted Tool Presentation,
not the model text. TUI and Web show it in expanded tool details. Output truncation
retains it, and terminal error results preserve it together with image attachments.

Python/Pillow normalizes read images and tool-result images after extension hooks,
before history admission. The default limit is 2000 pixels per side and 4.5 MiB
of base64 payload. Encoding tries PNG then JPEG quality steps, reducing dimensions
if necessary, and adds a coordinate-mapping note. Work runs off the event loop.
Unreadable read-tool images return an error; undecodable images already produced
by other tools are retained, matching upstream's preservation behavior. Set
`[agent] image_auto_resize = false` to keep image dimensions and encoding size;
the setting applies to read tools, post-extension results, and user attachments
admitted by SessionManager (including queued follow-ups). Original attachment
Artifacts remain unchanged; the model-sized image and coordinate note are persisted
in message history before the run begins. The existing upload-size limit remains
separate from resizing. BMP and TIFF reads
convert to PNG, including when resizing is disabled. Other formats are supported
only when Pillow can decode them; platform-specific HEIC/SVG backends are not bundled.

For a SessionManager-owned host, extensions can call
`await api.send_user_message(content, deliver_as="steer" | "follow_up")`.
Content accepts text or a list of text/image blocks. Image blocks can use Pi's
`{type: "image", data: "<base64>", mimeType: "image/png"}` form or the existing
`source` form. Images are inspected and stored as content-addressed Artifacts,
then use the same attachment resizing and frozen route capability checks as
frontend images. The current per-image 2 MiB upload limit also applies here.
While running, steering enters the next decision; follow-up enters the existing
durable queue and is admitted after the current answer. When idle, either delivery
mode starts a new run in the same session and retains its extension state. The
binding resolves the current run at delivery time instead of retaining a stale
run ID, and survives resource reload. Both use the same run/session control
path as frontend input. Text is literal by default, including `/` and `!` prefixes;
`expand_prompt_templates=True` opts into normal command/template expansion.
The queue persists this flag and image references through restart and retry (database migration 7;
older records default to their existing expansion behavior). Cancelled steering
is restored without reinterpreting its already-prepared text. The API currently
requires a session-owned host. Steering accepts image content as well as text;
unconsumed image steering is restored into the attachment queue when cancelled.
Closing the session invalidates the sender.

`api.send_message()` provides Pi-style custom messages without converting them
into ordinary frontend input. Each message keeps its `custom_type`, content,
display flag, details and extension provenance in the append-only Session Ledger.
During a run, omitted delivery defaults to steering, `deliver_as="steer"` injects
it before the next model decision, `deliver_as="follow_up"` starts the next agent
iteration, and `deliver_as="next_turn"` holds it until the next user turn. An
explicit `trigger_turn=False` appends it after the active answer so tool-call/result
pairs are not split. When
idle, `trigger_turn=True` starts a native Python Turn; otherwise it only appends.
`api.notify()` publishes a separate durable thread-level info/warning/error event;
it is visible in TUI/Web but never enters model history. No Node process or second
session store is involved.

The `input` hook runs before Skill/template expansion for initial messages,
steering and queued follow-ups. Its event includes `text`, native image blocks,
`source` and `streaming_behavior`. Return `{"action": "continue"}`, return
`{"action": "transform", "text": "...", "images": [...]}` to chain a rewrite,
or return `{"action": "handled"}` to consume the input locally. Omitting images
retains them; an empty list removes them. Ordinary handler errors are logged and
later handlers continue; cancellation propagates. Queued messages persist the
processed content and do not rerun input hooks on dispatch or retry. Cancelled
steering is likewise restored without a second interception.

A handled input does not create a Run, model request or queue record. IPC start
responses expose `handled: true` with an empty run/turn ID; queue responses have
`message: null`. HTTP returns `{"handled": true}`. Clients must not wait for a
completion event in that case. Headless JSON/NDJSON emits `input.handled` rather
than inventing a successful coding result. TUI and Web recognize handled submissions.

The `context` hook runs before each model step's request snapshot is created.
It receives native messages (`user`, `assistant`, and `toolResult`) copied from
the current conversation. Handlers run in registration order, can edit that copy
in place, or return `{"messages": [...]}` to replace it. The next handler sees
the preceding result. Sync and async handlers are supported. Ordinary handler
errors are logged and later handlers still run; cancellation propagates.
The transformed messages then pass through the normal model/image adaptation and
are persisted in the RequestSnapshot actually consumed by the Provider. These
request-only changes do not rewrite conversation history and are not accumulated
across steps. A transport retry reuses the same snapshot rather than invoking
the context hook again for that failed attempt.

Extensions receive the native loop lifecycle events `agent_start`, `agent_end`,
`turn_start`, `turn_end`, `message_start`, `message_update`, `message_end`,
`tool_execution_start`, and `tool_execution_end`. `turnIndex` is zero-based and
resets for every agent run; `turn_start` also carries a millisecond timestamp.
An abnormal provider, tool or cancellation exit still emits exactly one
`agent_end`, allowing extensions to release run-scoped state. Event payloads are
copied between handlers so observation cannot mutate the loop accidentally.

`message_end` is the intentional exception: a handler may return
`{"message": replacement}`. Replacements are chained in registration order and
must preserve the original role. The accepted replacement becomes the model
history, persisted Session Ledger message and final result; invalid replacements
are logged and ignored. This matches Pi's finalized-message interception rather
than providing a display-only hook.

Session-owned hosts also receive `session_start` (`new`, `resume`, `fork`, or
`reload`), `session_info_changed`, `session_before_fork`, and
`session_shutdown`. A fork-start event identifies the source `thread.jsonl`.
Returning `{"cancel": true}` from `session_before_fork` prevents the fork before
any new Session or Ledger is created. Reload emits shutdown on the old host before
loading the new module, while daemon/session shutdown emits `quit`; this makes
extension state ownership observable rather than relying only on Python cleanup.

After `session_start`, exact `resources_discover` handlers receive the workspace,
plus a `startup` or `reload` reason. Their `skillPaths`, `promptPaths`, and
`themePaths` are resolved relative to the Python extension file that registered
the handler, deduplicated, and frozen into that session. Discovered Skills drive
both explicit input expansion and the model-visible capability catalog; discovered
prompt templates drive the shared TUI/Web command list and input expansion.
Reload first removes all old projections, so deleted contributions cannot remain
visible. Theme paths are exposed in session context; custom theme rendering is
still pending.

Tree navigation emits `session_before_tree` with the target, old leaf, common
ancestor, branch entries and current summary preference. A handler may cancel,
replace the summary instructions, set a label, or return a complete local summary;
the latter skips the summary Provider call. Only a successful append-only branch
selection emits `session_tree`, including the final leaf and whether the summary
came from an extension.

Both manual and automatic context compaction emit `session_before_compact` with
the messages, token estimate, settings, reason and retry intent. A handler may
cancel or provide a complete compaction result. Extension summaries use the same
protocol validation, recent-context retention and append-only Ledger commit as
default summaries. Success emits `session_compact` only after commit; cancellation,
invalid output, model failure and persistence failure emit `session_compact_failed`.
Threshold, manual and overflow paths are distinguished, and overflow sets
`willRetry: true`.

The explicit `before_agent_start` hook runs once before the model loop. Its event
contains `prompt`, `images`, and `system_prompt`; returning `{"system_prompt": text}`
replaces the base instruction for this run. Hooks run in registration order and
receive the previous hook's prompt. File append instructions and runtime context
remain separately assembled layers. The resulting request uses the ordinary
Request Snapshot path. A returned `message` with `custom_type`, `content`, optional
`display`, and `details` is admitted to the Ledger before entering model context.
Text and text/image content-block lists are supported; null content becomes an
empty list. Model history projects it as a user-context message, while the Ledger
preserves extension provenance and details. `display: false` (the default) hides
it from display history without excluding it from the model. Visible text messages
use a source-labelled generic card in TUI and Web. Their persistent message IDs
are shared by initial display and replay. Extension-provided renderers and custom
image presentation are still pending.

Session-owned extensions can publish declarative UI contributions through
`set_status`, `set_working_message`, `set_working_visible`,
`set_hidden_thinking_label`, `set_widget`, `set_title`, `set_editor_text`,
`paste_to_editor`, and `set_tools_expanded`. Core keeps the current status/widget
projection in session context and publishes every change as a durable
`extension.ui_updated` event. TUI and Web consume the same event: both render
status and text widgets, update the window title and editor, and apply the default
tool-detail state. UI contributions are intentionally data, not executable client
callbacks, so one Python extension works in both frontends. Reload clears the old
projection and rebinds every session API on the replacement host rather than only
restoring ordinary message delivery.

These files run trusted Python code in the Core process, not in a sandbox; only
configure files you trust. Project TOML cannot add extension paths. Executable
custom component factories, custom message/image renderers, header/footer
replacement, and hot reload during an active run remain unsupported. It is a
working extension path, not a claim of full extension parity.

Python extensions can call `api.register_provider(name, config)` with Pi-style
`name`, `baseUrl`, `apiKey`, `headers`, `api`, and `models` fields. Anthropic Messages,
OpenAI Chat Completions, and OpenAI Responses registrations become session-scoped
routes backed by the existing Python wire adapters. Literal and `$ENV_VAR`
credentials stay in memory; the shared Provider Catalog and credential files are
not rewritten. `api.set_model(provider, model)` persists that selection only for
the owning session and accepts extension providers or configured Catalog routes;
`api.get_model()` returns the current session's safe provider/model summary, and
`api.unregister_provider(name)` removes the contribution.
The Web model drawer reads the same extension catalog and can select its models.
Omitting `models` creates a Pi-style override for an existing Catalog Provider:
the extension may replace its `baseUrl`, credential, API wire format, or headers
while inheriting the selected model and capability metadata. The override remains
session-scoped and is applied by Catalog ID, so a custom route using that Catalog
is covered without rewriting `routes.json`.
Provider- and model-level headers are merged with model headers taking precedence;
their values support `$ENV_VAR`, `${ENV_VAR}`, `$$`, and `$!` expansion and remain
outside Ledger/Receipt payloads. An explicit `Authorization` header overrides the
adapter-generated Bearer header. A value beginning with `!` executes the remaining
command and uses trimmed stdout; non-zero or empty results fail without exposing
stdout/stderr. OAuth, dynamic model refresh, and a
fully custom streaming implementation remain unsupported and fail explicitly.

Session-owned extensions also expose native state and inspection actions:
`append_entry(custom_type, data)` writes an `extension.entry` Ledger event that is
excluded from model history; `get_session_name()` and `set_session_name()` use the
same metadata and notifications as TUI/Web rename; `set_label(entry_id, label)`
adds or clears a persistent history-tree bookmark. `get_system_prompt()` returns
the actual assembled prompt for the current run, while `get_context_usage()`
returns the latest `{tokens, contextWindow, percent}` snapshot from normalized
Provider usage. These APIs are session scoped and are invalidated when the host
closes. `get_commands()` exposes the current extension command catalog, and
`await api.exec(command, args, cwd=..., timeout_ms=...)` runs a parameterized
subprocess without a shell-string intermediary, returning separate stdout,
stderr, exit code, and cancellation state. It is a trusted-extension convenience
API, not a model tool; model-initiated commands still use the permission and
sandbox pipeline.

`before_provider_request` runs once before the authoritative Request Snapshot is
written. It receives a provider-neutral payload containing `messages`, `system`,
and `tool_schemas`; a returned replacement becomes both the frozen snapshot and
the actual adapter input, so retries do not rerun the hook. `after_provider_response`
observes the normalized `LlmResponse` after a completed adapter call. This is not
yet a raw HTTP payload/header interception API.

`[agent] steering_mode` and `follow_up_mode` each accept `one-at-a-time` (default)
or `all`. Steering is consumed before the next model decision; follow-ups are
consumed only after the current response has no more tool calls. The all mode
admits queued follow-ups together without starting a separate run. Each input is
still persisted before its queue record is acknowledged. Independent Shell or
mode-changing commands remain queued for separate execution.

The default model-led loop does not inject a keyword-derived Task Strategy into
the system prompt or automatically scan and inject a Repository Map before each
request. Project instruction files remain available; source exploration happens
through the selected tools. Explicit planning and experimental strategy overrides
retain their own contracts rather than shaping every ordinary chat turn.
Ordinary direct turns also do not publish a synthetic task-profile card or an
empty "understanding" phase: the frontend enters running state from `run.started`,
then shows real model output and actual tool activity. Explicit Plan, Delegate, or
experimental routing still emits its visible proposal and approval lifecycle.

TUI and Web send `!command` directly to the Python shell runner, without a model
request or provider readiness check. The result is shown and included in the next
model context. `!!command` preserves the execution events but excludes the command
and result from model history. Commands submitted during a model run are queued
as separate executions rather than injected as steering instructions. Both forms
still use the existing permission and tool pipeline.

Before a `!` or `!!` command reaches that pipeline, Python extensions receive the
Pi-compatible `user_bash` event (`command`, `excludeFromContext`, and `cwd`). The
first handler returning a complete `result` supplies output, exit code,
cancellation, truncation, and full-output-path metadata, and prevents local Bash
execution. Invalid or failing handlers are logged and normal execution continues.
Custom streaming Bash operation objects are not yet implemented.

Shell completions are stored as `user.shell_completed` records. The optional
`display_messages` field in `session.get_history` includes native `bashExecution`
entries, including `!!` commands; the existing `messages` field remains model-ready
and excludes them. TUI reopening and switching use this display projection, so
excluding a command from the model does not erase it from the visible session.
Display-only native message IDs let TUI history restoration and durable event
replay update the same answer widget. They are not added to provider messages.
Real-daemon Textual tests cover reopening both an ordinary answer and an excluded
Shell execution without producing a second answer widget or legacy Markdown copy.
Native tool history also uses the live tool-card renderer. Cards are indexed by
run and tool-call ID, so replay fills in authoritative timing/presentation on the
existing card instead of adding another entry. Switching sessions clears these
view-local indexes and reconstructs them from the selected session.

On Windows the startup sandbox probe also launches the actual Git Bash runtime.
An ACL runner that executes Python but cannot start MSYS Bash is not advertised as
available. In that case commands use the existing explicit-approval path with
`unavailable` enforcement; a failed command is never silently retried unsandboxed.

Interactive chat sessions have no default fixed step cutoff. Explicit
`agent.max_steps` still applies; scripted runs and Workers retain a 20-step
default. Users can cancel long-running interactive tasks to control consumption.

Project instructions follow Pi's per-directory selection order:
`AGENTS.override.md`, `AGENTS.md`, `AGENTS.MD`, `CLAUDE.md`, `CLAUDE.MD`.
Only the first readable file in each directory is selected, from filesystem
ancestors to the workspace directory. A nested linked Worktree's own copy
shadows the same-named main-repository instruction file. User instructions are
selected from `~/.coderook/`; existing user and workspace `context.md` additions
remain supported. Instruction files are reread when assembling each run.

`SYSTEM.md` replaces the default coding persona; `APPEND_SYSTEM.md` appends
instructions while preserving runtime and project context. Each file is selected
independently from trusted `<workspace>/.coderook/`, falling back to
`~/.coderook/`. Explicit SDK/Worker role overrides take priority over `SYSTEM.md`.
Both files are reread at the next run, not during an already-running model step.

The Web draft page obtains its Skill/template command catalog through
`GET /v1/workspace/input-commands`, without creating a session or calling a model.
`/reload` refreshes this catalog on both draft and existing-session pages; it is
handled before provider readiness and ordinary task submission. Existing-session
catalogs continue to use that session's authority.

User configuration `agent.prompt_paths` adds explicit Markdown files or template
directories after the default user/project locations, following Pi's resource
ordering. Discovery and expansion share a single loader, so duplicate names and
unreadable templates resolve consistently. Relative paths use the workspace root.

Native `read` can load the enabled Skill directories advertised for the current
run, including their relative reference files and images. These read roots are
captured during tool assembly; they do not expand `edit` or `write` permissions.
Skill loading no longer depends on shell access to the user's home directory.
Skill precedence follows Pi's first-discovered ordering: user, project, then
explicit paths. CodeRook's bundled and legacy locations are compatibility
fallbacks after these sources. `resolve`, `show`, and execution discovery use the
same winner even when multiple files in one directory declare the same name.

Context projection includes cache-read/cache-write input even when uncached input
is zero. It uses the provider's declared context window when available, otherwise
derives capacity from reported context utilization. If a response omits usage,
the next request is estimated again rather than disabling automatic compaction
after the first step. Output reserve and newly appended tool results are included.
Request preparation runs after admitted steer/follow-up messages enter context,
including on the first request. Long queued input therefore participates in the
same capacity check; next-turn hooks remain a separate, earlier phase.

`AgentLoop.run()` calls `agent_runtime.driver.execute_context`, which binds
Python providers, tools, permissions and persistence to
`agent_runtime.loop.run_agent_loop`. The former `_run_impl` loop and its separate
`_run_act_phase`/parallel-batch executor are removed. Parallel execution tests now
submit tool calls through the production model loop, not a legacy private helper.
Unused legacy tool-name intent labels, Todo-based end-turn deferral helpers and
their continuation prompts have also been removed. Explicit `tasks` tool users
can still see their task-board state, but pending tasks do not force extra model
calls or fabricate user messages after a normal answer.

- The inner loop processes tool results and steering messages.
- The outer loop claims same-mode follow-up messages from the session's durable
  queue after an answer. Mode changes and idle sessions use session-level dispatch.
- Messages remain independent user, assistant and tool-result records internally.
  `messages.py` converts formats only at the existing provider boundary.
- Real user steering and follow-ups reset the repeat-tool observation chain.
  Advisory notices are appended after paired tool results and before the next
  request; they do not reset their own observation chain or block tool execution.
  Successful tool results omit the optional `is_error` field consistently with
  the Ledger projection; failures retain `is_error=true`.
- Parallel results enter history in declaration order; sequential tools or
  conflicting resource claims make the batch sequential. Disjoint declared
  write claims remain parallelizable. Batch tasks are named and joined on exit.
- Truncated tool calls return errors without execution. A text-only length
  stop preserves the partial response and reports incomplete, rather than
  fabricating another user prompt.
- A context-overflow failure can compact and retry the model request once.
- Unexpected failures outside the model-request phase report `runtime_error`,
  rather than blaming the model for tool-stage or persistence failures. Typed
  Provider, cancellation and invariant failures retain their specific reasons.
- Frontends consume `agent.message` snapshots. Text and thinking are distinct
  blocks, and a thinking-first response must not collapse the answer.
- TUI phase events update the status header without adding stock explanations to
  the timeline. Native `read` is classified as exploration rather than modification.
  Web hides default `model_led` profile cards; explicit experimental/Plan records
  retain their existing presentation.
- Native successful answers do not append a redundant TUI receipt card. When a
  receipt card is shown for failures or legacy events, it defaults to a borderless
  one-line summary. Click, Enter or Space expands the evidence and review commands;
  failure and failed-verification indicators remain in the summary. A Textual
  component check uses the production CSS to verify height and keyboard expansion.
- The final answer is never replaced by a completion badge. Native assistant text
  is rendered directly; if a frontend missed the streamed message, the persisted
  `run.finished.result_summary` restores the answer immediately. A thinking-only
  message does not count as a visible answer, and Web uses the same event fallback
  while the receipt projection is still loading.

## Session compaction

The default `compaction.strategy = "session"` uses a fixed recent token window
(`keep_recent_tokens = 20000`) instead of retaining a fraction of all history.
`agent_runtime/compaction.py` prepares the old history, an optional current-turn
prefix, the untouched recent messages and the previous summary. Tool-call/result
pairs are retained together. History and a split-turn prefix are summarized
separately, using Markdown checkpoints and previous-summary updates.

The existing ledger commit path remains append-only. A reopened session derives
the same compacted context. Summary text does not stream into the user's answer;
length-stopped or failed summaries leave the original context unchanged. Overflow
uses the same recent-window preparation instead of summarizing all messages.
Summary usage forwarded to an active run carries `purpose=summary`: it contributes
to total tokens and estimated cost, with `summary_*_tokens` counters in Runtime
usage, but does not replace the main conversation's context percentage in Runtime
or TUI. Standalone branch summaries append `session.auxiliary_usage` to the Ledger
and Runtime event projection, with a separate operation ID and no synthetic coding
Turn. Reported usage is retained even when the summary is truncated. These records
contribute to session cost, the HTTP usage endpoint and subsequent cost routing.
Session bootstrap restores missing auxiliary usage from the Ledger. The projection
uses each session's Ledger sequence as the idempotency key, so repeated bootstrap
does not double count a summary; the original usage timestamp is preserved.
The old `truncate`, `structured` and `adaptive_evidence` strategies remain explicit
options for reproducing earlier experiments, not the default product behavior.

## Session paths and historical forks

`agent_runtime/session_tree.py` reconstructs parent paths from the append-only
ledger. Old linear sessions require no rewrite. A `session.branch_selected` entry
changes the parent for subsequent entries; inactive branches remain available.
Compaction is projected only along the selected path.

`session.tree` (IPC) and `GET /v1/threads/{id}/tree` return history entries.
`session.fork` and the existing HTTP fork endpoint accept an optional `leaf_seq`
(a Ledger sequence, not a Runtime/SSE cursor). They create a separate session
whose model context ends at that node, without rewinding workspace files.
TUI `/tree` and the Web history drawer expose this operation. Both also expose
in-place navigation and optional branch-summary generation as described below.

## Prompt assembly

`agent_runtime/prompt.py` builds the base prompt from the actual tool schemas.
It follows the upstream minimal role, available-tool snippets and conditional
guidelines. The old long base prompt is removed. The runtime no longer tells the
model to suppress progress narration, use English reasoning, maintain a task
board for every multi-step request, or call a possibly unavailable memory tool.
Explicit user/project instructions, mode and runtime environment still apply.
Default unrestricted Act runs now advertise `read`, `bash`, `edit` and `write`,
plus explicitly configured MCP tools. Explicit Plan/strategy/preset tool lists
retain their constrained catalogs. Historical tool names remain callable through
the existing permission pipeline but are not advertised by the default surface.

`read` provides offset/limit pagination and image results. It reads the current
file on every invocation, including external editor changes between requests.
Native `read` and legacy `read_image` bypass the old text-only read cache, which
cannot preserve multimodal results. Other legacy tools retain their existing
cache policy. `edit` matches all
replacements against the original file, rejects overlaps, and commits once with
Checkpoint support. Its fuzzy matching port preserves untouched lines rather
than normalizing the entire file. `write` retains Python atomic file operations.
Edit arguments also accept Pi's JSON-encoded list, single replacement object and
legacy `oldText`/`newText` forms. Normalization copies arguments instead of
mutating the original ledger payload; the advertised schema stays canonical.

Extension instructions are rebuilt from the actual registered model tool surface.
The four-tool mode lists skill descriptions and file locations for progressive
loading through `read` or approved `bash`, rather than requiring an unadvertised
`skill` tool. It does not instruct the model to start workers when `agent` is
absent. Full skill bodies are not eagerly inserted into the prompt.

The native `bash` tool now selects real Bash (Git Bash on Windows, including a
custom Git installation discovered on PATH). It has an optional per-call timeout
and no default tool timeout. It starts each command independently, cleans up the
process group on cancellation/timeout, and retains the last 2000 lines/50KB in
memory. Larger output is saved under `.coderook/tool-output/`, with a path in the
result that `read` can page through. Legacy `Bash` family calls still use the old
host-shell implementation. Process-wide user budgets can still end a run.

## Compaction budgets

Automatic and manual compaction now share the configured strategy, recent window,
and `reserve_tokens` (default 16384). Session summaries cap output at 80% of this
reserve for history and 50% for a split-turn prefix, additionally limited by the
provider's existing output ceiling and any enclosing Goal budget. The automatic
trigger also checks the estimated next input plus this reserve against capacity.
Summary budget scopes are task-local and restored after each request.

## Remaining migration

The Core now exposes `session.navigate` and `POST /v1/threads/{id}/navigate`
with a Ledger `target_seq`. Selecting a user message returns its text for the
editor and selects its parent (including the empty root); selecting an assistant
message selects that node. The operation appends a branch selection, preserves
the old path, and publishes a durable `session.navigated` notification. It does
not revert files. TUI `/tree` uses Enter to navigate in place and Ctrl+F to fork.
Navigation reloads the selected context and restores user text to the composer
without sending it. Durable navigation from another client also reloads the TUI;
historical replay does not recursively trigger navigation. The Web picker now
offers both navigation and fork. It loads the fixed selected-path projection
from thread context, excludes pre-navigation runs, and appends subsequent live
turns. Refresh uses the same projection; user text is restored without sending.
Tool call/results in selected history remain tool cards, not raw user messages.
Browser-level visual validation remains outstanding.

Optional branch summarization is available through `summarize: true` on navigation,
Ctrl+S in the TUI history picker, and the Web history checkbox. It summarizes
only the old path after the common ancestor, excludes tool-result bodies, bounds
the input by context space, and caps output at 4096 tokens. An incomplete summary
does not navigate. The finished summary is persisted with the branch-selection
event and becomes part of the selected model context. No model request occurs
for ordinary navigation. All three summary paths now share `complete_summary`
and the configured LLM retry policy. Each retry starts from an unmodified request;
stream fragments stay out of the answer timeline, authentication failures do not
retry, and cancellation interrupts backoff. Summary usage is attributed as described
above; request attempts and terminal outcomes are recorded as auxiliary Ledger events.

Compaction and branch summaries now append cumulative, sorted `read-files` and
`modified-files` lists computed from tool calls. Modified paths are excluded
from read-only paths; previous generated lists survive another compaction.
These lists track operation targets, not verified success, and do not replace
tool results or receipts. Summary input uses role-based text serialization,
caps each tool result at 2000 characters, and omits image bytes and signatures.
The original Ledger content is unchanged.

The Python lifecycle now distinguishes `agent_end` from `agent_settled`.
`agent_end` closes the model/tool loop, while `agent_settled` fires exactly once
after retry, compaction, steering, and same-run follow-up handling can no longer
schedule more work. It is emitted on both successful and failed native runs.

Context estimation now follows Pi's structured character heuristic: text,
thinking and tool arguments contribute their visible size; each image contributes
an estimated 1200 tokens, regardless of base64 length. This applies to compaction
planning and missing-usage fallback, including images nested in tool results.
It is an approximation, not a provider billing claim; reported usage remains
authoritative when available.
For the next native-loop request, the estimate anchors to the previous response's
input and cache usage, adds its output and only the newly appended messages, then
reserves output space. Previously measured system/tool-schema cost is therefore
not lost when estimating the next request. The same usage anchor is restored
across later runs when the route/model and history-prefix digest still match;
changed branches and compacted prefixes do not reuse an unrelated count.
Before the first native-loop request, restored history is checked against the
Provider's context window using the structured estimate plus system/tool schema
and output reserve. This allows pre-request compaction without first triggering
an overflow. A zero automatic-compaction threshold disables this path as well.

Running-task interaction follows Pi's default one-at-a-time steering queue: consecutive user
corrections reach separate model turns, including messages received during
compaction. The internal `InteractionManager` supports explicit `all` delivery
for callers that need batch behavior. TUI `/delivery` and the Web settings drawer
now read and update both delivery modes through the same typed Core commands. The
selection applies immediately, persists in the user state directory, and is
restored on the next Core start; TOML values remain the initial defaults before a
user choice exists.
Plain-text follow-ups in the current mode are claimed from the durable queue
by the native loop after the current answer, one at a time. They keep the same
run and context. Queue removal follows transcript persistence. Image follow-ups
use the same loop and retain structured image blocks in the conversation.
Queue admission callbacks live in the Python driver, never inside model messages
or native-loop event snapshots. Explicit cancellation retains unconsumed steering
and queued follow-ups as blocked durable messages; they cannot automatically
restart the cancelled task. After their own Stop action completes, TUI and Web
restore those messages ahead of the existing editor draft, retain image attachments,
and remove the restored queue entries. Nothing is submitted automatically. If the
user has switched sessions, the stopped session's messages stay in its durable
queue rather than entering another session's editor. Headless cancellation also
retains the blocked queue. A real-daemon, headless complete-TUI test verifies
restored text, preservation of the existing draft, and an empty queue after Stop;
Web has passed the static build but still needs browser interaction validation.
Pi-style `/skill:name arguments` commands expand into a user-context Skill block,
including source file and relative-reference directory. Initial input, steering,
and follow-ups share this expansion. Unknown Skill names remain literal input;
existing source trust checks still apply. This path does not override the system
prompt or change the active tool catalog. Legacy `/name` Skill commands are aliases
for the same expansion when no prompt template matches. They no longer replace
the system prompt or apply `allowed_tools` as a per-Skill execution whitelist;
authority remains controlled by the current mode, profile and tool permissions.
Skill bodies are literal instructions; argument substitution belongs to prompt
templates, not Skills. Queued mode changes still use the existing new-run dispatcher.
Markdown prompt templates in `~/.coderook/prompts/` and trusted-workspace
`.coderook/prompts/` now expand `/name arguments` in initial input, steering,
and same-run follow-ups. User templates win duplicate names, as in the pinned Pi
loader. Expansion supports quoted arguments, positional/all-argument placeholders,
defaults, and slices; values are never recursively expanded. The session context
catalog reads YAML `description` and `argument-hint` metadata using Python's
PyYAML safe loader. Without a description, it shows the first non-empty body line
(up to 60 characters), without substituting arguments. TUI completion, Ctrl+P,
and the Web command picker display the optional parameter hint. Metadata does not
enter the expanded user prompt. Malformed YAML templates are skipped consistently
by discovery and expansion, allowing the next valid same-name template to load.
The session context
response exposes a shared `input_commands` catalog: TUI slash completion and Web
composer completion consume it instead of inspecting the client's working directory.
Web supports pointer selection and arrow/Tab/Enter completion. TUI Ctrl+P includes
the same input commands under Extensions; selecting them fills the composer and
does not submit. Built-in commands take precedence in that palette. Extra template
paths are supported through `agent.prompt_paths`; live browser visual checks and
executable custom renderer parity remain pending. `agent.skill_paths` supports
explicit Skill files, package directories and collections. Session discovery and
Runner tool assembly receive the same configured paths. Explicit unmanaged sources
are user-trusted; managed sources retain their metadata trust and digest checks.
Skill frontmatter uses safe YAML parsing, including folded/literal descriptions
and UTF-8 BOM. Extra metadata is ignored rather than interpreted as runtime
configuration. `disable-model-invocation: true` hides a Skill from the model's
automatic-selection prompt while keeping `/skill:name` and command discovery
available for explicit user invocation.
Skill collections are searched recursively. A directory containing `SKILL.md`
is one package, so its reference/examples subdirectories are not rediscovered as
independent Skills. Hidden directories and `node_modules` are skipped. Discovery
and `/skill:name` resolution use the manifest's declared name rather than requiring
it to match the file or directory name.
Collection discovery reads `.gitignore`, `.ignore` and `.fdignore`, applying
directory-relative patterns and negations through the existing Python `pathspec`
dependency. Ignored Skills are absent from both the catalog and named resolution;
explicitly configured Markdown files are still loaded directly, as in Pi.
TUI and Web `/reload` refresh the current session's template/Skill command catalog
without a model request or daemon restart. Template and Skill bodies are read on
use, so edited resources are available to the next input. This is resource
rediscovery, not arbitrary Python plugin, Provider, or active-tool hot reloading.
The complete-TUI smoke adds a template while the daemon is running and verifies
that `/reload` discovers it before any model request.
The real-daemon fake-provider smoke covers both an ordinary answer and an
in-flight queued follow-up, including a single run, final text, and queue removal.
It also verifies that an image follow-up reaches the actual Provider request.
A headless complete-Textual-app smoke connects to the real isolated daemon,
executes native file reading through the fake Provider, and checks that the final
answer is rendered as expanded Markdown exactly once. It closes the TUI and opens
the same session again to verify persisted-answer rendering. This exercises the
application message pump and IPC rather than only renderer mocks; it is not a
manual terminal or browser visual acceptance test.
Initial session image attachments now persist alongside text and their Artifact
reference, rather than disappearing after one request. This increases local
Ledger size and may increase subsequent model input cost until compaction.
Both native `read` and legacy `read_image` now retain images inside their paired
tool result in model history and the transcript. The OpenAI Chat and Responses
adapters split nested tool images into image-capable user content at the wire
boundary while retaining the tool-call ID and textual result. Anthropic retains
the nested representation. Legacy callers can still explicitly use the old
pending-image API; built-in image-read results no longer use it.
For non-vision routes, request preparation replaces historical user/tool images
with Pi-style omission placeholders. The prepared RequestSnapshot matches the
actual request; original images remain in the context and Ledger for future
vision-capable turns. Adjacent images collapse to one placeholder. New attachment
submission still follows the frontend's route-readiness check.
For Anthropic Messages routes, request preparation also normalizes incompatible
tool-call IDs to legal identifiers of at most 64 characters and rewrites the
matching tool-result references. A deterministic hash suffix distinguishes long
IDs with the same prefix; already legal IDs are unchanged. This transformation is
recorded in the RequestSnapshot, not written back into conversation history.
Legacy user messages containing both ordinary content and tool results are split
into ordered native user/tool-result messages. This lets nested result images and
call IDs receive the same model-switch transformation as standalone tool results,
without changing the original Ledger records or discarding adjacent user text.
New thinking and redacted-thinking blocks retain their originating model, wire
format and route in local history. Request preparation strips this internal
metadata, preserves signed blocks for the same origin, converts readable thinking
to plain text for another origin, and drops opaque redacted blocks from that
request. Legacy blocks without provenance use the cross-model conversion rather
than assuming that their signatures belong to the selected route. Original blocks
remain unchanged in the Ledger.

Completed model responses also append a separate `context.usage_anchor` event.
On a subsequent run or reopened session, the driver can use that response's
input/cache/output usage plus estimated trailing messages to trigger compaction
before the first request. Anchors are matched against the current route/model and
the exact history prefix; changed branches or compacted prefixes do not reuse an
unrelated count. Sessions without a matching anchor retain request-size estimation.
These events do not add model-visible messages or count usage a second time.

Provider selection is native Python session state rather than a process-global UI
preference. New sessions snapshot the current Catalog default; `session.set_model`
persists an explicit route/model for subsequent turns, and a fork inherits it. TUI
and Web use the same operation after a successful Doctor probe. Request preparation
resolves the selected model ephemerally, so one session never rewrites the shared
Provider route or changes another session's next request.

Thinking level follows the same session-owned rule. New sessions snapshot the
selected route's level, forks inherit it, and `/thinking off|low|medium|high`, the
Web model drawer, `coderook --thinking ...`, and `coderook -p --thinking ...` all
write the same Python Session state. The override is applied only while resolving
that session's next Provider request; it never mutates the shared route catalog.
Python extensions can inspect and change it through `get_thinking_level()` and
`set_thinking_level()`.

Session-owned Python extensions also expose Pi-style runtime control through
`is_idle()`, `has_pending_messages()`, `wait_for_idle()`, `abort()`,
`compact(focus)`, `reload()`, and `shutdown()`.
They call the same SessionManager operations as TUI/Web, so cancellation preserves
pending input, compaction remains append-only, and resource reload refreshes the
shared command catalog instead of creating a second extension runtime.

Extension commands can also use `get_system_prompt_options()`, `new_session()`,
`fork()`, `navigate_tree()`, and `switch_session()`. These operations call the
native SessionManager and Session Ledger instead of manipulating JSONL files.
New sessions inherit the owning session's preset, route, model and thinking level;
forks keep lineage; navigation shares the same branch summary and label path as
TUI/Web; switching accepts a session ID, session directory, or `thread.jsonl`
path and runs the cancellable `session_before_switch` lifecycle event. Results
include the target `session_id`, which suits CodeRook's multi-session daemon rather
than replacing a process-global session object. `new_session()` supports `setup`
and `with_session` callbacks, while `fork()` and `switch_session()` support
`with_session`; each callback receives the newly bound Python `ExtensionAPI`, so
handoff logic can safely send messages or update metadata in the target session.

Python extensions can request `select()`, `confirm()`, and free-form `input()`
through the same durable question cards used by built-in tools. Prompts created by
an idle extension command are persisted as thread-level Runtime events, so Web can
reconnect to them without inventing a fake Agent turn. Visible idle custom messages
use the same thread-level projection, while their model-visible content remains in
the Session Ledger.

Session export is also shared by CLI, TUI and Web. Markdown and JSON remain
available for editing and machine processing; HTML produces a self-contained,
responsive conversation page with session lineage, notes, collapsible thinking
and tool details, and embedded conversation images. It contains no external
scripts, styles or network dependencies, and the Web frontend downloads this
portable HTML form by default.

This is not a claim that the entire product has been ported. Provider and tool
services intentionally remain native Python implementations rather than launching
Pi or Node as a subprocess. The core session, tool, provider, compaction, queue,
branch and extension-control paths are native Python. Browser-level interaction
validation and some product-surface parity still require migration and verification;
custom renderers and themes are deliberately outside the core-runtime migration.
The functional architecture document remains the reference for other subsystems.

## Installed-package smoke

The wheel smoke checks packaged resources and attribution, then runs native
edit/read calls with a fake provider, YAML prompt expansion, Pillow image resizing,
extension activation/disposal, first-run configuration, an isolated Core, IPC ping,
and the served Web shell. It does not start the user's normal daemon or call a
paid model. To avoid accidentally relying on development-only dependencies, run
it in an isolated environment containing the wheel's declared dependencies:

```powershell
uv build
uv run --isolated --no-project --with ./dist/coderook-0.2.0b1-py3-none-any.whl python scripts/smoke_wheel.py dist/coderook-0.2.0b1-py3-none-any.whl
```

This verifies installation and backend entry points, not browser interaction or
complete feature parity with Pi.
