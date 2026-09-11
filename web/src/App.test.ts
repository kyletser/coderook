import { describe, expect, it } from "vitest";

import {
  activeFileMention,
  applyExtensionUiUpdate,
  appendRuntimeEvent,
  displayableThreads,
  eventBelongsToThread,
  finishesCurrentThreadLoad,
  followUpUsesQueue,
  isSimpleProductQuestion,
  modelContentFor,
  parentWorkspacePath,
  preferredThreadId,
  resolveWebLocale,
  resolveWebTheme,
  resultSummaryFor,
  resultStatusIsFailure,
  runKindFromEvents,
  summarizeVerification,
  verificationHasFailure,
  workspaceHasUserProject,
  workspacePathIsDirectoryError,
} from "./App";
import type { RuntimeEvent, ThreadRecord } from "./types";

describe("Web task submission", () => {
  it("preserves direct shell commands instead of generating a model instruction", () => {
    const content = modelContentFor("!pytest -q", []);

    expect(content).toBe("!pytest -q");
    expect(modelContentFor('!!printf "%s" "$VALUE"', ["VALUE"])).toBe('!!printf "%s" "$VALUE"');
  });

  it("queues follow-ups while a direct user shell command is active", () => {
    const started: RuntimeEvent = {
      thread_id: "thread-1",
      turn_id: "turn-shell",
      seq: 1,
      type: "run.started",
      payload: { run_kind: "user_shell" },
      ts: "2026-09-11T00:00:00Z",
    };

    expect(runKindFromEvents([started])).toBe("user_shell");
    expect(followUpUsesQueue("user_shell", false, "解释命令输出")).toBe(true);
    expect(followUpUsesQueue("agent", false, "补充检查 tests")).toBe(false);
    expect(runKindFromEvents([
      started,
      { ...started, seq: 2, type: "run.finished", payload: {} },
    ])).toBe("agent");
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

  it("finishes restoring when a newer refresh supersedes the initial thread load", () => {
    expect(finishesCurrentThreadLoad("thread-1", "thread-1", 2, 2)).toBe(true);
    expect(finishesCurrentThreadLoad("thread-1", "thread-1", 2, 1)).toBe(false);
    expect(finishesCurrentThreadLoad("thread-2", "thread-1", 2, 2)).toBe(false);
  });

  it("restores the newest non-empty thread before a newer unused draft", () => {
    const base = {
      title: "",
      workspace: "C:/repo",
      status: "idle",
      created_at: "2026-08-30T00:00:00Z",
      updated_at: "2026-08-30T00:00:00Z",
    };
    const threads: ThreadRecord[] = [
      { ...base, id: "empty-new", turn_count: 0 },
      { ...base, id: "used", turn_count: 2 },
    ];

    expect(preferredThreadId(threads)).toBe("used");
    expect(preferredThreadId([threads[0]])).toBe("empty-new");
    expect(preferredThreadId([])).toBe("");
  });

  it("hides only untitled zero-turn sessions from the task list", () => {
    const base = {
      workspace: "C:/repo",
      status: "idle",
      created_at: "2026-08-30T00:00:00Z",
      updated_at: "2026-08-30T00:00:00Z",
    };
    const threads: ThreadRecord[] = [
      { ...base, id: "empty", title: "", turn_count: 0 },
      { ...base, id: "named", title: "Draft task", turn_count: 0 },
      { ...base, id: "used", title: "", turn_count: 1 },
    ];

    expect(displayableThreads(threads).map((thread) => thread.id)).toEqual(["named", "used"]);
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

  it("applies extension UI contributions without discarding unrelated state", () => {
    let state = applyExtensionUiUpdate({}, { kind: "status", key: "branch", value: "main" });
    state = applyExtensionUiUpdate(state, {
      kind: "widget",
      key: "hint",
      value: { content: "Use /review", placement: "above" },
    });
    state = applyExtensionUiUpdate(state, { kind: "tools_expanded", value: true });

    expect(state.statuses).toEqual({ branch: "main" });
    expect(state.widgets?.hint.content).toBe("Use /review");
    expect(state.tools_expanded).toBe(true);
    expect(applyExtensionUiUpdate(state, { kind: "status", key: "branch", value: null }).statuses).toEqual({});
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
    expect(resultStatusIsFailure("completed", true)).toBe(true);
    expect(resultStatusIsFailure("incomplete")).toBe(true);
    expect(resultStatusIsFailure("length")).toBe(true);
    expect(resultStatusIsFailure("transport_error")).toBe(true);
  });

  it("treats a failed verification verdict as an unsuccessful task result", () => {
    expect(verificationHasFailure([{ verdict: "fail", status: "ok" }])).toBe(true);
    expect(verificationHasFailure([{ verdict: "pass" }])).toBe(false);
  });

  it("keeps unavailable verification distinct from passed evidence", () => {
    expect(summarizeVerification([{ status: "unavailable" }])).toEqual({
      status: "unknown",
      passed: 0,
      total: 0,
      unknown: 1,
    });
    expect(summarizeVerification([{ verdict: "pass", gate_count: 2, passed: 2 }])).toEqual({
      status: "pass",
      passed: 2,
      total: 2,
      unknown: 0,
    });
    expect(summarizeVerification([{ verdict: "failed", gate_count: 1, passed: 0 }]).status).toBe("fail");
    expect(summarizeVerification([
      { verdict: "pass", gate_count: 1, passed: 1 },
      { status: "unavailable" },
    ])).toEqual({ status: "pass", passed: 1, total: 1, unknown: 0 });
    expect(summarizeVerification([
      { verdict: "pass", gate_count: 1, passed: 1 },
      { status: "unavailable", gate_count: 1 },
    ])).toEqual({ status: "unknown", passed: 1, total: 2, unknown: 1 });
  });

  it("uses the persisted run result while the receipt projection is still loading", () => {
    const event: RuntimeEvent = {
      thread_id: "thread-1",
      turn_id: "turn-1",
      seq: 9,
      type: "run.finished",
      payload: { status: "success", result_summary: "Final answer" },
      ts: "2026-08-30T00:00:00Z",
    };

    expect(resultSummaryFor(event.payload, null, "")).toBe("Final answer");
    expect(resultSummaryFor(event.payload, {
      result_summary: "Receipt answer",
      failure_category: undefined,
    }, "")).toBe("Receipt answer");
  });

  it("normalizes persisted interface preferences to supported values", () => {
    expect(resolveWebLocale("en-GB")).toBe("en-US");
    expect(resolveWebLocale("zh-TW")).toBe("zh-CN");
    expect(resolveWebTheme("high-contrast")).toBe("high-contrast");
    expect(resolveWebTheme("dark")).toBe("light");
  });

  it("keeps simple Chinese and English product questions out of the planning display", () => {
    expect(isSimpleProductQuestion("你能做什么？")).toBe(true);
    expect(isSimpleProductQuestion("What model are you? ")).toBe(true);
    expect(isSimpleProductQuestion("Fix the model router tests")).toBe(false);
  });

  it("keeps file and task entry points closed in the neutral welcome workspace", () => {
    expect(workspaceHasUserProject("C:\\Users\\demo\\.coderook\\welcome-workspace")).toBe(false);
    expect(workspaceHasUserProject("/home/demo/.coderook/welcome-workspace/")).toBe(false);
    expect(workspaceHasUserProject("C:\\Users\\demo\\project")).toBe(true);
  });
});
