import type { RuntimeEvent } from "./types";

let csrfToken = "";

async function establishSession(): Promise<{ csrf_token: string; workspace: string }> {
  const response = await fetch("/v1/web/session", { credentials: "same-origin" });
  if (!response.ok) throw new Error(await decodeError(response));
  const session = (await response.json()) as { csrf_token: string; workspace: string };
  csrfToken = session.csrf_token;
  return session;
}

async function decodeError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as {
      error?: string;
      code?: string;
      category?: string;
      message?: string;
      provider_status?: number;
    };
    if (payload.code === "provider_validation_failed") {
      const categoryLabels: Record<string, string> = {
        credential: "API Key 无效或被 Provider 拒绝",
        tls: "TLS 证书或安全连接失败",
        schema: "Provider 不接受当前请求格式",
        model: "Provider 不支持这个模型 ID",
        network: "无法连接 Provider",
        streaming: "Provider 没有返回流式响应",
        termination: "Provider 响应没有正常结束",
        capability: "模型能力验证失败",
      };
      const detail = payload.message === "declared tool capability probe failed"
        ? "工具调用兼容性验证失败"
        : categoryLabels[payload.category || ""] || payload.message || "模型验证失败";
      const upstream = payload.provider_status ? `（Provider HTTP ${payload.provider_status}）` : "";
      return `${detail}${upstream}。API Key 不会被清空，可以修改模型或重试。`;
    }
    return payload.error || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const send = () => {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
      headers.set("X-CodeRook-CSRF", csrfToken);
    }
    return fetch(path, { ...init, headers, credentials: "same-origin" });
  };
  let response = await send();
  if (response.status === 401 && path !== "/v1/web/session") {
    await establishSession();
    response = await send();
  }
  if (!response.ok) throw new Error(await decodeError(response));
  return (await response.json()) as T;
}

export async function bootstrap(): Promise<{ workspace: string }> {
  const session = await establishSession();
  if (location.hash) history.replaceState(null, "", `${location.pathname}${location.search}`);
  return { workspace: session.workspace };
}

export async function streamEvents(
  threadId: string,
  afterSeq: number,
  signal: AbortSignal,
  onEvent: (event: RuntimeEvent) => void,
  tail?: number,
): Promise<number> {
  const tailQuery = tail && afterSeq === 0 ? `&tail=${tail}` : "";
  const url = `/v1/threads/${encodeURIComponent(threadId)}/events?after_seq=${afterSeq}${tailQuery}`;
  const connect = () => fetch(url, { credentials: "same-origin", signal });
  let response = await connect();
  if (response.status === 401 && !signal.aborted) {
    await establishSession();
    response = await connect();
  }
  if (!response.ok || !response.body) throw new Error(await decodeError(response));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = afterSeq;
  while (!signal.aborted) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = block
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (data) {
        const event = JSON.parse(data) as RuntimeEvent;
        if (event.seq > cursor) {
          cursor = event.seq;
          onEvent(event);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
  return cursor;
}
