import type { RuntimeEvent } from "./types";

let csrfToken = "";

async function decodeError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { error?: string };
    return payload.error || `${response.status} ${response.statusText}`;
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken) {
    headers.set("X-CodeRook-CSRF", csrfToken);
  }
  const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!response.ok) throw new Error(await decodeError(response));
  return (await response.json()) as T;
}

export async function bootstrap(): Promise<{ workspace: string }> {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const launchToken = fragment.get("launch");
  const session = launchToken
    ? await request<{ csrf_token: string; workspace: string }>("/v1/web/bootstrap", {
        method: "POST",
        body: JSON.stringify({ launch_token: launchToken }),
      })
    : await request<{ csrf_token: string; workspace: string }>("/v1/web/session");
  csrfToken = session.csrf_token;
  if (launchToken) history.replaceState(null, "", `${location.pathname}${location.search}`);
  return { workspace: session.workspace };
}

export async function streamEvents(
  threadId: string,
  afterSeq: number,
  signal: AbortSignal,
  onEvent: (event: RuntimeEvent) => void,
): Promise<number> {
  const response = await fetch(
    `/v1/threads/${encodeURIComponent(threadId)}/events?after_seq=${afterSeq}`,
    { credentials: "same-origin", signal },
  );
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
