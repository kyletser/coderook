import * as vscode from "vscode";

type JsonObject = Record<string, unknown>;

let threadId = "";
let turnId = "";
let eventCursor = 0;
let streamAbort: AbortController | undefined;
let output: vscode.OutputChannel;

// 读取 runtime API 地址与 Bearer token，环境变量优先于空配置值。
function configuration(): {baseUrl: string; token: string} {
  const config = vscode.workspace.getConfiguration("coderook");
  return {
    baseUrl: config.get<string>("baseUrl", "http://127.0.0.1:7438").replace(/\/$/, ""),
    token: config.get<string>("apiToken", "") || process.env.CODEROOK_API_TOKEN || "",
  };
}

// 发送 JSON 请求并把非成功响应转换为可见错误。
async function request(path: string, init: RequestInit = {}): Promise<unknown> {
  const {baseUrl, token} = configuration();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(`${baseUrl}${path}`, {...init, headers});
  if (!response.ok) {
    throw new Error(`CodeRook API ${response.status}: ${await response.text()}`);
  }
  return response.json();
}

// 复用当前 thread，缺失时创建一个默认 thread。
async function ensureThread(): Promise<string> {
  if (threadId) {
    return threadId;
  }
  const created = await request("/v1/threads", {
    method: "POST",
    body: JSON.stringify({title: "VS Code", mode: "chat"}),
  }) as JsonObject;
  threadId = String(created.id ?? "");
  if (!threadId) {
    throw new Error("CodeRook did not return a thread id");
  }
  return threadId;
}

// 交互创建并选中新的 durable thread，并允许 Extension Host smoke 直接传入标题。
async function newThread(titleOverride?: string): Promise<string | undefined> {
  const title = titleOverride ?? await vscode.window.showInputBox({prompt: "Thread title", value: "VS Code"});
  if (title === undefined) {
    return undefined;
  }
  const created = await request("/v1/threads", {
    method: "POST",
    body: JSON.stringify({title, mode: "chat"}),
  }) as JsonObject;
  threadId = String(created.id ?? "");
  turnId = "";
  eventCursor = 0;
  vscode.window.showInformationMessage(`CodeRook thread: ${threadId}`);
  return threadId;
}

// 从服务端 durable thread 列表恢复选择项，并允许 smoke 按 id 无交互恢复。
async function resumeThread(threadOverride?: string): Promise<string | undefined> {
  let selected = threadOverride;
  if (!selected) {
    const threads = await request("/v1/threads") as JsonObject[];
    const picked = await vscode.window.showQuickPick(
      threads.map(item => ({
        label: String(item.title || item.id),
        description: String(item.id),
        id: String(item.id),
      })),
      {placeHolder: "Resume a CodeRook thread"},
    );
    if (!picked) {
      return undefined;
    }
    selected = picked.id;
  }
  threadId = selected;
  turnId = "";
  eventCursor = 0;
  await startEventStream();
  return threadId;
}

// 创建一个 act turn 并启动按游标恢复的事件流。
async function sendTask(): Promise<void> {
  const content = await vscode.window.showInputBox({prompt: "Task for CodeRook"});
  if (!content?.trim()) {
    return;
  }
  const id = await ensureThread();
  const turn = await request(`/v1/threads/${encodeURIComponent(id)}/turns`, {
    method: "POST",
    body: JSON.stringify({content, mode: "act"}),
  }) as JsonObject;
  turnId = String(turn.id ?? "");
  output.show(true);
  output.appendLine(`\n> ${content}\n`);
  await startEventStream();
}

// 从审批上下文提取可选择 hunk，并返回计划标识与用户选择。
async function selectPatchHunks(params: JsonObject): Promise<{
  selectedHunks?: string[];
  patchPlanId?: string;
}> {
  const context = (params._approval_context ?? {}) as JsonObject;
  const plan = (context.patch_plan ?? {}) as JsonObject;
  const files = Array.isArray(plan.files) ? plan.files as JsonObject[] : [];
  const items = files.flatMap(file => {
    const path = String(file.path ?? "file");
    const hunks = Array.isArray(file.hunks) ? file.hunks as JsonObject[] : [];
    return hunks.filter(hunk => hunk.selectable !== false).map(hunk => ({
      label: `${path} ${String(hunk.header ?? "hunk")}`,
      description: `+${String(hunk.additions ?? 0)} -${String(hunk.removals ?? 0)}`,
      id: String(hunk.id ?? ""),
      picked: true,
    })).filter(item => item.id);
  });
  if (!items.length) {
    return {};
  }
  const selected = await vscode.window.showQuickPick(items, {
    canPickMany: true,
    placeHolder: "Select patch hunks to apply",
  });
  if (selected === undefined) {
    return {selectedHunks: []};
  }
  return {
    selectedHunks: selected.map(item => item.id),
    patchPlanId: String(plan.id ?? "") || undefined,
  };
}

// 显示审批详情，支持普通允许/拒绝和 PatchPlan 的逐 hunk 选择。
async function respondToPermission(record: JsonObject): Promise<void> {
  const payload = (record.payload ?? {}) as JsonObject;
  const toolUseId = String(payload.tool_use_id ?? "");
  if (!toolUseId) {
    return;
  }
  const params = (payload.params ?? {}) as JsonObject;
  const choice = await vscode.window.showWarningMessage(
    `CodeRook requests ${String(payload.tool_name ?? "tool")} permission`,
    {modal: true, detail: JSON.stringify(params, null, 2)},
    "Allow once",
    "Deny",
  );
  const decision = choice === "Allow once" ? "allow_once" : "deny_once";
  const selection = decision === "allow_once" ? await selectPatchHunks(params) : {};
  await request(`/v1/permissions/${encodeURIComponent(toolUseId)}`, {
    method: "POST",
    body: JSON.stringify({
      decision,
      selected_hunks: selection.selectedHunks,
      patch_plan_id: selection.patchPlanId,
    }),
  });
}

// 去重处理 runtime 事件并触发审批与终态反馈。
async function handleEvent(record: JsonObject): Promise<void> {
  const seq = Number(record.seq ?? 0);
  if (seq <= eventCursor) {
    return;
  }
  eventCursor = seq;
  const type = String(record.type ?? "event");
  const payload = (record.payload ?? {}) as JsonObject;
  output.appendLine(`[${seq}] ${type} ${JSON.stringify(payload)}`);
  if (type === "permission.requested") {
    await respondToPermission(record);
  }
  if (type === "run.finished" || type === "turn.completed" || type === "turn.failed") {
    vscode.window.setStatusBarMessage(`CodeRook: ${type}`, 5000);
  }
}

// 按 Last-Event-ID 持续消费 SSE，断线后从最后序号恢复。
async function startEventStream(): Promise<void> {
  if (!threadId) {
    return;
  }
  streamAbort?.abort();
  streamAbort = new AbortController();
  const currentAbort = streamAbort;
  const {baseUrl, token} = configuration();
  const headers: Record<string, string> = {
    Accept: "text/event-stream",
    "Last-Event-ID": String(eventCursor),
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  void (async () => {
    while (!currentAbort.signal.aborted) {
      try {
        const response = await fetch(
          `${baseUrl}/v1/threads/${encodeURIComponent(threadId)}/events`,
          {headers, signal: currentAbort.signal},
        );
        if (!response.ok || !response.body) {
          throw new Error(`event stream returned ${response.status}`);
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!currentAbort.signal.aborted) {
          const {done, value} = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, {stream: true});
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const data = frame.split("\n").filter(line => line.startsWith("data:"))
              .map(line => line.slice(5).trim()).join("\n");
            if (data) {
              await handleEvent(JSON.parse(data) as JsonObject);
              headers["Last-Event-ID"] = String(eventCursor);
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      } catch (error) {
        if (currentAbort.signal.aborted) {
          return;
        }
        output.appendLine(`event stream reconnect: ${String(error)}`);
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  })();
}

// 向当前活动 turn 发送 steering 指令。
async function steer(): Promise<void> {
  if (!turnId) {
    vscode.window.showWarningMessage("No active CodeRook turn");
    return;
  }
  const content = await vscode.window.showInputBox({prompt: "Steering instruction"});
  if (!content?.trim()) {
    return;
  }
  await request(`/v1/turns/${encodeURIComponent(turnId)}/steer`, {
    method: "POST",
    body: JSON.stringify({content}),
  });
}

// 中断当前活动 turn。
async function interrupt(): Promise<void> {
  if (!turnId) {
    vscode.window.showWarningMessage("No active CodeRook turn");
    return;
  }
  await request(`/v1/turns/${encodeURIComponent(turnId)}/interrupt`, {method: "POST"});
}

// 选择变更文件并以 VS Code diff 文档打开服务端生成的 unified diff。
async function openDiff(pathOverride?: string): Promise<string | undefined> {
  const overview = await request("/v1/workspace/diff?scope=all") as JsonObject;
  const files = Array.isArray(overview.files) ? overview.files as JsonObject[] : [];
  let selectedPath = pathOverride;
  if (!selectedPath) {
    const picked = await vscode.window.showQuickPick(
      files.map(file => ({
        label: String(file.path ?? ""),
        description: String(file.status ?? "changed"),
      })),
      {placeHolder: "Open CodeRook workspace diff"},
    );
    selectedPath = picked?.label || ".";
  }
  const diff = selectedPath === "." ? overview : await request(
    `/v1/workspace/diff?scope=all&path=${encodeURIComponent(selectedPath)}`,
  ) as JsonObject;
  const document = await vscode.workspace.openTextDocument({
    language: "diff",
    content: String(diff.diff ?? "No textual diff available."),
  });
  await vscode.window.showTextDocument(document, {preview: true});
  return document.uri.toString();
}

// 为命令统一捕获异常并显示简洁错误。
function guarded<TArgs extends unknown[], TResult>(
  action: (...args: TArgs) => Promise<TResult>,
): (...args: TArgs) => Promise<TResult | undefined> {
  return async (...args: TArgs) => {
    try {
      return await action(...args);
    } catch (error) {
      vscode.window.showErrorMessage(`CodeRook: ${String(error)}`);
      return undefined;
    }
  };
}

// 注册 CodeRook 命令与输出通道。
export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel("CodeRook", {log: true});
  context.subscriptions.push(
    output,
    vscode.commands.registerCommand("coderook.newThread", guarded(newThread)),
    vscode.commands.registerCommand("coderook.resumeThread", guarded(resumeThread)),
    vscode.commands.registerCommand("coderook.send", guarded(sendTask)),
    vscode.commands.registerCommand("coderook.steer", guarded(steer)),
    vscode.commands.registerCommand("coderook.interrupt", guarded(interrupt)),
    vscode.commands.registerCommand("coderook.openDiff", guarded(openDiff)),
  );
}

// 关闭活动 SSE 请求。
export function deactivate(): void {
  streamAbort?.abort();
}
