import * as path from "node:path";

import {runTests} from "@vscode/test-electron";

// 下载并启动隔离的 VS Code Extension Host，执行扩展 smoke suite。
async function main(): Promise<void> {
  const extensionDevelopmentPath = path.resolve(__dirname, "../../..");
  const extensionTestsPath = path.resolve(__dirname, "./suite/index");
  const workspacePath = process.env.CODEROOK_VSCODE_WORKSPACE || path.resolve(
    extensionDevelopmentPath,
    "../..",
  );
  await runTests({
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [
      workspacePath,
      "--disable-extensions",
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes",
    ],
  });
}

void main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
