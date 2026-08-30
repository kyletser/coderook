import { describe, expect, it } from "vitest";

import {
  activeFileMention,
  appendRuntimeEvent,
  eventBelongsToThread,
  modelContentFor,
  parentWorkspacePath,
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
});
