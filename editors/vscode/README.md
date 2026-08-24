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
must already be running. Any future extension distribution would remain independent from the
Python wheel and the v1 TUI release contract.

The distribution workflow defines `npm run test:host` under Xvfb against an isolated real
CodeRook daemon. It verifies extension activation, command registration, durable thread
creation/resume, workspace diff, and approval UI evidence. This job runs only when a maintainer
manually dispatches the focused `vscode` distribution target; the tag release does not package a
VSIX. A workflow definition is not evidence for the current commit until that dispatch produces an
artifact. The extension is not published to the VS Code Marketplace.
