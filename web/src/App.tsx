import { createContext, FormEvent, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
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
  TurnReceipt,
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
type ThreadContext = { estimated_tokens?: number };
type ProjectRecord = {
  id: string;
  name: string;
  root: string;
  kind: "blank" | "existing";
  created_at: number;
  last_opened_at: number;
  active: boolean;
};
type ProjectCatalog = {
  projects: ProjectRecord[];
  active_workspace: string;
  default_projects_root: string;
};
type DirectoryListing = {
  path: string;
  parent: string | null;
  roots: string[];
  directories: { name: string; path: string }[];
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
type ProductDialogOptions = {
  title: string;
  description?: string;
  detail?: string;
  input?: "text" | "multiline";
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string | null;
  danger?: boolean;
};
type PendingProductDialog = ProductDialogOptions & { resolve(value: string | null): void };
type ProductDialogController = (options: ProductDialogOptions) => Promise<string | null>;
export type WebLocale = "zh-CN" | "en-US";
export type WebTheme = "light" | "high-contrast";
type InterfacePreferences = {
  locale: WebLocale;
  setLocale(locale: WebLocale): void;
  theme: WebTheme;
  setTheme(theme: WebTheme): void;
};

const TURN_PAGE_SIZE = 30;
const MAX_CACHED_EVENTS = 5000;
const LOCALE_STORAGE_KEY = "coderook.web.locale";
const THEME_STORAGE_KEY = "coderook.web.theme";
const ProductDialogContext = createContext<ProductDialogController | null>(null);
const InterfacePreferencesContext = createContext<InterfacePreferences | null>(null);

export function resolveWebLocale(value: string | null | undefined): WebLocale {
  return value?.toLowerCase().startsWith("en") ? "en-US" : "zh-CN";
}

function initialWebLocale(): WebLocale {
  if (typeof window === "undefined") return "zh-CN";
  return resolveWebLocale(window.localStorage.getItem(LOCALE_STORAGE_KEY) || window.navigator.language);
}

export function resolveWebTheme(value: string | null | undefined): WebTheme {
  return value === "high-contrast" ? "high-contrast" : "light";
}

function initialWebTheme(): WebTheme {
  if (typeof window === "undefined") return "light";
  return resolveWebTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
}

let activeWebLocale = initialWebLocale();

function tr(chinese: string, english: string): string {
  return activeWebLocale === "en-US" ? english : chinese;
}

function InterfacePreferencesProvider({ children }: { children: ReactNode }): ReactElement {
  const [locale, updateLocale] = useState<WebLocale>(activeWebLocale);
  const [theme, updateTheme] = useState<WebTheme>(initialWebTheme);
  const setLocale = useCallback((next: WebLocale) => {
    activeWebLocale = next;
    window.localStorage.setItem(LOCALE_STORAGE_KEY, next);
    document.documentElement.lang = next;
    updateLocale(next);
  }, []);
  const setTheme = useCallback((next: WebTheme) => {
    window.localStorage.setItem(THEME_STORAGE_KEY, next);
    document.documentElement.dataset.theme = next;
    updateTheme(next);
  }, []);
  useEffect(() => {
    activeWebLocale = locale;
    document.documentElement.lang = locale;
    document.documentElement.dataset.theme = theme;
  }, [locale, theme]);
  const value = useMemo(() => ({ locale, setLocale, theme, setTheme }), [locale, setLocale, theme, setTheme]);
  return <InterfacePreferencesContext.Provider value={value}>{children}</InterfacePreferencesContext.Provider>;
}

function useInterfacePreferences(): InterfacePreferences {
  const preferences = useContext(InterfacePreferencesContext);
  if (!preferences) throw new Error("InterfacePreferencesProvider is missing");
  return preferences;
}

function ProductDialogLayer({ pending, onResolve }: { pending: PendingProductDialog; onResolve(value: string | null): void }): ReactElement {
  const [value, setValue] = useState(pending.initialValue || "");
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const hasInput = Boolean(pending.input);
  const canConfirm = !hasInput || Boolean(value.trim());
  useEffect(() => { inputRef.current?.focus(); inputRef.current?.select(); }, []);
  const finish = (confirmed: boolean) => onResolve(confirmed ? (hasInput ? value.trim() : "confirmed") : null);
  return <div className="product-dialog-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) finish(false); }}>
    <form ref={formRef} className="product-dialog" role="dialog" aria-modal="true" aria-labelledby="product-dialog-title" onSubmit={(event) => { event.preventDefault(); if (canConfirm) finish(true); }} onKeyDown={(event) => {
      if (event.key === "Escape") { finish(false); return; }
      if (event.key !== "Tab") return;
      const controls = Array.from(formRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled)") || []);
      if (!controls.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }}>
      <header><div><small>CODEROOK</small><h2 id="product-dialog-title">{pending.title}</h2></div><button type="button" aria-label={tr("关闭", "Close")} onClick={() => finish(false)}>×</button></header>
      {pending.description && <p>{pending.description}</p>}
      {pending.detail && <pre>{pending.detail}</pre>}
      {pending.input === "text" && <input ref={(node) => { inputRef.current = node; }} value={value} placeholder={pending.placeholder} onChange={(event) => setValue(event.target.value)} />}
      {pending.input === "multiline" && <textarea ref={(node) => { inputRef.current = node; }} value={value} placeholder={pending.placeholder} onChange={(event) => setValue(event.target.value)} />}
      <footer>{pending.cancelLabel !== null && <button type="button" onClick={() => finish(false)}>{pending.cancelLabel || tr("取消", "Cancel")}</button>}<button className={pending.danger ? "danger" : "primary"} disabled={!canConfirm}>{pending.confirmLabel || tr("确认", "Confirm")}</button></footer>
    </form>
  </div>;
}

function ProductDialogProvider({ children }: { children: ReactNode }): ReactElement {
  const [pending, setPending] = useState<PendingProductDialog | null>(null);
  const open = useCallback<ProductDialogController>((options) => new Promise((resolve) => setPending({ ...options, resolve })), []);
  const close = (value: string | null) => {
    const current = pending;
    setPending(null);
    current?.resolve(value);
  };
  return <ProductDialogContext.Provider value={open}>{children}{pending && <ProductDialogLayer pending={pending} onResolve={close} />}</ProductDialogContext.Provider>;
}

function useProductDialog(): ProductDialogController {
  const controller = useContext(ProductDialogContext);
  if (!controller) throw new Error("ProductDialogProvider is missing");
  return controller;
}

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

function phaseLabel(value: string): string {
  const labels: Record<string, [string, string]> = {
    understanding: ["理解任务", "Understanding"],
    exploring: ["探索代码", "Exploring"],
    planning: ["规划方案", "Planning"],
    waiting_confirmation: ["等待确认", "Waiting for approval"],
    executing: ["执行修改", "Making changes"],
    verifying: ["运行验证", "Verifying"],
    reviewing: ["审查结果", "Reviewing"],
    completed: ["任务完成", "Completed"],
    failed: ["任务失败", "Failed"],
    interrupted: ["任务中断", "Interrupted"],
  };
  const label = labels[value];
  return label ? tr(label[0], label[1]) : value;
}

function statusLabel(value: string): string {
  const labels: Record<string, [string, string]> = {
    idle: ["空闲", "Idle"],
    running: ["运行中", "Running"],
    waiting: ["等待中", "Waiting"],
    waiting_permission: ["等待权限", "Waiting for permission"],
    waiting_input: ["等待输入", "Waiting for input"],
    queued: ["已排队", "Queued"],
    dispatching: ["发送中", "Dispatching"],
    active: ["进行中", "Active"],
    paused: ["已暂停", "Paused"],
    paused_needs_confirmation: ["等待确认恢复", "Waiting for resume confirmation"],
    blocked: ["受阻", "Blocked"],
    completed: ["已完成", "Completed"],
    succeeded: ["已完成", "Succeeded"],
    cancelled: ["已取消", "Cancelled"],
    interrupted: ["待恢复", "Recovery available"],
    failed: ["失败", "Failed"],
    archived: ["已归档", "Archived"],
    connected: ["已连接", "Connected"],
    disconnected: ["未连接", "Disconnected"],
    applied: ["已应用", "Applied"],
  };
  const label = labels[value];
  return label ? tr(label[0], label[1]) : value;
}

function displayTime(value: string): string {
  if (!value) return "";
  return new Intl.DateTimeFormat(activeWebLocale, {
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
  const labels: Record<string, [string, string]> = {
    "the remote route has credentials but has not passed a basic Doctor probe": ["已保存凭据，但当前路由尚未通过 Doctor 验证。", "Credentials are saved, but this route has not passed Doctor."],
    "the active route Doctor receipt is missing or stale": ["当前路由的 Doctor 验证已缺失或过期。", "The active route's Doctor result is missing or stale."],
    "no provider route is configured": ["尚未配置可用的模型路由。", "No model route is configured."],
    "the active route credential is missing": ["当前路由缺少 API Key。", "The active route is missing its API key."],
  };
  const label = labels[reason];
  return label ? tr(label[0], label[1]) : reason;
}

function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error(tr("无法读取图片", "Unable to read the image")));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}

function eventTitle(event: RuntimeEvent): string {
  if (event.type === "run.phase_changed") {
    return phaseLabel(textValue(event.payload.phase));
  }
  const names: Record<string, string> = {
    "input.admitted": tr("你", "You"),
    "task.profiled": tr("已理解", "Task understood"),
    "tool.call_started": tr("正在使用工具", "Using a tool"),
    "tool.call_finished": tr("工具完成", "Tool completed"),
    "llm.retry": tr("模型重试", "Retrying model"),
    "context.compaction_committed": tr("上下文已整理", "Context compacted"),
    "context.compacted": tr("上下文已整理", "Context compacted"),
    "plan.ready": tr("执行计划", "Execution plan"),
    "permission.requested": tr("需要权限", "Permission required"),
    "user_question.asked": tr("需要你的回答", "Your answer is needed"),
    "recovery.available": tr("发现可恢复任务", "Task recovery available"),
    "run.outcome": tr("任务结果", "Task result"),
    "run.finished": tr("任务结果", "Task result"),
    "turn.finished": tr("本轮结束", "Turn finished"),
    "turn.failed": tr("本轮未完成", "Turn incomplete"),
    "turn.interrupted": tr("本轮已中断", "Turn interrupted"),
  };
  if (names[event.type]) return names[event.type];
  const presentation = event.payload.presentation as Record<string, unknown> | undefined;
  return presentation?.title ? textValue(presentation.title) : event.type.replaceAll(".", " · ");
}

function messageContent(value: unknown): string {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return textValue(value);
  return value.map((block) => {
    if (!block || typeof block !== "object") return textValue(block);
    const record = block as Record<string, unknown>;
    if (record.type === "text") return textValue(record.text);
    if (record.type === "image") return tr("[图片]", "[image]");
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
    run_command: tr("运行命令", "Run command"),
    run_tests: tr("运行验证", "Run checks"),
    read_file: tr("读取文件", "Read file"),
    browse_files: tr("浏览文件", "Browse files"),
    search_code: tr("搜索代码", "Search code"),
    edit_code: tr("修改代码", "Edit code"),
    git: tr("检查 Git", "Inspect Git"),
    web: tr("访问网络", "Access web"),
    worker: tr("调用 Agent", "Run agent"),
  };
  const semantic = labels[semanticAction];
  if (semantic) {
    if (semanticAction === "run_command") return state === "failed" ? tr("运行失败", "Command failed") : state === "running" ? tr("正在运行", "Running") : tr("已运行", "Command finished");
    if (semanticAction === "run_tests") return state === "failed" ? tr("验证失败", "Checks failed") : state === "running" ? tr("正在验证", "Running checks") : tr("已验证", "Checks passed");
    return state === "failed" ? tr(`${semantic}失败`, `${semantic} failed`) : state === "running" ? tr(`正在${semantic}`, `Running ${semantic.toLowerCase()}`) : semantic;
  }
  if (["Bash", "Run", "bash", "run"].includes(toolName)) {
    return state === "failed" ? tr("命令执行失败", "Command failed") : state === "running" ? tr("正在运行命令", "Running command") : tr("运行命令", "Run command");
  }
  if (["File", "file"].includes(toolName)) {
    const action = textValue(params.action);
    const labels: Record<string, string> = {
      read: tr("读取文件", "Read file"),
      list: tr("浏览文件", "Browse files"),
      write: tr("写入文件", "Write file"),
      patch: tr("修改文件", "Edit file"),
      search: tr("搜索文件", "Search files"),
    };
    const label = labels[action] || tr("处理文件", "Process file");
    return state === "failed" ? tr(`${label}失败`, `${label} failed`) : state === "running" ? tr(`正在${label}`, `Running ${label.toLowerCase()}`) : label;
  }
  if (["Git", "git"].includes(toolName)) {
    return state === "failed" ? tr("Git 操作失败", "Git operation failed") : state === "running" ? tr("正在执行 Git 操作", "Running Git operation") : tr("Git 操作", "Git operation");
  }
  return state === "failed" ? tr(`${toolName} 失败`, `${toolName} failed`) : state === "running" ? tr(`正在使用 ${toolName}`, `Using ${toolName}`) : tr(`使用 ${toolName}`, `Use ${toolName}`);
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

export function resultStatusIsFailure(status: string): boolean {
  return !["completed", "success", "succeeded"].includes(status.trim().toLowerCase());
}

function taskProfile(event: RuntimeEvent): Record<string, unknown> {
  const profile = event.payload.profile;
  return profile && typeof profile === "object" && !Array.isArray(profile)
    ? profile as Record<string, unknown>
    : {};
}

function taskProfileTitle(profile: Record<string, unknown>): string {
  const strategy = textValue(profile.strategy);
  if (strategy === "delegate") return tr("评估任务拆分与并行执行", "Evaluating safe parallel work");
  const intent = textValue(profile.intent);
  const titles: Record<string, string> = {
    explain: tr("理解问题并整理回答", "Understanding the question"),
    inspect: tr("定位相关实现与证据", "Locating relevant code and evidence"),
    fix: strategy === "plan_first" ? tr("定位根因并规划修复", "Finding the root cause and planning a fix") : tr("定位问题并准备修复", "Locating the issue and preparing a fix"),
    refactor: tr("梳理重构范围与兼容边界", "Scoping the refactor and compatibility"),
    test: tr("确定验证范围与执行方式", "Choosing the verification scope"),
    multi_file_change: tr("拆解跨文件改动与依赖", "Mapping multi-file changes and dependencies"),
  };
  return titles[intent] || tr("分析任务与执行方式", "Analyzing the task and execution strategy");
}

export function isSimpleProductQuestion(value: string): boolean {
  return /(你好|您好|你是谁|什么模型|具体型号|你能做什么|你能干什么|你会什么|有什么功能|怎么使用|如何使用)/i.test(value)
    || /^\s*(hi|hello|who are you|what model (are you|is this)|what can you do|what do you do|how (do i|to) use (this|coderook))\s*[?!.]*\s*$/i.test(value);
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

export function workspaceHasUserProject(workspace: string): boolean {
  return !/(?:^|[\\/])\.coderook[\\/]welcome-workspace[\\/]?$/i.test(workspace);
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

export function workspacePathIsDirectoryError(reason: unknown): boolean {
  const message = reason instanceof Error ? reason.message : String(reason);
  return message.toLowerCase().includes("workspace path is not a file");
}

export function appendRuntimeEvent(
  current: RuntimeEvent[],
  event: RuntimeEvent,
): RuntimeEvent[] {
  if (current.some((item) => item.seq === event.seq)) return current;
  const next = [...current, event].sort((left, right) => left.seq - right.seq);
  return next.length > MAX_CACHED_EVENTS ? next.slice(-MAX_CACHED_EVENTS) : next;
}

export function preferredThreadId(threads: ThreadRecord[]): string {
  return threads.find((thread) => (thread.turn_count || 0) > 0)?.id || threads[0]?.id || "";
}

function AppShell({
  initialWorkspace,
  onWorkspaceChanged,
}: {
  initialWorkspace: string;
  onWorkspaceChanged(workspace: string): void;
}): ReactElement {
  const dialog = useProductDialog();
  const preferences = useInterfacePreferences();
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
  const [projectHubOpen, setProjectHubOpen] = useState(false);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [activeModel, setActiveModel] = useState(tr("未配置模型", "No model configured"));
  const [sessionsReady, setSessionsReady] = useState(false);
  const [threadLoading, setThreadLoading] = useState(false);
  const [hasOlderTurns, setHasOlderTurns] = useState(false);
  const [loadingOlderTurns, setLoadingOlderTurns] = useState(false);
  const [contextTokens, setContextTokens] = useState(0);
  const [composerCaret, setComposerCaret] = useState(0);
  const [fileSuggestions, setFileSuggestions] = useState<WorkspaceEntry[]>([]);
  const [fileSuggestionIndex, setFileSuggestionIndex] = useState(0);
  const cursors = useRef<Record<string, number>>({});
  const eventCache = useRef<Record<string, RuntimeEvent[]>>({});
  const threadLoadVersions = useRef<Record<string, number>>({});
  const queueLoadVersions = useRef<Record<string, number>>({});
  const initializedSelection = useRef(false);
  const composerDrafts = useRef<Record<string, string>>({});
  const attachmentDrafts = useRef<Record<string, ImageAttachment[]>>({});
  const fileReferenceDrafts = useRef<Record<string, string[]>>({});
  const selectedIdRef = useRef("");
  const composerInputRef = useRef<HTMLTextAreaElement | null>(null);
  const timelineRef = useRef<HTMLElement | null>(null);
  const previousTimelineSize = useRef(0);
  const pendingHistoryScroll = useRef<{ height: number; top: number } | null>(null);
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
    setActiveModel(textValue(active?.model) || tr("未配置模型", "No model configured"));
  }, [preferences.locale]);

  const refreshThreads = useCallback(async () => {
    const result = await request<ThreadRecord[]>("/v1/threads");
    result.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    setThreads(result);
    if (!initializedSelection.current) {
      initializedSelection.current = true;
      setSelectedId(preferredThreadId(result));
    }
  }, []);

  useEffect(() => {
    void refreshThreads()
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setSessionsReady(true));
  }, [refreshThreads]);

  useEffect(() => {
    void refreshActiveModel().catch(() => setActiveModel(tr("模型状态未知", "Model status unavailable")));
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

  const loadThread = useCallback(async (
    threadId: string,
    signal?: AbortSignal,
    showLoading = false,
  ) => {
    const version = (threadLoadVersions.current[threadId] || 0) + 1;
    threadLoadVersions.current[threadId] = version;
    if (showLoading && selectedIdRef.current === threadId) setThreadLoading(true);
    try {
      const [turnPage, loadedQueue, loadedContext] = await Promise.all([
        request<TurnRecord[]>(
          `/v1/threads/${encodeURIComponent(threadId)}/turns?limit=${TURN_PAGE_SIZE + 1}`,
          { signal },
        ),
        request<QueuedMessage[]>(
          `/v1/threads/${encodeURIComponent(threadId)}/queue`,
          { signal },
        ),
        request<ThreadContext>(
          `/v1/threads/${encodeURIComponent(threadId)}/context`,
          { signal },
        ),
      ]);
      const loadedTurns = turnPage.slice(-TURN_PAGE_SIZE);
      const loadedItems = await Promise.all(
        loadedTurns.map((turn) =>
          request<TurnItem[]>(`/v1/turns/${encodeURIComponent(turn.id)}/items`, { signal }),
        ),
      );
      if (
        signal?.aborted
        || selectedIdRef.current !== threadId
        || threadLoadVersions.current[threadId] !== version
      ) return;
      setTurns(loadedTurns);
      setItems(loadedItems.flat());
      setQueuedMessages(loadedQueue);
      setHasOlderTurns(turnPage.length > TURN_PAGE_SIZE);
      setContextTokens(Number(loadedContext.estimated_tokens || 0));
    } finally {
      if (
        showLoading
        && selectedIdRef.current === threadId
        && threadLoadVersions.current[threadId] === version
      ) setThreadLoading(false);
    }
  }, []);

  const loadQueue = useCallback(async (threadId: string) => {
    const version = (queueLoadVersions.current[threadId] || 0) + 1;
    queueLoadVersions.current[threadId] = version;
    const loaded = await request<QueuedMessage[]>(
      `/v1/threads/${encodeURIComponent(threadId)}/queue`,
    );
    if (
      selectedIdRef.current === threadId
      && queueLoadVersions.current[threadId] === version
    ) setQueuedMessages(loaded);
  }, []);

  useEffect(() => {
    const reconcile = () => {
      if (document.visibilityState === "hidden") return;
      void refreshThreads().catch(() => undefined);
      void refreshActiveModel().catch(() => undefined);
      if (selectedIdRef.current) void loadThread(selectedIdRef.current).catch(() => undefined);
    };
    window.addEventListener("focus", reconcile);
    document.addEventListener("visibilitychange", reconcile);
    return () => {
      window.removeEventListener("focus", reconcile);
      document.removeEventListener("visibilitychange", reconcile);
    };
  }, [loadThread, refreshActiveModel, refreshThreads]);

  useEffect(() => {
    if (!selectedId) {
      setTurns([]);
      setItems([]);
      setEvents([]);
      setQueuedMessages([]);
      setPhase("idle");
      setThreadLoading(false);
      setHasOlderTurns(false);
      setContextTokens(0);
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
    void loadThread(selectedId, controller.signal, true)
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
            cursors.current[selectedId] ? undefined : MAX_CACHED_EVENTS,
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
    setQueueMode(false);
    setComposer(composerDrafts.current[nextKey] || "");
    setAttachments(attachmentDrafts.current[nextKey] || []);
    setFileReferences(fileReferenceDrafts.current[nextKey] || []);
    setQueuedMessages([]);
    setTurns([]);
    setItems([]);
    setHasOlderTurns(false);
    setContextTokens(0);
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
    if (!content || sending || !sessionsReady || threadLoading) return;
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
          setNotice(tr("消息已加入队列，将在当前任务结束后发送", "Message queued and will be sent after the current task."));
          return;
        }
        if (attachments.length > 0) {
          setError(tr("运行中的纠偏暂不支持图片。请切换到“排队”发送，图片会随下一轮任务提交。", "Images cannot be attached to an active steer message. Switch to Queue to send them with the next task."));
          return;
        }
        await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/steer`, {
          method: "POST",
          body: JSON.stringify({ content: submitted }),
        });
        setNotice(tr("纠偏消息已送达当前任务", "Steer message sent to the active task."));
      } else {
        const provider = await request<ProviderCatalog>("/v1/providers");
        if (!provider.readiness.local_ready) {
          setDrawer("models");
          setNotice(tr("先完成模型配置，当前输入已为你保留", "Configure a model first. Your draft has been preserved."));
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

  const loadOlderTurns = async () => {
    if (!selectedId || loadingOlderTurns || !hasOlderTurns || turns.length === 0) return;
    const threadId = selectedId;
    const version = threadLoadVersions.current[threadId] || 0;
    const before = turns[0].id;
    const timeline = timelineRef.current;
    pendingHistoryScroll.current = timeline
      ? { height: timeline.scrollHeight, top: timeline.scrollTop }
      : null;
    setLoadingOlderTurns(true);
    try {
      const turnPage = await request<TurnRecord[]>(
        `/v1/threads/${encodeURIComponent(threadId)}/turns?limit=${TURN_PAGE_SIZE + 1}&before=${encodeURIComponent(before)}`,
      );
      const olderTurns = turnPage.slice(-TURN_PAGE_SIZE);
      const olderItems = await Promise.all(
        olderTurns.map((turn) =>
          request<TurnItem[]>(`/v1/turns/${encodeURIComponent(turn.id)}/items`),
        ),
      );
      if (
        selectedIdRef.current !== threadId
        || threadLoadVersions.current[threadId] !== version
      ) {
        pendingHistoryScroll.current = null;
        return;
      }
      setTurns((current) => {
        const byId = new Map([...olderTurns, ...current].map((turn) => [turn.id, turn]));
        return [...byId.values()].sort((left, right) => {
          const timeOrder = left.created_at.localeCompare(right.created_at);
          return timeOrder || left.id.localeCompare(right.id);
        });
      });
      setItems((current) => {
        const byId = new Map([...olderItems.flat(), ...current].map((item) => [item.id, item]));
        return [...byId.values()];
      });
      setHasOlderTurns(turnPage.length > TURN_PAGE_SIZE);
      if (olderTurns.length === 0) pendingHistoryScroll.current = null;
    } catch (reason) {
      pendingHistoryScroll.current = null;
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (selectedIdRef.current === threadId) setLoadingOlderTurns(false);
    }
  };

  const cancel = async () => {
    if (!activeTurn) return;
    try {
      await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/interrupt`, {
        method: "POST",
        body: "{}",
      });
      setNotice(tr("已请求停止当前任务", "Stop requested for the active task."));
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
        const title = await dialog({
          title: tr("重命名会话", "Rename session"),
          description: tr("名称会同步到使用同一 Core 的 TUI 与 Web。", "The new name will be shared by TUI and Web through the same Core."),
          input: "text",
          initialValue: selectedThread?.title || "",
          placeholder: tr("输入新的会话名称", "Enter a new session name"),
          confirmLabel: tr("保存名称", "Save name"),
        });
        if (!title?.trim()) return;
        const updated = await request<ThreadRecord>(`/v1/threads/${encodeURIComponent(selectedId)}`, {
          method: "PATCH",
          body: JSON.stringify({ title: title.trim() }),
        });
        setThreads((current) => current.map((thread) => thread.id === updated.id ? updated : thread));
        return;
      }
      if (action === "fork") {
        const forked = await request<ThreadRecord>(`/v1/threads/${encodeURIComponent(selectedId)}/fork`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setThreads((current) => [forked, ...current]);
        selectThread(forked.id);
        return;
      }
      if (action === "export") {
        const exported = await request<{ filename: string; content: string }>(
          `/v1/threads/${encodeURIComponent(selectedId)}/export?format=markdown`,
        );
        const blob = new Blob([exported.content], { type: "text/markdown" });
        const link = document.createElement("a");
        link.href = URL.createObjectURL(blob);
        link.download = exported.filename;
        link.click();
        URL.revokeObjectURL(link.href);
        return;
      }
      if (!await dialog({
        title: tr("删除会话", "Delete session"),
        description: tr(`将永久删除“${selectedThread?.title || "未命名任务"}”及其本地执行记录，此操作不能撤销。`, `This permanently deletes “${selectedThread?.title || "Untitled task"}” and its local execution history. This cannot be undone.`),
        confirmLabel: tr("永久删除", "Delete permanently"),
        danger: true,
      })) return;
      await request(`/v1/threads/${encodeURIComponent(selectedId)}`, {
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
    const loadedTurnIds = new Set(turns.map((turn) => turn.id));
    for (const event of events) {
      if (event.turn_id && !loadedTurnIds.has(event.turn_id)) continue;
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
  }, [activeTurn?.id, events, items, turns]);

  useEffect(() => {
    const timeline = timelineRef.current;
    if (!timeline || timelineEntries.length === 0) return;
    const historyScroll = pendingHistoryScroll.current;
    if (historyScroll) {
      pendingHistoryScroll.current = null;
      window.requestAnimationFrame(() => {
        timeline.scrollTop = historyScroll.top + timeline.scrollHeight - historyScroll.height;
      });
      previousTimelineSize.current = timelineEntries.length;
      return;
    }
    const firstLoad = previousTimelineSize.current === 0;
    const nearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 140;
    if (firstLoad || nearBottom) {
      window.requestAnimationFrame(() => {
        timeline.scrollTop = timeline.scrollHeight;
      });
    }
    previousTimelineSize.current = timelineEntries.length;
  }, [timelineEntries]);
  const tokenUsage = contextTokens;
  const projectSelected = workspaceHasUserProject(workspace);
  const workspaceName = projectSelected
    ? workspace.split(/[\\/]/).filter(Boolean).pop() || "workspace"
    : tr("选择项目", "Choose project");

  return (
    <div className={`app-shell ${drawer ? "inspector-open" : ""}`} lang={preferences.locale}>
      <aside className={`sidebar ${mobileSidebarOpen ? "mobile-open" : ""}`}>
        <div className="workspace-head"><span className="app-mark"><Icon name="rook" size={18} /></span><button className="project-switcher" type="button" title={workspace} onClick={() => { setProjectHubOpen(true); setMobileSidebarOpen(false); }}><b>{workspaceName}</b><small>{tr("切换或新建项目", "Switch or create project")}</small></button><span className="project-chevron">⌄</span><button className="mobile-sidebar-close" aria-label={tr("关闭导航", "Close navigation")} onClick={() => setMobileSidebarOpen(false)}>×</button></div>
        <button className="new-thread" onClick={projectSelected ? beginDraft : () => setProjectHubOpen(true)}><Icon name="plus" size={15} /><span>{projectSelected ? tr("新建任务", "New task") : tr("选择或新建项目", "Choose or create project")}</span></button>
        <nav className="workspace-nav" aria-label={tr("工作区工具", "Workspace tools")}>
          <button disabled={!projectSelected} className={drawer === "files" ? "active" : ""} onClick={() => { setInspectorFile(""); setDrawer(drawer === "files" ? null : "files"); setMobileSidebarOpen(false); }}><Icon name="files" size={15} /><span>{tr("文件", "Files")}</span></button>
          <button disabled={!projectSelected} className={drawer === "changes" ? "active" : ""} onClick={() => { setDrawer(drawer === "changes" ? null : "changes"); setMobileSidebarOpen(false); }}><Icon name="changes" size={15} /><span>{tr("变更", "Changes")}</span></button>
          <button className={drawer === "models" ? "active" : ""} onClick={() => { setDrawer(drawer === "models" ? null : "models"); setMobileSidebarOpen(false); }}><Icon name="models" size={15} /><span>{tr("模型", "Models")}</span></button>
          <button className={drawer === "advanced" ? "active" : ""} onClick={() => { setDrawer(drawer === "advanced" ? null : "advanced"); setMobileSidebarOpen(false); }}><Icon name="settings" size={15} /><span>{tr("设置", "Settings")}</span></button>
        </nav>
        <div className="section-title"><span>{tr("最近任务", "Recent tasks")}</span><span>{threads.length}</span></div>
        <nav className="sessions">
          {threads.map((thread) => (
            <button
              className={`session ${thread.id === selectedId ? "selected" : ""}`}
              key={thread.id}
              onClick={() => selectThread(thread.id)}
            >
              <span>{thread.title || tr("未命名任务", "Untitled task")}</span>
              <small><i className={`session-status ${thread.status}`} />{statusLabel(thread.status)} <em>· {displayTime(thread.updated_at)}</em></small>
            </button>
          ))}
          {!threads.length && <p className="empty">{tr("还没有会话。直接在右侧描述任务即可。", "No sessions yet. Describe a task on the right to get started.")}</p>}
        </nav>
        <div className="sidebar-foot"><span className="connection-dot" />{tr("本机 Core 已连接", "Local Core connected")}<small>0.2 beta</small></div>
      </aside>

      <main className="main">
        <header className="topbar">
          <button className="mobile-sidebar-toggle" aria-label={tr("打开导航", "Open navigation")} onClick={() => setMobileSidebarOpen(true)}><Icon name="menu" size={18} /></button>
          <div className="task-identity"><small title={workspace}>{workspaceName}</small><strong>{selectedThread?.title || tr("新任务", "New task")}</strong></div>
          <button className="active-model" type="button" title={tr("切换模型", "Switch model")} onClick={() => setDrawer("models")}><Icon name="models" size={13} />{activeModel}</button>
          <div className="run-state"><span className={activeTurn ? "pulse" : "dot"} />{activeTurn ? phaseLabel(phase) || tr("正在工作", "Working") : tr("就绪", "Ready")}</div>
          <div className="session-menu">
            <button title={tr("重命名", "Rename")} aria-label={tr("重命名", "Rename")} disabled={!selectedId} onClick={() => void sessionAction("rename")}><Icon name="edit" size={16} /></button>
            <button title={tr("Fork 会话", "Fork session")} aria-label={tr("Fork 会话", "Fork session")} disabled={!selectedId} onClick={() => void sessionAction("fork")}><Icon name="fork" size={16} /></button>
            <button title={tr("导出", "Export")} aria-label={tr("导出", "Export")} disabled={!selectedId} onClick={() => void sessionAction("export")}><Icon name="download" size={16} /></button>
            <button className="danger-action" title={tr("删除", "Delete")} aria-label={tr("删除", "Delete")} disabled={!selectedId} onClick={() => void sessionAction("delete")}><Icon name="trash" size={16} /></button>
          </div>
        </header>

        <section className="timeline" ref={timelineRef}>
          {hasOlderTurns && timelineEntries.length > 0 && (
            <button
              type="button"
              className="history-loader"
              disabled={loadingOlderTurns}
              onClick={() => void loadOlderTurns()}
            >{loadingOlderTurns ? tr("正在加载…", "Loading…") : tr("加载更早记录", "Load earlier history")}</button>
          )}
          {!timelineEntries.length && (
            <div className="welcome-card">
              <span className="welcome-kicker">CODEROOK · LOCAL AGENT</span>
              <h1>{projectSelected ? tr("今天想完成什么？", "What would you like to accomplish?") : tr("选择一个项目开始", "Choose a project to get started")}</h1>
              <p>{projectSelected ? tr("描述一个目标。CodeRook 会理解代码、执行修改、运行验证，并留下可审查和可恢复的结果。", "Describe a goal. CodeRook will understand the code, make changes, run checks, and leave a reviewable, recoverable result.") : tr("创建一个新的空白项目，或者打开电脑上已有的项目文件夹。CodeRook 只会把选中的目录作为工作区。", "Create a blank project or open an existing folder on this computer. CodeRook uses only the selected directory as its workspace.")}</p>
              {!projectSelected ? <div className="welcome-project-action"><button className="primary" onClick={() => setProjectHubOpen(true)}><Icon name="plus" size={15} />{tr("选择或新建项目", "Choose or create project")}</button></div> : <div className="suggestions">
                <button onClick={() => setComposer(tr("解释这个仓库的核心架构和数据流", "Explain this repository's core architecture and data flow"))}><span><b>{tr("理解代码库", "Understand the codebase")}</b><small>{tr("梳理架构、模块与关键数据流", "Map the architecture, modules, and key data flows")}</small></span><Icon name="arrow" size={16} /></button>
                <button onClick={() => setComposer(tr("检查当前改动，找出最可能的缺陷", "Review the current changes and identify the most likely defects"))}><span><b>{tr("审查当前改动", "Review current changes")}</b><small>{tr("检查风险、缺陷与验证缺口", "Inspect risks, defects, and verification gaps")}</small></span><Icon name="arrow" size={16} /></button>
                <button onClick={() => setComposer(tr("运行最相关的测试并修复失败", "Run the most relevant tests and fix any failures"))}><span><b>{tr("修复测试失败", "Fix failing tests")}</b><small>{tr("定位问题、修改代码并重新验证", "Locate the issue, edit the code, and verify again")}</small></span><Icon name="arrow" size={16} /></button>
              </div>}
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
              onRetry={(prompt) => { setComposer(prompt); setNotice(tr("重试建议已放入输入框，可修改后发送", "A retry suggestion was placed in the composer. Edit it before sending.")); }}
            />
          ) : entry.kind === "tool_group" ? (
            <ToolActivityGroup
              key={entry.key}
              tools={entry.tools}
              onOpenLocation={(path) => { setInspectorFile(path); setDrawer("files"); }}
              onRetry={(prompt) => { setComposer(prompt); setNotice(tr("重试建议已放入输入框，可修改后发送", "A retry suggestion was placed in the composer. Edit it before sending.")); }}
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
          {error && <div className="error-card"><b>{tr("需要处理", "Action required")}</b><p>{error}</p><button onClick={() => setError("")}>{tr("关闭", "Close")}</button></div>}
        </section>

        {projectSelected && <form className="composer" onSubmit={(event) => void send(event)}>
          {queuedMessages.length > 0 && <div className="message-queue" aria-label={tr("待发送消息", "Queued messages")}>
            {queuedMessages.map((message, index) => <div className={`queued-message ${message.status}`} key={message.id}>
              <span>{message.status === "dispatching" ? tr("正在发送", "Sending") : message.status === "blocked" ? tr("需要处理", "Action required") : tr(`排队 ${index + 1}`, `Queued ${index + 1}`)}</span>
              <p title={message.display_content}>{message.display_content}</p>
              {message.status === "blocked" && <button type="button" onClick={() => void queueAction(message, "retry")}>{tr("重试", "Retry")}</button>}
              {message.status !== "dispatching" && <button type="button" aria-label={tr("移除排队消息", "Remove queued message")} onClick={() => void queueAction(message, "remove")}>×</button>}
              {message.error && <small>{message.error}</small>}
            </div>)}
          </div>}
          {(attachments.length > 0 || fileReferences.length > 0) && <div className="attachment-row">
            {fileReferences.map((path) => <span key={`file:${path}`}>@{path}<button type="button" onClick={() => { setFileReferences((current) => current.filter((item) => item !== path)); setComposer((current) => current.replaceAll(`@${path}`, "").replace(/\s{2,}/g, " ")); }}>×</button></span>)}
            {attachments.map((attachment) => <span key={attachment.sha256}>{attachment.name}<button type="button" onClick={() => setAttachments((current) => current.filter((item) => item.sha256 !== attachment.sha256))}>×</button></span>)}
          </div>}
          {fileSuggestions.length > 0 && <div className="file-mention-menu" role="listbox" aria-label={tr("文件建议", "File suggestions")}>
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
            placeholder={threadLoading ? tr("正在恢复会话…", "Restoring session…") : activeTurn ? tr("输入纠偏消息…", "Steer the active task…") : tr("向 CodeRook 提问或描述任务", "Ask CodeRook or describe a task")}
            rows={1}
          />
          <div className="composer-bar">
            <div className="composer-tools">
              <button type="button" className="context-button" onClick={() => setDrawer("files")} title={tr("添加文件上下文", "Add file context")}><Icon name="plus" size={16} /></button>
              <label className="mode-select"><select aria-label={tr("运行模式", "Run mode")} value={mode} onChange={(event) => setMode(event.target.value as RunMode)}><option value="act">{tr("执行", "Act")}</option><option value="plan">{tr("规划", "Plan")}</option><option value="review">{tr("审查", "Review")}</option></select></label>
              <label className="attach-button" title={tr("添加图片", "Attach images")}><Icon name="image" size={15} /><input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={(event) => { void attachImages(event.target.files); event.target.value = ""; }} /></label>
              {activeTurn && <button type="button" className={`queue-toggle ${queueMode ? "active" : ""}`} onClick={() => setQueueMode((current) => !current)}>{queueMode ? tr(`排队 ${queuedMessages.length}`, `Queue ${queuedMessages.length}`) : tr("纠偏", "Steer")}</button>}
            </div>
            <div className="composer-meta">
              <span>{tr("上下文", "Context")} {tokenUsage ? `${Math.round(tokenUsage / 1000)}k` : "—"}</span>
              {activeTurn && <button type="button" className="stop" onClick={() => void cancel()}><Icon name="stop" size={13} />{tr("停止", "Stop")}</button>}
              <button className="send" aria-label={activeTurn ? tr("发送纠偏", "Send steer") : tr("发送任务", "Send task")} title={activeTurn ? tr("发送纠偏", "Send steer") : tr("发送任务", "Send task")} disabled={!composer.trim() || sending || !sessionsReady || threadLoading}><Icon name="arrowUp" size={16} /></button>
            </div>
          </div>
        </form>}
      </main>

      {mobileSidebarOpen && <button className="sidebar-scrim" aria-label={tr("关闭导航", "Close navigation")} onClick={() => setMobileSidebarOpen(false)} />}

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
      {projectHubOpen && <ProjectHub workspace={workspace} onActivated={onWorkspaceChanged} onClose={() => setProjectHubOpen(false)} onError={setError} />}
    </div>
  );
}

function ProjectHub({
  workspace,
  onActivated,
  onClose,
  onError,
}: {
  workspace: string;
  onActivated(workspace: string): void;
  onClose(): void;
  onError(value: string): void;
}): ReactElement {
  const [catalog, setCatalog] = useState<ProjectCatalog | null>(null);
  const [tab, setTab] = useState<"projects" | "create" | "open">("projects");
  const [name, setName] = useState("");
  const [parent, setParent] = useState("");
  const [listing, setListing] = useState<DirectoryListing | null>(null);
  const [busy, setBusy] = useState(false);
  const [switching, setSwitching] = useState("");

  const loadCatalog = useCallback(async () => {
    const value = await request<ProjectCatalog>("/v1/projects");
    setCatalog(value);
    setParent((current) => current || value.default_projects_root);
  }, []);
  const browse = useCallback(async (path = "") => {
    const value = await request<DirectoryListing>(`/v1/filesystem/directories?path=${encodeURIComponent(path)}`);
    setListing(value);
  }, []);
  useEffect(() => { void loadCatalog().catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason))); }, [loadCatalog, onError]);
  useEffect(() => { if (tab === "open" && !listing) void browse().catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason))); }, [browse, listing, onError, tab]);

  const activate = async (project: Pick<ProjectRecord, "id" | "root">) => {
    setBusy(true);
    setSwitching(project.root);
    try {
      const result = await request<{ workspace: string }>("/v1/projects/activate", {
        method: "POST",
        body: JSON.stringify({ project_id: project.id }),
      });
      if (result.workspace === workspace) onClose();
      else onActivated(result.workspace);
    } catch (reason: unknown) {
      setBusy(false);
      setSwitching("");
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const createProject = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      const project = await request<ProjectRecord>("/v1/projects", {
        method: "POST",
        body: JSON.stringify({ name, parent: parent || undefined }),
      });
      await activate(project);
    } catch (reason: unknown) {
      setBusy(false);
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const openCurrent = async () => {
    if (!listing?.path) return;
    setBusy(true);
    try {
      const project = await request<ProjectRecord>("/v1/projects/open", {
        method: "POST",
        body: JSON.stringify({ path: listing.path }),
      });
      await activate(project);
    } catch (reason: unknown) {
      setBusy(false);
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return <div className="project-hub-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
    <section className="project-hub" role="dialog" aria-modal="true" aria-label={tr("项目", "Projects")}>
      <header><div><span className="app-mark"><Icon name="rook" size={17} /></span><div><small>CODEROOK</small><h2>{tr("项目", "Projects")}</h2></div></div><button type="button" disabled={busy} onClick={onClose}>×</button></header>
      <nav><button className={tab === "projects" ? "active" : ""} onClick={() => setTab("projects")}>{tr("最近项目", "Recent")}</button><button className={tab === "create" ? "active" : ""} onClick={() => setTab("create")}>{tr("新建项目", "New project")}</button><button className={tab === "open" ? "active" : ""} onClick={() => setTab("open")}>{tr("打开文件夹", "Open folder")}</button></nav>
      <div className="project-hub-content">
        {switching && <div className="project-switching"><span className="pulse" />{tr("正在打开项目…", "Opening project…")}<small>{switching}</small></div>}
        {!switching && tab === "projects" && <div className="project-list">
          {catalog?.projects.map((project) => <button type="button" key={project.id} className={project.active ? "active" : ""} disabled={busy || project.active} onClick={() => void activate(project)}><span className="project-avatar">{project.name.slice(0, 1).toUpperCase()}</span><span><b>{project.name}</b><small>{project.root}</small></span>{project.active ? <em>{tr("当前", "Current")}</em> : <Icon name="arrow" size={15} />}</button>)}
          {!catalog?.projects.length && <p className="empty">{tr("还没有项目。创建空白项目或打开电脑上的文件夹。", "No projects yet. Create a blank project or open a folder on this computer.")}</p>}
        </div>}
        {!switching && tab === "create" && <form className="project-create" onSubmit={(event) => void createProject(event)}><h3>{tr("创建空白项目", "Create a blank project")}</h3><p>{tr("CodeRook 会创建一个独立文件夹，并把它作为 Agent 唯一可访问的工作区。", "CodeRook creates an independent folder and uses it as the agent's workspace.")}</p><label>{tr("项目名称", "Project name")}<input value={name} autoFocus placeholder="my-project" onChange={(event) => setName(event.target.value)} /></label><label>{tr("保存位置", "Location")}<input value={parent} onChange={(event) => setParent(event.target.value)} /></label><small>{tr("默认位置", "Default")}: {catalog?.default_projects_root}</small><button className="primary" disabled={busy || !name.trim()}>{tr("创建并打开", "Create and open")}</button></form>}
        {!switching && tab === "open" && <div className="directory-picker"><header><button type="button" disabled={!listing?.parent} onClick={() => void browse(listing?.parent || "")}>←</button><span title={listing?.path || tr("此电脑", "This computer")}>{listing?.path || tr("此电脑", "This computer")}</span>{listing?.path && <button className="primary" type="button" disabled={busy} onClick={() => void openCurrent()}>{tr("选择此文件夹", "Select folder")}</button>}</header><div>{listing?.roots.map((root) => <button type="button" key={root} onClick={() => void browse(root)}><span className="folder-icon">▣</span><b>{root}</b><Icon name="arrow" size={14} /></button>)}{listing?.directories.map((directory) => <button type="button" key={directory.path} onClick={() => void browse(directory.path)}><span className="folder-icon">▰</span><b>{directory.name}</b><Icon name="arrow" size={14} /></button>)}</div></div>}
      </div>
      <footer><span>{tr("当前工作区", "Current workspace")}</span><code title={workspace}>{workspace}</code></footer>
    </section>
  </div>;
}

function TurnItemCard({ item }: { item: TurnItem }): ReactElement {
  const payload = item.payload;
  if (item.kind === "message") {
    const role = textValue(payload.role) === "user" ? "user" : "assistant";
    return (
      <article className={`message-card ${role}`}>
        <div className="message-meta"><b>{role === "user" ? tr("你", "You") : "CodeRook"}</b><time>{displayTime(item.created_at)}</time></div>
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
  const toolName = textValue(resultPayload.tool_name || callPayload.tool_name || presentation.title || tr("工具", "Tool"));
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
  const retryPrompt = tr(
    `请先诊断失败原因，再重试“${toolActionLabel(toolName, params, "succeeded", semanticAction)}”${target ? `（${target}）` : ""}。不要原样重复已经失败的调用。`,
    `Diagnose the failure, then retry “${toolActionLabel(toolName, params, "succeeded", semanticAction)}”${target ? ` (${target})` : ""}. Do not repeat the failed call unchanged.`,
  );
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
  const failureExcerpt = output.split(/\r?\n/).find((line) => line.trim())?.trim().slice(0, 180) || tr("操作未完成", "Operation did not complete");
  const summary = (
    <>
      {["run_command", "run_tests"].includes(semanticAction)
        ? <span className={`tool-kind-icon ${failed ? "failed" : ""}`}><Icon name="terminal" size={13} /></span>
        : <span className="tool-status">{failed ? "×" : running ? "◌" : "✓"}</span>}
      <b>{title}</b>
      {locations[0]
        ? openableLocation
          ? <button className="tool-location" type="button" title={tr(`打开 ${openableLocation}`, `Open ${openableLocation}`)} onClick={(event) => { event.preventDefault(); event.stopPropagation(); onOpenLocation(openableLocation); }}>{openableLocation}</button>
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
            {Object.keys(params).length > 0 && <><small>{tr("输入", "Input")}</small><pre>{textValue(params)}</pre></>}
            {output && <><small>{failed ? tr("错误", "Error") : tr("输出", "Output")}</small><pre>{output}</pre></>}
            {failed && <div className="tool-recovery-actions"><span title={output}>{failureExcerpt}</span><div><button type="button" onClick={() => onRetry(retryPrompt)}>{tr("修改后重试", "Edit and retry")}</button><button type="button" onClick={() => void browserBridge.copyText(output || textValue(params))}>{tr("复制错误", "Copy error")}</button></div></div>}
          </div>
        </details>
      ) : <div className="tool-item-head">{summary}</div>}
      {failed && !hasDetails && <div className="tool-recovery-actions always-visible"><span>{failureExcerpt}</span><div><button type="button" onClick={() => onRetry(retryPrompt)}>{tr("修改后重试", "Edit and retry")}</button></div></div>}
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
  let summary = runningCount ? tr(`正在执行 ${tools.length} 个操作`, `Running ${tools.length} operations`) : tr(`执行了 ${tools.length} 个操作`, `Ran ${tools.length} operations`);
  if ([...actions].every((action) => ["read_file", "browse_files", "search_code", "git"].includes(action))) summary = runningCount ? tr("正在检查工作区", "Inspecting the workspace") : tr("检查了工作区", "Inspected the workspace");
  else if (actions.size === 1 && actions.has("run_command")) summary = runningCount ? tr(`正在运行 ${tools.length} 个命令`, `Running ${tools.length} commands`) : tr(`运行了 ${tools.length} 个命令`, `Ran ${tools.length} commands`);
  else if (actions.has("edit_code") && actions.size === 1) summary = runningCount ? tr("正在修改代码", "Editing code") : tr("修改了代码", "Edited code");
  else if (actions.size === 1 && actions.has("run_tests")) summary = runningCount ? tr("正在运行验证", "Running checks") : tr("运行了验证", "Ran checks");
  return (
    <details className={`tool-activity ${failedCount ? "failed" : ""} ${runningCount ? "running" : ""}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="tool-activity-head">
        {actions.size === 1 && (actions.has("run_command") || actions.has("run_tests"))
          ? <span className="tool-kind-icon"><Icon name="terminal" size={13} /></span>
          : <span className="tool-status">{failedCount ? "×" : runningCount ? "◌" : "✓"}</span>}
        <b>{summary}</b>
        <span className="tool-chevron" />
        {failedCount > 0 && <span className="tool-failed-count">{failedCount} {tr("失败", "failed")}</span>}
        {runningCount > 0 && <span className="tool-running-count">{tr("进行中", "running")}</span>}
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
    return <ResultCard event={event} detail={detail} onOpenChanges={onOpenChanges} />;
  }
  return (
    <article className={`event-card ${event.type.replaceAll(".", "-")}`}>
      <div className="event-icon">{isPermission || isPlan || isQuestion ? "?" : "●"}</div>
      <div className="event-body">
        <div className="event-head"><b>{eventTitle(event)}</b><time>{displayTime(event.ts)}</time></div>
        {detail && <pre>{detail}</pre>}
        {responded && <div className="card-resolved">{tr("已处理", "Resolved")}</div>}
        {!responded && isPermission && toolId && (
          <div className="card-actions">
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_once", session_id: threadId }, tr("已允许本次操作", "Allowed once"))}>{tr("本次允许", "Allow once")}</button>
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_session", session_id: threadId }, tr("本会话已允许", "Allowed for this session"))}>{tr("本会话允许", "Allow for session")}</button>
            <button className="danger" onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "deny_once", session_id: threadId }, tr("已拒绝", "Denied"))}>{tr("拒绝", "Deny")}</button>
          </div>
        )}
        {!responded && isPlan && event.turn_id && (
          <><div className="answer-row"><input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder={tr("可选：说明希望怎样修改计划", "Optional: explain how the plan should change")} /><button disabled={!answer.trim()} onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "revise", revision: answer }, tr("已要求修改计划", "Plan revision requested"))}>{tr("要求修改", "Request changes")}</button></div><div className="card-actions">
              <button onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "approve" }, tr("计划已批准", "Plan approved"))}>{tr("批准计划", "Approve plan")}</button>
              <button className="danger" onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "cancel" }, tr("计划已取消", "Plan cancelled"))}>{tr("取消", "Cancel")}</button>
            </div></>
        )}
        {!responded && isQuestion && questionId && (
          <div className="answer-row">
            <input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder={tr("输入回答", "Enter your answer")} />
            <button disabled={!answer.trim()} onClick={() => void post(`/v1/questions/${questionId}`, { answer }, tr("回答已送达", "Answer sent"))}>{tr("回答", "Answer")}</button>
          </div>
        )}
        {!responded && isRecovery && (
          <div className="card-actions"><button onClick={() => void post(`/v1/threads/${threadId}/turns`, { content: "Continue from the last durable recovery point. Re-check uncertain file or command state before making any modification.", mode: "act" }, tr("已从安全位置继续", "Continuing from the safe recovery point"))}>{tr("从安全位置继续", "Continue safely")}</button><button onClick={onOpenChanges}>{tr("查看中断前变更", "View pre-interruption changes")}</button></div>
        )}
      </div>
    </article>
  );
}

function ResultCard({ event, detail, onOpenChanges }: { event: RuntimeEvent; detail: string; onOpenChanges(): void }): ReactElement {
  const [receipt, setReceipt] = useState<TurnReceipt | null>(null);
  const turnId = event.turn_id || textValue(event.payload.run_id);
  useEffect(() => {
    if (!turnId) return;
    const controller = new AbortController();
    request<TurnReceipt>(`/v1/turns/${encodeURIComponent(turnId)}/receipt`, { signal: controller.signal })
      .then(setReceipt)
      .catch(() => undefined);
    return () => controller.abort();
  }, [turnId]);
  const status = textValue(receipt?.outcome || receipt?.status || event.payload.status || event.payload.outcome || "failed");
  const failed = resultStatusIsFailure(status);
  const changes = receipt?.changes || [];
  const changedFiles = receipt?.files_changed?.length || changes.length;
  const additions = changes.reduce((total, change) => total + Number(change.additions || 0), 0);
  const deletions = changes.reduce((total, change) => total + Number(change.deletions || 0), 0);
  const verification = receipt?.verification || [];
  const verificationFailed = verification.some((item) => ["failed", "error", "timeout"].includes(textValue(item.status).toLowerCase()));
  const model = textValue(receipt?.route?.model);
  const cost = typeof receipt?.cost === "number" ? `$${receipt.cost.toFixed(4)}` : "";
  const summary = textValue(receipt?.result_summary || receipt?.failure_category || detail).trim();
  const copied = [failed ? tr("本轮未完成", "Turn incomplete") : tr("本轮完成", "Turn complete"), summary, changedFiles ? tr(`${changedFiles} 个文件 +${additions}/-${deletions}`, `${changedFiles} files +${additions}/-${deletions}`) : "", verification.length ? tr(`${verification.length} 项验证`, `${verification.length} checks`) : ""].filter(Boolean).join(" · ");
  return (
    <article className={`result-inline ${failed ? "failed" : ""}`}>
      <span>{failed ? tr("本轮未完成", "Turn incomplete") : tr("本轮完成", "Turn complete")}</span>
      {summary && <small>{summary}</small>}
      <div className="result-evidence">
        {changedFiles > 0 && <em>{tr(`${changedFiles} 个文件`, `${changedFiles} files`)} · +{additions} / -{deletions}</em>}
        {verification.length > 0 && <em className={verificationFailed ? "failed" : ""}>{verificationFailed ? tr("验证失败", "Checks failed") : tr(`${verification.length} 项验证通过`, `${verification.length} checks passed`)}</em>}
        {model && <em>{model}{cost ? ` · ${cost}` : ""}</em>}
      </div>
      <div className="result-actions"><button onClick={onOpenChanges}>{tr("查看变更", "View changes")}</button><button onClick={() => void browserBridge.copyText(copied || eventTitle(event))}>{tr("复制结果", "Copy result")}</button></div>
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
      <header><div><span className="panel-eyebrow">INSPECTOR</span><h2>{drawer === "files" ? tr("工作区文件", "Workspace files") : drawer === "changes" ? tr("变更审查", "Change review") : drawer === "models" ? tr("模型与 Provider", "Models and providers") : tr("设置与能力", "Settings and capabilities")}</h2><small title={workspace}>{workspace.split(/[\\/]/).filter(Boolean).pop()}</small></div><button aria-label={tr("关闭检查器", "Close inspector")} onClick={onClose}>×</button></header>
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
      .catch((reason: unknown) => {
        if (workspacePathIsDirectoryError(reason)) {
          setPreview(null);
          setCurrentPath(initialFile);
          setQuery("");
          return;
        }
        onError(reason instanceof Error ? reason.message : String(reason));
      });
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
    {!preview && <div className="file-navigation"><button disabled={currentPath === "."} onClick={() => setCurrentPath(parentWorkspacePath(currentPath))}>←</button><span title={currentPath}>{currentPath === "." ? tr("工作区", "Workspace") : currentPath}</span></div>}
    <input className="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tr("搜索文件…", "Search files…")} />
    {preview ? <div className="file-preview"><div><button onClick={() => setPreview(null)}>← {tr("返回", "Back")}</button><button onClick={() => onReference(preview.path)}>{tr("引用", "Reference")} @</button></div><b>{preview.path}</b><pre>{preview.binary ? tr("二进制文件暂不显示", "Binary file preview is unavailable") : preview.content}</pre></div> : <div className="file-list">{entries.map((entry) => <button key={entry.path} onClick={() => void open(entry)}><span>{entry.kind === "directory" ? "▸" : "·"} {entry.name}</span><small>{entry.size === null ? "" : `${entry.size} B`}</small></button>)}</div>}
  </div>;
}

function ChangesPanel({ threadId, onError }: { threadId: string; onError(value: string): void }): ReactElement {
  const dialog = useProductDialog();
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [context, setContext] = useState<Record<string, unknown>>({});
  const [commitMessage, setCommitMessage] = useState("chore: apply CodeRook changes");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const load = useCallback((signal?: AbortSignal) => {
    setLoading(true);
    setLoadError("");
    Promise.all([
      request<DiffPayload>("/v1/workspace/diff?scope=all", { signal }),
      threadId ? request<Record<string, unknown>>(`/v1/threads/${threadId}/context`, { signal }) : Promise.resolve({}),
    ])
      .then(([nextDiff, nextContext]) => {
        setDiff(nextDiff);
        setContext(nextContext);
        setSelectedPaths((nextDiff.files || []).map((file) => textValue(file.path)).filter(Boolean));
      })
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
  const stageSelected = async () => {
    if (!threadId || !diff?.state_digest) return;
    const paths = selectedPaths.filter(Boolean);
    if (!paths.length) return;
    try {
      const staged = await request<DiffPayload>("/v1/workspace/stage", { method: "POST", body: JSON.stringify({ thread_id: threadId, paths, expected_digest: diff.state_digest, confirmed: true }) });
      setDiff(staged);
      setSelectedPaths([]);
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const commit = async () => {
    if (!threadId || !diff?.state_digest || !commitMessage.trim()) return;
    try {
      const result = await request<{ commit: string }>("/v1/workspace/commit", { method: "POST", body: JSON.stringify({ thread_id: threadId, message: commitMessage, expected_digest: diff.state_digest, confirmed: true }) });
      await dialog({
        title: tr("本地提交已创建", "Local commit created"),
        description: tr(`Commit ${result.commit.slice(0, 12)} 已写入当前仓库，没有自动 push。`, `Commit ${result.commit.slice(0, 12)} was created in the current repository. Nothing was pushed.`),
        confirmLabel: tr("知道了", "Done"),
        cancelLabel: null,
      });
      load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const rewind = async (checkpoint: Record<string, unknown>) => {
    try {
      const id = textValue(checkpoint.checkpoint_id);
      const preview = await request<Record<string, unknown>>(`/v1/threads/${encodeURIComponent(threadId)}/checkpoints/${encodeURIComponent(id)}/preview`);
      if (!await dialog({
        title: tr("恢复 Checkpoint", "Restore checkpoint"),
        description: tr("恢复会改写下列工作区文件，请先确认目标和冲突。", "Restoring will rewrite the workspace files below. Review the target and conflicts first."),
        detail: tr(`文件：${textValue(preview.paths)}\n冲突：${textValue(preview.conflicts || "无")}`, `Files: ${textValue(preview.paths)}\nConflicts: ${textValue(preview.conflicts || "none")}`),
        confirmLabel: tr("确认恢复", "Restore checkpoint"),
        danger: true,
      })) return;
      await request(`/v1/threads/${encodeURIComponent(threadId)}/checkpoints/${encodeURIComponent(id)}/rewind`, { method: "POST", body: JSON.stringify({ confirmed: true, expected_digest: preview.state_digest, run_id: context.checkpoint_run_id }) });
      load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="panel-content">
    <div className="panel-toolbar"><span>{loading ? tr("正在读取…", "Loading…") : tr(`${files.length} 个变更文件`, `${files.length} changed files`)}</span><button disabled={loading} onClick={() => load()}>{tr("刷新", "Refresh")}</button><button disabled={!selectedPaths.length || !threadId || loading} onClick={() => void stageSelected()}>{tr("Stage 选中", "Stage selected")}{selectedPaths.length ? ` (${selectedPaths.length})` : ""}</button></div>
    {loadError && <div className="panel-error"><b>{tr("无法读取变更", "Unable to load changes")}</b><p>{loadError}</p><button onClick={() => load()}>{tr("重试", "Retry")}</button></div>}
    {!loadError && !files.length && !loading ? <p className="empty">{tr("工作区没有未提交变更。", "The workspace has no uncommitted changes.")}</p> : files.map((file, index) => {
      const path = textValue(file.path);
      const checked = selectedPaths.includes(path);
      return <details className="diff-file" key={`${path}-${index}`} open={index === 0}><summary><label className="diff-select" onClick={(event) => event.stopPropagation()}><input type="checkbox" checked={checked} onChange={() => setSelectedPaths((current) => checked ? current.filter((item) => item !== path) : [...current, path])} /><span className="sr-only">{tr("选择", "Select")} {path}</span></label><b>{path}</b><span>+{textValue(file.additions || 0)} / -{textValue(file.deletions || 0)}</span></summary><pre>{textValue(file.patch || file.diff || file)}</pre></details>;
    })}
    {textValue(diff?.scope) === "staged" && <div className="commit-row"><input value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} /><button onClick={() => void commit()}>{tr("创建本地 Commit", "Create local commit")}</button><small>{tr("不会自动 push，也不会运行仓库 hooks。", "Nothing will be pushed and repository hooks will not run.")}</small></div>}
    {checkpoints.length > 0 && <div className="checkpoints"><h3>{tr("恢复点", "Checkpoints")}</h3>{checkpoints.map((checkpoint) => <button key={textValue(checkpoint.checkpoint_id)} onClick={() => void rewind(checkpoint)}><span>{textValue(checkpoint.label || checkpoint.checkpoint_id)}</span><small>{statusLabel(textValue(checkpoint.status))}</small></button>)}</div>}
  </div>;
}

function ModelsPanel({ onError }: { onError(value: string): void }): ReactElement {
  const dialog = useProductDialog();
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
        if (!await dialog({
          title: tr("删除模型路由", "Delete model route"),
          description: tr(`将删除路由“${routeId}”及其由 CodeRook 管理的凭据。环境变量中的密钥不会被修改。`, `This deletes route “${routeId}” and credentials managed by CodeRook. Environment variables will not be changed.`),
          confirmLabel: tr("删除路由", "Delete route"),
          danger: true,
        })) return;
        await request(`/v1/providers/${encodeURIComponent(routeId)}`, { method: "DELETE", body: JSON.stringify({ confirmed: true, delete_credential: true }) });
      } else {
        await request(`/v1/providers/${encodeURIComponent(routeId)}/activate`, { method: "POST", body: "{}" });
      }
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  return <div className="panel-content">
    <div className={`readiness ${catalog?.readiness.local_ready ? "ready" : "warning"}`}><b>{catalog?.readiness.local_ready ? tr("模型已就绪", "Model ready") : tr("需要配置模型", "Model setup required")}</b><p>{readinessReason(catalog?.readiness.reason)}</p></div>
    {validationError && <div className="panel-error provider-validation-error"><b>{tr("模型验证未通过", "Model verification failed")}</b><p>{validationError}</p><button onClick={() => setValidationError("")}>{tr("知道了", "Dismiss")}</button></div>}
    <form className="provider-form" onSubmit={(event) => void save(event)}>
      <label>Provider<select value={presetId} onChange={(event) => selectPreset(event.target.value)}>{catalog?.presets.map((item) => <option key={item.id} value={item.id}>{item.name}{item.local ? tr(" · 本地", " · Local") : ""}</option>)}</select></label>
      <label>{tr("模型", "Model")}<input value={model} onChange={(event) => { setModel(event.target.value); setValidationError(""); }} list="provider-models" /></label>
      <datalist id="provider-models">{preset?.models.map((item) => <option key={item} value={item} />)}</datalist>
      {preset?.credential_required && <label>API Key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => { setApiKey(event.target.value); setValidationError(""); }} placeholder={tr("只发送到本地 Core，不写入浏览器", "Sent only to the local Core and never stored in the browser")} /></label>}
      <div className="capability-tags">{preset && Object.entries(preset.capabilities).filter(([, enabled]) => enabled).map(([name]) => <span key={name}>{name}</span>)}</div>
      <button className="primary" disabled={!model || saving}>{saving ? tr("正在验证…", "Verifying…") : tr("Doctor 验证并启用", "Verify with Doctor and activate")}</button>
    </form>
    <h3>{tr("已配置路由", "Configured routes")}</h3>
    {catalog?.routes.map((route) => {
      const routeId = textValue(route.id);
      const active = catalog.active_route_id === route.id;
      return <div className="route-row" key={routeId}><div><b>{routeId}</b><small>{textValue(route.model)}</small></div><div className="route-actions"><span>{active ? tr("当前", "Active") : textValue(route.credential_source)}</span>{!active && <button onClick={() => void routeAction(routeId, "activate")}>{tr("启用", "Activate")}</button>}<button onClick={() => void routeAction(routeId, "delete")}>{tr("删除", "Delete")}</button></div></div>;
    })}
  </div>;
}

function AdvancedPanel({ threadId, onError }: { threadId: string; onError(value: string): void }): ReactElement {
  const dialog = useProductDialog();
  const preferences = useInterfacePreferences();
  const [tab, setTab] = useState<"interface" | "goals" | "workers" | "skills" | "mcp" | "memory">("interface");
  const [data, setData] = useState<Record<string, unknown>>({});
  const [objective, setObjective] = useState("");
  const [memoryBody, setMemoryBody] = useState("");
  const [skillSource, setSkillSource] = useState("");
  const endpoint = tab === "interface" ? "" : tab === "goals" ? `/v1/goals?thread_id=${encodeURIComponent(threadId)}` : tab === "workers" ? `/v1/workers?thread_id=${encodeURIComponent(threadId)}` : tab === "skills" ? "/v1/skills" : tab === "mcp" ? "/v1/mcp" : "/v1/memories";
  const load = useCallback(() => {
    if (tab === "interface") { setData({}); return Promise.resolve(); }
    if ((tab === "goals" || tab === "workers") && !threadId) { setData({}); return Promise.resolve(); }
    return request<Record<string, unknown>>(endpoint).then(setData).catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason)));
  }, [endpoint, onError, tab, threadId]);
  useEffect(() => { void load(); }, [load]);
  const mutate = async (path: string, payload: Record<string, unknown>): Promise<boolean> => {
    try { await request(path, { method: "POST", body: JSON.stringify(payload) }); await load(); return true; }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); return false; }
  };
  const goals = (data.goals || []) as Array<Record<string, unknown>>;
  const workers = (data.workers || []) as Array<Record<string, unknown>>;
  const skills = (data.skills || []) as Array<Record<string, unknown>>;
  const servers = (data.servers || []) as Array<Record<string, unknown>>;
  const memories = (data.memories || []) as Array<Record<string, unknown>>;
  const memorySettings = (data.settings || {}) as Record<string, unknown>;
  const workerFollowup = async (workerId: string) => {
    const message = await dialog({
      title: tr("向 Worker 发送后续指令", "Send follow-up to worker"),
      description: tr("指令只会发送给当前会话中的这个 Worker。", "This instruction is sent only to this worker in the current session."),
      input: "multiline",
      placeholder: tr("说明下一步要调查、修改或验证什么", "Describe what to investigate, change, or verify next"),
      confirmLabel: tr("发送指令", "Send instruction"),
    });
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
      const summary = files.length ? files.join("\n") : tr("没有可应用的文件", "No files are available to apply");
      if (!digest || !await dialog({
        title: tr("审查 Worker 变更", "Review worker changes"),
        description: tr("确认这些文件属于 Worker 的 Write Claim，且验证证据满足任务要求。", "Confirm these files are within the worker's write claim and the verification evidence satisfies the task."),
        detail: summary,
        confirmLabel: tr("审查通过", "Approve review"),
      })) return;
      await request(`/v1/workers/${encodeURIComponent(workerId)}/review`, {
        method: "POST",
        body: JSON.stringify({
          session_id: threadId,
          approved: true,
          confirmed: true,
          expected_digest: digest,
        }),
      });
      if (!await dialog({
        title: tr("应用到主工作区", "Apply to main workspace"),
        description: tr("审查已通过。下一步会把 Worker Worktree 中的变更应用到当前工作区，但不会自动提交或 push。", "Review passed. The worker worktree changes will be applied to the current workspace without committing or pushing."),
        confirmLabel: tr("应用变更", "Apply changes"),
      })) { await load(); return; }
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
      if (!previewResult.confirmation_required) throw new Error(tr("Skill 安装预览缺少确认门禁", "The skill installation preview is missing its confirmation gate"));
      if (!await dialog({
        title: tr(`安装 Skill：${textValue(preview.name)}`, `Install skill: ${textValue(preview.name)}`),
        description: tr(`Digest：${textValue(preview.digest)}。安装范围仅限当前项目。`, `Digest: ${textValue(preview.digest)}. The installation is limited to this project.`),
        detail: files || tr("预览没有包含文件", "The preview contains no files"),
        confirmLabel: tr("确认安装", "Install skill"),
      })) return;
      await request("/v1/skills/install", {
        method: "POST",
        body: JSON.stringify({ source, scope: "project", trust: "untrusted", confirmed: true, overwrite: Boolean(preview.overwrite) }),
      });
      setSkillSource("");
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const editMemory = async (memory: Record<string, unknown>) => {
    const body = await dialog({
      title: tr("编辑项目记忆", "Edit project memory"),
      description: tr("该内容会在后续相关任务中参与本地记忆检索。", "This content may be retrieved locally in related future tasks."),
      input: "multiline",
      initialValue: textValue(memory.body),
      placeholder: tr("输入需要保留的项目事实", "Enter a project fact to preserve"),
      confirmLabel: tr("保存记忆", "Save memory"),
    });
    if (!body?.trim() || body.trim() === textValue(memory.body).trim()) return;
    try {
      await request(`/v1/memories/${encodeURIComponent(textValue(memory.id))}`, {
        method: "PATCH",
        body: JSON.stringify({ body: body.trim() }),
      });
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const deleteMemory = async (memory: Record<string, unknown>) => {
    if (!await dialog({
      title: tr("删除项目记忆", "Delete project memory"),
      description: tr(`将永久删除“${textValue(memory.name)}”，后续任务不再检索这条内容。`, `This permanently deletes “${textValue(memory.name)}” so future tasks cannot retrieve it.`),
      confirmLabel: tr("删除记忆", "Delete memory"),
      danger: true,
    })) return;
    try {
      await request(`/v1/memories/${encodeURIComponent(textValue(memory.id))}`, { method: "DELETE", body: JSON.stringify({ confirmed: true }) });
      await load();
    } catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const cancelWorker = async (workerId: string) => {
    if (!await dialog({
      title: tr("取消 Worker", "Cancel worker"),
      description: tr("将停止这个 Worker 及其受管进程；已经产生的 Worktree 变更仍可检查。", "This stops the worker and its managed processes. Existing worktree changes remain available for review."),
      confirmLabel: tr("停止 Worker", "Stop worker"),
      danger: true,
    })) return;
    await mutate(`/v1/workers/${encodeURIComponent(workerId)}/cancel`, { session_id: threadId });
  };
  const clearGoal = async (goal: Record<string, unknown>) => {
    if (!await dialog({
      title: tr("取消 Goal", "Cancel goal"),
      description: tr(`将停止“${textValue(goal.objective)}”的后续自动轮次，已有会话和代码变更会保留。`, `This stops future automatic turns for “${textValue(goal.objective)}”. Existing sessions and code changes are preserved.`),
      confirmLabel: tr("取消 Goal", "Cancel goal"),
      danger: true,
    })) return;
    await mutate(`/v1/goals/${encodeURIComponent(textValue(goal.id))}/clear`, {});
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
  const tabLabels = {
    interface: tr("界面", "Interface"), goals: "Goals", workers: "Workers", skills: "Skills", mcp: "MCP", memory: tr("记忆", "Memory"),
  };
  return <div className="panel-content"><div className="advanced-tabs">{(["interface", "goals", "workers", "skills", "mcp", "memory"] as const).map((name) => <button className={tab === name ? "active" : ""} key={name} onClick={() => setTab(name)}>{tabLabels[name]}</button>)}</div>
    {tab === "interface" && <div className="advanced-list interface-preferences">
      <section><div><b>{tr("界面语言", "Interface language")}</b><span className="stable">{preferences.locale}</span></div><p>{tr("仅改变 CodeRook Web 的界面文案；模型回答、日志和代码内容保持原文。", "Changes only CodeRook Web labels. Model responses, logs, and code remain unchanged.")}</p><select aria-label={tr("界面语言", "Interface language")} value={preferences.locale} onChange={(event) => preferences.setLocale(event.target.value as WebLocale)}><option value="zh-CN">简体中文</option><option value="en-US">English</option></select></section>
      <section><div><b>{tr("显示对比度", "Display contrast")}</b><span className="stable">{preferences.theme === "light" ? tr("浅色", "Light") : tr("高对比", "High contrast")}</span></div><p>{tr("默认保持浅色产品界面；高对比模式加强文字、边框和焦点可见性。", "The default stays light. High contrast strengthens text, borders, and focus visibility.")}</p><select aria-label={tr("显示对比度", "Display contrast")} value={preferences.theme} onChange={(event) => preferences.setTheme(event.target.value as WebTheme)}><option value="light">{tr("浅色", "Light")}</option><option value="high-contrast">{tr("高对比", "High contrast")}</option></select></section>
    </div>}
    {tab === "goals" && <div className="advanced-list">
      <form className="inline-create" onSubmit={async (event) => { event.preventDefault(); if (await mutate("/v1/goals", { session_id: threadId, objective, start: false })) setObjective(""); }}><input value={objective} onChange={(event) => setObjective(event.target.value)} placeholder={tr("创建有界长任务 Goal", "Create a bounded long-running goal")} /><button disabled={!threadId || !objective.trim()}>{tr("创建", "Create")}</button></form>
      {goals.map((goal) => { const status = textValue(goal.status); const nonterminal = ["active", "paused", "blocked"].includes(status); return <section key={textValue(goal.id)}><div><b>{textValue(goal.objective)}</b><span className="stable">{statusLabel(status)}</span></div><p>{tr("轮次", "Turns")} {textValue(goal.auto_turns_used || 0)} / {textValue(goal.max_auto_turns || 3)} · Token {textValue(goal.tokens_used || 0)} / {textValue(goal.token_budget || "∞")}</p><div className="card-actions">{status === "active" ? <button onClick={() => void mutate(`/v1/goals/${goal.id}/pause`, {})}>{tr("暂停", "Pause")}</button> : <button onClick={() => void mutate(`/v1/goals/${goal.id}/resume`, {})}>{nonterminal ? tr("恢复", "Resume") : tr("重新开启", "Restart")}</button>}{nonterminal && <button className="danger" onClick={() => void clearGoal(goal)}>{tr("取消", "Cancel")}</button>}</div></section>; })}
    </div>}
    {tab === "workers" && <div className="advanced-list">
      {workers.length === 0 && <p className="empty">{tr("当前会话没有 Worker。符合独立验收和 Write Claim 条件时，Agent 才会委派。", "This session has no workers. CodeRook delegates only when tasks have independent acceptance criteria and non-overlapping write claims.")}</p>}
      {workers.map((worker) => { const workerId = textValue(worker.worker_id || worker.id); const status = textValue(worker.status); return <section key={workerId}><div><b>{textValue(worker.description || workerId)}</b><span className="stable">{statusLabel(status)}</span></div><p>{textValue(worker.model)} · {textValue(worker.backend || "builtin")} · {worker.read_only ? tr("只读", "Read-only") : tr("独立 Worktree", "Isolated worktree")}</p><div className="card-actions">{["queued", "running", "waiting"].includes(status) && <><button onClick={() => void workerFollowup(workerId)}>{tr("跟进", "Follow up")}</button><button onClick={() => void cancelWorker(workerId)}>{tr("取消 Worker", "Cancel worker")}</button></>}{status === "completed" && !worker.read_only && worker.handoff_status !== "applied" && <button onClick={() => void workerReviewApply(workerId)}>{tr("审查并应用", "Review and apply")}</button>}</div></section>; })}
    </div>}
    {tab === "skills" && <div className="advanced-list"><form className="inline-create" onSubmit={(event) => void installSkill(event)}><input value={skillSource} onChange={(event) => setSkillSource(event.target.value)} placeholder={tr("工作区内 Skill 文件或目录", "Skill file or directory inside the workspace")} /><button disabled={!skillSource.trim()}>{tr("预览安装", "Preview installation")}</button></form>{skills.map((skill) => <section key={textValue(skill.name)}><div><b>{textValue(skill.name)}</b><span className="stable">{textValue(skill.trust)}</span></div><p>{textValue(skill.description)}</p><small>{textValue(skill.scope)} · {textValue(skill.integrity)}</small></section>)}{!skills.length && <p className="empty">{tr("暂无 Skill。安装必须先预览文件与 digest，再明确确认。", "No skills installed. Installation requires a file and digest preview followed by explicit confirmation.")}</p>}</div>}
    {tab === "mcp" && <div className="advanced-list">{servers.map((server) => <section key={textValue(server.name)}><div><b>{textValue(server.name)}</b><span className={server.status === "connected" ? "stable" : "labs"}>{statusLabel(textValue(server.status))}</span></div><p>{textValue(server.transport)} · {textValue(server.tool_count)} tools</p>{server.error ? <small>{textValue(server.error)}</small> : null}</section>)}{!servers.length && <p className="empty">{tr("没有配置 MCP Tool Server。", "No MCP tool servers are configured.")}</p>}</div>}
    {tab === "memory" && <div className="advanced-list"><div className="memory-settings"><span>{tr("自动记忆", "Automatic memory")}: {textValue(memorySettings.auto_save) === "off" ? tr("已关闭", "Off") : tr("保存前询问", "Ask before saving")}</span><button onClick={() => void toggleMemoryAuto()}>{textValue(memorySettings.auto_save) === "off" ? tr("开启询问", "Enable prompts") : tr("关闭", "Turn off")}</button></div><form className="inline-create" onSubmit={async (event) => { event.preventDefault(); if (await mutate("/v1/memories", { name: memoryBody.slice(0, 40), body: memoryBody, memory_type: "project", source_session_id: threadId })) setMemoryBody(""); }}><input value={memoryBody} onChange={(event) => setMemoryBody(event.target.value)} placeholder={tr("添加项目记忆", "Add project memory")} /><button disabled={!memoryBody.trim()}>{tr("添加", "Add")}</button></form>{memories.map((memory) => <section key={textValue(memory.id)}><div><b>{textValue(memory.name)}</b><span className="stable">{memory.pinned ? "pinned" : textValue(memory.type)}</span></div><p>{textValue(memory.body)}</p><div className="card-actions"><button onClick={() => void editMemory(memory)}>{tr("编辑", "Edit")}</button><button className="danger" onClick={() => void deleteMemory(memory)}>{tr("删除", "Delete")}</button></div></section>)}</div>}
    <div className="labs-note"><b>{tr("Labs 已隐藏", "Labs are hidden")}</b><p>{tr("Fleet、Workflow、ACP、Hooks 和 Tool Program 不进入默认 Web 导航。", "Fleet, Workflow, ACP, Hooks, and Tool Program are not shown in the default Web navigation.")}</p></div>
  </div>;
}

function AppContent(): ReactElement {
  const [ready, setReady] = useState(false);
  const [fatal, setFatal] = useState("");
  const [workspace, setWorkspace] = useState("");
  useEffect(() => {
    bootstrap().then((result) => { setWorkspace(result.workspace); setReady(true); }).catch((reason: unknown) => setFatal(reason instanceof Error ? reason.message : String(reason)));
  }, []);
  if (fatal) return <div className="fatal"><span>♜</span><h1>{tr("无法连接本地 CodeRook Core", "Unable to connect to the local CodeRook Core")}</h1><p>{fatal}</p><p>{tr("请刷新页面；如果 Core 未运行，再执行", "Refresh the page. If Core is not running, run")} <code>coderook web</code>。</p></div>;
  if (!ready) return <div className="loading"><span>♜</span><p>{tr("正在连接本地工作区…", "Connecting to the local workspace…")}</p></div>;
  return <ProductDialogProvider><AppShell key={workspace} initialWorkspace={workspace} onWorkspaceChanged={setWorkspace} /></ProductDialogProvider>;
}

export function App(): ReactElement {
  return <InterfacePreferencesProvider><AppContent /></InterfacePreferencesProvider>;
}
