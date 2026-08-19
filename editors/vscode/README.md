# CodeRook VS Code prototype

This minimal extension validates that the public Runtime API is sufficient for an IDE
client. It does not add an IDE-only daemon endpoint or duplicate runtime state.

Supported commands:

- create or resume a durable thread;
- send a task and stream durable SSE events with `Last-Event-ID` recovery;
- approve or deny a `permission.requested` event;
- inspect the structured workspace diff;
- steer or interrupt the active turn.

Set `coderook.baseUrl` and `coderook.apiToken`, run `npm install && npm run compile`, then
launch the extension host. The daemon must already be running. This directory is a prototype
and is intentionally not included in the Python wheel.
