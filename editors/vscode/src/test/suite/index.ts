import * as assert from "node:assert/strict";
import * as fs from "node:fs";

import * as vscode from "vscode";

// 读取 smoke 必需环境变量，缺失时立即给出可诊断错误。
function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

// 在真实 Extension Host 中连接隔离 daemon 并验证激活、恢复与 diff 路径。
export async function run(): Promise<void> {
  const baseUrl = requiredEnv("CODEROOK_VSCODE_TEST_BASE_URL");
  const token = requiredEnv("CODEROOK_API_TOKEN");
  const evidencePath = requiredEnv("CODEROOK_VSCODE_EVIDENCE_PATH");
  const config = vscode.workspace.getConfiguration("coderook");
  await config.update("baseUrl", baseUrl, vscode.ConfigurationTarget.Global);
  await config.update("apiToken", token, vscode.ConfigurationTarget.Global);

  const extension = vscode.extensions.getExtension("coderook.coderook-vscode");
  assert.ok(extension, "CodeRook extension is installed in the test host");
  await extension.activate();

  const expectedCommands = [
    "coderook.newThread",
    "coderook.resumeThread",
    "coderook.send",
    "coderook.steer",
    "coderook.interrupt",
    "coderook.openDiff",
  ];
  const commands = await vscode.commands.getCommands(true);
  for (const command of expectedCommands) {
    assert.ok(commands.includes(command), `${command} is not registered`);
  }

  const threadId = await vscode.commands.executeCommand<string>(
    "coderook.newThread",
    "Extension Host smoke",
  );
  assert.ok(threadId, "newThread did not return a durable thread id");
  const restoredId = await vscode.commands.executeCommand<string>(
    "coderook.resumeThread",
    threadId,
  );
  assert.equal(restoredId, threadId);

  const documentUri = await vscode.commands.executeCommand<string>(
    "coderook.openDiff",
    ".",
  );
  assert.ok(documentUri, "openDiff did not open a document");
  const document = vscode.workspace.textDocuments.find(
    item => item.uri.toString() === documentUri,
  );
  assert.equal(document?.languageId, "diff");

  const response = await fetch(`${baseUrl}/v1/threads`, {
    headers: {Authorization: `Bearer ${token}`},
  });
  assert.equal(response.status, 200);
  const threads = await response.json() as Array<{id?: string}>;
  assert.ok(threads.some(item => item.id === threadId));

  fs.writeFileSync(evidencePath, `${JSON.stringify({
    schema_version: 1,
    status: "passed",
    commit: process.env.CODEROOK_VSCODE_TEST_COMMIT || "unknown",
    platform: process.platform,
    vscode_version: vscode.version,
    extension_id: extension.id,
    daemon_base_url: baseUrl,
    capabilities: {
      activation: true,
      commands: expectedCommands,
      create_thread: true,
      resume_thread: true,
      open_diff: true,
    },
  }, null, 2)}\n`, "utf8");
}
