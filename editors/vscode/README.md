# CodeRook for VS Code

This minimal extension validates that the public Runtime API is sufficient for an IDE
client. It does not add an IDE-only daemon endpoint or duplicate runtime state.

Supported commands:

- create or resume a durable thread;
- send a task and stream durable SSE events with `Last-Event-ID` recovery;
- approve or deny a `permission.requested` event;
- inspect the structured workspace diff;
- steer or interrupt the active turn.

Set `coderook.baseUrl` and `coderook.apiToken`, run `npm ci && npm run compile`, then launch
the extension host. Run `npm run package` to produce `dist/coderook-vscode.vsix`. The daemon
must already be running. The extension is distributed independently from the Python wheel.

The distribution workflow runs `npm run test:host` under Xvfb against an isolated real
CodeRook daemon. It verifies extension activation, command registration, durable thread
creation/resume, and opening the workspace diff, then uploads a commit-bound JSON report.
Permission UI and Marketplace publication still require separate manual release evidence.
