import { describe, expect, it } from "vitest";
import { piMessageSnapshots } from "./piMessages";
import type { RuntimeEvent } from "./types";

describe("Pi message lifecycle", () => {
  it("replays a visible extension message once with its source", () => {
    const event = {seq: 1, type: "agent.message", thread_id: "t", turn_id: "r", ts: "now",
      payload: {role: "custom", message_id: "r:extension:0", custom_type: "notice",
        content: [{type: "text", text: "Extension note"}]} } as RuntimeEvent;
    expect(piMessageSnapshots([event, event])).toEqual([event]);
  });
  it("keeps one message with the final text and separate thinking blocks", () => {
    const events = [1, 2, 3].map(seq => ({
      seq, type: "agent.message", thread_id: "thread", turn_id: "turn", ts: `time-${seq}`,
      payload: { role: "assistant", message_id: "one", phase: seq === 3 ? "end" : "update",
        content: [{ type: "thinking", thinking: "analysis" },
          { type: "text", text: seq === 3 ? "final answer" : "partial" }] },
    })) as RuntimeEvent[];
    const result = piMessageSnapshots([...events, events[1]]);
    expect(result).toHaveLength(1);
    expect(result[0].ts).toBe("time-1");
    expect(result[0].payload.content).toEqual(events[2].payload.content);
  });
  it("does not combine messages from different turns", () => {
    const result = piMessageSnapshots(["a", "b"].map(turn_id => ({
      seq: 1, type: "agent.message", thread_id: "thread", turn_id, ts: "time",
      payload: { role: "assistant", message_id: "same", content: [] },
    })) as RuntimeEvent[]);
    expect(result).toHaveLength(2);
  });
});
