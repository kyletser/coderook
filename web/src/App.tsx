import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement, ReactNode } from "react";
import { bootstrap, request, streamEvents } from "./api";
import { browserBridge } from "./platform";
import type {
  DiffPayload,
  ProviderCatalog,
  RunMode,
  RuntimeEvent,
  ThreadRecord,
  TurnItem,
  TurnRecord,
  WorkspaceEntry,
} from "./types";

type Drawer = "files" | "changes" | "models" | "advanced" | null;
type ImageAttachment = {
  sha256: string;
  media_type: string;
  size: number;
  width: number;
  height: number;
  name: string;
};
type QueuedMessage = {
  id: string;
  content: string;
  display_content: string;
  mode: RunMode;
  attachments: Omit<ImageAttachment, "name">[];
  status: "queued" | "dispatching" | "blocked";
  error: string;
};
type ToolTimelineEntry = {
  kind: "tool";
  key: string;
  timestamp: string;
  turnId: string;
  call?: TurnItem;
  result?: TurnItem;
  progress?: RuntimeEvent;
};
type TimelineEntry =
  | { kind: "item"; key: string; timestamp: string; item: TurnItem }
  | ToolTimelineEntry
  | { kind: "tool_group"; key: string; timestamp: string; tools: ToolTimelineEntry[] }
  | { kind: "event"; key: string; timestamp: string; event: RuntimeEvent };
type IconName = "rook" | "menu" | "plus" | "files" | "changes" | "models" | "settings" | "edit" | "fork" | "download" | "trash" | "arrow" | "arrowUp" | "image" | "stop" | "terminal";

const iconPaths: Record<IconName, string> = {
  rook: "M7 3h2v3h2V3h2v3h2V3h2v5l-2 2v8h2v3H5v-3h2v-8L5 8V3h2m2 7v8h6v-8H9Z",
  menu: "M4 7h16M4 12h16M4 17h16",
  plus: "M12 5v14M5 12h14",
  files: "M4 5.5A1.5 1.5 0 0 1 5.5 4H10l2 2h6.5A1.5 1.5 0 0 1 20 7.5v11a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Z",
  changes: "M7 7h10M7 12h7M7 17h4M17 14v6m-3-3h6",
  models: "M12 3a3 3 0 0 0-3 3v1H8a3 3 0 0 0-3 3v1a3 3 0 0 0 3 3h1v1a3 3 0 0 0 6 0v-1h1a3 3 0 0 0 3-3v-1a3 3 0 0 0-3-3h-1V6a3 3 0 0 0-3-3Zm0 4v10M9 10h6M9 14h6",
  settings: "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7Zm0-5v2m0 13v2m8.5-8.5h-2m-13 0h-2m14.51-6.01-1.42 1.42M6.9 17.1l-1.42 1.42m13.03 0-1.42-1.42M6.9 6.9 5.48 5.48",
  edit: "M4 20h4l11-11-4-4L4 16v4Zm9.5-13.5 4 4",
  fork: "M7 4v5a3 3 0 0 0 3 3h4a3 3 0 0 1 3 3v5M17 4v4M14 5l3 3 3-3",
  download: "M12 3v12m-4-4 4 4 4-4M5 20h14",
  trash: "M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7m3 4v5m4-5v5",
  arrow: "M5 12h14m-5-5 5 5-5 5",
  arrowUp: "M12 19V5m-5 5 5-5 5 5",
  image: "M4 5h16v14H4V5Zm3 10 3-3 2 2 2-2 3 3M8.5 9a1 1 0 1 0 0 .01",
  stop: "M7 7h10v10H7V7Z",
  terminal: "M4 5h16v14H4V5Zm3 4 2.5 2.5L7 14m5 0h5",
};

function Icon({ name, size = 18 }: { name: IconName; size?: number }): ReactElement {
  return <svg aria-hidden="true" className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={iconPaths[name]} /></svg>;
}

const phaseLabels: Record<string, string> = {
  understanding: "理解任务",
  exploring: "探索代码",
  planning: "规划方案",
  waiting_confirmation: "等待确认",
  executing: "执行修改",
  verifying: "运行验证",
  reviewing: "审查结果",
  completed: "任务完成",
  failed: "任务失败",
  interrupted: "任务中断",
};

const statusLabels: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  interrupted: "待恢复",
  failed: "失败",
  archived: "已归档",
};

function displayTime(value: string): string {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function textValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  return JSON.stringify(value, null, 2);
}

function readinessReason(value: unknown): string {
  const reason = textValue(value);
  const labels: Record<string, string> = {
    "the remote route has credentials but has not passed a basic Doctor probe": "已保存凭据，但当前路由尚未通过 Doctor 验证。",
    "the active route Doctor receipt is missing or stale": "当前路由的 Doctor 验证已缺失或过期。",
    "no provider route is configured": "尚未配置可用的模型路由。",
    "the active route credential is missing": "当前路由缺少 API Key。",
  };
  return labels[reason] || reason;
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("无法读取图片"));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}

function eventTitle(event: RuntimeEvent): string {
  const presentation = event.payload.presentation as Record<string, unknown> | undefined;
  if (presentation?.title) return textValue(presentation.title);
  if (event.type === "run.phase_changed") {
    return phaseLabels[textValue(event.payload.phase)] || textValue(event.payload.phase);
  }
  const names: Record<string, string> = {
    "input.admitted": "你",
    "task.profiled": "已理解",
    "tool.call_started": "正在使用工具",
    "tool.call_finished": "工具完成",
    "llm.retry": "模型重试",
    "context.compaction_committed": "上下文已整理",
    "context.compacted": "上下文已整理",
    "plan.ready": "执行计划",
    "permission.requested": "需要权限",
    "user_question.asked": "需要你的回答",
    "recovery.available": "发现可恢复任务",
    "run.outcome": "任务结果",
    "run.finished": "任务结果",
    "turn.finished": "本轮结束",
    "turn.failed": "本轮未完成",
    "turn.interrupted": "本轮已中断",
  };
  return names[event.type] || event.type.replaceAll(".", " · ");
}

function messageContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return textValue(value);
  return value.map((block) => {
    if (!block || typeof block !== "object") return textValue(block);
    const record = block as Record<string, unknown>;
    if (record.type === "text") return textValue(record.text);
    if (record.type === "image") return "[图片]";
    return textValue(record);
  }).filter(Boolean).join("\n");
}

function inlineMarkdown(text: string, keyPrefix: string): ReactNode[] {
  const tokens = text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/g);
  return tokens.filter(Boolean).map((token, index) => {
    const key = `${keyPrefix}-${index}`;
    if (token.startsWith("**") && token.endsWith("**")) return <strong key={key}>{token.slice(2, -2)}</strong>;
    if (token.startsWith("`") && token.endsWith("`")) return <code key={key}>{token.slice(1, -1)}</code>;
    const link = token.match(/^\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)$/);
    if (link) return <a key={key} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    return token;
  });
}

function MarkdownText({ content }: { content: string }): ReactElement {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let codeLines: string[] | null = null;
  let codeLanguage = "";
  for (const [index, line] of lines.entries()) {
    if (line.startsWith("```")) {
      if (codeLines === null) {
        codeLines = [];
        codeLanguage = line.slice(3).trim();
      } else {
        blocks.push(<pre className="markdown-code" key={`code-${index}`}><code data-language={codeLanguage}>{codeLines.join("\n")}</code></pre>);
        codeLines = null;
        codeLanguage = "";
      }
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    if (!line.trim()) {
      blocks.push(<div className="markdown-gap" key={`gap-${index}`} />);
      continue;
    }
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      blocks.push(<div className={`markdown-heading level-${level}`} key={`heading-${index}`}>{inlineMarkdown(heading[2], `heading-${index}`)}</div>);
      continue;
    }
    const bullet = line.match(/^\s*[-*]\s+(.+)$/);
    if (bullet) {
      blocks.push(<div className="markdown-list-row" key={`bullet-${index}`}><span>•</span><div>{inlineMarkdown(bullet[1], `bullet-${index}`)}</div></div>);
      continue;
    }
    const ordered = line.match(/^\s*(\d+)\.\s+(.+)$/);
    if (ordered) {
      blocks.push(<div className="markdown-list-row" key={`ordered-${index}`}><span>{ordered[1]}.</span><div>{inlineMarkdown(ordered[2], `ordered-${index}`)}</div></div>);
      continue;
    }
    const quote = line.match(/^>\s?(.+)$/);
    if (quote) {
      blocks.push(<blockquote key={`quote-${index}`}>{inlineMarkdown(quote[1], `quote-${index}`)}</blockquote>);
      continue;
    }
    blocks.push(<p key={`paragraph-${index}`}>{inlineMarkdown(line, `paragraph-${index}`)}</p>);
  }
  if (codeLines !== null) blocks.push(<pre className="markdown-code" key="code-open"><code data-language={codeLanguage}>{codeLines.join("\n")}</code></pre>);
  return <div className="markdown-content">{blocks}</div>;
}

function toolActionLabel(
  toolName: string,
  params: Record<string, unknown>,
  state: "running" | "succeeded" | "failed",
  semanticAction = "",
): string {
  const labels: Record<string, string> = {
    run_command: "运行命令",
    run_tests: "运行验证",
    read_file: "读取文件",
    browse_files: "浏览文件",
    search_code: "搜索代码",
    edit_code: "修改代码",
    git: "检查 Git",
    web: "访问网络",
    worker: "调用 Agent",
  };
  const semantic = labels[semanticAction];
  if (semantic) {
    if (semanticAction === "run_command") return state === "failed" ? "运行失败" : state === "running" ? "正在运行" : "已运行";
    if (semanticAction === "run_tests") return state === "failed" ? "验证失败" : state === "running" ? "正在验证" : "已验证";
    return state === "failed" ? `${semantic}失败` : state === "running" ? `正在${semantic}` : semantic;
  }
  if (["Bash", "Run", "bash", "run"].includes(toolName)) {
    return state === "failed" ? "命令执行失败" : state === "running" ? "正在运行命令" : "运行命令";
  }
  if (["File", "file"].includes(toolName)) {
    const action = textValue(params.action);
    const labels: Record<string, string> = {
      read: "读取文件",
      list: "浏览文件",
      write: "写入文件",
      patch: "修改文件",
      search: "搜索文件",
    };
    const label = labels[action] || "处理文件";
    return state === "failed" ? `${label}失败` : state === "running" ? `正在${label}` : label;
  }
  if (["Git", "git"].includes(toolName)) {
    return state === "failed" ? "Git 操作失败" : state === "running" ? "正在执行 Git 操作" : "Git 操作";
  }
  return state === "failed" ? `${toolName} 失败` : state === "running" ? `正在使用 ${toolName}` : `使用 ${toolName}`;
}

function inferToolAction(
  toolName: string,
  params: Record<string, unknown>,
  presentation: Record<string, unknown>,
): string {
  const declared = textValue(presentation.action);
  if (declared) return declared;
  const normalizedName = toolName.toLowerCase();
  if (["bash", "run", "background_run"].includes(normalizedName)) {
    const command = textValue(params.command).toLowerCase();
    return /(^|\s)(pytest|mypy|ruff|vitest|jest|npm test|pnpm test|cargo test|go test)(\s|$)/.test(command)
      ? "run_tests"
      : "run_command";
  }
  if (["git", "git_diff", "git_status"].includes(normalizedName)) return "git";
  if (["grep", "glob", "search", "memory_search"].includes(normalizedName)) return "search_code";
  if (["read_file", "read_image"].includes(normalizedName)) return "read_file";
  if (["list_dir", "list_files"].includes(normalizedName)) return "browse_files";
  if (["edit_file", "write_file", "apply_patch"].includes(normalizedName)) return "edit_code";
  if (normalizedName === "file") {
    const action = textValue(params.action).toLowerCase();
    if (["read", "image"].includes(action)) return "read_file";
    if (["list", "browse"].includes(action)) return "browse_files";
    if (["search", "grep", "glob"].includes(action)) return "search_code";
    if (["write", "patch", "edit", "delete", "move"].includes(action)) return "edit_code";
  }
  if (normalizedName.startsWith("web_")) return "web";
  if (normalizedName.startsWith("agent") || normalizedName.startsWith("worker")) return "worker";
  return "";
}

function toolElapsed(value: unknown, showFast = false): string {
  const elapsed = Number(value || 0);
  if (!Number.isFinite(elapsed) || elapsed <= 0) return "";
  if (elapsed < 1000 && !showFast) return "";
  return elapsed < 1000 ? `${Math.round(elapsed)} ms` : `${(elapsed / 1000).toFixed(elapsed < 10000 ? 1 : 0)} s`;
}

function showTimelineEvent(event: RuntimeEvent): boolean {
  if (["llm.chunk", "llm.usage", "runtime.event_appended", "turn.started", "turn.completed", "message.completed"].includes(event.type)) return false;
  if (event.type.startsWith("tool.call_")) return false;
  return [
    "task.profiled",
    "llm.retry",
    "context.compaction_committed",
    "context.compacted",
    "plan.ready",
    "permission.requested",
    "user_question.asked",
    "recovery.available",
    "run.outcome",
    "run.finished",
    "turn.finished",
    "turn.failed",
    "turn.interrupted",
    "steer.admitted",
  ].includes(event.type) || event.type.startsWith("worker.") || event.type.startsWith("subagent.");
}

function syntheticToolItem(event: RuntimeEvent, kind: "tool_call" | "tool_result"): TurnItem {
  const id = textValue(event.payload.tool_use_id || event.payload.tool_call_id) || `event-${event.seq}`;
  return {
    id: `${event.turn_id || textValue(event.payload.run_id)}:${kind}:${id}`,
    turn_id: event.turn_id || textValue(event.payload.run_id),
    kind,
    payload: event.payload,
    tool_call_id: id,
    created_at: event.ts,
  };
}

function groupToolEntries(entries: TimelineEntry[]): TimelineEntry[] {
  const grouped: TimelineEntry[] = [];
  for (const entry of entries) {
    if (entry.kind !== "tool") {
      grouped.push(entry);
      continue;
    }
    const previous = grouped[grouped.length - 1];
    if (previous?.kind === "tool_group" && previous.tools[0]?.turnId === entry.turnId) {
      previous.tools.push(entry);
      continue;
    }
    if (previous?.kind === "tool" && previous.turnId === entry.turnId) {
      grouped[grouped.length - 1] = {
        kind: "tool_group",
        key: `tool-group:${entry.turnId}:${previous.key}`,
        timestamp: previous.timestamp,
        tools: [previous, entry],
      };
      continue;
    }
    grouped.push(entry);
  }
  return grouped;
}

function eventDetail(event: RuntimeEvent): string {
  const payload = event.payload;
  const presentation = payload.presentation as Record<string, unknown> | undefined;
  for (const candidate of [
    presentation?.summary,
    presentation?.subject,
    payload.summary,
    payload.content,
    payload.message,
    payload.request,
    payload.reason,
    payload.plan,
    payload.question,
  ]) {
    const text = textValue(candidate).trim();
    if (text) return text;
  }
  if (event.type === "run.phase_changed") return "";
  return "";
}

function taskProfile(event: RuntimeEvent): Record<string, unknown> {
  const profile = event.payload.profile;
  return profile && typeof profile === "object" && !Array.isArray(profile)
    ? profile as Record<string, unknown>
    : {};
}

function taskProfileTitle(profile: Record<string, unknown>): string {
  const strategy = textValue(profile.strategy);
  if (strategy === "delegate") return "评估任务拆分与并行执行";
  const intent = textValue(profile.intent);
  const titles: Record<string, string> = {
    explain: "理解问题并整理回答",
    inspect: "定位相关实现与证据",
    fix: strategy === "plan_first" ? "定位根因并规划修复" : "定位问题并准备修复",
    refactor: "梳理重构范围与兼容边界",
    test: "确定验证范围与执行方式",
    multi_file_change: "拆解跨文件改动与依赖",
  };
  return titles[intent] || "分析任务与执行方式";
}

function isSimpleProductQuestion(value: string): boolean {
  return /(你好|您好|你是谁|什么模型|具体型号|你能做什么|你能干什么|你会什么|有什么功能|怎么使用|如何使用)/i.test(value);
}

export function modelContentFor(visibleContent: string, fileReferences: string[]): string {
  const command = visibleContent.startsWith("!") && visibleContent.length > 1
    ? visibleContent.slice(1).trim()
    : "";
  const base = command
    ? `The user explicitly requested this exact shell command. Run it through the normal permission and sandbox tool pipeline, then report its exit status and important output without changing the command: ${command}`
    : visibleContent;
  const selected = fileReferences
    .filter((path) => visibleContent.includes(`@${path}`))
    .slice(0, 8);
  if (!selected.length) return base;
  return `${base}\n\nBounded file references selected by the user: ${JSON.stringify(selected)}. Read only the ranges needed for this task; do not inject entire files by default.`;
}

export function eventBelongsToThread(activeThreadId: string, streamThreadId: string): boolean {
  return Boolean(activeThreadId) && activeThreadId === streamThreadId;
}

export function parentWorkspacePath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/") || ".";
}

export type ActiveFileMention = { query: string; start: number; end: number };

export function activeFileMention(value: string, caret: number): ActiveFileMention | null {
  const safeCaret = Math.max(0, Math.min(caret, value.length));
  const before = value.slice(0, safeCaret);
  const match = before.match(/(?:^|\s)@([^\s@]*)$/);
  if (!match) return null;
  const query = match[1] || "";
  return { query, start: safeCaret - query.length - 1, end: safeCaret };
}

export function appendRuntimeEvent(
  current: RuntimeEvent[],
  event: RuntimeEvent,
): RuntimeEvent[] {
  if (current.some((item) => item.seq === event.seq)) return current;
  return [...current, event];
}

function AppShell({ initialWorkspace }: { initialWorkspace: string }): ReactElement {
  const [workspace] = useState(initialWorkspace);
  const [threads, setThreads] = useState<ThreadRecord[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [turns, setTurns] = useState<TurnRecord[]>([]);
  const [items, setItems] = useState<TurnItem[]>([]);
  const [events, setEvents] = useState<RuntimeEvent[]>([]);
  const [composer, setComposer] = useState("");
  const [mode, setMode] = useState<RunMode>("act");
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [phase, setPhase] = useState("idle");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [sending, setSending] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [attachments, setAttachments] = useState<ImageAttachment[]>([]);
  const [fileReferences, setFileReferences] = useState<string[]>([]);
  const [inspectorFile, setInspectorFile] = useState("");
  const [queueMode, setQueueMode] = useState(false);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [activeModel, setActiveModel] = useState("未配置模型");
  const [composerCaret, setComposerCaret] = useState(0);
  const [fileSuggestions, setFileSuggestions] = useState<WorkspaceEntry[]>([]);
  const [fileSuggestionIndex, setFileSuggestionIndex] = useState(0);
  const cursors = useRef<Record<string, number>>({});
  const eventCache = useRef<Record<string, RuntimeEvent[]>>({});
  const initializedSelection = useRef(false);
  const composerDrafts = useRef<Record<string, string>>({});
  const attachmentDrafts = useRef<Record<string, ImageAttachment[]>>({});
  const fileReferenceDrafts = useRef<Record<string, string[]>>({});
  const selectedIdRef = useRef("");
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null);
  const timelineRef = useRef<HTMLElement | null>(null);
  const previousTimelineSize = useRef(0);
  selectedIdRef.current = selectedId;
  const selectedThread = threads.find((thread) => thread.id === selectedId);
  const activeTurn = [...turns].reverse().find((turn) =>
    ["running", "waiting", "waiting_permission", "waiting_input"].includes(turn.status),
  );
  const fileMention = useMemo(
    () => activeFileMention(composer, composerCaret),
    [composer, composerCaret],
  );

  const refreshActiveModel = useCallback(async () => {
    const catalog = await request<ProviderCatalog>("/v1/providers");
    const active = catalog.routes.find((route) => route.id === catalog.active_route_id);
    setActiveModel(textValue(active?.model) || "未配置模型");
  }, []);

  const refreshThreads = useCallback(async () => {
    const result = await request<ThreadRecord[]>("/v1/threads");
    result.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    setThreads(result);
    if (!initializedSelection.current) {
      initializedSelection.current = true;
      setSelectedId(result[0]?.id || "");
    }
  }, []);

  useEffect(() => {
    void refreshThreads()
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [refreshThreads]);

  useEffect(() => {
    void refreshActiveModel().catch(() => setActiveModel("模型状态未知"));
  }, [drawer, refreshActiveModel]);

  useEffect(() => {
    if (!fileMention || fileReferences.length >= 8) {
      setFileSuggestions([]);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      request<{ entries: WorkspaceEntry[] }>(
        `/v1/workspace/files?query=${encodeURIComponent(fileMention.query)}&limit=12`,
        { signal: controller.signal },
      ).then((result) => {
        setFileSuggestions(result.entries.filter((entry) => entry.kind === "file").slice(0, 8));
        setFileSuggestionIndex(0);
      }).catch(() => {
        if (!controller.signal.aborted) setFileSuggestions([]);
      });
    }, 160);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [fileMention?.query, fileReferences.length]);

  const loadThread = useCallback(async (threadId: string, signal?: AbortSignal) => {
    const [loadedTurns, loadedQueue] = await Promise.all([
      request<TurnRecord[]>(
        `/v1/threads/${encodeURIComponent(threadId)}/turns`,
        { signal },
      ),
      request<QueuedMessage[]>(
        `/v1/threads/${encodeURIComponent(threadId)}/queue`,
        { signal },
      ),
    ]);
    const loadedItems = await Promise.all(
      loadedTurns.map((turn) =>
        request<TurnItem[]>(`/v1/turns/${encodeURIComponent(turn.id)}/items`, { signal }),
      ),
    );
    if (signal?.aborted || selectedIdRef.current !== threadId) return;
    setTurns(loadedTurns);
    setItems(loadedItems.flat());
    setQueuedMessages(loadedQueue);
  }, []);

  const loadQueue = useCallback(async (threadId: string) => {
    const loaded = await request<QueuedMessage[]>(
      `/v1/threads/${encodeURIComponent(threadId)}/queue`,
    );
    if (selectedIdRef.current === threadId) setQueuedMessages(loaded);
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setTurns([]);
      setItems([]);
      setEvents([]);
      setQueuedMessages([]);
      setPhase("idle");
      return;
    }
    const controller = new AbortController();
    setError("");
    setTurns([]);
    setItems([]);
    const cachedEvents = eventCache.current[selectedId] || [];
    setEvents(cachedEvents);
    setQueuedMessages([]);
    const cachedPhase = [...cachedEvents].reverse().find(
      (event) => event.type === "run.phase_changed",
    );
    setPhase(textValue(cachedPhase?.payload.phase) || "idle");
    void loadThread(selectedId, controller.signal)
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
    });
    return () => controller.abort();
  }, [loadThread, selectedId]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    let stopped = false;
    const consume = async () => {
      while (!stopped) {
        try {
          const cursor = await streamEvents(
            selectedId,
            cursors.current[selectedId] || 0,
            controller.signal,
            (event) => {
              if (!eventBelongsToThread(selectedIdRef.current, selectedId)) return;
              cursors.current[selectedId] = Math.max(cursors.current[selectedId] || 0, event.seq);
              setEvents((current) => {
                const next = appendRuntimeEvent(current, event);
                eventCache.current[selectedId] = next;
                return next;
              });
              if (event.type === "run.phase_changed") {
                setPhase(textValue(event.payload.phase) || "working");
              }
              if (event.type.startsWith("queue.message_")) {
                void loadQueue(selectedId);
              }
              if (["turn.finished", "turn.completed", "turn.failed", "turn.interrupted", "run.outcome", "run.finished"].includes(event.type)) {
                void refreshThreads();
                void loadThread(selectedId);
                if (event.turn_id && event.type.startsWith("turn.")) {
                  setTurns((current) => current.map((turn) => turn.id === event.turn_id ? { ...turn, status: textValue(event.payload.status || event.payload.outcome || "completed") } : turn));
                }
                void browserBridge.notify("CodeRook", eventTitle(event));
              }
            },
          );
          cursors.current[selectedId] = cursor;
        } catch (reason) {
          if (controller.signal.aborted) return;
          setNotice(`事件流正在重连：${reason instanceof Error ? reason.message : String(reason)}`);
          await new Promise((resolve) => window.setTimeout(resolve, 900));
        }
      }
    };
    void consume();
    return () => {
      stopped = true;
      controller.abort();
    };
  }, [loadQueue, loadThread, refreshThreads, selectedId]);

  const createThread = useCallback(async (): Promise<string> => {
    const created = await request<ThreadRecord>("/v1/threads", {
      method: "POST",
      body: JSON.stringify({ title: "", mode: "chat" }),
    });
    setThreads((current) => [created, ...current]);
    setSelectedId(created.id);
    previousTimelineSize.current = 0;
    return created.id;
  }, []);

  const selectThread = useCallback((threadId: string) => {
    const currentKey = selectedId || "__new__";
    const nextKey = threadId || "__new__";
    composerDrafts.current[currentKey] = composer;
    attachmentDrafts.current[currentKey] = attachments;
    fileReferenceDrafts.current[currentKey] = fileReferences;
    setSelectedId(threadId);
    setComposer(composerDrafts.current[nextKey] || "");
    setAttachments(attachmentDrafts.current[nextKey] || []);
    setFileReferences(fileReferenceDrafts.current[nextKey] || []);
    setQueuedMessages([]);
    setTurns([]);
    setItems([]);
    const cachedEvents = eventCache.current[threadId] || [];
    setEvents(cachedEvents);
    const cachedPhase = [...cachedEvents].reverse().find(
      (event) => event.type === "run.phase_changed",
    );
    setPhase(textValue(cachedPhase?.payload.phase) || "idle");
    previousTimelineSize.current = 0;
    setNotice("");
    setError("");
    setMobileSidebarOpen(false);
  }, [attachments, composer, fileReferences, selectedId]);

  const beginDraft = useCallback(() => {
    selectThread("");
    setAttachments([]);
    setDrawer(null);
  }, [selectThread]);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const content = composer.trim();
    if (!content || sending) return;
    setSending(true);
    setError("");
    try {
      const submitted = modelContentFor(content, fileReferences);
      if (activeTurn) {
        if (queueMode) {
          await request<QueuedMessage>(
            `/v1/threads/${encodeURIComponent(selectedId)}/queue`,
            {
              method: "POST",
              body: JSON.stringify({
                content: submitted,
                display_content: content,
                mode,
                attachments: attachments.map(({ name: _name, ...attachment }) => attachment),
              }),
            },
          );
          await loadQueue(selectedId);
          setComposer("");
          composerDrafts.current[selectedId || "__new__"] = "";
          setAttachments([]);
          setFileReferences([]);
          setNotice("消息已加入队列，将在当前任务结束后发送");
          return;
        }
        if (attachments.length > 0) {
          setError("运行中的纠偏暂不支持图片。请切换到“排队”发送，图片会随下一轮任务提交。");
          return;
        }
        await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/steer`, {
          method: "POST",
          body: JSON.stringify({ content: submitted }),
        });
        setNotice("纠偏消息已送达当前任务");
      } else {
        const provider = await request<ProviderCatalog>("/v1/providers");
        if (!provider.readiness.local_ready) {
          setDrawer("models");
          setNotice("先完成模型配置，当前输入已为你保留");
          return;
        }
        const threadId = selectedId || (await createThread());
        const started = await request<TurnRecord>(
          `/v1/threads/${encodeURIComponent(threadId)}/turns`,
          {
            method: "POST",
            body: JSON.stringify({
              content: submitted,
              display_content: content,
              mode,
              attachments: attachments.map(({ name: _name, ...attachment }) => attachment),
            }),
          },
        );
        setTurns((current) => [...current, started]);
        void loadThread(threadId);
      }
      setComposer("");
      composerDrafts.current[selectedId || "__new__"] = "";
      setAttachments([]);
      setFileReferences([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSending(false);
    }
  };

  const chooseFileSuggestion = (entry: WorkspaceEntry) => {
    if (!fileMention || entry.kind !== "file") return;
    const insertion = `@${entry.path} `;
    const next = `${composer.slice(0, fileMention.start)}${insertion}${composer.slice(fileMention.end)}`;
    const nextCaret = fileMention.start + insertion.length;
    setComposer(next);
    setComposerCaret(nextCaret);
    setFileReferences((current) => current.includes(entry.path) ? current : [...current, entry.path]);
    setFileSuggestions([]);
    window.requestAnimationFrame(() => {
      composerInputRef.current?.focus();
      composerInputRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  };

  const queueAction = async (message: QueuedMessage, action: "remove" | "retry") => {
    if (!selectedId) return;
    try {
      await request(
        `/v1/threads/${encodeURIComponent(selectedId)}/queue/${encodeURIComponent(message.id)}${action === "retry" ? "/retry" : ""}`,
        { method: action === "retry" ? "POST" : "DELETE", body: "{}" },
      );
      await loadQueue(selectedId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const cancel = async () => {
    if (!activeTurn) return;
    try {
      await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/interrupt`, {
        method: "POST",
        body: "{}",
      });
      setNotice("已请求停止当前任务");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const attachImages = async (files: FileList | null) => {
    if (!files) return;
    for (const file of Array.from(files).slice(0, 4 - attachments.length)) {
      if (!file.type.startsWith("image/") || file.size > 2 * 1024 * 1024) {
        setError(`${file.name} 不是受支持的 2 MiB 以内图片`);
        continue;
      }
      try {
        const uploaded = await request<Omit<ImageAttachment, "name">>(
          "/v1/artifacts/images",
          { method: "POST", body: JSON.stringify({ data_base64: await fileBase64(file) }) },
        );
        setAttachments((current) => [...current, { ...uploaded, name: file.name }]);
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    }
  };

  const sessionAction = async (action: "rename" | "fork" | "delete" | "export") => {
    if (!selectedId) return;
    try {
      if (action === "rename") {
        const title = prompt("输入新的会话名称", selectedThread?.title || "");
        if (!title?.trim()) return;
        const updated = await request<ThreadRecord>(`/v1/threads/${selectedId}`, {
          method: "PATCH",
          body: JSON.stringify({ title: title.trim() }),
        });
        setThreads((current) => current.map((thread) => thread.id === updated.id ? updated : thread));
        return;
      }
      if (action === "fork") {
        const forked = await request<ThreadRecord>(`/v1/threads/${selectedId}/fork`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setThreads((current) => [forked, ...current]);
        selectThread(forked.id);
        return;
      }
      if (action === "export") {
        const exported = await request<{ filename: string; content: string }>(
          `/v1/threads/${selectedId}/export?format=markdown`,
        );
        const blob = new Blob([exported.content], { type: "text/markdown" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = exported.filename;
        link.click();
        URL.revokeObjectURL(link.href);
        return;
      }
      if (!confirm("删除这个会话？此操作不能撤销。")) return;
      await request(`/v1/threads/${selectedId}`, {
        method: "DELETE",
        body: JSON.stringify({ confirmed: true }),
      });
      setThreads((current) => current.filter((thread) => thread.id !== selectedId));
      selectThread(threads.find((thread) => thread.id !== selectedId)?.id || "");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const timelineEntries = useMemo<TimelineEntry[]>(() => {
    const toolId = (item: TurnItem) => textValue(
      item.tool_call_id || item.payload.tool_use_id || item.payload.tool_call_id || item.payload.id,
    );
    const calls = new Map<string, TurnItem>();
    const results = new Map<string, TurnItem>();
    const progress = new Map<string, RuntimeEvent>();
    for (const item of items) {
      const id = toolId(item);
      if (!id) continue;
      if (item.kind === "tool_call") calls.set(id, item);
      if (item.kind === "tool_result") results.set(id, item);
    }
    for (const event of events) {
      const id = textValue(event.payload.tool_use_id || event.payload.tool_call_id);
      if (!id) continue;
      if (event.type === "tool.call_started" && !calls.has(id)) {
        calls.set(id, syntheticToolItem(event, "tool_call"));
      }
      if (["tool.call_finished", "tool.call_failed"].includes(event.type) && !results.has(id)) {
        if (event.type === "tool.call_failed" && event.payload.terminal === false) continue;
        results.set(id, syntheticToolItem(event, "tool_result"));
      }
      if (event.type === "tool.call_progress") progress.set(id, event);
    }
    const resolvedPermissionIds = new Set(
      events
        .filter((event) => ["permission.granted", "permission.denied", "permission.resolved"].includes(event.type))
        .map((event) => textValue(event.payload.tool_use_id || event.payload.permission_id))
        .filter(Boolean),
    );
    const resolvedPlanRuns = new Set(
      events
        .filter((event) => event.type === "plan.resolved")
        .map((event) => textValue(event.payload.run_id || event.turn_id))
        .filter(Boolean),
    );
    const resolvedRecoveryRuns = new Set(
      events
        .filter((event) => event.type === "recovery.resolved")
        .map((event) => textValue(event.payload.run_id || event.turn_id))
        .filter(Boolean),
    );
    const assistantTurns = new Set(
      items
        .filter((item) => item.kind === "message" && textValue(item.payload.role) === "assistant" && messageContent(item.payload.content).trim())
        .map((item) => item.turn_id)
        .filter(Boolean),
    );
    const userTextByTurn = new Map(
      items
        .filter((item) => item.kind === "message" && textValue(item.payload.role) === "user")
        .map((item) => [item.turn_id, messageContent(item.payload.content)]),
    );
    const resultPriority: Record<string, number> = { "turn.finished": 1, "run.finished": 2, "run.outcome": 3 };
    const preferredResultSeq = new Map<string, number>();
    for (const event of events) {
      if (!(event.type in resultPriority)) continue;
      const runId = textValue(event.payload.run_id || event.turn_id);
      if (!runId) continue;
      const previousSeq = preferredResultSeq.get(runId);
      const previous = previousSeq === undefined ? undefined : events.find((candidate) => candidate.seq === previousSeq);
      if (!previous || resultPriority[event.type] >= resultPriority[previous.type]) preferredResultSeq.set(runId, event.seq);
    }
    const entries: TimelineEntry[] = [];
    for (const item of items) {
      if (["tool_call", "tool_result"].includes(item.kind)) continue;
      entries.push({ kind: "item", key: `item:${item.id}`, timestamp: item.created_at, item });
    }
    for (const [id, call] of calls) {
      const result = results.get(id);
      entries.push({
        kind: "tool",
        key: `tool:${id}`,
        timestamp: call.created_at,
        turnId: call.turn_id,
        call,
        result,
        progress: progress.get(id),
      });
    }
    for (const [id, result] of results) {
      if (calls.has(id)) continue;
      entries.push({
        kind: "tool",
        key: `tool:${id}`,
        timestamp: result.created_at,
        turnId: result.turn_id,
        result,
        progress: progress.get(id),
      });
    }
    for (const event of events) {
      const eventRunId = textValue(event.payload.run_id || event.turn_id);
      if (event.type === "task.profiled" && (
        textValue(taskProfile(event).intent) === "answer"
        || isSimpleProductQuestion(userTextByTurn.get(eventRunId) || "")
      )) continue;
      if (event.type === "permission.requested" && resolvedPermissionIds.has(textValue(event.payload.tool_use_id || event.payload.permission_id))) continue;
      if (event.type === "plan.ready" && resolvedPlanRuns.has(eventRunId)) continue;
      if (event.type === "recovery.available" && resolvedRecoveryRuns.has(eventRunId)) continue;
      if (event.type === "user_question.asked" && event.turn_id && event.turn_id !== activeTurn?.id) continue;
      if (event.type in resultPriority && assistantTurns.has(eventRunId)) continue;
      if (event.type in resultPriority && eventRunId && preferredResultSeq.get(eventRunId) !== event.seq) continue;
      if (!showTimelineEvent(event)) continue;
      entries.push({
        kind: "event",
        key: `event:${event.seq}`,
        timestamp: event.ts,
        event,
      });
    }
    const sorted = entries.sort((left, right) => {
      const timeOrder = left.timestamp.localeCompare(right.timestamp);
      if (timeOrder !== 0) return timeOrder;
      return left.key.localeCompare(right.key);
    });
    return groupToolEntries(sorted);
  }, [activeTurn?.id, events, items]);

  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline || timelineEntries.length === 0) return;
    const firstLoad = previousTimelineSize.current === 0;
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 140;
    if (firstLoad || nearBottom) {
      window.requestAnimationFrame(() => {
        timeline.scrollTop = timeline.scrollHeight;
      });
    }
    previousTimelineSize.current = timelineEntries.length;
  }, [timelineEntries]);
  const tokenUsage = turns.reduce(
    (total, turn) => total + Number(turn.usage.input_tokens || 0) + Number(turn.usage.output_tokens || 0),
    0,
  );
  const workspaceName = workspace.split(/[\\/]/).filter(Boolean).pop() || "workspace";

  return (
    <div className={`app-shell ${drawer ? "inspector-open" : ""}`}>
      <aside className={`sidebar ${mobileSidebarOpen ? "mobile-open" : ""}`}>
        <div className="workspace-head"><span className="app-mark"><Icon name="rook" size={18} /></span><div><b>CodeRook</b><small>{workspaceName}</small></div><button className="mobile-sidebar-close" aria-label="关闭导航" onClick={() => setMobileSidebarOpen(false)}>×</button></div>
        <button className="new-thread" onClick={beginDraft}><Icon name="plus" size={15} /><span>新建任务</span></button>
        <nav className="workspace-nav" aria-label="工作区工具">
          <button className={drawer === "files" ? "active" : ""} onClick={() => { setInspectorFile(""); setDrawer(drawer === "files" ? null : "files"); setMobileSidebarOpen(false); }}><Icon name="files" size={15} /><span>文件</span></button>
          <button className={drawer === "changes" ? "active" : ""} onClick={() => { setDrawer(drawer === "changes" ? null : "changes"); setMobileSidebarOpen(false); }}><Icon name="changes" size={15} /><span>变更</span></button>
          <button className={drawer === "models" ? "active" : ""} onClick={() => { setDrawer(drawer === "models" ? null : "models"); setMobileSidebarOpen(false); }}><Icon name="models" size={15} /><span>模型</span></button>
          <button className={drawer === "advanced" ? "active" : ""} onClick={() => { setDrawer(drawer === "advanced" ? null : "advanced"); setMobileSidebarOpen(false); }}><Icon name="settings" size={15} /><span>设置</span></button>
        </nav>
        <div className="section-title"><span>最近任务</span><span>{threads.length}</span></div>
        <nav className="sessions">
          {threads.map((thread) => (
            <button
              className={`session ${thread.id === selectedId ? "selected" : ""}`}
              key={thread.id}
              onClick={() => selectThread(thread.id)}
            >
              <span>{thread.title || "未命名任务"}</span>
              <small><i className={`session-status ${thread.status}`} />{statusLabels[thread.status] || thread.status} <em>· {displayTime(thread.updated_at)}</em></small>
            </button>
          ))}
          {!threads.length && <p className="empty">还没有会话。直接在右侧描述任务即可。</p>}
        </nav>
        <div className="sidebar-foot"><span className="connection-dot" />本机 Core 已连接<small>0.2 beta</small></div>
      </aside>

      <main className="main">
        <header className="topbar">
          <button className="mobile-sidebar-toggle" aria-label="打开导航" onClick={() => setMobileSidebarOpen(true)}><Icon name="menu" size={18} /></button>
          <div className="task-identity"><small title={workspace}>{workspaceName}</small><strong>{selectedThread?.title || "新任务"}</strong></div>
          <button className="active-model" type="button" title="切换模型" onClick={() => setDrawer("models")}><Icon name="models" size={13} />{activeModel}</button>
          <div className="run-state"><span className={activeTurn ? "pulse" : "dot"} />{activeTurn ? phaseLabels[phase] || "正在工作" : "就绪"}</div>
          <div className="session-menu">
            <button title="重命名" aria-label="重命名" disabled={!selectedId} onClick={() => void sessionAction("rename")}><Icon name="edit" size={16} /></button>
            <button title="Fork 会话" aria-label="Fork 会话" disabled={!selectedId} onClick={() => void sessionAction("fork")}><Icon name="fork" size={16} /></button>
            <button title="导出" aria-label="导出" disabled={!selectedId} onClick={() => void sessionAction("export")}><Icon name="download" size={16} /></button>
            <button className="danger-action" title="删除" aria-label="删除" disabled={!selectedId} onClick={() => void sessionAction("delete")}><Icon name="trash" size={16} /></button>
          </div>
        </header>

        <section className="timeline" ref={timelineRef}>
          {!timelineEntries.length && (
            <div className="welcome-card">
              <span className="welcome-kicker">CODEROOK · LOCAL AGENT</span>
              <h1>今天想完成什么？</h1>
              <p>描述一个目标。CodeRook 会理解代码、执行修改、运行验证，并留下可审查和可恢复的结果。</p>
              <div className="suggestions">
                <button onClick={() => setComposer("解释这个仓库的核心架构和数据流")}><span><b>理解代码库</b><small>梳理架构、模块与关键数据流</small></span><Icon name="arrow" size={16} /></button>
                <button onClick={() => setComposer("检查当前改动，找出最可能的缺陷")}><span><b>审查当前改动</b><small>检查风险、缺陷与验证缺口</small></span><Icon name="arrow" size={16} /></button>
                <button onClick={() => setComposer("运行最相关的测试并修复失败")}><span><b>修复测试失败</b><small>定位问题、修改代码并重新验证</small></span><Icon name="arrow" size={16} /></button>
              </div>
            </div>
          )}
          {timelineEntries.map((entry) => entry.kind === "item" ? (
            <TurnItemCard key={entry.key} item={entry.item} />
          ) : entry.kind === "tool" ? (
            <TurnToolCard
              key={entry.key}
              call={entry.call}
              result={entry.result}
              progress={entry.progress}
              onOpenLocation={(path) => { setInspectorFile(path); setDrawer("files"); }}
              onRetry={(prompt) => { setComposer(prompt); setNotice("重试建议已放入输入框，可修改后发送"); }}
            />
          ) : entry.kind === "tool_group" ? (
            <ToolActivityGroup
              key={entry.key}
              tools={entry.tools}
              onOpenLocation={(path) => { setInspectorFile(path); setDrawer("files"); }}
              onRetry={(prompt) => { setComposer(prompt); setNotice("重试建议已放入输入框，可修改后发送"); }}
            />
          ) : (
            <EventCard
              key={entry.key}
              event={entry.event}
              threadId={selectedId}
              onError={setError}
              onNotice={setNotice}
              onOpenChanges={() => setDrawer("changes")}
            />
          ))}
          {notice && <div className="notice">{notice}<button onClick={() => setNotice("")}>×</button></div>}
          {error && <div className="error-card"><b>需要处理</b><p>{error}</p><button onClick={() => setError("")}>关闭</button></div>}
        </section>

        <form className="composer" onSubmit={(event) => void send(event)}>
          {queuedMessages.length > 0 && <div className="message-queue" aria-label="待发送消息">
            {queuedMessages.map((message, index) => <div className={`queued-message ${message.status}`} key={message.id}>
              <span>{message.status === "dispatching" ? "正在发送" : message.status === "blocked" ? "需要处理" : `排队 ${index + 1}`}</span>
              <p title={message.display_content}>{message.display_content}</p>
              {message.status === "blocked" && <button type="button" onClick={() => void queueAction(message, "retry")}>重试</button>}
              <button type="button" aria-label="移除排队消息" onClick={() => void queueAction(message, "remove")}>×</button>
              {message.error && <small>{message.error}</small>}
            </div>)}
          </div>}
          {(attachments.length > 0 || fileReferences.length > 0) && <div className="attachment-row">
            {fileReferences.map((path) => <span key={`file:${path}`}>@{path}<button type="button" onClick={() => { setFileReferences((current) => current.filter((item) => item !== path)); setComposer((current) => current.replaceAll(`@${path}`, "").replace(/\s{2,}/g, " ")); }}>×</button></span>)}
            {attachments.map((attachment) => <span key={attachment.sha256}>{attachment.name}<button type="button" onClick={() => setAttachments((current) => current.filter((item) => item.sha256 !== attachment.sha256))}>×</button></span>)}
          </div>}
          {fileSuggestions.length > 0 && <div className="file-mention-menu" role="listbox" aria-label="文件建议">
            {fileSuggestions.map((entry, index) => <button
              type="button"
              role="option"
              aria-selected={index === fileSuggestionIndex}
              className={index === fileSuggestionIndex ? "selected" : ""}
              key={entry.path}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => chooseFileSuggestion(entry)}
            ><b>{entry.name}</b><small>{entry.path}</small></button>)}
          </div>}
          <textarea
            ref={composerInputRef}
            value={composer}
            onChange={(event) => {
              const value = event.target.value;
              setComposer(value);
              setComposerCaret(event.target.selectionStart);
              setFileReferences((current) => current.filter((path) => value.includes(`@${path}`)));
            }}
            onClick={(event) => setComposerCaret(event.currentTarget.selectionStart)}
            onKeyUp={(event) => setComposerCaret(event.currentTarget.selectionStart)}
            onKeyDown={(event) => {
              if (fileSuggestions.length > 0 && event.key === "ArrowDown") {
                event.preventDefault();
                setFileSuggestionIndex((current) => (current + 1) % fileSuggestions.length);
                return;
              }
              if (fileSuggestions.length > 0 && event.key === "ArrowUp") {
                event.preventDefault();
                setFileSuggestionIndex((current) => (current - 1 + fileSuggestions.length) % fileSuggestions.length);
                return;
              }
              if (fileSuggestions.length > 0 && ["Enter", "Tab"].includes(event.key)) {
                event.preventDefault();
                chooseFileSuggestion(fileSuggestions[fileSuggestionIndex]);
                return;
              }
              if (event.key === "Escape" && fileSuggestions.length > 0) {
                event.preventDefault();
                setFileSuggestions([]);
                return;
              }
              if (event.nativeEvent.isComposing) return;
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder={activeTurn ? "输入纠偏消息…" : "向 CodeRook 提问或描述任务"}
            rows={1}
          />
          <div className="composer-bar">
            <div className="composer-tools">
              <button type="button" className="context-button" onClick={() => setDrawer("files")} title="添加文件上下文"><Icon name="plus" size={16} /></button>
              <label className="mode-select"><select aria-label="运行模式" value={mode} onChange={(event) => setMode(event.target.value as RunMode)}><option value="act">执行</option><option value="plan">规划</option><option value="review">审查</option></select></label>
              <label className="attach-button" title="添加图片"><Icon name="image" size={15} /><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={(event) => { void attachImages(event.target.files); event.target.value = ""; }} /></label>
              {activeTurn && <button type="button" className={`queue-toggle ${queueMode ? "active" : ""}`} onClick={() => setQueueMode((current) => !current)}>{queueMode ? `排队 ${queuedMessages.length}` : "纠偏"}</button>}
            </div>
            <div className="composer-meta">
              <span>上下文 {tokenUsage ? `${Math.round(tokenUsage / 1000)}k` : "—"}</span>
              {activeTurn && <button type="button" className="stop" onClick={() => void cancel()}><Icon name="stop" size={13} />停止</button>}
              <button className="send" aria-label={activeTurn ? "发送纠偏" : "发送任务"} title={activeTurn ? "发送纠偏" : "发送任务"} disabled={!composer.trim() || sending}><Icon name="arrowUp" size={16} /></button>
            </div>
          </div>
        </form>
      </main>

      {mobileSidebarOpen && <button className="sidebar-scrim" aria-label="关闭导航" onClick={() => setMobileSidebarOpen(false)} />}

      {drawer && (
        <DrawerPanel
          drawer={drawer}
          threadId={selectedId}
          workspace={workspace}
          initialFile={inspectorFile}
          onClose={() => setDrawer(null)}
          onReference={(path) => {
            setComposer((current) => `${current}${current ? " " : ""}@${path} `);
            setFileReferences((current) => current.includes(path) ? current : [...current, path]);
            setDrawer(null);
          }}
          onError={setError}
        />
      )}
    </div>
  );
}

function TurnItemCard({ item }: { item: TurnItem }): ReactElement {
  const payload = item.payload;
  if (item.kind === "message") {
    const role = textValue(payload.role) === "user" ? "user" : "assistant";
    return (
      <article className={`message-card ${role}`}>
        <div className="message-meta"><b>{role === "user" ? "你" : "CodeRook"}</b><time>{displayTime(item.created_at)}</time></div>
        <div className="message-content">
          {role === "assistant"
            ? <MarkdownText content={messageContent(payload.content)} />
            : messageContent(payload.content)}
        </div>
      </article>
    );
  }
  return <></>;
}

type ToolCardInfo = {
  failed: boolean;
  running: boolean;
  params: Record<string, unknown>;
  presentation: Record<string, unknown>;
  title: string;
  subject: string;
  output: string;
  elapsedMs: number;
  locations: string[];
  retryPrompt: string;
  semanticAction: string;
};

function toolCardInfo(call?: TurnItem, result?: TurnItem, progress?: RuntimeEvent): ToolCardInfo {
  const callPayload = call?.payload || {};
  const resultPayload = result?.payload || {};
  const rawPresentation = resultPayload.presentation || progress?.payload.presentation || callPayload.presentation;
  const presentation = rawPresentation && typeof rawPresentation === "object" ? rawPresentation as Record<string, unknown> : {};
  const toolName = textValue(resultPayload.tool_name || callPayload.tool_name || presentation.title || "工具");
  const failed = Boolean(resultPayload.is_error || resultPayload.error_message || resultPayload.error_class) || ["error", "failed"].includes(textValue(resultPayload.status));
  const running = !result;
  const rawParams = callPayload.params;
  const params = rawParams && typeof rawParams === "object" ? rawParams as Record<string, unknown> : {};
  const semanticAction = inferToolAction(toolName, params, presentation);
  const subject = textValue(presentation.subject || presentation.command || params.command || params.path || params.query);
  const output = textValue(presentation.summary || resultPayload.error_message || resultPayload.output || resultPayload.result || progress?.payload.output_tail);
  const state = failed ? "failed" : running ? "running" : "succeeded";
  const title = toolActionLabel(toolName, params, state, semanticAction);
  const elapsedMs = Number(resultPayload.elapsed_ms || progress?.payload.elapsed_ms || presentation.elapsed_ms || 0);
  const rawLocations = presentation.locations;
  const locations = Array.isArray(rawLocations)
    ? rawLocations.map(textValue).filter(Boolean)
    : textValue(params.path) ? [textValue(params.path)] : [];
  const target = locations[0] || subject;
  const retryPrompt = `请先诊断失败原因，再重试“${toolActionLabel(toolName, params, "succeeded", semanticAction)}”${target ? `（${target}）` : ""}。不要原样重复已经失败的调用。`;
  return { failed, running, params, presentation, title, subject, output, elapsedMs, locations, retryPrompt, semanticAction };
}

function TurnToolCard({
  call,
  result,
  progress,
  nested = false,
  onOpenLocation,
  onRetry,
}: {
  call?: TurnItem;
  result?: TurnItem;
  progress?: RuntimeEvent;
  nested?: boolean;
  onOpenLocation(path: string): void;
  onRetry(prompt: string): void;
}): ReactElement {
  const info = toolCardInfo(call, result, progress);
  const { failed, running, params, title, subject, output, elapsedMs, locations, retryPrompt, semanticAction } = info;
  const elapsed = toolElapsed(elapsedMs);
  const openableLocation = ["read_file", "edit_code"].includes(semanticAction) ? locations[0] : "";
  const hasDetails = Object.keys(params).length > 0 || Boolean(output);
  const failureExcerpt = output.split(/\r?\n/).find((line) => line.trim())?.trim().slice(0, 180) || "操作未完成";
  const summary = (
    <>
      {["run_command", "run_tests"].includes(semanticAction)
        ? <span className={`tool-kind-icon ${failed ? "failed" : ""}`}><Icon name="terminal" size={13} /></span>
        : <span className="tool-status">{failed ? "×" : running ? "◌" : "✓"}</span>}
      <b>{title}</b>
      {locations[0]
        ? openableLocation
          ? <button className="tool-location" type="button" title={`打开 ${openableLocation}`} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onOpenLocation(openableLocation); }}>{openableLocation}</button>
          : <code>{locations[0]}</code>
        : subject && <code>{subject}</code>}
      {elapsed && <small>{elapsed}</small>}
      {hasDetails && <span className="tool-chevron" />}
    </>
  );
  return (
    <article className={`tool-item ${nested ? "nested" : ""} ${failed ? "failed" : ""} ${running ? "running" : ""}`}>
      {hasDetails ? (
        <details>
          <summary className="tool-item-head">{summary}</summary>
          <div className="tool-detail">
            {Object.keys(params).length > 0 && <><small>输入</small><pre>{textValue(params)}</pre></>}
            {output && <><small>{failed ? "错误" : "输出"}</small><pre>{output}</pre></>}
            {failed && <div className="tool-recovery-actions"><span title={output}>{failureExcerpt}</span><div><button type="button" onClick={() => onRetry(retryPrompt)}>修改后重试</button><button type="button" onClick={() => void browserBridge.copyText(output || textValue(params))}>复制错误</button></div></div>}
          </div>
        </details>
      ) : <div className="tool-item-head">{summary}</div>}
      {failed && !hasDetails && <div className="tool-recovery-actions always-visible"><span>{failureExcerpt}</span><div><button type="button" onClick={() => onRetry(retryPrompt)}>修改后重试</button></div></div>}
    </article>
  );
}

function ToolActivityGroup({
  tools,
  onOpenLocation,
  onRetry,
}: {
  tools: ToolTimelineEntry[];
  onOpenLocation(path: string): void;
  onRetry(prompt: string): void;
}): ReactElement {
  const infos = tools.map((tool) => toolCardInfo(tool.call, tool.result, tool.progress));
  const failedCount = infos.filter((info) => info.failed).length;
  const runningCount = infos.filter((info) => info.running).length;
  const [open, setOpen] = useState(false);
  const elapsedMs = infos.reduce((total, info) => total + Math.max(0, info.elapsedMs), 0);
  const actions = new Set(infos.map((info) => info.semanticAction).filter(Boolean));
  let summary = runningCount ? `正在执行 ${tools.length} 个操作` : `执行了 ${tools.length} 个操作`;
  if ([...actions].every((action) => ["read_file", "browse_files", "search_code", "git"].includes(action))) summary = runningCount ? "正在检查工作区" : "检查了工作区";
  else if (actions.size === 1 && actions.has("run_command")) summary = runningCount ? `正在运行 ${tools.length} 个命令` : `运行了 ${tools.length} 个命令`;
  else if (actions.has("edit_code") && actions.size === 1) summary = runningCount ? "正在修改代码" : "修改了代码";
  else if (actions.size === 1 && actions.has("run_tests")) summary = runningCount ? "正在运行验证" : "运行了验证";
  return (
    <details className={`tool-activity ${failedCount ? "failed" : ""} ${runningCount ? "running" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="tool-activity-head">
        {actions.size === 1 && (actions.has("run_command") || actions.has("run_tests"))
          ? <span className="tool-kind-icon"><Icon name="terminal" size={13} /></span>
          : <span className="tool-status">{failedCount ? "×" : runningCount ? "◌" : "✓"}</span>}
        <b>{summary}</b>
        <span className="tool-chevron" />
        {failedCount > 0 && <span className="tool-failed-count">{failedCount} 失败</span>}
        {runningCount > 0 && <span className="tool-running-count">进行中</span>}
        <small>{toolElapsed(elapsedMs)}</small>
      </summary>
      <div className="tool-activity-body">
        {tools.map((tool) => <TurnToolCard key={tool.key} call={tool.call} result={tool.result} progress={tool.progress} nested onOpenLocation={onOpenLocation} onRetry={onRetry} />)}
      </div>
    </details>
  );
}

function EventCard({
  event,
  threadId,
  onError,
  onNotice,
  onOpenChanges,
}: {
  event: RuntimeEvent;
  threadId: string;
  onError(value: string): void;
  onNotice(value: string): void;
  onOpenChanges(): void;
}): ReactElement {
  const detail = eventDetail(event);
  const isPermission = event.type === "permission.requested";
  const isPlan = event.type === "plan.ready";
  const isQuestion = event.type === "user_question.asked";
  const isTaskProfile = event.type === "task.profiled";
  const isResult = ["run.outcome", "run.finished", "turn.finished", "turn.failed", "turn.interrupted"].includes(event.type);
  const isRecovery = event.type === "recovery.available";
  const [answer, setAnswer] = useState("");
  const [responded, setResponded] = useState(false);
  const post = async (path: string, payload: Record<string, unknown>, success: string) => {
    try {
      await request(path, { method: "POST", body: JSON.stringify(payload) });
      setResponded(true);
      onNotice(success);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const toolId = textValue(event.payload.tool_use_id || event.payload.permission_id);
  const questionId = textValue(event.payload.question_id);
  if (isTaskProfile) {
    const profile = taskProfile(event);
    const summary = textValue(profile.user_summary).trim();
    return (
      <details className="intent-activity">
        <summary><b>{taskProfileTitle(profile)}</b><span className="tool-chevron" /></summary>
        {summary && <p>{summary}</p>}
      </details>
    );
  }
  if (isResult) {
    const status = textValue(event.payload.status || event.payload.outcome).toLowerCase();
    const failed = ["failed", "error", "incomplete", "interrupted", "cancelled"].includes(status);
    return (
      <article className={`result-inline ${failed ? "failed" : ""}`}>
        <span>{failed ? "本轮未完成" : "本轮完成"}</span>
        {detail && <small>{detail}</small>}
        <div><button onClick={onOpenChanges}>查看变更</button><button onClick={() => void browserBridge.copyText(detail || eventTitle(event))}>复制</button></div>
      </article>
    );
  }
  return (
    <article className={`event-card ${event.type.replaceAll(".", "-")}`}>
      <div className="event-icon">{isPermission || isPlan || isQuestion ? "?" : "●"}</div>
      <div className="event-body">
        <div className="event-head"><b>{eventTitle(event)}</b><time>{displayTime(event.ts)}</time></div>
        {detail && <pre>{detail}</pre>}
        {responded && <div className="card-resolved">已处理</div>}
        {!responded && isPermission && toolId && (
          <div className="card-actions">
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_once" }, "已允许本次操作")}>本次允许</button>
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_session" }, "本会话已允许")}>本会话允许</button>
            <button className="danger" onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "deny_once" }, "已拒绝")}>拒绝</button>
          </div>
        )}
        {!responded && isPlan && event.turn_id && (
          <><div className="answer-row"><input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder="可选：说明希望怎样修改计划" /><button disabled={!answer.trim()} onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "revise", revision: answer }, "已要求修改计划")}>要求修改</button></div><div className="card-actions">
              <button onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "approve" }, "计划已批准")}>批准计划</button>
              <button className="danger" onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "cancel" }, "计划已取消")}>取消</button>
            </div></>
        )}
        {!responded && isQuestion && questionId && (
          <div className="answer-row">
            <input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder="输入回答" />
            <button disabled={!answer.trim()} onClick={() => void post(`/v1/questions/${questionId}`, { answer }, "回答已送达")}>回答</button>
          </div>
        )}
        {!responded && isRecovery && (
          <div className="card-actions"><button onClick={() => void post(`/v1/threads/${threadId}/turns`, { content: "Continue from the last durable recovery point. Re-check uncertain file or command state before making any modification.", mode: "act" }, "已从安全位置继续")}>从安全位置继续</button><button onClick={onOpenChanges}>查看中断前变更</button></div>
        )}
      </div>
    </article>
  );
}

function DrawerPanel({
  drawer,
  threadId,
  workspace,
  initialFile,
  onClose,
  onReference,
  onError,
}: {
  drawer: Exclude<Drawer, null>;
  threadId: string;
  workspace: string;
  initialFile: string;
  onClose(): void;
  onReference(path: string): void;
  onError(value: string): void;
}): ReactElement {
  return (
    <aside className="drawer">
      <header><div><span className="panel-eyebrow">INSPECTOR</span><h2>{drawer === "files" ? "工作区文件" : drawer === "changes" ? "变更审查" : drawer === "models" ? "模型与 Provider" : "设置与能力"}</h2><small title={workspace}>{workspace.split(/[\\/]/).filter(Boolean).pop()}</small></div><button aria-label="关闭检查器" onClick={onClose}>×</button></header>
      {drawer === "files" && <FilesPanel initialFile={initialFile} onReference={onReference} onError={onError} />}
      {drawer === "changes" && <ChangesPanel threadId={threadId} onError={onError} />}
      {drawer === "models" && <ModelsPanel onError={onError} />}
      {drawer === "advanced" && <AdvancedPanel threadId={threadId} onError={onError} />}
    </aside>
  );
}

function FilesPanel({ initialFile, onReference, onError }: { initialFile: string; onReference(path: string): void; onError(value: string): void }): ReactElement {
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [query, setQuery] = useState("");
  const [currentPath, setCurrentPath] = useState(".");
  const [preview, setPreview] = useState<{ path: string; content: string; binary: boolean } | null>(null);
  useEffect(() => {
    if (!initialFile) return;
    request<{ path: string; content: string; binary: boolean }>(`/v1/workspace/file?path=${encodeURIComponent(initialFile)}`)
      .then(setPreview)
      .catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason)));
  }, [initialFile, onError]);
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      request<{ entries: WorkspaceEntry[] }>(
        `/v1/workspace/files?path=${encodeURIComponent(currentPath)}&query=${encodeURIComponent(query)}`,
        { signal: controller.signal },
      )
        .then((result) => setEntries(result.entries))
        .catch((reason: unknown) => {
          if (!controller.signal.aborted) {
            onError(reason instanceof Error ? reason.message : String(reason));
          }
        });
    }, 150);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [currentPath, onError, query]);
  const open = async (entry: WorkspaceEntry) => {
    if (entry.kind === "directory") {
      setCurrentPath(entry.path);
      setQuery("");
      return;
    }
    try {
      const file = await request<{ path: string; content: string; binary: boolean }>(`/v1/workspace/file?path=${encodeURIComponent(entry.path)}`);
      setPreview(file);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  return <div className="panel-content">
    {!preview && <div className="file-navigation"><button disabled={currentPath === "."} onClick={() => setCurrentPath(parentWorkspacePath(currentPath))}>←</button><span title={currentPath}>{currentPath === "." ? "工作区" : currentPath}</span></div>}
    <input className="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文件…" />
    {preview ? <div className="file-preview"><div><button onClick={() => setPreview(null)}>← 返回</button><button onClick={() => onReference(preview.path)}>引用 @</button></div><b>{preview.path}</b><pre>{preview.binary ? "二进制文件暂不显示" : preview.content}</pre></div> : <div className="file-list">{entries.map((entry) => <button key={entry.path} onClick={() => void open(entry)}><span>{entry.kind === "directory" ? "▸" : "·"} {entry.name}</span><small>{entry.size === null ? "" : `${entry.size} B`}</small></button>)}</div>}
  </div>;
}

function ChangesPanel({ threadId, onError }: { threadId: string; onError(value: string): void }): ReactElement {
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [context, setContext] = useState<Record<string, unknown>>({});
  const [commitMessage, setCommitMessage] = useState("chore: apply CodeRook changes");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const load = useCallback((signal?: AbortSignal) => {
    setLoading(true);
    setLoadError("");
    Promise.all([
      request<DiffPayload>("/v1/workspace/diff?scope=all", { signal }),
      threadId ? request<Record<string, unknown>>(`/v1/threads/${threadId}/context`, { signal }) : Promise.resolve({}),
    ])
      .then(([nextDiff, nextContext]) => { setDiff(nextDiff); setContext(nextContext); })
      .catch((reason: unknown) => {
        if (signal?.aborted) return;
        const message = reason instanceof Error ? reason.message : String(reason);
        setLoadError(message);
        onError(message);
      })
      .finally(() => { if (!signal?.aborted) setLoading(false); });
  }, [onError, threadId]);
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  }, [load]);
  const files = diff?.files || [];
  const checkpoints = (context.checkpoints || []) as Array<Record<string, unknown>>;
  const stageAll = async () => {
    if (!threadId || !diff?.state_digest) return;
    const paths = files.map((file) => textValue(file.path)).filter(Boolean);
    try {
      const staged = await request<DiffPayload>("/v1/workspace/stage", { method: "POST", body: JSON.stringify({ thread_id: threadId, paths, expected_digest: diff.state_digest, confirmed: true }) });
      setDiff(staged);
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const commit = async () => {
    if (!threadId || !diff?.state_digest || !commitMessage.trim()) return;
    try {
      const result = await request<{ commit: string }>("/v1/workspace/commit", { method: "POST", body: JSON.stringify({ thread_id: threadId, message: commitMessage, expected_digest: diff.state_digest, confirmed: true }) });
      alert(`本地提交已创建：${result.commit.slice(0, 12)}`);
      load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const rewind = async (checkpoint: Record<string, unknown>) => {
    try {
      const id = textValue(checkpoint.checkpoint_id);
      const preview = await request<Record<string, unknown>>(`/v1/threads/${threadId}/checkpoints/${id}/preview`);
      if (!confirm(`恢复 ${textValue(preview.paths)}？当前冲突：${textValue(preview.conflicts || "无")}`)) return;
      await request(`/v1/threads/${threadId}/checkpoints/${id}/rewind`, { method: "POST", body: JSON.stringify({ confirmed: true, expected_digest: preview.state_digest, run_id: context.checkpoint_run_id }) });
      load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="panel-content"><div className="panel-toolbar"><span>{loading ? "正在读取…" : `${files.length} 个变更文件`}</span><button disabled={loading} onClick={() => load()}>刷新</button><button disabled={!files.length || !threadId || loading} onClick={() => void stageAll()}>Stage 全部</button></div>{loadError && <div className="panel-error"><b>无法读取变更</b><p>{loadError}</p><button onClick={() => load()}>重试</button></div>}{!loadError && !files.length && !loading ? <p className="empty">工作区没有未提交变更。</p> : files.map((file, index) => <details className="diff-file" key={`${textValue(file.path)}-${index}`} open={index === 0}><summary><b>{textValue(file.path)}</b><span>+{textValue(file.additions || 0)} / -{textValue(file.deletions || 0)}</span></summary><pre>{textValue(file.patch || file.diff || file)}</pre></details>)}{textValue(diff?.scope) === "staged" && <div className="commit-row"><input value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} /><button onClick={() => void commit()}>创建本地 Commit</button><small>不会自动 push，也不会运行仓库 hooks。</small></div>}{checkpoints.length > 0 && <div className="checkpoints"><h3>恢复点</h3>{checkpoints.map((checkpoint) => <button key={textValue(checkpoint.checkpoint_id)} onClick={() => void rewind(checkpoint)}><span>{textValue(checkpoint.label || checkpoint.checkpoint_id)}</span><small>{textValue(checkpoint.status)}</small></button>)}</div>}</div>;
}

function ModelsPanel({ onError }: { onError(value: string): void }): ReactElement {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [presetId, setPresetId] = useState("deepseek");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const [validationError, setValidationError] = useState("");
  const load = useCallback(() => request<ProviderCatalog>("/v1/providers").then((value) => { setCatalog(value); const preset = value.presets[0]; if (preset) { setPresetId((current) => current || preset.id); setModel((current) => current || preset.models[0] || ""); } }).catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason))), [onError]);
  useEffect(() => { void load(); }, [load]);
  const preset = catalog?.presets.find((item) => item.id === presetId);
  const selectPreset = (value: string) => { setPresetId(value); const selected = catalog?.presets.find((item) => item.id === value); setModel(selected?.models[0] || ""); setApiKey(""); setValidationError(""); };
  const save = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setValidationError("");
    try { await request("/v1/providers", { method: "POST", body: JSON.stringify({ route_id: presetId, preset_id: presetId, model, api_key: apiKey || undefined, activate: true, update: catalog?.routes.some((route) => route.id === presetId) }) }); setApiKey(""); await load(); }
    catch (reason) { setValidationError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  };
  const routeAction = async (routeId: string, action: "activate" | "delete") => {
    try {
      if (action === "delete") {
        if (!confirm(`删除路由 ${routeId} 及其受管凭据？`)) return;
        await request(`/v1/providers/${encodeURIComponent(routeId)}`, { method: "DELETE", body: JSON.stringify({ confirmed: true, delete_credential: true }) });
      } else {
        await request(`/v1/providers/${encodeURIComponent(routeId)}/activate`, { method: "POST", body: "{}" });
      }
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="panel-content"><div className={`readiness ${catalog?.readiness.local_ready ? "ready" : "warning"}`}><b>{catalog?.readiness.local_ready ? "模型已就绪" : "需要配置模型"}</b><p>{readinessReason(catalog?.readiness.reason)}</p></div>{validationError && <div className="panel-error provider-validation-error"><b>模型验证未通过</b><p>{validationError}</p><button onClick={() => setValidationError("")}>知道了</button></div>}<form className="provider-form" onSubmit={(event) => void save(event)}><label>Provider<select value={presetId} onChange={(event) => selectPreset(event.target.value)}>{catalog?.presets.map((item) => <option key={item.id} value={item.id}>{item.name}{item.local ? " · 本地" : ""}</option>)}</select></label><label>模型<input value={model} onChange={(event) => { setModel(event.target.value); setValidationError(""); }} list="provider-models" /></label><datalist id="provider-models">{preset?.models.map((item) => <option key={item} value={item} />)}</datalist>{preset?.credential_required && <label>API Key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setValidationError(""); }} placeholder="只发送到本地 Core，不写入浏览器" /></label>}<div className="capability-tags">{preset && Object.entries(preset.capabilities).filter(([, enabled]) => enabled).map(([name]) => <span key={name}>{name}</span>)}</div><button className="primary" disabled={!model || saving}>{saving ? "正在验证…" : "Doctor 验证并启用"}</button></form><h3>已配置路由</h3>{catalog?.routes.map((route) => { const routeId = textValue(route.id); const active = catalog.active_route_id === route.id; return <div className="route-row" key={routeId}><div><b>{routeId}</b><small>{textValue(route.model)}</small></div><div className="route-actions"><span>{active ? "当前" : textValue(route.credential_source)}</span>{!active && <button onClick={() => void routeAction(routeId, "activate")}>启用</button>}<button onClick={() => void routeAction(routeId, "delete")}>删除</button></div></div>; })}</div>;
}

function AdvancedPanel({ threadId, onError }: { threadId: string; onError(value: string): void }): ReactElement {
  const [tab, setTab] = useState<"goals" | "workers" | "skills" | "mcp" | "memory">("goals");
  const [data, setData] = useState<Record<string, unknown>>({});
  const [objective, setObjective] = useState("");
  const [memoryBody, setMemoryBody] = useState("");
  const [skillSource, setSkillSource] = useState("");
  const endpoint = tab === "goals" ? `/v1/goals?thread_id=${encodeURIComponent(threadId)}` : tab === "workers" ? `/v1/workers?thread_id=${encodeURIComponent(threadId)}` : tab === "skills" ? "/v1/skills" : tab === "mcp" ? "/v1/mcp" : "/v1/memories";
  const load = useCallback(() => {
    if ((tab === "goals" || tab === "workers") && !threadId) { setData({}); return Promise.resolve(); }
    return request<Record<string, unknown>>(endpoint).then(setData).catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason)));
  }, [endpoint, onError, tab, threadId]);
  useEffect(() => { void load(); }, [load]);
  const mutate = async (path: string, payload: Record<string, unknown>) => {
    try { await request(path, { method: "POST", body: JSON.stringify(payload) }); await load(); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const goals = (data.goals || []) as Array<Record<string, unknown>>;
  const workers = (data.workers || []) as Array<Record<string, unknown>>;
  const skills = (data.skills || []) as Array<Record<string, unknown>>;
  const servers = (data.servers || []) as Array<Record<string, unknown>>;
  const memories = (data.memories || []) as Array<Record<string, unknown>>;
  const memorySettings = (data.settings || {}) as Record<string, unknown>;
  const workerFollowup = async (workerId: string) => {
    const message = prompt("向 Worker 发送后续指令");
    if (!message?.trim()) return;
    await mutate(`/v1/workers/${encodeURIComponent(workerId)}/followup`, {
      session_id: threadId,
      message: message.trim(),
    });
  };
  const workerReviewApply = async (workerId: string) => {
    try {
      const preview = await request<Record<string, unknown>>(`/v1/workers/${encodeURIComponent(workerId)}/review`, {
        method: "POST",
        body: JSON.stringify({ session_id: threadId, approved: true, confirmed: false }),
      });
      const files = (preview.changed_files || []) as string[];
      const digest = textValue(preview.state_digest);
      const summary = files.length ? files.join("\n") : "没有可应用的文件";
      if (!digest || !confirm(`审查 Worker 变更：\n\n${summary}\n\n确认审查通过？`)) return;
      await request(`/v1/workers/${encodeURIComponent(workerId)}/review`, {
        method: "POST",
        body: JSON.stringify({
          session_id: threadId,
          approved: true,
          confirmed: true,
          expected_digest: digest,
        }),
      });
      if (!confirm("审查已通过。是否将这批变更应用到主工作区？")) { await load(); return; }
      await request(`/v1/workers/${encodeURIComponent(workerId)}/apply`, {
        method: "POST",
        body: JSON.stringify({ session_id: threadId, expected_digest: digest, confirmed: true }),
      });
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const installSkill = async (event: FormEvent) => {
    event.preventDefault();
    const source = skillSource.trim();
    if (!source) return;
    try {
      const previewResult = await request<Record<string, unknown>>("/v1/skills/install", {
        method: "POST",
        body: JSON.stringify({ source, scope: "project", trust: "untrusted", confirmed: false }),
      });
      const preview = (previewResult.preview || {}) as Record<string, unknown>;
      const files = ((preview.files || []) as string[]).join("\n");
      if (!previewResult.confirmation_required || !confirm(`安装 Skill：${textValue(preview.name)}\nDigest：${textValue(preview.digest)}\n\n${files}\n\n确认安装到当前项目？`)) return;
      await request("/v1/skills/install", {
        method: "POST",
        body: JSON.stringify({ source, scope: "project", trust: "untrusted", confirmed: true, overwrite: Boolean(preview.overwrite) }),
      });
      setSkillSource("");
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const editMemory = async (memory: Record<string, unknown>) => {
    const body = prompt("编辑记忆内容", textValue(memory.body));
    if (!body?.trim() || body.trim() === textValue(memory.body).trim()) return;
    try {
      await request(`/v1/memories/${encodeURIComponent(textValue(memory.id))}`, {
        method: "PATCH",
        body: JSON.stringify({ body: body.trim() }),
      });
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const toggleMemoryAuto = async () => {
    const autoSave = textValue(memorySettings.auto_save) === "off" ? "prompt" : "off";
    try {
      await request("/v1/memory/settings", {
        method: "PATCH",
        body: JSON.stringify({ auto_save: autoSave }),
      });
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="panel-content"><div className="advanced-tabs">{(["goals", "workers", "skills", "mcp", "memory"] as const).map((name) => <button className={tab === name ? "active" : ""} key={name} onClick={() => setTab(name)}>{name}</button>)}</div>
    {tab === "goals" && <div className="advanced-list"><form className="inline-create" onSubmit={(event) => { event.preventDefault(); void mutate("/v1/goals", { session_id: threadId, objective, start: false }); setObjective(""); }}><input value={objective} onChange={(event) => setObjective(event.target.value)} placeholder="创建有界长任务 Goal" /><button disabled={!threadId || !objective.trim()}>创建</button></form>{goals.map((goal) => <section key={textValue(goal.id)}><div><b>{textValue(goal.objective)}</b><span className="stable">{textValue(goal.status)}</span></div><p>轮次 {textValue(goal.auto_turns_used || 0)} / {textValue(goal.max_auto_turns || 3)} · Token {textValue(goal.tokens_used || 0)} / {textValue(goal.token_budget || "∞")}</p><div className="card-actions">{goal.status === "active" ? <button onClick={() => void mutate(`/v1/goals/${goal.id}/pause`, {})}>暂停</button> : <button onClick={() => void mutate(`/v1/goals/${goal.id}/resume`, {})}>恢复</button>}<button className="danger" onClick={() => void mutate(`/v1/goals/${goal.id}/clear`, {})}>取消</button></div></section>)}</div>}
    {tab === "workers" && <div className="advanced-list">{workers.length === 0 && <p className="empty">当前会话没有 Worker。符合独立验收和 Write Claim 条件时，Agent 才会委派。</p>}{workers.map((worker) => { const workerId = textValue(worker.worker_id || worker.id); const status = textValue(worker.status); return <section key={workerId}><div><b>{textValue(worker.description || workerId)}</b><span className="stable">{status}</span></div><p>{textValue(worker.model)} · {textValue(worker.backend || "builtin")} · {worker.read_only ? "只读" : "独立 Worktree"}</p><div className="card-actions">{["queued", "running", "waiting"].includes(status) && <><button onClick={() => void workerFollowup(workerId)}>跟进</button><button onClick={() => void mutate(`/v1/workers/${encodeURIComponent(workerId)}/cancel`, { session_id: threadId })}>取消 Worker</button></>}{status === "completed" && !worker.read_only && worker.handoff_status !== "applied" && <button onClick={() => void workerReviewApply(workerId)}>审查并应用</button>}</div></section>; })}</div>}
    {tab === "skills" && <div className="advanced-list"><form className="inline-create" onSubmit={(event) => void installSkill(event)}><input value={skillSource} onChange={(event) => setSkillSource(event.target.value)} placeholder="工作区内 Skill 文件或目录" /><button disabled={!skillSource.trim()}>预览安装</button></form>{skills.map((skill) => <section key={textValue(skill.name)}><div><b>{textValue(skill.name)}</b><span className="stable">{textValue(skill.trust)}</span></div><p>{textValue(skill.description)}</p><small>{textValue(skill.scope)} · {textValue(skill.integrity)}</small></section>)}{!skills.length && <p className="empty">暂无 Skill。安装必须先预览文件与 digest，再明确确认。</p>}</div>}
    {tab === "mcp" && <div className="advanced-list">{servers.map((server) => <section key={textValue(server.name)}><div><b>{textValue(server.name)}</b><span className={server.status === "connected" ? "stable" : "labs"}>{textValue(server.status)}</span></div><p>{textValue(server.transport)} · {textValue(server.tool_count)} tools</p>{server.error ? <small>{textValue(server.error)}</small> : null}</section>)}{!servers.length && <p className="empty">没有配置 MCP Tool Server。</p>}</div>}
    {tab === "memory" && <div className="advanced-list"><div className="memory-settings"><span>自动记忆：{textValue(memorySettings.auto_save) === "off" ? "已关闭" : "保存前询问"}</span><button onClick={() => void toggleMemoryAuto()}>{textValue(memorySettings.auto_save) === "off" ? "开启询问" : "关闭"}</button></div><form className="inline-create" onSubmit={(event) => { event.preventDefault(); void mutate("/v1/memories", { name: memoryBody.slice(0, 40), body: memoryBody, memory_type: "project", source_session_id: threadId }); setMemoryBody(""); }}><input value={memoryBody} onChange={(event) => setMemoryBody(event.target.value)} placeholder="添加项目记忆" /><button disabled={!memoryBody.trim()}>添加</button></form>{memories.map((memory) => <section key={textValue(memory.id)}><div><b>{textValue(memory.name)}</b><span className="stable">{memory.pinned ? "pinned" : textValue(memory.type)}</span></div><p>{textValue(memory.body)}</p><div className="card-actions"><button onClick={() => void editMemory(memory)}>编辑</button><button className="danger" onClick={async () => { if (!confirm("删除这条记忆？")) return; try { await request(`/v1/memories/${memory.id}`, { method: "DELETE", body: JSON.stringify({ confirmed: true }) }); await load(); } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); } }}>删除</button></div></section>)}</div>}
    <div className="labs-note"><b>Labs 已隐藏</b><p>Fleet、Workflow、ACP、Hooks 和 Tool Program 不进入默认 Web 导航。</p></div>
  </div>;
}

export function App(): ReactElement {
  const [ready, setReady] = useState(false);
  const [fatal, setFatal] = useState("");
  const [workspace, setWorkspace] = useState("");
  useEffect(() => {
    bootstrap().then((result) => { setWorkspace(result.workspace); setReady(true); }).catch((reason: unknown) => setFatal(reason instanceof Error ? reason.message : String(reason)));
  }, []);
  if (fatal) return <div className="fatal"><span>♜</span><h1>无法连接本地 CodeRook Core</h1><p>{fatal}</p><p>请重新运行 <code>coderook web</code> 获取一次性启动链接。</p></div>;
  if (!ready) return <div className="loading"><span>♜</span><p>正在连接本地工作区…</p></div>;
  return <AppShell initialWorkspace={workspace} />;
}
