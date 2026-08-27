import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactElement } from "react";
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
    "turn.finished": "本轮结束",
  };
  return names[event.type] || event.type.replaceAll(".", " · ");
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
  const [attachments, setAttachments] = useState<ImageAttachment[]>([]);
  const [queueMode, setQueueMode] = useState(false);
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);
  const cursors = useRef<Record<string, number>>({});
  const selectedThread = threads.find((thread) => thread.id === selectedId);
  const activeTurn = [...turns].reverse().find((turn) =>
    ["running", "waiting_permission", "waiting_input"].includes(turn.status),
  );

  const refreshThreads = useCallback(async () => {
    const result = await request<ThreadRecord[]>("/v1/threads");
    result.sort((left, right) => right.updated_at.localeCompare(left.updated_at));
    setThreads(result);
    setSelectedId((current) => current || result[0]?.id || "");
  }, []);

  useEffect(() => {
    void refreshThreads()
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [refreshThreads]);

  useEffect(() => {
    if (!selectedId) {
      setTurns([]);
      setItems([]);
      setEvents([]);
      return;
    }
    setError("");
    setEvents([]);
    Promise.all([
      request<TurnRecord[]>(`/v1/threads/${encodeURIComponent(selectedId)}/turns`),
      request<{ estimated_tokens?: number }>(
        `/v1/threads/${encodeURIComponent(selectedId)}/context`,
      ),
    ])
      .then(async ([loadedTurns]) => {
        setTurns(loadedTurns);
        const loadedItems = await Promise.all(
          loadedTurns.map((turn) =>
            request<TurnItem[]>(`/v1/turns/${encodeURIComponent(turn.id)}/items`),
          ),
        );
        setItems(loadedItems.flat());
      })
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason)));
  }, [selectedId]);

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
              cursors.current[selectedId] = Math.max(cursors.current[selectedId] || 0, event.seq);
              setEvents((current) =>
                current.some((item) => item.seq === event.seq) ? current : [...current, event],
              );
              if (event.type === "run.phase_changed") {
                setPhase(textValue(event.payload.phase) || "working");
              }
              if (["turn.finished", "run.outcome"].includes(event.type)) {
                void refreshThreads();
                if (event.turn_id) {
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
  }, [refreshThreads, selectedId]);

  const createThread = useCallback(async (): Promise<string> => {
    const created = await request<ThreadRecord>("/v1/threads", {
      method: "POST",
      body: JSON.stringify({ title: "新任务", mode: "chat" }),
    });
    setThreads((current) => [created, ...current]);
    setSelectedId(created.id);
    return created.id;
  }, []);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const content = composer.trim();
    if (!content || sending) return;
    setSending(true);
    setError("");
    try {
      const threadId = selectedId || (await createThread());
      if (activeTurn) {
        if (queueMode) {
          setQueuedMessages((current) => [...current, content]);
          setComposer("");
          setNotice("消息已加入队列，将在当前任务结束后发送");
          return;
        }
        await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/steer`, {
          method: "POST",
          body: JSON.stringify({ content }),
        });
        setNotice("纠偏消息已送达当前任务");
      } else {
        const submitted = content.startsWith("!") && content.length > 1
          ? `The user explicitly requested this exact shell command. Run it through the normal permission and sandbox tool pipeline, then report its exit status and important output without changing the command: ${content.slice(1).trim()}`
          : content;
        const started = await request<TurnRecord>(
          `/v1/threads/${encodeURIComponent(threadId)}/turns`,
          {
            method: "POST",
            body: JSON.stringify({
              content: submitted,
              mode,
              attachments: attachments.map(({ name: _name, ...attachment }) => attachment),
            }),
          },
        );
        setTurns((current) => [...current, started]);
      }
      setComposer("");
      setAttachments([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSending(false);
    }
  };

  useEffect(() => {
    if (activeTurn || !selectedId || queuedMessages.length === 0 || sending) return;
    const next = queuedMessages[0];
    setSending(true);
    request<TurnRecord>(`/v1/threads/${encodeURIComponent(selectedId)}/turns`, {
      method: "POST",
      body: JSON.stringify({ content: next, mode }),
    }).then((started) => {
      setTurns((current) => [...current, started]);
      setQueuedMessages((current) => current.slice(1));
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : String(reason))).finally(() => setSending(false));
  }, [activeTurn, mode, queuedMessages, selectedId, sending]);

  const cancel = async () => {
    if (!activeTurn) return;
    await request(`/v1/turns/${encodeURIComponent(activeTurn.id)}/interrupt`, {
      method: "POST",
      body: "{}",
    });
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
      setSelectedId(forked.id);
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
    setSelectedId(threads.find((thread) => thread.id !== selectedId)?.id || "");
  };

  const visibleEvents = useMemo(
    () => events.filter((event) => !["llm.chunk", "runtime.event_appended"].includes(event.type)),
    [events],
  );
  const tokenUsage = turns.reduce(
    (total, turn) => total + Number(turn.usage.input_tokens || 0) + Number(turn.usage.output_tokens || 0),
    0,
  );

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="rook">♜</span><div><b>CodeRook</b><small>local coding agent</small></div></div>
        <button className="new-thread" onClick={() => void createThread()}>＋ 新任务</button>
        <div className="section-title"><span>会话</span><span>{threads.length}</span></div>
        <nav className="sessions">
          {threads.map((thread) => (
            <button
              className={`session ${thread.id === selectedId ? "selected" : ""}`}
              key={thread.id}
              onClick={() => setSelectedId(thread.id)}
            >
              <span>{thread.title || "未命名任务"}</span>
              <small>{thread.status} · {displayTime(thread.updated_at)}</small>
            </button>
          ))}
          {!threads.length && <p className="empty">还没有会话。直接在右侧描述任务即可。</p>}
        </nav>
        <div className="side-actions">
          <button onClick={() => setDrawer("files")}>文件</button>
          <button onClick={() => setDrawer("changes")}>变更</button>
          <button onClick={() => setDrawer("models")}>模型</button>
          <button onClick={() => setDrawer("advanced")}>高级</button>
        </div>
      </aside>

      <main className="main">
        <header className="topbar">
          <div><strong>{selectedThread?.title || "欢迎使用 CodeRook"}</strong><small title={workspace}>{workspace.split(/[\\/]/).pop()}</small></div>
          <div className="run-state"><span className={activeTurn ? "pulse" : "dot"} />{activeTurn ? phaseLabels[phase] || "正在工作" : "就绪"}</div>
          <div className="session-menu">
            <button disabled={!selectedId} onClick={() => void sessionAction("rename")}>重命名</button>
            <button disabled={!selectedId} onClick={() => void sessionAction("fork")}>Fork</button>
            <button disabled={!selectedId} onClick={() => void sessionAction("export")}>导出</button>
            <button disabled={!selectedId} onClick={() => void sessionAction("delete")}>删除</button>
          </div>
        </header>

        <section className="timeline">
          {!visibleEvents.length && !items.length && (
            <div className="welcome-card">
              <span className="welcome-icon">♜</span>
              <h1>把代码任务交给我</h1>
              <p>我会先理解仓库，再按权限执行修改、验证结果，并把 Diff 和恢复入口交给你。</p>
              <div className="suggestions">
                <button onClick={() => setComposer("解释这个仓库的核心架构和数据流")}>理解当前项目</button>
                <button onClick={() => setComposer("检查当前改动，找出最可能的缺陷")}>审查当前改动</button>
                <button onClick={() => setComposer("运行最相关的测试并修复失败")}>修复测试失败</button>
              </div>
            </div>
          )}
          {visibleEvents.map((event) => (
            <EventCard
              key={event.seq}
              event={event}
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
          {attachments.length > 0 && <div className="attachment-row">{attachments.map((attachment) => <span key={attachment.sha256}>{attachment.name}<button type="button" onClick={() => setAttachments((current) => current.filter((item) => item.sha256 !== attachment.sha256))}>×</button></span>)}</div>}
          <textarea
            value={composer}
            onChange={(event) => setComposer(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder={activeTurn ? "输入纠偏消息，Enter 立即送达…" : "描述任务，@ 引用文件，! 运行命令…"}
            rows={3}
          />
          <div className="composer-bar">
            <div className="mode-switch">
              {(["act", "plan", "review"] as RunMode[]).map((value) => (
                <button type="button" className={mode === value ? "active" : ""} onClick={() => setMode(value)} key={value}>{value.toUpperCase()}</button>
              ))}
              <button type="button" onClick={() => setDrawer("files")}>@ 文件</button>
              <label className="attach-button">图片<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple onChange={(event) => { void attachImages(event.target.files); event.target.value = ""; }} /></label>
              {activeTurn && <button type="button" className={queueMode ? "active" : ""} onClick={() => setQueueMode((current) => !current)}>{queueMode ? `QUEUE ${queuedMessages.length}` : "STEER"}</button>}
            </div>
            <div className="composer-meta">
              <span>ASK</span><span>ctx {tokenUsage ? `${Math.round(tokenUsage / 1000)}k` : "—"}</span>
              {activeTurn && <button type="button" className="stop" onClick={() => void cancel()}>停止</button>}
              <button className="send" disabled={!composer.trim() || sending}>{activeTurn ? "纠偏" : "发送"} ↑</button>
            </div>
          </div>
        </form>
      </main>

      {drawer && (
        <DrawerPanel
          drawer={drawer}
          threadId={selectedId}
          workspace={workspace}
          onClose={() => setDrawer(null)}
          onReference={(path) => {
            setComposer((current) => `${current}${current ? " " : ""}@${path} `);
            setDrawer(null);
          }}
          onError={setError}
        />
      )}
    </div>
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
  const isResult = ["run.outcome", "turn.finished"].includes(event.type);
  const isRecovery = event.type === "recovery.available";
  const [answer, setAnswer] = useState("");
  const post = async (path: string, payload: Record<string, unknown>, success: string) => {
    try {
      await request(path, { method: "POST", body: JSON.stringify(payload) });
      onNotice(success);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  const toolId = textValue(event.payload.tool_use_id || event.payload.permission_id);
  const questionId = textValue(event.payload.question_id);
  return (
    <article className={`event-card ${event.type.replaceAll(".", "-")}`}>
      <div className="event-icon">{isResult ? "✓" : isPermission || isPlan || isQuestion ? "?" : "●"}</div>
      <div className="event-body">
        <div className="event-head"><b>{eventTitle(event)}</b><time>{displayTime(event.ts)}</time></div>
        {detail && <pre>{detail}</pre>}
        {isPermission && toolId && (
          <div className="card-actions">
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_once" }, "已允许本次操作")}>本次允许</button>
            <button onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "allow_session" }, "本会话已允许")}>本会话允许</button>
            <button className="danger" onClick={() => void post(`/v1/permissions/${toolId}`, { decision: "deny_once" }, "已拒绝")}>拒绝</button>
          </div>
        )}
        {isPlan && event.turn_id && (
          <><div className="answer-row"><input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder="可选：说明希望怎样修改计划" /><button disabled={!answer.trim()} onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "revise", revision: answer }, "已要求修改计划")}>要求修改</button></div><div className="card-actions">
              <button onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "approve" }, "计划已批准")}>批准计划</button>
              <button className="danger" onClick={() => void post(`/v1/threads/${threadId}/turns/${event.turn_id}/plan`, { decision: "cancel" }, "计划已取消")}>取消</button>
            </div></>
        )}
        {isQuestion && questionId && (
          <div className="answer-row">
            <input value={answer} onChange={(input) => setAnswer(input.target.value)} placeholder="输入回答" />
            <button onClick={() => void post(`/v1/questions/${questionId}`, { answer }, "回答已送达")}>回答</button>
          </div>
        )}
        {isResult && (
          <div className="card-actions"><button onClick={onOpenChanges}>查看变更</button><button onClick={() => void browserBridge.copyText(detail || eventTitle(event))}>复制结果</button></div>
        )}
        {isRecovery && (
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
  onClose,
  onReference,
  onError,
}: {
  drawer: Exclude<Drawer, null>;
  threadId: string;
  workspace: string;
  onClose(): void;
  onReference(path: string): void;
  onError(value: string): void;
}): ReactElement {
  return (
    <aside className="drawer">
      <header><div><small>{workspace}</small><h2>{drawer === "files" ? "工作区文件" : drawer === "changes" ? "Change Center" : drawer === "models" ? "模型与 Provider" : "高级能力"}</h2></div><button onClick={onClose}>×</button></header>
      {drawer === "files" && <FilesPanel onReference={onReference} onError={onError} />}
      {drawer === "changes" && <ChangesPanel threadId={threadId} onError={onError} />}
      {drawer === "models" && <ModelsPanel onError={onError} />}
      {drawer === "advanced" && <AdvancedPanel threadId={threadId} onError={onError} />}
    </aside>
  );
}

function FilesPanel({ onReference, onError }: { onReference(path: string): void; onError(value: string): void }): ReactElement {
  const [entries, setEntries] = useState<WorkspaceEntry[]>([]);
  const [query, setQuery] = useState("");
  const [preview, setPreview] = useState<{ path: string; content: string; binary: boolean } | null>(null);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      request<{ entries: WorkspaceEntry[] }>(`/v1/workspace/files?query=${encodeURIComponent(query)}`)
        .then((result) => setEntries(result.entries))
        .catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason)));
    }, 150);
    return () => window.clearTimeout(timer);
  }, [onError, query]);
  const open = async (entry: WorkspaceEntry) => {
    if (entry.kind === "directory") return;
    try {
      const file = await request<{ path: string; content: string; binary: boolean }>(`/v1/workspace/file?path=${encodeURIComponent(entry.path)}`);
      setPreview(file);
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : String(reason));
    }
  };
  return <div className="panel-content"><input className="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文件…" />{preview ? <div className="file-preview"><div><button onClick={() => setPreview(null)}>← 返回</button><button onClick={() => onReference(preview.path)}>引用 @</button></div><b>{preview.path}</b><pre>{preview.binary ? "二进制文件暂不显示" : preview.content}</pre></div> : <div className="file-list">{entries.map((entry) => <button key={entry.path} onClick={() => void open(entry)}><span>{entry.kind === "directory" ? "▸" : "·"} {entry.path}</span><small>{entry.size === null ? "" : `${entry.size} B`}</small></button>)}</div>}</div>;
}

function ChangesPanel({ threadId, onError }: { threadId: string; onError(value: string): void }): ReactElement {
  const [diff, setDiff] = useState<DiffPayload | null>(null);
  const [context, setContext] = useState<Record<string, unknown>>({});
  const [commitMessage, setCommitMessage] = useState("chore: apply CodeRook changes");
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    Promise.all([
      request<DiffPayload>("/v1/workspace/diff?scope=all"),
      threadId ? request<Record<string, unknown>>(`/v1/threads/${threadId}/context`) : Promise.resolve({}),
    ])
      .then(([nextDiff, nextContext]) => { setDiff(nextDiff); setContext(nextContext); })
      .catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason)))
      .finally(() => setLoading(false));
  }, [onError, threadId]);
  useEffect(load, [load]);
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
  return <div className="panel-content"><div className="panel-toolbar"><span>{loading ? "正在读取…" : `${files.length} 个变更文件`}</span><button onClick={load}>刷新</button><button disabled={!files.length || !threadId} onClick={() => void stageAll()}>Stage 全部</button></div>{!files.length && !loading ? <p className="empty">工作区没有未提交变更。</p> : files.map((file, index) => <details className="diff-file" key={`${textValue(file.path)}-${index}`} open={index === 0}><summary><b>{textValue(file.path)}</b><span>+{textValue(file.additions || 0)} / -{textValue(file.deletions || 0)}</span></summary><pre>{textValue(file.patch || file.diff || file)}</pre></details>)}{textValue(diff?.scope) === "staged" && <div className="commit-row"><input value={commitMessage} onChange={(event) => setCommitMessage(event.target.value)} /><button onClick={() => void commit()}>创建本地 Commit</button><small>不会自动 push，也不会运行仓库 hooks。</small></div>}{checkpoints.length > 0 && <div className="checkpoints"><h3>恢复点</h3>{checkpoints.map((checkpoint) => <button key={textValue(checkpoint.checkpoint_id)} onClick={() => void rewind(checkpoint)}><span>{textValue(checkpoint.label || checkpoint.checkpoint_id)}</span><small>{textValue(checkpoint.status)}</small></button>)}</div>}</div>;
}

function ModelsPanel({ onError }: { onError(value: string): void }): ReactElement {
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [presetId, setPresetId] = useState("deepseek");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const load = useCallback(() => request<ProviderCatalog>("/v1/providers").then((value) => { setCatalog(value); const preset = value.presets[0]; if (preset) { setPresetId((current) => current || preset.id); setModel((current) => current || preset.models[0] || ""); } }).catch((reason: unknown) => onError(reason instanceof Error ? reason.message : String(reason))), [onError]);
  useEffect(() => { void load(); }, [load]);
  const preset = catalog?.presets.find((item) => item.id === presetId);
  const selectPreset = (value: string) => { setPresetId(value); const selected = catalog?.presets.find((item) => item.id === value); setModel(selected?.models[0] || ""); setApiKey(""); };
  const save = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true);
    try { await request("/v1/providers", { method: "POST", body: JSON.stringify({ route_id: presetId, preset_id: presetId, model, api_key: apiKey || undefined, activate: true, update: catalog?.routes.some((route) => route.id === presetId) }) }); setApiKey(""); await load(); }
    catch (reason) { onError(reason instanceof Error ? reason.message : String(reason)); }
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
  return <div className="panel-content"><div className={`readiness ${catalog?.readiness.local_ready ? "ready" : "warning"}`}><b>{catalog?.readiness.local_ready ? "模型已就绪" : "需要配置模型"}</b><p>{catalog?.readiness.reason}</p></div><form className="provider-form" onSubmit={(event) => void save(event)}><label>Provider<select value={presetId} onChange={(event) => selectPreset(event.target.value)}>{catalog?.presets.map((item) => <option key={item.id} value={item.id}>{item.name}{item.local ? " · 本地" : ""}</option>)}</select></label><label>模型<input value={model} onChange={(event) => setModel(event.target.value)} list="provider-models" /></label><datalist id="provider-models">{preset?.models.map((item) => <option key={item} value={item} />)}</datalist>{preset?.credential_required && <label>API Key<input type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="只发送到本地 Core，不写入浏览器" /></label>}<div className="capability-tags">{preset && Object.entries(preset.capabilities).filter(([, enabled]) => enabled).map(([name]) => <span key={name}>{name}</span>)}</div><button className="primary" disabled={!model || saving}>{saving ? "正在验证…" : "Doctor 验证并启用"}</button></form><h3>已配置路由</h3>{catalog?.routes.map((route) => { const routeId = textValue(route.id); const active = catalog.active_route_id === route.id; return <div className="route-row" key={routeId}><div><b>{routeId}</b><small>{textValue(route.model)}</small></div><div className="route-actions"><span>{active ? "当前" : textValue(route.credential_source)}</span>{!active && <button onClick={() => void routeAction(routeId, "activate")}>启用</button>}<button onClick={() => void routeAction(routeId, "delete")}>删除</button></div></div>; })}</div>;
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
