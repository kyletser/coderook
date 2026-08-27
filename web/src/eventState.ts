import type { RuntimeEvent } from "./types";

export interface EventState {
  events: RuntimeEvent[];
  cursor: number;
  phase: string;
  pendingPermissions: string[];
}

export const emptyEventState: EventState = {
  events: [],
  cursor: 0,
  phase: "idle",
  pendingPermissions: [],
};

export function reduceRuntimeEvent(state: EventState, event: RuntimeEvent): EventState {
  if (event.seq <= state.cursor || state.events.some((item) => item.seq === event.seq)) return state;
  let phase = state.phase;
  if (event.type === "run.phase_changed" && typeof event.payload.phase === "string") {
    phase = event.payload.phase;
  }
  let pendingPermissions = state.pendingPermissions;
  const permissionId = event.payload.tool_use_id;
  if (event.type === "permission.requested" && typeof permissionId === "string") {
    pendingPermissions = [...new Set([...pendingPermissions, permissionId])];
  }
  if (event.type === "permission.resolved" && typeof permissionId === "string") {
    pendingPermissions = pendingPermissions.filter((item) => item !== permissionId);
  }
  return {
    events: [...state.events, event],
    cursor: event.seq,
    phase,
    pendingPermissions,
  };
}
