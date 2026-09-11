import type { RuntimeEvent, TurnItem } from "./types";

export interface NavigationProjection {
  thread_id: string;
  ledger_seq: number;
  messages: Array<{
    role: string;
    content?: unknown;
    command?: string;
    output?: string;
    status?: string;
    exclude_from_context?: boolean;
  }>;
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
    if (message.role === "bashExecution") {
      const toolCallId = `history-shell:${navigation.ledger_seq}:${history.length}`;
      const command = String(message.command || "");
      history.push({
        id: `${toolCallId}:request`, turn_id: "", kind: "message",
        payload: { role: "user", content: `${message.exclude_from_context ? "!!" : "!"}${command}` },
        created_at: "",
      });
      history.push({
        id: `${toolCallId}:call`, turn_id: "", kind: "tool_call", tool_call_id: toolCallId,
        payload: { tool_name: "Bash", params: { command } }, created_at: "",
      });
      history.push({
        id: `${toolCallId}:result`, turn_id: "", kind: "tool_result", tool_call_id: toolCallId,
        payload: { output: String(message.output || ""), is_error: message.status !== "success" },
        created_at: "",
      });
      continue;
    }
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
