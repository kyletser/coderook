import type { RuntimeEvent, TurnItem } from "./types";

export interface NavigationProjection {
  thread_id: string;
  ledger_seq: number;
  messages: Array<{ role: string; content: unknown }>;
  excluded_turn_ids: string[];
}

export function navigationTimeline(
  navigation: NavigationProjection | null,
  items: TurnItem[],
  events: RuntimeEvent[],
): { items: TurnItem[]; events: RuntimeEvent[] } {
  if (!navigation) return { items, events };
  const excluded = new Set(navigation.excluded_turn_ids);
  const history: TurnItem[] = [];
  for (const message of navigation.messages) {
    const blocks = Array.isArray(message.content) ? message.content : [{ type: "text", text: message.content }];
    for (const block of blocks) {
      if (!block || typeof block !== "object") continue;
      const item: TurnItem = {
        id: `history:${navigation.ledger_seq}:${String(history.length).padStart(8, "0")}`,
        turn_id: "", kind: "message", payload: {}, created_at: "",
      };
      if (block.type === "tool_use") {
        item.kind = "tool_call";
        item.tool_call_id = String(block.id);
        item.payload = { tool_name: block.name, params: block.input };
      } else if (block.type === "tool_result") {
        item.kind = "tool_result";
        item.tool_call_id = String(block.tool_use_id);
        item.payload = { output: block.content, is_error: block.is_error };
      } else if (block.type === "text" || block.type === "image") {
        item.payload = { role: message.role, content: [block] };
      } else continue;
      history.push(item);
    }
  }
  return {
    items: [...history, ...items.filter(item => !excluded.has(item.turn_id))],
    events: events.filter(event => !excluded.has(event.turn_id || String(event.payload.run_id || ""))),
  };
}
