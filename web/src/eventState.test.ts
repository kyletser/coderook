import { describe, expect, it } from "vitest";
import { emptyEventState, reduceRuntimeEvent } from "./eventState";
import type { RuntimeEvent } from "./types";

function event(seq: number, type: string, payload: Record<string, unknown>): RuntimeEvent {
  return { thread_id: "thread-1", seq, type, payload, ts: "2026-08-27T00:00:00Z" };
}

describe("runtime event reducer", () => {
  it("deduplicates durable replay and keeps the newest phase", () => {
    const changed = event(4, "run.phase_changed", { phase: "verifying" });
    const once = reduceRuntimeEvent(emptyEventState, changed);
    const replayed = reduceRuntimeEvent(once, changed);
    expect(once.phase).toBe("verifying");
    expect(replayed).toBe(once);
    expect(replayed.events).toHaveLength(1);
  });

  it("tracks permission cards until the matching durable resolution", () => {
    const waiting = reduceRuntimeEvent(
      emptyEventState,
      event(1, "permission.requested", { tool_use_id: "tool-1" }),
    );
    const resolved = reduceRuntimeEvent(
      waiting,
      event(2, "permission.resolved", { tool_use_id: "tool-1" }),
    );
    expect(waiting.pendingPermissions).toEqual(["tool-1"]);
    expect(resolved.pendingPermissions).toEqual([]);
  });
});
