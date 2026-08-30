import { describe, expect, it } from "vitest";

import { eventBelongsToThread, modelContentFor, parentWorkspacePath } from "./App";

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
});
