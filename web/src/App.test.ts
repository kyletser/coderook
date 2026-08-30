import { describe, expect, it } from "vitest";

import {
  activeFileMention,
  appendRuntimeEvent,
  eventBelongsToThread,
  modelContentFor,
  parentWorkspacePath,
  resultStatusIsFailure,
  workspacePathIsDirectoryError,
} from "./App";
import type { RuntimeEvent } from "./types";

describe("Web task submission", () => {
  it("keeps the visible shell command separate from the model instruction", () => {
    const content = modelContentFor("!pytest -q", []);

    expect(content).toContain("exact shell command");
    expect(content).toContain("pytest -q");
  });

  it("adds only selected file references still present in the composer", () => {
    const content = modelContentFor("检查 @src/app.py", ["src/app.py", "src/old.py"]);

    expect(content).toContain('["src/app.py"]');
    expect(content).not.toContain("src/old.py");
  });

  it("rejects events emitted by a previously selected thread", () => {
    expect(eventBelongsToThread("thread-b", "thread-a")).toBe(false);
    expect(eventBelongsToThread("thread-b", "thread-b")).toBe(true);
  });

  it("navigates to the parent workspace directory without escaping root", () => {
    expect(parentWorkspacePath("src/code_rook/core")).toBe("src/code_rook");
    expect(parentWorkspacePath("src")).toBe(".");
    expect(parentWorkspacePath(".")).toBe(".");
  });

  it("extracts the active inline file mention at the caret", () => {
    const value = "修复 @src/code_ro";
    const mention = activeFileMention(value, value.length);

    expect(mention?.query).toBe("src/code_ro");
    expect(value.slice(mention?.start, mention?.end)).toBe("@src/code_ro");
    expect(activeFileMention(`${value} 继续`, `${value} 继续`.length)).toBeNull();
  });

  it("keeps a per-thread event cache free of duplicate replay rows", () => {
    const event: RuntimeEvent = {
      thread_id: "thread-1",
      seq: 1,
      type: "run.phase_changed",
      payload: { phase: "exploring" },
      ts: "2026-08-30T00:00:00Z",
    };

    expect(appendRuntimeEvent(appendRuntimeEvent([], event), event)).toEqual([event]);
  });

  it("bounds long-session event memory while preserving the newest cursor", () => {
    const events: RuntimeEvent[] = Array.from({ length: 5000 }, (_, index) => ({
      thread_id: "thread-1",
      seq: index + 1,
      type: "tool.call_progress",
      payload: {},
      ts: "2026-08-30T00:00:00Z",
    }));
    const latest: RuntimeEvent = { ...events[0], seq: 5001 };

    const bounded = appendRuntimeEvent(events, latest);

    expect(bounded).toHaveLength(5000);
    expect(bounded[0].seq).toBe(2);
    expect(bounded.at(-1)?.seq).toBe(5001);
  });

  it("recognizes directory preview errors so the file drawer can browse them", () => {
    expect(workspacePathIsDirectoryError(new Error("workspace path is not a file"))).toBe(true);
    expect(workspacePathIsDirectoryError(new Error("workspace path does not exist"))).toBe(false);
  });

  it("never presents incomplete model termination as a successful result", () => {
    expect(resultStatusIsFailure("completed")).toBe(false);
    expect(resultStatusIsFailure("incomplete")).toBe(true);
    expect(resultStatusIsFailure("length")).toBe(true);
    expect(resultStatusIsFailure("transport_error")).toBe(true);
  });
});
