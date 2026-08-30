export type RunMode = "act" | "plan" | "review";

export interface ThreadRecord {
  id: string;
  title: string;
  workspace: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TurnRecord {
  id: string;
  thread_id: string;
  status: string;
  mode?: string;
  usage: Record<string, number | string>;
  created_at: string;
  updated_at: string;
}

export interface TurnItem {
  id: string;
  turn_id: string;
  kind: string;
  payload: Record<string, unknown>;
  tool_call_id?: string | null;
  created_at: string;
}

export interface RuntimeEvent {
  thread_id: string;
  turn_id?: string | null;
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

export interface TurnReceipt {
  turn_id: string;
  status: string;
  outcome?: string | null;
  cost?: number | string;
  result_summary?: string | null;
  failure_category?: string | null;
  files_changed: string[];
  changes: Array<{ path: string; additions?: number | null; deletions?: number | null }>;
  verification: Array<Record<string, unknown>>;
  route?: Record<string, unknown> | null;
  unavailable: string[];
}

export interface ProviderCatalog {
  active_route_id: string | null;
  readiness: {
    status: string;
    local_ready: boolean;
    reason: string;
  };
  routes: Array<Record<string, unknown>>;
  presets: Array<{
    id: string;
    name: string;
    description: string;
    base_url: string;
    models: string[];
    credential_required: boolean;
    local: boolean;
    capabilities: Record<string, boolean>;
  }>;
}

export interface WorkspaceEntry {
  path: string;
  name: string;
  kind: "directory" | "file";
  size: number | null;
}

export interface DiffPayload {
  files?: Array<Record<string, unknown>>;
  state_digest?: string;
  summary?: Record<string, unknown>;
  [key: string]: unknown;
}
