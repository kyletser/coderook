import { expect, it } from "vitest";
import { navigationTimeline, type NavigationProjection } from "./sessionNavigation";
import type { RuntimeEvent, TurnItem } from "./types";

it("reconstructs the chosen path while excluding old runs and retaining new work", () => {
  const navigation: NavigationProjection = {
    thread_id: "s", ledger_seq: 8, excluded_turn_ids: ["old"],
    messages: [
      { role: "user", content: "Question" },
      { role: "assistant", content: [{ type: "tool_use", id: "tool", name: "read", input: { path: "a.py" } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "tool", content: "source" }] },
      { role: "assistant", content: [{ type: "thinking", thinking: "private" }, { type: "text", text: "Answer" }] },
    ],
  };
  const items = ["old", "new"].map(turn_id => ({ id: turn_id, turn_id, kind: "message", payload: { content: turn_id }, created_at: "now" })) as TurnItem[];
  const events = ["old", "new"].map((turn_id, seq) => ({ turn_id, seq, thread_id: "s", type: "agent.message", payload: {}, ts: "now" })) as RuntimeEvent[];
  const result = navigationTimeline(navigation, items, events);
  expect(result.items.map(item => item.kind)).toEqual(["message", "tool_call", "tool_result", "message", "message"]);
  expect(result.items.at(-1)?.turn_id).toBe("new");
  expect(result.events).toEqual([events[1]]);
  expect(JSON.stringify(result.items)).not.toContain("private");
  expect(navigationTimeline(null, items, events)).toEqual({ items, events });
});

it("renders inherited direct shell history as a command and tool result", () => {
  const navigation: NavigationProjection = {
    thread_id: "s", ledger_seq: 9, excluded_turn_ids: [],
    messages: [{
      role: "bashExecution", command: "echo visible", output: "visible",
      status: "success", exclude_from_context: false,
    }],
  };

  const result = navigationTimeline(navigation, [], []);

  expect(result.items.map(item => item.kind)).toEqual(["message", "tool_call", "tool_result"]);
  expect(result.items[0].payload.content).toBe("!echo visible");
  expect(result.items[1].payload).toEqual({ tool_name: "Bash", params: { command: "echo visible" } });
  expect(result.items[2].payload).toEqual({ output: "visible", is_error: false });
});
