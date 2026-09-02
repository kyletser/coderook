# CodeRook

**A local-first coding agent that makes every run reviewable, recoverable, and evidence-backed.**

[中文快速开始](docs/zh-CN/README.md) · [中文使用说明](docs/guides/USER_GUIDE.md) · [Architecture](docs/reference/FUNCTIONAL_ARCHITECTURE.md) · [Release status](docs/status/RELEASE_SCORECARD.md)

> **Status: 0.2.0-beta.1 candidate / v1.0.0 NO-GO.** CodeRook is installable from source today.
> Public packages, cross-platform release artifacts, real-model benchmark results, and the v1 tag
> have not been published. See the scorecard before relying on it for production work.

CodeRook is a TUI-and-Web coding agent built around one persistent local daemon. It can inspect a
repository, plan changes, edit files, run verification, preserve sessions across reconnects, and
show the evidence behind a result. It supports bring-your-own-key providers and local models; it
does not require an account with a hosted CodeRook service and sends no default telemetry.

## Why CodeRook

- **Durable by design.** Sessions, turns, event cursors, receipts, checkpoints, and runtime
  projections survive client reconnects and make interrupted work diagnosable.
- **Review before trust.** Tool capabilities, per-turn authority snapshots, approval prompts,
  workspace boundaries, change inspection, read-only review, and rewind are part of one path.
- **Evidence, not a green-looking chat.** Run result cards distinguish completion, failure,
  interruption, incomplete model output, content filtering, transport failure, unavailable
  verification, changed files, route/model, usage, and receipts.
- **Provider-independent.** The shared catalog covers DeepSeek, OpenAI, Anthropic, Gemini,
  Kimi/Moonshot, OpenRouter, SiliconFlow, Ollama, and LM Studio, plus custom OpenAI Chat,
  OpenAI Responses, and Anthropic Messages routes.

## Quick start from source

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), and Git.

```bash
git clone https://github.com/kyletser/coderook.git
cd coderook
uv sync
uv run coderook
```

`coderook` opens the TUI and starts or reuses the local `coderook-core` daemon. The first screen
does **not** force API configuration: sessions, help, and settings remain available. Before the
first coding task, readiness checks require an active route and a resolvable credential (or a
reachable no-key local route); a blocked submission keeps the draft and does not create a failed
run.

Open the same workspace in a local browser without installing Node.js:

```bash
uv run coderook web
uv run coderook web C:\path\to\repo
uv run coderook web --no-open
```

The Web UI binds only to `127.0.0.1`. Opening its stable local URL automatically establishes an
HttpOnly SameSite browser session; no expiring launch link is required. Provider API keys and the
Core bearer token never enter browser storage or ordinary responses. TUI and Web share
the same sessions, durable event cursor, permissions, receipts, Checkpoints and Change Center.

Use the project switcher in the Web sidebar to create a blank workspace under
`~/CodeRookProjects`, choose another location, or open an existing local folder. Switching projects
rebinds the idle Core in place: the browser page, authenticated session, HTTP listener, and IPC
listener stay alive while workspace-scoped sessions, tools, Shell, MCP, Workers, artifacts, and
Change Center are replaced. Each session remains bound to one explicit workspace. When launched
without an explicit path from CodeRook's own editable source checkout, Web opens a neutral project
picker and keeps the agent source unavailable until a user project is selected.

Explicit product entries are:

```text
coderook          # TUI (default)
coderook tui      # TUI (explicit)
coderook web      # local browser workspace
coderook run ...  # script/headless mode
```

Configure a route in the TUI with `/config`, or use the CLI:

```bash
uv run coderook configure
uv run coderook provider list
uv run coderook provider test
```

Ollama (`127.0.0.1:11434`) and LM Studio (`127.0.0.1:1234`) presets use loopback endpoints and do
not require API keys. Remote credentials are stored in the system keyring when available, with a
permission-restricted user file as fallback. Repository `.env` files are never loaded
automatically; an env file is read only when a caller explicitly supplies it.

```bash
uv run coderook --env-file C:\\path\\to\\deployment.env
# or, when running the daemon directly:
uv run coderook-core --env-file C:\\path\\to\\deployment.env
```

The selected path is explicitly forwarded to the auto-started Core. Each process parses the file
without interpolation into a read-only in-memory overlay; it never mutates the parent environment
or sends a credential through IPC. Process environment values take precedence. The file cannot
select `CODEROOK_CONFIG`, and loading it does not copy secrets into repository configuration.
Because CodeRook does not persist a daemon
configuration fingerprint, using `--env-file` safely restarts an idle managed Core for the same
workspace. A busy, unmanaged, or `--no-auto-core` daemon fails closed instead of guessing which
credential overlay it uses.

## Product workflow

```text
Open a repository
  -> restore or create a session
  -> select a provider/model
  -> describe a task
  -> review plan and permissions
  -> inspect edits and verification
  -> read the run result and receipt
  -> review, continue, or rewind
```

Useful TUI commands:

| Command | Purpose |
|---|---|
| `/config`, `/provider`, `/model`, `/doctor` | Configure and diagnose model routes |
| `/plan`, `/mode`, `/permissions`, `/trust`, `/sandbox` | Control execution and safety |
| `/sessions`, `/new`, `/rename`, `/fork`, `/export`, `/delete` | Manage durable sessions |
| `/changes` (`/diff` alias), `/stage`, `/commit`, `/review`, `/turn`, `/rewind` | Inspect, select, commit, and recover changes |
| `/goal` | Manage a durable goal and its bounded continuation policy |
| `/compact [focus]` | Compact older context while preserving task facts and complete tool pairs |
| `/workers` | Start, inspect, steer, retry, cancel, review, and explicitly apply a session worker |
| `/history status\|on\|off\|clear` | Control workspace-scoped input history |
| `/language zh-CN\|en-US` | Switch the persisted TUI language preference |

The result card links to `/changes`, `/review`, `/rewind`, and `/turn`. The Change Center supports
file and hunk navigation and keeps current workspace state separate from durable Turn evidence.
Its `state_digest` is a scope-bound review token over the exact HEAD ref/commit, index, tracked and
untracked state, and the canonical visible payload. `/stage <path...> --yes` accepts an `all` review
and adds only explicitly selected, fully reviewable files; its response supplies the `staged` review
required by `/commit <subject> --yes`, and the TUI opens that final staged view before commit can be
confirmed. Commits are local and run without hooks, signing, or push.
Both actions require no active Turn anywhere in the workspace, healthy audit storage, and a trusted
workspace. Conflicts, opaque evidence gaps, truncated reviews, outside-workspace staged files, ref or
index races, and stale review tokens fail closed.

## Safety boundary

- Linux can use bubblewrap and macOS can use Seatbelt after a real execution probe succeeds.
  The enforced profiles expose the workspace, required runtime paths, and temporary directories
  instead of read-binding the whole host filesystem.
- Windows uses a probed Restricted Token + NTFS ACL backend that confines writes to the workspace
  and a private temporary directory. It is reported as `partial`: reads and network remain outside
  this boundary, and every Shell/Run action still requires explicit approval.
- Shell environments are allow-listed and remove common API-key, cloud, Git, and SSH credential
  variables. This is defense in depth, not a general secret-detection guarantee.
- If the event ledger or runtime projection fails to persist, CodeRook emits `audit.degraded` and
  denies non-read tool actions. Diagnostics and read-only inspection remain available.
- Domain-specific outbound allow-lists are rejected when no OS backend can enforce them; they do
  not silently become unrestricted network access.

Read the [threat model](docs/reference/THREAT_MODEL.md) for the exact trust assumptions.

## Stable contracts and Labs

The runtime capability response is the source of truth for feature level. The current public
stable contract flags cover durable threads/turns, event cursor replay, SSE replay, receipts,
interrupt/steer, permission responses, Provider Catalog/readiness, checkpoints, Change Center,
bounded Goal continuation, basic subagents, Skills, MCP Tools, and Memory. Fleet workers,
declarative workflows, Hooks v2, MCP Resources/Prompts, and the VS Code prototype are **Labs**.
VS Code is not a v1 release surface.

Labs are disabled and hidden by default. Maintainers can opt in for one process tree with
`CODEROOK_LABS=1 uv run coderook` (PowerShell: set `$env:CODEROOK_LABS = "1"` before launch).
Restart an already-running Core after changing the flag. When Labs are disabled, CodeRook does not
load project/user Hooks and does not expose or resume Workflow/Fleet control planes. Labs features
are not exempt from the same permission and audit boundaries, but their UX and recovery semantics
may still change before v1.

## Installation and release artifacts

There is no public CodeRook PyPI or GitHub Release at the time of this baseline. Source install is
the supported way to evaluate the project.

The tag-driven release workflow is prepared to build:

- PyPI wheel/sdist through Trusted Publishing;
- self-contained Windows x64, Linux x64/arm64, and macOS x64/arm64 archives;
- a GHCR image, checksums, SPDX SBOMs, provenance, and signatures;
- Homebrew formula and Scoop manifest files attached to the GitHub Release.

The repository does **not** currently publish or maintain an external Homebrew tap or Scoop
bucket. A generated formula/manifest in a future Release asset is not equivalent to
`brew install` or `scoop install` availability. See [Releasing](docs/operations/RELEASING.md).

## Architecture

```text
coderook / coderook-tui -- authenticated JSON-RPC/NDJSON --+
                                                            |
browser SPA -- HttpOnly cookie, HTTP/JSON + durable SSE -----+
                                                            v
coderook-core
  |-- agent loop, provider routes, tools, permissions, sandbox plans
  |-- session ledger, runtime projection, events, receipts, checkpoints
  |-- repository index, diagnostics, memory, MCP, background work
  `-- local static Web assets and authenticated API
```

The daemon owns state; the TUI and script-oriented CLI are clients. Task events are replayed from
the durable thread stream with a per-session sequence cursor, while global daemon events remain a
separate channel. Details are in the
[functional architecture](docs/reference/FUNCTIONAL_ARCHITECTURE.md).

Before each Turn, CodeRook freezes a deterministic TaskProfile that controls planning,
model-visible tools, long-context policy, and whether delegation is permitted. Low-confidence work
must ask one focused clarification; ambiguous mutation then remains read-only until a Plan Ticket is
recorded and approved through the durable Plan Review flow. Adaptive compaction appends a shadow
projection instead of rewriting the Ledger and refuses to commit if
Ledger-backed goals, constraints, pending approvals, or failures lose their source-event references.
Multi-agent plans are bounded to three Workers, require a Delegation Ticket, reject dependency
cycles and overlapping Write Claims, and keep writes in independent worktrees until digest-bound review.
These mechanisms have reproducible experiment runners, but the repository does not claim quality
improvements until their raw reports exist. See the
[reliability experiment guide](docs/guides/RELIABILITY_EXPERIMENTS.md).

## CLI and development

```bash
uv run coderook run --goal "Inspect this repository" --output-format stream-json
uv run coderook review --goal "Review the current diff" --output-format json
uv run coderook doctor runtime --json
uv run coderook trace --follow
```

```bash
uv run ruff check .
uv run python scripts/check_brand.py
uv run python scripts/check_public_repo.py
uv run mypy src
uv run mypy --platform linux src
uv run pytest -q
uv run python scripts/gen_protocol_doc.py --check
uv build
uv run python scripts/smoke_wheel.py dist
```

The checked-in GitHub CI definition is intentionally a single fast Ubuntu gate. Cross-platform sandbox, recovery,
distribution, MCP, security, and real-model evidence run only by manual dispatch or release tag.
The existence of those workflows is not evidence that the current commit passed them.

## Release honesty

CodeRook will not be tagged `v1.0.0` until the scorecard is GO. Current blockers include real-model
pass@1 reports, two-wire-format repetitions, public Aider/SWE-bench harness artifacts,
current-commit cross-platform security/recovery/install evidence, and first-user onboarding tests.
The built-in **50 任务离线 benchmark** currently proves fixture, verifier, budget, and report
contracts only; it is not a model-quality result.

- [Documentation index](docs/README.md)
- [Chinese user guide](docs/guides/USER_GUIDE.md)
- [Runtime API](docs/reference/RUNTIME_API.md)
- [Compatibility policy](docs/reference/COMPATIBILITY.md)
- [Wire protocol](docs/reference/WIRE_PROTOCOL.md)
- [Public benchmark protocol](docs/reference/PUBLIC_BENCHMARKS.md)
- [Release scorecard](docs/status/RELEASE_SCORECARD.md)
- [Roadmap](docs/status/ROADMAP.md)
- [Security policy](.github/SECURITY.md)
- [Contributing](.github/CONTRIBUTING.md)
- [Support](.github/SUPPORT.md)
- [Code of Conduct](.github/CODE_OF_CONDUCT.md)
- [Governance](.github/GOVERNANCE.md)
- [Changelog](CHANGELOG.md)

## License

[MIT](LICENSE)
