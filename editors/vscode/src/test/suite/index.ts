import * as assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

import * as vscode from "vscode";

import {showPermissionPromptForTest} from "../../extension";

// 读取 smoke 必需环境变量，缺失时立即给出可诊断错误。
function requiredEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

// 在 Xvfb 中等待审批模态框出现，截取真实根窗口并关闭模态框。
function captureApprovalScreenshot(target: string): Promise<void> {
  fs.mkdirSync(path.dirname(target), {recursive: true});
  return new Promise((resolve, reject) => {
    const script = [
      "sleep 1",
      "status=0",
      'import -window root "$1" || status=$?',
      "xdotool key Escape",
      "exit $status",
    ].join("; ");
    const child = spawn("bash", ["-lc", script, "capture", target], {
      stdio: "inherit",
    });
    child.once("error", reject);
    child.once("exit", code => {
      if (code === 0) {
        resolve();
      } else {
        reject(new Error(`approval screenshot command exited with ${String(code)}`));
      }
    });
  });
}

// 打开扩展自身的审批 UI，并返回真实 PNG 的大小和内容哈希。
async function captureApprovalVisual(): Promise<{
  path: string;
  bytes: number;
  sha256: string;
}> {
  const screenshotPath = requiredEnv("CODEROOK_VSCODE_SCREENSHOT_PATH");
  const capture = captureApprovalScreenshot(screenshotPath);
  await showPermissionPromptForTest({
    payload: {
      tool_use_id: "vscode-smoke-approval",
      tool_name: "File",
      params: {
        path: "README.md",
        _approval_context: {
          patch_plan: {
            id: "vscode-smoke-plan",
            files: [{
              path: "README.md",
              hunks: [{
                id: "vscode-smoke-hunk",
                header: "@@ -1,1 +1,1 @@",
                additions: 1,
                removals: 1,
                selectable: true,
              }],
            }],
          },
        },
      },
    },
  });
  await capture;
  const image = fs.readFileSync(screenshotPath);
  assert.ok(image.length >= 20_000, "approval screenshot is unexpectedly small");
  assert.deepEqual(
    image.subarray(0, 8),
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    "approval screenshot is not a PNG",
  );
  return {
    path: path.basename(screenshotPath),
    bytes: image.length,
    sha256: createHash("sha256").update(image).digest("hex"),
  };
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

  const approvalScreenshot = process.env.CODEROOK_VSCODE_CAPTURE_APPROVAL === "1"
    ? await captureApprovalVisual()
    : undefined;

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
      approval_visual: approvalScreenshot !== undefined,
    },
    approval_screenshot: approvalScreenshot,
  }, null, 2)}\n`, "utf8");
}
