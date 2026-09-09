import { useEffect, useState } from "react";
import { request } from "./api";
import type { ThreadRecord } from "./types";

type Entry = {
  seq: number;
  parent_seq: number | null;
  active: boolean;
  preview: string;
};

export function SessionTreePanel({ threadId, tr, onFork, onNavigate, onError }: {
  threadId: string;
  tr(zh: string, en: string): string;
  onFork(thread: ThreadRecord, editorText: string): void;
  onNavigate(threadId: string, editorText: string): void;
  onError(message: string): void;
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<number | null>(null);
  const [summarize, setSummarize] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setEntries([]);
    request<{ entries: Entry[] }>(`/v1/threads/${encodeURIComponent(threadId)}/tree`, { signal: controller.signal })
      .then((result) => setEntries(result.entries))
      .catch((error: unknown) => { if (!controller.signal.aborted) onError(String(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [threadId, onError]);

  async function fork(seq: number) {
    setPending(seq);
    try {
      const thread = await request<ThreadRecord & { editor_text?: string }>(`/v1/threads/${encodeURIComponent(threadId)}/fork`, {
        method: "POST", body: JSON.stringify({ leaf_seq: seq }),
      });
      onFork(thread, thread.editor_text || "");
    } catch (error) {
      onError(String(error));
    } finally {
      setPending(null);
    }
  }

  async function navigate(seq: number) {
    setPending(seq);
    try {
      const result = await request<{ editor_text: string }>(`/v1/threads/${encodeURIComponent(threadId)}/navigate`, {
        method: "POST", body: JSON.stringify({ target_seq: seq, summarize }),
      });
      onNavigate(threadId, result.editor_text);
    } catch (error) {
      onError(String(error));
    } finally {
      setPending(null);
    }
  }

  return <section className="panel-content">
    <p>{tr("切换到历史位置，或新建独立分支。选中用户消息会将原文放回输入框，不自动发送，也不回滚文件。", "Navigate to a history entry or fork a separate session. User messages return to the composer without sending. Files are not reverted.")}</p>
    <label><input type="checkbox" checked={summarize} disabled={pending !== null} onChange={event => setSummarize(event.target.checked)} />{tr("带上离开分支的摘要（额外调用模型）", "Carry a branch summary (additional model call)")}</label>
    {loading && <p>{tr("正在读取历史…", "Loading history…")}</p>}
    {!loading && !entries.some((entry) => entry.preview) && <p>{tr("还没有可分支的消息", "No messages to branch from yet")}</p>}
    {entries.filter((entry) => entry.preview).map((entry) => <article key={entry.seq} className="tree-entry">
      <small>#{entry.seq} ← {entry.parent_seq ?? "root"} · {entry.active ? tr("当前路径", "Current path") : tr("其他分支", "Other branch")}</small>
      <p>{entry.preview}</p>
      <button disabled={pending !== null} onClick={() => void navigate(entry.seq)}>
        {pending === entry.seq ? tr("正在处理…", "Working…") : tr("切换到这里", "Navigate here")}
      </button>
      <button disabled={pending !== null} onClick={() => void fork(entry.seq)}>
        {tr("新建分支", "Fork session")}
      </button>
    </article>)}
  </section>;
}
