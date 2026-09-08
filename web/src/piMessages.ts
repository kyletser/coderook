import type { RuntimeEvent } from "./types";

// 合并同一上游消息的快照，保持首个位置和最后内容，不把每次更新渲染成新消息。
export function piMessageSnapshots(events: RuntimeEvent[]): RuntimeEvent[] {
  const messages = new Map<string, RuntimeEvent>();
  for (const event of events) {
    if (event.type !== "agent.message" || !["assistant", "custom"].includes(String(event.payload.role))) continue;
    const id = `${event.turn_id}:${String(event.payload.message_id)}`;
    const previous = messages.get(id);
    if (!previous || event.seq > previous.seq) {
      messages.set(id, { ...event, ts: previous?.ts ?? event.ts });
    }
  }
  return [...messages.values()];
}
